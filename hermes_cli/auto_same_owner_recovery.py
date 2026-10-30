"""Hourly automatic same-owner recovery (t_df999f30).

Scheduled lane: for each technically stopped (transient/capability) triage or
blocked card, issue ONE fresh work-order revision bound to that exact stop and
return the card to its own owner's queue so normal dispatch admits it.

Safety controls:
- protected decisions (gus_admin_cli.classify_hold == 'protected'), needs_input,
  review status, and unassigned cards are never touched;
- one automatic revision per distinct stop event (no unchanged retry);
- at most MAX_PER_DAY automatic revisions per card per rolling 24h;
- cooldown after the stop before acting;
- explicit SKIP list for cards waiting on an external party.
No approval is granted; recorded action holds are preserved by the work order.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import owner_work_preparation as prep
from hermes_cli import owner_work_start as ows

ACTOR = 'auto-same-owner-recovery'
COOLDOWN = 3600
MAX_PER_DAY = 3
SKIP = frozenset({'t_5c913fbd', 't_8011d505'})  # waiting on Ava's side


def _auto_orders(conn, task_id, since):
    out = []
    for row in conn.execute("SELECT payload,created_at FROM task_events WHERE task_id=? AND kind=?",
                            (task_id, ows.EVENT)):
        try:
            payload = json.loads(row['payload'])
        except (TypeError, ValueError):
            continue
        if payload.get('actor') == ACTOR:
            out.append((payload, row['created_at']))
    return [p for p, at in out], sum(1 for _, at in out if at > since)


_STOP_KINDS = ('blocked', 'triaged', 'escalated', 'block_loop_detected', 'dependency_wait')


# Every known producer of title/body text, mapped to the field(s) it writes.
# Unknown event kinds that name a text field in their payload are honoured too,
# and producers that do not say which field changed are treated as both.
_TEXT_PRODUCERS = {
    'created': ('title', 'body'),
    'edited': None,               # payload 'fields'; NULL payload (dashboard PATCH) = both
    'specified': None,            # payload 'changed_fields'; NULL = both (fail closed)
    'instructions_repaired': ('body',),
    'owner_handoff': ('body',),
    'readiness_prepared': ('body',),
}


def _fields_written(kind, payload):
    if kind in _TEXT_PRODUCERS and _TEXT_PRODUCERS[kind] is not None:
        return set(_TEXT_PRODUCERS[kind])
    if kind == 'specified' and not payload:
        # kanban_db.specify_triage_task emits payload NULL exactly when no field
        # changed (it writes {'changed_fields': [...]} otherwise): a known no-op.
        return set()
    named = set()
    if isinstance(payload, dict):
        for key in ('fields', 'changed_fields'):
            val = payload.get(key)
            if isinstance(val, (list, tuple)):
                named |= {f for f in val if f in ('title', 'body')}
    if kind in _TEXT_PRODUCERS:
        if isinstance(payload, dict) and any(k in payload for k in ('fields', 'changed_fields')):
            return named
        return {'title', 'body'}
    return named


def _sources(conn, task_id):
    """Every recorded text with the event-sequence position at which it entered
    the record, tracked per field. Comments are positioned by durable row
    identity (see _comment_answered), never by content. Unknown provenance
    gets +inf so it can never count as answered."""
    INF = float('inf')
    task = conn.execute('SELECT title,body FROM tasks WHERE id=?', (task_id,)).fetchone()
    events = conn.execute('SELECT id,kind,payload FROM task_events WHERE task_id=? ORDER BY id',
                          (task_id,)).fetchall()
    seq = {'title': 0, 'body': 0}
    out = []
    for ev in events:
        try:
            payload = json.loads(ev['payload'] or '{}') or {}
        except (TypeError, ValueError):
            payload = {}
        for field in _fields_written(ev['kind'], payload):
            seq[field] = ev['id']
        if ev['kind'] in _STOP_KINDS and isinstance(payload, dict):
            out.append((ev['id'], str(payload.get('reason') or '')))
    out.append((seq['title'] or INF, task['title'] or ''))
    out.append((seq['body'] or INF, task['body'] or ''))
    return out


def _release_row(conn, task_id):
    return conn.execute("SELECT id,payload,created_at FROM task_events WHERE task_id=? "
                        "AND kind='unblocked' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def _comments(conn, task_id):
    """(answered, body) per comment. A comment is answered only when its row id
    is at or below the comment-row boundary recorded by the newest trusted core
    release (kb.unblock_task writes 'comment_boundary'). Releases without a
    recorded boundary fall back to strictly-earlier timestamps (same second =
    new, fail closed). Content never decides provenance."""
    rel = _release_row(conn, task_id)
    boundary = at = None
    if rel is not None:
        try:
            boundary = (json.loads(rel['payload'] or '{}') or {}).get('comment_boundary')
        except (TypeError, ValueError):
            boundary = None
        at = rel['created_at']
    out = []
    for cid, body, created in conn.execute(
            'SELECT id,body,created_at FROM task_comments WHERE task_id=? ORDER BY id', (task_id,)):
        if rel is None:
            answered = False
        elif isinstance(boundary, int):
            answered = cid <= boundary
        else:
            answered = created is not None and at is not None and created < at
        out.append((answered, body or ''))
    return out


def _last_release(conn, task_id):
    """Event id of the newest trusted core release (kb.unblock_task's 'unblocked'
    event). Automatic recovery writes a different event kind, so it can never
    answer a hold itself."""
    row = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='unblocked'",
                       (task_id,)).fetchone()
    return row[0] or 0


def complete_record_hold(conn, task_id):
    """Unresolved-obligation check over the WHOLE record, provenance-aware.

    Scans title, body, every comment and every stop-kind event reason, plus the
    needs_input block kind. An explicit restriction (any prohibition, wait,
    hold or conditional approval wording; fail-closed vocabulary rather than a
    phrase list) holds unless a trusted core release was recorded AFTER that
    text entered the record. Decision-class subjects (payment, signature, ...)
    hold regardless of history. Returns a plain reason when held, else None.
    """
    from gateway.gus_admin_cli import PROTECTED
    task = conn.execute('SELECT block_kind FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        return 'missing task'
    if task['block_kind'] == 'needs_input':
        return 'waiting on a human answer'
    sources = _sources(conn, task_id)
    released = _last_release(conn, task_id)
    comments = _comments(conn, task_id)
    scan = [(seq < released, text) for seq, text in sources] + comments
    for answered, text in scan:
        norm = _normalize(text)
        m = _find_hold(norm)
        if m and not answered:
            return f'Explicit hold recorded: "{norm[max(0, m[0] - 40):m[1] + 80][:160]}"'
    # A requested fix already includes its deploy and the restart that loads it,
    # so "restart" merely mentioned in the history is not a decision subject.
    for _seq, text in [(None, t) for _a, t in scan]:
        for m in PROTECTED.finditer(text):
            if m.group(0).lower() in _HISTORY_EXEMPT:
                continue
            return f'Needs Chris to decide: this involves {m.group(0).lower()}.'
    return None


_HISTORY_EXEMPT = frozenset({'restart'})

import re as _re

# Non-negated stop vocabulary: prohibitions, waits, holds and approval
# conditions. Deliberately broad (fail closed).
RESTRICTION = _re.compile(
    r"\b(?:not\s+(?:allowed|permitted|authori[sz]ed|approved|yet)"
    r"|no\s+(?:auto\w*|restart\w*|deploy\w*|proceed\w*|resum\w*|retr\w*|action|go\b)"
    r"|prohibit\w*|forbid\w*|forbidden|disallow\w*|bann?ed|block(?:ed)?\s+on\s+chris"
    r"|wait(?:ing)?\s+(?:for|on|until)|hold\s+(?:off|for|until)|on\s+hold|held\s+(?:for|until|pending)"
    r"|stop\s+(?:all|work|here|until)|halt\w*|freeze|frozen"
    r"|until\s+\w+(?:'s)?\s+(?:approv\w*|sign\w*|confirm\w*|says?|ok\w*|go\w*)"
    r"|(?:without|before|pending|awaiting|requires?|needs?)\s+(?:\w+(?:'s)?\s+)?"
    r"(?:approv\w*|sign[- ]?off|consent\w*|go[- ]ahead|confirm\w*|permission|authori[sz]ation|ok\b))",
    _re.I)

# Negated directives are scoped by their object. "Do not modify unrelated
# files" constrains HOW the work is done; "Do not proceed/restart/resume"
# stops the work itself. A negation is a hold when its object is a
# work-progress verb, when it is conditional (until/unless/without/before/
# pending), or when it has no clear scoped object (fail closed).
NEGATION = _re.compile(
    r"\b(?:do\s+not|don'?t|does\s+not|doesn'?t|never|must\s+not|mustn'?t|shall\s+not"
    r"|should\s+not|shouldn'?t|may\s+not|cannot|can'?t|can\s+not|no\s+one\s+should)\b", _re.I)
# A governed action verb, matched as a whole word (with its inflections) so
# that nouns like "runtime" or "workspaces" never count as "run"/"work".
_VERB = (r"(?:proceed\w*|continu\w*|resum\w*|restart\w*|reboot\w*|redeploy\w*|deploy\w*|releas\w*"
         r"|ship(?:s|ped|ping)?|launch\w*|start(?:s|ed|ing)?|begin\w*|run(?:s|ning)?|rerun\w*"
         r"|retr(?:y|ies|ied|ying)|execut\w*|act(?:s|ed|ing)?|action|send(?:s|ing)?|publish\w*"
         r"|post(?:s|ed|ing)?|merg(?:e|es|ed|ing)|push(?:es|ed|ing)?|pay(?:s|ing)?|sign(?:s|ed|ing)?"
         r"|submit\w*|approv\w*|unblock\w*|requeue\w*|dispatch\w*|claim\w*|go(?:es|ing)?"
         r"|move\s+(?:on|forward|ahead)|touch\s+(?:this|it|anything)|do\s+(?:anything|it|this|that)"
         r"|anything|any\s+(?:work|action|change|step)|work(?:s|ing)?)\b")
# Modifiers/fillers that may sit between the negation (or a coordinator) and
# the verb it governs: "do not silently restart", "or then restart".
_LEAD = (r"(?:(?:\w+ly|yet|further|again|now|ever|even|then|also|just|any\s+further|auto"
         r"|the|this|that|it|task|card|work)\s+)*")
_PROGRESS = _re.compile(r"^" + _LEAD + _VERB, _re.I)
_SPECIFY_AUDIT = _re.compile(
    r"^Specified \u2014 updated (?:title|body|assignee)(?:, (?:title|body|assignee))* and promoted to todo\.$")
# The negation distributes over coordinated predicates ("modify X or restart"),
# so every conjunct is checked for a governed head verb. Objects/complements
# ("coverage for restart behavior") are not heads and do not hold.
_COORD = _re.compile(r"\s*(?:,|/|\bor\b|\band\b|\bnor\b|\bor\s+else\b)\s*", _re.I)
_CONDITION = _re.compile(r"\b(?:until|unless|without|before|pending|awaiting|till)\b", _re.I)
_CLAUSE_END = _re.compile(r"[.;!?\n]")


def _find_hold(text):
    """Return (start, end) of the first unresolved stop in normalized text, else None."""
    m = RESTRICTION.search(text)
    if m:
        return (m.start(), m.end())
    for n in NEGATION.finditer(text):
        rest = text[n.end():]
        end = _CLAUSE_END.search(rest)
        clause = (rest[:end.start()] if end else rest).strip()
        words = _re.findall(r"[\w']+", clause)
        conjuncts = [c for c in _COORD.split(clause) if c and c.strip()]
        if (not words or _CONDITION.search(clause)
                or any(_PROGRESS.match(c.strip()) for c in conjuncts)
                or not _pure_scope(conjuncts)):
            return (n.start(), n.end() + len(clause) + 1)
    return None


# Positive evidence of a pure how-to constraint: the negation must govern a
# known scoping verb ("modify unrelated files", "repeat the same approach").
# Anything else after a negation ("attempt to restart", "under any
# circumstances restart", unknown heads) holds (fail closed).
_SCOPE_HEAD = _re.compile(
    r"^(?:\w+ly\s+)*(?:modif\w*|chang\w*|edit\w*|alter\w*|remov\w*|delet\w*|drop(?:s|ped|ping)?"
    r"|renam\w*|repeat\w*|reus\w*|use|uses|using|add(?:s|ed|ing)?|includ\w*|introduc\w*"
    r"|expos\w*|shar(?:e|es|ed|ing)|print\w*|log(?:s|ged|ging)?|leak\w*|commit\w*|hard-?cod\w*"
    r"|rewrit\w*|refactor\w*|overwrit\w*|break(?:s|ing)?|skip\w*|duplicat\w*|creat\w*|cop(?:y|ies|ied|ying)"
    r"|reformat\w*|stor(?:e|es|ed|ing)|past(?:e|es|ed|ing)|echo\w*|weaken\w*|loosen\w*|disabl\w*)\b", _re.I)
_GOVERNED_WORD = _re.compile(r"\b" + _VERB, _re.I)


# A governed word inside a scoping verb's object/complement is harmless only
# as an attributive modifier inside a prepositional phrase ("coverage for
# restart behavior"): preceded by a non-"to" preposition (with optional
# determiners/adjectives) and followed by the noun it modifies. As an
# infinitive ("use this task to restart production") or as the object itself
# ("repeat the restart") it is the governed action and holds.
_PREP = (r"(?:for|about|of|around|in|on|covering|regarding|concerning|from|with|into|"
         r"over|under|across|behind|within|by)")
_ATTRIBUTIVE = _re.compile(
    r"\b" + _PREP + r"\s+(?:(?:the|a|an|this|that|these|those|any|all|our|its|existing|"
    r"current|related|\w+ly)\s+)*$", _re.I)


# Positive evidence of a descriptive (noun-modifier) use: the governed word
# must be an uninflected base form (a gerund/participle/finite form such as
# "restarting" or "restarted" governs an object and is an action) and must be
# immediately followed by a known descriptive head noun. Anything else is
# ambiguous and holds (fail closed).
_DESCRIPTIVE_NOUN = _re.compile(
    r"(?:behaviou?rs?|coverage|tests?|testing|logic|paths?|handling|handlers?|flows?|code"
    r"|semantics|cases?|scenarios?|scripts?|docs?|documentation|notes?|timing|windows?"
    r"|checks?|guards?|messages?|labels?|wording|copy|history|records?|counts?|metrics?"
    r"|settings?|options?|flags?|buttons?|fields?|fixtures?|helpers?|routines?)\b", _re.I)
_INFLECTED = _re.compile(r"(?:ing|ed|es)$|(?<![su])s$", _re.I)


# Second form of positive evidence, bound to the protected object: the
# governed word (any form, e.g. a gerund) heads a prepositional complement
# that directly follows a descriptive head noun ("test coverage for
# restarting services", "documentation about deploying applications"). The
# phrase then describes what that object covers. A preposition after a
# non-descriptive noun ("this task for restarting", "the service by
# restarting") is adverbial (purpose/means) and holds. "by"/"to" never count.
_NOUN_COMPLEMENT = _re.compile(
    r"\b" + _DESCRIPTIVE_NOUN.pattern + r"\s+(?:for|about|of|around|on|covering|regarding|concerning)\s+$",
    _re.I)

# The noun-complement exemption is bound to the whole governing construction:
# the governing predicate must be a preservation verb (it protects an existing
# artifact from being removed/altered) and its direct object must be a
# documentary artifact (coverage, tests, documentation...) that the
# prepositional phrase describes. Instrument/creation predicates ("use
# scripts for restarting production", "add helpers for deploying") make the
# governed word the purpose of the action and hold. Instrument nouns
# (scripts, helpers, routines, handlers...) never qualify.
_PRESERVE_HEAD = _re.compile(
    r"^(?:\w+ly\s+)*(?:modif\w*|chang\w*|edit\w*|alter\w*|remov\w*|delet\w*|drop(?:s|ped|ping)?"
    r"|renam\w*|rewrit\w*|overwrit\w*|break(?:s|ing)?|skip\w*|weaken\w*|loosen\w*|disabl\w*"
    r"|reformat\w*)\b", _re.I)
# Modifiers before the documentary head are open-class (any adjective or
# noun-adjunct such as "automated", "operator"), bounded to four tokens and
# excluding conjunctions, prepositions, negators and instrument nouns so a
# second predicate or an instrument cannot hide inside the object phrase.
# Instrument/target exclusion is by grammatical role, not vocabulary: the
# documentary noun must be the HEAD of the object phrase, so a pre-head word
# ("production test coverage", "service documentation") is only a noun
# adjunct of that documentary object and cannot be the instrument or target.
# An instrument head ("use scripts for ...") or a means phrase ("change the
# service by restarting") never matches the documentary-head form and holds.
_MODIFIER_STOP = r"(?:and|or|but|nor|then|to|by|with|via|using|for|about|of|on|in|not|never|no)\b"
_DOC_OBJECT = _re.compile(
    r"^(?:(?!" + _MODIFIER_STOP + r")[a-z][\w-]*\s+){0,4}"
    r"(?:coverage|tests?|testing|docs?|documentation|notes?|wording|copy|history|records?"
    r"|messages?|labels?|assertions?|specs?|comments?)"
    r"\s+(?:for|about|of|around|on|covering|regarding|concerning)\s+$", _re.I)


# The complete documentary object of a preservation verb, bound before any
# token is judged: up to four pre-head modifiers then the documentary head
# noun, ending the phrase (end, punctuation or a descriptive preposition).
# Words in the modifier slots are nominal adjuncts of that object
# ("deployment documentation", "restart test coverage") and are never the
# governed action; the object must start the complement (direct object).
_DOC_HEAD = (r"(?:coverage|tests?|testing|docs?|documentation|notes?|wording|copy|history|records?"
             r"|messages?|labels?|assertions?|specs?|comments?)")
_DOC_OBJECT_SPAN = _re.compile(
    r"^\s*((?:(?!" + _MODIFIER_STOP + r")[a-z][\w-]*\s+){0,4})" + _DOC_HEAD +
    r"(?=\s*$|\s*[.,;:!?]|\s+(?:for|about|of|around|on|covering|regarding|concerning)\b)", _re.I)


def _complement_clean(complement, head=None):
    preserving = bool(head and _PRESERVE_HEAD.match(head))
    adjunct_end = 0
    if preserving:
        m = _DOC_OBJECT_SPAN.match(complement)
        if m:
            adjunct_end = m.end(1)
    for g in _GOVERNED_WORD.finditer(complement):
        if g.end() <= adjunct_end and " " not in g.group(0):
            continue
        before = complement[:g.start()]
        after = complement[g.end():].lstrip()
        word = g.group(0)
        if " " in word:
            return False
        if preserving and _DOC_OBJECT.match(before.strip() + " "):
            continue
        if (_INFLECTED.search(word)
                or not _ATTRIBUTIVE.search(before)
                or not _DESCRIPTIVE_NOUN.match(after)):
            return False
    return True


def _pure_scope(conjuncts):
    parts = [c.strip() for c in conjuncts if c.strip()]
    if not parts:
        return False
    first = _SCOPE_HEAD.match(parts[0])
    if not first or not _complement_clean(parts[0][first.end():], first.group(0)):
        return False
    for c in parts[1:]:
        # Later conjuncts are another scoping predicate (whose complement is
        # validated the same way) or a noun continuation of the first
        # predicate's object (governed by that same predicate).
        h = _SCOPE_HEAD.match(c)
        if h:
            ok = _complement_clean(c[h.end():], h.group(0))
        else:
            ok = _complement_clean(c, first.group(0))
        if not ok:
            return False
    return True


# Retained names for importers/tests of the previous release.
EXPLICIT_HOLD = EXPLICIT_STOP = RESTRICTION  # plus NEGATION via _find_hold


_QUOTES = str.maketrans({'\u2019': "'", '\u2018': "'", '\u02bc': "'", '\u2032': "'"})


def _normalize(text):
    """Matching-only normalization: unify apostrophes and collapse all whitespace
    (line wraps included). Stored text is never rewritten."""
    return ' '.join(str(text or '').translate(_QUOTES).split())


def _evidence_since(conn, task_id, orders, stop=None):
    """True when something substantive changed since the last automatic order:
    a new comment, a run whose summary differs from the stop it recovered, or a
    new stop whose reason differs (a fresh stop ID alone is not enough)."""
    if not orders:
        return True
    last = orders[-1]
    if conn.execute('SELECT 1 FROM task_comments WHERE task_id=? AND id>? LIMIT 1',
                    (task_id, last.get('latest_comment_id') or 0)).fetchone():
        return True
    prior_reason = ' '.join(str((last.get('preserved_stop') or {}).get('reason') or '').split())
    if stop is not None:
        cur_reason = ' '.join(str(stop.get('reason') or '').split())
        if cur_reason and cur_reason != prior_reason:
            return True
    for (summary,) in conn.execute('SELECT summary FROM task_runs WHERE task_id=? AND id>?',
                                   (task_id, last.get('prior_run_id') or 0)):
        s = ' '.join(str(summary or '').split())
        if s and s != prior_reason and not s.startswith('Held: this card already ran once'):
            return True
    return False


def candidates(conn, now=None):
    now = int(time.time()) if now is None else now
    rows = conn.execute("SELECT id FROM tasks WHERE status IN ('triage','blocked') "
                        "AND block_kind IN ('transient','capability') "
                        "AND COALESCE(assignee,'') NOT IN ('','default') ORDER BY id").fetchall()
    from gateway.gus_admin_cli import classify_hold
    result = []
    for (tid,) in rows:
        if tid in SKIP:
            result.append((tid, 'skip', 'waiting on external party')); continue
        task = kb.get_task(conn, tid)
        kind, why = classify_hold(conn, task)
        if kind != 'technical':
            result.append((tid, 'hold', why)); continue
        held = complete_record_hold(conn, tid)
        if held:
            result.append((tid, 'hold', held)); continue
        stop = ows._stop(conn, tid)
        if stop is None:
            result.append((tid, 'hold', 'no recorded stop')); continue
        stop_at = conn.execute('SELECT created_at FROM task_events WHERE id=?', (stop['event_id'],)).fetchone()[0]
        if now - stop_at < COOLDOWN:
            result.append((tid, 'wait', 'cooldown')); continue
        orders, recent = _auto_orders(conn, tid, now - 86400)
        if any((o.get('preserved_stop') or {}).get('event_id') == stop['event_id'] for o in orders):
            result.append((tid, 'hold', 'already auto-recovered this exact stop; needs changed input')); continue
        if not _evidence_since(conn, tid, orders, stop):
            result.append((tid, 'hold', 'same failure recurred with no new evidence; needs changed input')); continue
        if recent >= MAX_PER_DAY:
            result.append((tid, 'hold', 'daily automatic recovery cap reached')); continue
        result.append((tid, 'recover', stop))
    return result


def recover(conn, task_id, stop, now=None):
    """Issue one fresh revision bound to ``stop`` and requeue on the same owner."""
    task = prep._task(conn, task_id)
    prep._idle(conn, task)
    instruction = (f"Automatic same-owner recovery of stop event {stop['event_id']} ({stop['kind']}): "
                   f"{(stop.get('reason') or '')[:600]} Read that failure and the latest comments, "
                   "repair the cause or take a different authorized approach; do not repeat the same attempt. "
                   "All recorded holds and approval gates remain in force.")
    instruction = ' '.join(instruction.split())[:2000]
    with kb.write_txn(conn):
        cur = conn.execute('SELECT status,assignee FROM tasks WHERE id=?', (task_id,)).fetchone()
        if cur['status'] not in ('triage', 'blocked') or cur['assignee'] != task['assignee']:
            return None
        # Atomic recheck inside the write txn: still the selected stop, still no hold.
        now_stop = ows._stop(conn, task_id)
        if now_stop is None or now_stop.get('event_id') != stop.get('event_id'):
            return None
        if complete_record_hold(conn, task_id):
            return None
        target = kb._landing_status_after_parents(conn, task_id)
        binding = ows._binding(conn, task_id)
        order = {'instruction': instruction, 'recovery': True, 'phase': 'execute',
                 'binding_digest': binding, 'plan_digest': None}
        order['revision'] = prep._hash({'instruction': instruction, 'binding': binding, 'plan_digest': None})
        budget = ows._budget(task); budget['work_order'] = order
        conn.execute("UPDATE tasks SET status=?,block_kind=NULL,worker_budget_json=? WHERE id=?",
                     (target, json.dumps(budget, sort_keys=True), task_id))
        latest = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_comments WHERE task_id=?', (task_id,)).fetchone()[0]
        last_run = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0]
        kb._append_event(conn, task_id, ows.EVENT, {'version': 1, 'actor': ACTOR, 'work_order': order,
            'latest_comment_id': latest, 'prior_run_id': last_run, 'preserved_stop': stop,
            'source_status': cur['status'], 'status': target, 'automatic': True,
            'failure_history_preserved': True, 'action_restrictions_preserved': True,
            'approval_granted': False})
    return target


def run(conn, now=None, dry_run=False):
    report = []
    for tid, verdict, detail in candidates(conn, now):
        if verdict == 'recover':
            status = None if dry_run else recover(conn, tid, detail, now)
            report.append({'task': tid, 'action': 'recovered' if status else 'would_recover' if dry_run else 'raced',
                           'status': status, 'stop_event': detail['event_id']})
        else:
            report.append({'task': tid, 'action': verdict, 'detail': detail})
    return report


if __name__ == '__main__':
    import sys
    dry = '--dry-run' in sys.argv
    board = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--board=')), 'chris-home')
    with kb.connect_closing(board=board) as c:
        rep = run(c, dry_run=dry)
    acted = [r for r in rep if r['action'] in ('recovered', 'would_recover')]
    print(json.dumps(acted if not ('-v' in sys.argv) else rep, indent=1))
