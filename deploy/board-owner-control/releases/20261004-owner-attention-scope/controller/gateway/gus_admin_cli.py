"""Bounded native-worker CLI fallback for operations absent from tool schemas.

An installed worker's live assignment and existing terminal configuration are
required. This does not grant terminal access, enroll members or mint review
permission. Context is scoped to one CLI dispatch and checked again under the
mutation's write transaction.
"""
import os
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_active = ContextVar('native_admin_cli', default=None)
_owner_continuation = ContextVar('owner_board_continuation', default=False)
READS = frozenset({'list', 'ls', 'show', 'runs', 'stats', 'log', 'context'})
MUTATIONS = frozenset({'archive', 'promote', 'unblock', 'restore', 'specify', 'resume-triage', 'unlink'})

import re as _re

# t_f283b52b: decisions a peer can never treat as answered by a recorded
# comment; only an owner/core release (Chris) clears these. Narrower than
# PROTECTED on purpose: access/deploy stops that the user already answered on
# the exact card (bound by event/comment identity) may be resumed.
NEVER_PEER_RESUMABLE = _re.compile(
    r"capital|payment|purchase|spend|invoice|wire (transfer|funds|money|payment)|refund|"
    r"\bsignature|\bsign(ing|ed)?\b(?![- ]?(in|up|on)\b)|publish|external send|send to|outbound call|"
    r"legal fil|immigration|delete|deletion|privacy", _re.I)
# Independent QA/privacy/review edges are never removable by a worker.
PROTECTED_EDGE = _re.compile(r"\b(qa|review|reviewer|privacy|security|audit|verify release)\b", _re.I)
REVIEW_PROFILES = frozenset({'grace', 'claude-review', 'fable-review'})
AUTHORIZATION_TEXT = _re.compile(
    r"\b(approv\w*|authori[sz]\w*|unblock\w*|resume\w*|unpause\w*|continue|answered|"
    r"recovery|recovered|go ahead|releases?)\b", _re.I)


def authorization_text_ok(body):
    """t_f283b52b: bound comment must record a decision and carry no hold."""
    body = body or ''
    # Hold language inside the comment ("no restart", "preserve send gate")
    # describes preserved gates; never-peer subjects are checked separately.
    return bool(AUTHORIZATION_TEXT.search(body))

# Genuinely protected actions (t_9ebe1c65). Only these stay a human decision.
# Everything else that stopped (technical access, stale dependency, unfinished
# peer check, obsolete hold) is ordinary maintenance the user already
# authorised by asking for the work, and must not be re-asked.
import re as _re
PROTECTED = _re.compile(
    r"capital|payment|purchase|spend|invoice|wire (transfer|funds|money|payment)|refund|\bsign(ing|ature|ed)?\b(?![- ]in)|"
    r"publish|external send|send to|outbound call|legal fil|immigration|"
    r"credential|password|oauth|account (change|setup)|privacy|connor|"
    r"delete|deletion|restart|production deploy", _re.I)


# t_4541b7b8: retirement must name why the work is no longer wanted; age alone
# is never a reason. Private/creator lanes are outside worker cleanup scope.
RETIRE_KIND = _re.compile(r"\b(obsolete|duplicate|duplicates|superseded|abandoned|withdrawn|replaced by)\b", _re.I)
PRIVATE_TENANT = _re.compile(r"private|josh|creator|family", _re.I)


def retirement_reason_ok(reason):
    return bool(RETIRE_KIND.search(str(reason or '')))


def require_retirement_reason(reason):
    """Shared boundary for every retirement caller (worker, CLI, owner UI)."""
    if not retirement_reason_ok(reason):
        raise PermissionError('Retirement needs an explicit obsolete/duplicate/superseded/abandoned/withdrawn reason; age alone is not enough')


# t_fa49d830: an explicit hold recorded anywhere on the card (body, comments,
# block reason) is honoured. Any explicit authorization/approval hold is
# protected on its own: a missing protected-action keyword in the same
# sentence is never treated as consent (reviewer finding, run 4969).
HOLD_MARKER = _re.compile(
    r"explicit hold|\bhold\s*:|\bon hold\b|\bhold (for|until|pending)\b|"
    r"until (i|we) (approve|sign off|decide|confirm)|do not proceed|don't proceed|"
    # round 5: unconditional action prohibitions are holds as a class; no
    # 'until' suffix and no fixed word order (e.g. 'never push this to prod').
    r"\b(do not|don't|dont|never|must not|mustn't|should not|shouldn't|cannot|can't|no)\s+(\w+\s+){0,4}?"
    r"(deploy|deploying|deployment|ship|shipping|release|releasing|activate|activation|merge|merging|launch|launching|"
    r"send|sending|publish|publishing|repoint|repointing|restart|restarting|push|pushing|go live|rollout|roll out)\b|"
    r"\b(deploy|deployment|ship|release|activation|merge|launch|publish|repoint|restart|rollout)\w*\s+(is\s+)?(not allowed|forbidden|prohibited|blocked by chris)|"
    r"\b(deploy|deployment|ship|release|activation|merge|launch|publish|repoint|restart|rollout|push)\w*\b[^.!?;\n]*?[:,\-]\s*(do not|don't|never|must not|not)\b|"
    r"until (chris|the owner|a human|owner|human) (approves?|signs? off|decides?|confirms?)|"
    r"requires? (chris|owner|human|explicit)?\s*('s |\u2019s )?(approval|sign-?off|decision)|"
    # 'needs Chris to sign in' is an access failure, not a consent hold.
    r"needs? chris('s|\u2019s)? (approval|decision|sign-?off|go-?ahead|ok\b|to (approve|decide|sign off|confirm))|not authori[sz]ed|"
    # round 4: one possessive-aware consent grammar for awaiting/pending/waiting.
    r"(awaiting|pending|waiting (for|on)|waits? (for|on))\s+(the\s+)?((chris|owner|human|user)('s|\u2019s|s')?\s+)?(explicit\s+|final\s+)?(approval|decision|sign-?off|go-?ahead|consent)|"
    r"approval (gate|required)|chris (must|has to|needs to) (approve|decide|sign)", _re.I)


# round 7 (t_fa49d830): one compositional prohibition grammar. A sentence is
# an explicit hold when ANY supported prohibition form (prefix or postfix)
# co-occurs with ANY protected/action family, in any word order. Forms and
# vocabularies are unified here instead of phrase-by-phrase patches.
PROHIBITION = _re.compile(
    r"\b(do not|don't|dont|never|must not|mustn't|should not|shouldn't|cannot|can not|can't|"
    r"may not|not to|no|nor)\b|"
    r"\b(not (allowed|permitted|authori[sz]ed)|forbidden|prohibited|banned|off[- ]limits|"
    r"disallowed|blocked by chris)\b", _re.I)
ACTION = _re.compile(
    r"\b(deploy\w*|ship(s|ping|ped)?|releas(e|es|ing)|activat\w*|merg(e|es|ing)|launch\w*|"
    r"publish\w*|repoint\w*|restart\w*|push(es|ing)?|go(ing)? live|roll(ing)?[- ]?out\w*|"
    r"send(s|ing)?|delet\w*|remov(e|al|ing) (the )?(data|database|records?)|drop(ping)? (the )?(table|database)|"
    r"purchas\w*|buy(s|ing)?|spend\w*|payments?|pay(s|ing)?|invoic\w*|wire\w*|refund\w*|capital|"
    r"sign(s|ing|ature|ed)?(?![- ]in)|credentials?|passwords?|oauth|account (change|setup)s?|privacy|"
    r"legal fil\w*|immigration|outbound calls?|production)\b", _re.I)
_APOSTROPHES = str.maketrans({'\u2019': "'", '\u2018': "'", '\u02bc': "'", '\u2032': "'", '`': "'", '\u00b4': "'"})


_WS = _re.compile(r'[\s\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]+')


def _norm(text):
    # Matching only; the stored text is never rewritten. Round 8: every run of
    # whitespace (repeated spaces, tabs, soft line wraps, indentation, NBSP)
    # collapses to one space so the multiword grammar is formatting-invariant.
    return _WS.sub(' ', str(text or '').translate(_APOSTROPHES))


# Round 8: segment on semantic sentence boundaries only (terminal punctuation
# or a blank-line paragraph break). A soft line wrap never separates a
# prohibition/consent marker from its action/subject.
_SENTENCE_SPLIT = _re.compile(r'(?<=[.!?;])\s+|\n[ \t\u00a0]*\n\s*')


def _sentences(text):
    return [x for x in _SENTENCE_SPLIT.split(str(text or '')) if x and x.strip()]


# t_a9f7e29e: a pending decision names something that still waits on a
# person (until / awaiting / pending / without approval / yet / hold:). A
# scope disclaimer only limits what the task itself authorizes ("this sweep
# does not authorize restarts", "No external sends.") and a past-tense report
# records what did not happen ("No payment occurred."). Neither is a
# decision blocking the card.
DECISION = _re.compile(
    r"\buntil\b|\bawait\w*|\bpending\b|\bwait(s|ing)?\s+(for|on)\b|\bwithout\s+([\w']+\s+){0,3}?"
    r"(approval|sign-?off|consent|go-?ahead|decision|confirmation)|\byet\b|\bhold\s*:|\bon hold\b|"
    r"explicit hold|\bhold (for|until|pending)\b|do not proceed|don't proceed|"
    r"requires? (chris|owner|human|explicit)?\s*('s )?(approval|sign-?off|decision)|"
    r"needs? chris('s)? (approval|decision|sign-?off|go-?ahead|ok\b|to (approve|decide|sign off|confirm))|"
    r"approval (gate|required)|chris (must|has to|needs to) (approve|decide|sign)|"
    r"\b(ask|asks|asking) (chris|the owner)\b|\bfor (chris|owner)('s)? (approval|decision|review)", _re.I)
HOLD_DECISION_EXTRA = _re.compile(
    r"\b(consent|go-?ahead|sign-?off)\b|\bapprov(al|e|es)\b[^.;!?]*\b(chris|owner|human)\b|"
    r"\b(chris|owner|human)('s)? (approval|decision|consent)", _re.I)
SCOPE_DISCLAIMER = _re.compile(
    r"\b(does not|doesn't|do not|don't|is not|are not|isn't|aren't|not|nothing is|no \w+ (is|are))\s+"
    r"(\w+\s+){0,2}?authori[sz](e|es|ed|ing)?\b[^.;!?]*?\b(by|under|in|from|with)\s+(this|the|that)\s+"
    r"(sweep|task|card|run|clarification|resume|correction|lane|scope|instruction|request|authorization)|"
    r"\b(this|the) (sweep|task|card|run|lane|scope|clarification|authorization|resume)\b[^.;!?]*?\b"
    r"(does not|doesn't|never|cannot|can't) (authori[sz]e|grant|cover|permit|include)|"
    r"\b(are|is) (authori[sz]ed|permitted|allowed|in scope) (by|under|in) (this|the) "
    r"(sweep|task|card|run|clarification|resume|lane)|"
    r"^\W*no\b[^.;!?]*?\b(authori[sz]ed|permitted|allowed) (by|under|in) (this|the) "
    r"(sweep|task|card|run|clarification|resume|correction|lane|scope|instruction|request|authorization)", _re.I)
PAST_REPORT = _re.compile(
    r"^\W*(no|nothing|none|no new)\b[^.;!?]*?\b(was|were|is|are|has been|have been)?\s*"
    r"(performed|occurred|attempted|happened|changed|inferred|ingested|registered|accessed|found|"
    r"uploaded|created|charged|published|done|made|requested|executed|sent|used|submitted|assigned)\b", _re.I)


# Standing rules about honouring holds in general ("Never bypass a review
# gate, explicit hold or restart gate"; "Existing explicit holds remain
# controlling") name no pending decision of their own. The span covers the
# rule's own object list so "explicit hold" inside it is not re-read as a hold.
_GATE_NOUN = r"(gates?|holds?|protections?|denials?|reviews?|checks?|boundar(y|ies))"
GENERIC_RULE = _re.compile(
    r"\b(never|do not|don't|must not) bypass\b((\s*(,|or|and|nor)?\s*(a|an|the|any|every|all|existing)?\s*"
    r"((?!(?:await|pending|waiting|until|yet|for|on)\b)[\w/-]+\s+){0,2}?" + _GATE_NOUN + r"\b)+)(?!\s+(on|for|over|against|around|until|pending|covering)\b)|"
    r"\b(existing\s+)?(explicit\s+)?holds? remains? controlling\b|"
    r"\bpreserve (existing|all|every) (explicit )?(holds|gates|protections)\b", _re.I)

# t_67e74e1f: exemption is span-level, not sentence-level. Only the text a
# disclaimer pattern actually matched is removed; whatever remains in the same
# sentence ("..., and deployment is prohibited") is checked with the full
# classifier. Any recognized pending decision anywhere in the sentence
# disables the exemption entirely (baseline behaviour).
_DISCLAIMER_SPANS = (GENERIC_RULE, SCOPE_DISCLAIMER, PAST_REPORT)


# t_b97c5f67 round 5: generic rules are an allowlist too. The WHOLE sentence
# must be a standing rule whose objects are only gate nouns ("Never bypass a
# review gate, explicit hold or restart gate."). A coordinated action under the
# same negation ("Never bypass the review gate or deploy to production.") or any
# other mixed content keeps the sentence verbatim for the full classifier, so a
# shared prohibition is never stripped away with its negation.
# Only explicit gate descriptors are exempt. Unknown words remain protected;
# arbitrary modifiers can be verbs (e.g. "or publish reviews").
_GR_ITEM = (r"(?:(?:a|an|the|any|every|all|existing|other)\s+)?"
            r"(?:gates?|holds?|protections?|denials?|reviews?|checks?|boundaries|boundary|"
            r"(?:review|approval|restart|deployment|security|privacy|access|safety|release)\s+gates?|"
            r"explicit\s+holds?|privacy\s+boundaries|security\s+checks?|"
            r"tool\s+denials?|deployment/restart\s+gates?|security/privacy\s+boundar(?:y|ies))")
_GR_LIST = r"%s(?:\s*(?:,\s*(?:(?:or|and|nor)\s+)?|\s+(?:or|and|nor)\s+)%s)*" % (_GR_ITEM, _GR_ITEM)
GENERIC_FULL = _re.compile(
    r"\W*(?:(?:never|do not|don't|must not)\s+bypass\s+%s|"
    r"(?:existing\s+)?(?:explicit\s+)?holds?\s+remains?\s+controlling|"
    r"preserve\s+(?:existing|all|every)\s+(?:explicit\s+)?(?:holds|gates|protections))\s*[.!;]?\s*" % _GR_LIST,
    _re.I)


def _strip_rules(s):
    # Only a sentence that is entirely a generic gate rule is exempt.
    t = s.replace('\u2019', "'")
    m = GENERIC_FULL.fullmatch(t)
    if m:
        # Every list item must end in a gate noun, so a coordinated action
        # ("... or deploy to production") can never fullmatch.
        return ' '
    return s


def _has_decision(s):
    return bool(DECISION.search(s) or HOLD_DECISION_EXTRA.search(s))


# t_b97c5f67: scope/past-report exemptions are bounded to one grammatical
# clause. The sentence is split at commas, semicolons, dashes and clause
# conjunctions; a clause is dropped only when a scope/past pattern matches
# inside that clause and the clause carries no hold of its own once that
# span is removed. Every other clause is kept verbatim and the kept clauses
# are re-joined for the full classifier, so a ban before or after a
# disclaimer can never be swallowed. A sentence with no exempt clause is
# returned unchanged (identical to the pre-disclaimer baseline).
_CLAUSE_SPLIT = _re.compile(
    r"\s*[,;:\u2014\u2013]\s*|\s+-\s+|"
    r"\s+(?=(?:and|but|although|though|even if|even though|while|whereas|so|because|"
    r"however|nor|or)\b)", _re.I)
_CLAUSE_LEAD = _re.compile(
    r"^\W*(?:(?:and|but|although|though|even if|even though|while|whereas|so|because|"
    r"however|nor|or|also|then)\b\s*)+", _re.I)
_CLAUSE_SPANS = (SCOPE_DISCLAIMER, PAST_REPORT)


# t_b97c5f67 round 3: allowlist, not denylist. A clause is exempt only when
# the WHOLE clause is one of a few anchored templates whose subject is a bare
# noun list. Any function word, verb, parenthesis or subordinator inside the
# list (since / now that / regardless / except / permitted / ...) makes the
# clause ambiguous, and an ambiguous clause is kept for the full classifier.
_NOUN = (r"(?!(?:is|are|was|were|has|have|been|not|or|and|nor)\b)[a-z0-9][\w/'&.-]*")
_STOPS = r"(?:or|and|nor)"
_LIST_STOP = _re.compile(
    r"^(is|are|was|were|be|been|being|has|have|had|do|does|did|done|not|no|nothing|none|never|"
    r"must|may|might|can|cannot|could|should|would|will|shall|need|needs|until|unless|if|when|"
    r"while|since|because|now|that|which|who|whether|regardless|except|excluding|but|though|"
    r"although|so|than|then|after|before|once|without|only|yet|pending|awaiting|hold|"
    r"permitted|allowed|authori[sz]ed|prohibited|forbidden|banned|barred|approved|required|"
    r"occurred|performed|executed|sent)$", _re.I)
_PAST_VERB = (r"(performed|occurred|attempted|happened|changed|inferred|ingested|registered|"
              r"accessed|found|uploaded|created|charged|published|done|made|requested|executed|"
              r"sent|used|submitted|assigned)")
_SCOPE_NOUN = (r"(sweep|task|card|run|clarification|resume|correction|lane|scope|instruction|"
               r"request|authorization)")
_LIST_RX = r"(?P<list>%s(?:(?:,\s*|\s+)(?:%s\s+)?%s){0,24})" % (_NOUN, _STOPS, _NOUN)
_PURE_TEMPLATES = (
    # No restart, deploy, or send (was) performed / occurred.
    _re.compile(r"(?:no new|no|nothing|none)\s+%s?\s*(?:(?:was|were|has been|have been)\s+)?%s"
                % (_LIST_RX, _PAST_VERB), _re.I),
    _re.compile(r"(?:nothing|none)\s+(?:was|were|has been|have been)\s+%s" % _PAST_VERB, _re.I),
    # No sends, restarts or purchases (are) authorized by this sweep.
    _re.compile(r"(?:no|nothing)\s+%s?\s*(?:(?:is|are)\s+)?(?:authori[sz]ed|permitted|allowed|in scope)"
                r"\s+(?:by|under|in|from)\s+(?:this|the|that)\s+%s" % (_LIST_RX, _SCOPE_NOUN), _re.I),
    # This sweep does not authorize restarts or sends.
    _re.compile(r"(?:this|the)\s+%s\s+(?:does not|doesn't|cannot|can't|never)\s+"
                r"(?:authori[sz]e|grant|cover|permit|include)\s+%s" % (_SCOPE_NOUN, _LIST_RX), _re.I),
    # Restarts are not authorized by this sweep.
    _re.compile(r"%s\s+(?:is|are)\s+not\s+(?:authori[sz]ed|permitted|allowed|in scope)"
                r"\s+(?:by|under|in|from)\s+(?:this|the|that)\s+%s" % (_LIST_RX, _SCOPE_NOUN), _re.I),
)
# Elliptical continuation of an exempt past report ("No account was created,
# credentials accessed, fees charged"): exempt only directly after an exempt
# past-report clause.
_ELLIPTIC_PAST = _re.compile(r"%s\s+%s" % (_LIST_RX, _PAST_VERB), _re.I)


def _list_ok(m):
    items = _re.split(r"[\s,]+", m.groupdict().get('list') or '')
    items = [w for w in items if w and w.lower() not in ('or', 'and', 'nor')]
    return not any(_LIST_STOP.match(w) for w in items)


def _clean_clause(clause):
    return _CLAUSE_LEAD.sub('', clause).strip().rstrip('.!?;: ').strip()


def _clause_exempt(clause):
    c = _clean_clause(clause)
    if not c:
        return False
    for rx in _PURE_TEMPLATES:
        m = rx.fullmatch(c)
        if m and _list_ok(m):
            return True
    return False


# A fragment with no finite verb is a list item ("No restart, deploy, or send
# occurred"), not a clause. It joins the following fragment only when that
# fragment is itself a list continuation (verbless, or led by and/or/nor) and
# does not open a fresh negated clause ("No deploys, nothing was executed"
# stays two clauses, so the leading ban is kept).
_VERBISH = _re.compile(
    r"\b(is|are|was|were|be|been|being|has|have|had|do|does|did|must|may|might|can|"
    r"cannot|could|should|would|will|shall|needs?|never|not|occurred|happened)\b|n't\b|\b\w+ed\b", _re.I)
_LIST_CONT = _re.compile(r"^\W*(and|or|nor)\b", _re.I)
_NEW_NEG = _re.compile(r"^(no|nothing|none|never|do not|don't|must not|not)\b", _re.I)


def _clauses(s):
    frags = [p for p in _CLAUSE_SPLIT.split(s) if p and p.strip()]
    out, buf = [], ''
    for i, f in enumerate(frags):
        buf = f if not buf else buf + ', ' + f
        nxt = frags[i + 1] if i + 1 < len(frags) else None
        if nxt is not None and not _VERBISH.search(f):
            body = _CLAUSE_LEAD.sub('', nxt).strip()
            if (not _VERBISH.search(nxt) or _LIST_CONT.search(nxt)) and not _NEW_NEG.search(body):
                continue
        out.append(buf)
        buf = ''
    if buf:
        out.append(buf)
    return out


def _residual(s):
    s = _strip_rules(s)
    if not s.strip():
        return s
    parts = _clauses(s)
    kept, prev = [], False
    for p in parts:
        ex = _clause_exempt(p)
        if not ex and prev:
            m = _ELLIPTIC_PAST.fullmatch(_clean_clause(p))
            ex = bool(m and _list_ok(m))
        prev = ex
        if not ex:
            # A remaining negative coordination inherits its negation from
            # the preceding report/scope clause. Keep the original sentence
            # rather than stripping that antecedent and turning "nor may we
            # deploy" into an apparently affirmative permission. Pure reports
            # remain exempt; ambiguous connected clauses retain the hold.
            if _re.match(r"^\W*nor\b", p, _re.I):
                return s
            kept.append(p)
    if len(kept) == len(parts):
        return s
    return ', '.join(kept)


def _plain_hold(s):
    m = HOLD_MARKER.search(s)
    if m:
        p = PROTECTED.search(s)
        return p.group(0) if p else m.group(0)
    if PROHIBITION.search(s):
        p = PROTECTED.search(s) or ACTION.search(s)
        if p:
            return p.group(0)
    return None


def is_disclaimer(sentence):
    """True when the sentence only limits scope or reports a non-event."""
    s = _norm(sentence)
    if _has_decision(_strip_rules(s)):
        return False
    r = _residual(s)
    return r != s and _plain_hold(r) is None


def _decision_in_sentence(sentence):
    """Archive-grade check: only a still-pending decision protects."""
    if not _has_decision(_strip_rules(_norm(sentence))):
        return None
    return _hold_in_sentence(sentence)


def _hold_in_sentence(sentence):
    s = _norm(sentence)
    stripped = _strip_rules(s)
    if _has_decision(stripped):
        # A pending decision voids every exemption for the sentence
        # (baseline behaviour, full classifier on the original text).
        return _plain_hold(s)
    return _plain_hold(_residual(s))


def _explicit_hold_text(conn, task, decision_only=False):
    # t_fa49d830 round 3: location-invariant. Title, latest block reason, body
    # and comments all get the same explicit-hold check.
    texts = [str(getattr(task, 'title', '') or '')]
    try:
        texts.append(_last_block_reason(conn, task.id))
    except Exception:
        pass
    texts.append(str(getattr(task, 'body', '') or ''))
    try:
        texts += [r['body'] or '' for r in conn.execute(
            'SELECT body FROM task_comments WHERE task_id=? ORDER BY id', (task.id,)).fetchall()]
    except Exception:
        pass
    check = _decision_in_sentence if decision_only else _hold_in_sentence
    for text in texts:
        for sentence in _sentences(text):
            held = check(sentence)
            if held:
                return held
    return None


def _last_block_reason(conn, task_id):
    row = conn.execute(
        # t_fa49d830 round 9: read every stop event block_task really emits
        # (dependency_wait lands todo; block_loop_detected lands triage).
        "SELECT payload FROM task_events WHERE task_id=? AND kind IN "
        "('blocked','triaged','escalated','dependency_wait','block_loop_detected') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row or not row['payload']:
        return ''
    try:
        return str(json.loads(row['payload']).get('reason') or '')
    except Exception:
        return str(row['payload'])


STOP_EVENTS = ('blocked', 'triaged', 'escalated', 'dependency_wait', 'block_loop_detected')
# Releases that already passed validate_target (peers can never release a
# protected hold through them; owner/core release is the deliberate exit).
RELEASE_EVENTS = ('unblocked', 'promoted_manual', 'restore_released', 'triage_resumed')
TEXT_EDIT_EVENTS = ('instructions_repaired', 'specified', 'edited')


def _text_changed_since_release(conn, task, rel_id):
    """Round 11 (t_fa49d830): producer-independent text-edit detection.

    Every release event carries a fingerprint of the title/body it answered;
    any difference now means new material (dashboard PATCH, CLI, raw SQL).
    Releases without a fingerprint (pre-r11 history) fall back to known edit
    events, where 'edited' counts unless its payload names only non-text
    fields (complete_task summary/metadata edits).
    """
    from hermes_cli.kanban_db import released_text_sha
    row = conn.execute('SELECT payload FROM task_events WHERE id=?', (rel_id,)).fetchone()
    sha = None
    if row and row['payload']:
        try:
            sha = json.loads(row['payload']).get('_released_text_sha')
        except Exception:
            sha = None
    if sha:
        return sha != released_text_sha(task.title, task.body)
    for ev in conn.execute(
            'SELECT kind, payload FROM task_events WHERE task_id=? AND id>? AND kind IN (%s)'
            % ','.join('?' * len(TEXT_EDIT_EVENTS)), (task.id, rel_id, *TEXT_EDIT_EVENTS)):
        if ev['kind'] == 'instructions_repaired' or not ev['payload']:
            return True
        try:
            fields = json.loads(ev['payload']).get(
                'changed_fields' if ev['kind'] == 'specified' else 'fields')
        except Exception:
            return True
        if not isinstance(fields, list) or {'title', 'body'} & set(fields):
            return True
    return False


def _text_fields_to_rescan(conn, task, rel_id):
    """Round 14 (t_fa49d830): per-field answered identity plus chronology.

    Returns the subset of ('title', 'body') that must be re-scanned for holds.
    A field is answered only while its bytes equal what the release answered
    AND no real edit to that field happened after the release, so a hold
    removed and reasserted with identical bytes is a new obligation, while an
    edit to the other field never re-opens an answered hold. Releases without
    per-field identity fall back to the round-11 combined rule (fail closed).
    """
    from hermes_cli.kanban_db import released_field_sha
    row = conn.execute('SELECT payload FROM task_events WHERE id=?', (rel_id,)).fetchone()
    pl = {}
    if row and row['payload']:
        try:
            pl = json.loads(row['payload']) or {}
        except Exception:
            pl = {}
    tsha, bsha = pl.get('_released_title_sha'), pl.get('_released_body_sha')
    if not (isinstance(tsha, str) and isinstance(bsha, str)):
        return {'title', 'body'} if _text_changed_since_release(conn, task, rel_id) else set()
    out = set()
    if tsha != released_field_sha(task.title):
        out.add('title')
    if bsha != released_field_sha(task.body):
        out.add('body')
    for ev in conn.execute(
            'SELECT kind, payload FROM task_events WHERE task_id=? AND id>? AND kind IN (%s)'
            % ','.join('?' * len(TEXT_EDIT_EVENTS)), (task.id, rel_id, *TEXT_EDIT_EVENTS)):
        try:
            p = json.loads(ev['payload']) if ev['payload'] else None
        except Exception:
            p = None
        if ev['kind'] == 'instructions_repaired':
            out.add('body')
            continue
        # Round 15: producers record explicit scope; an explicit list (even
        # empty, e.g. status-only specify) is honoured per field. Only a
        # missing/malformed scope (legacy payload=None) is unknown.
        key = 'changed_fields' if ev['kind'] == 'specified' else 'fields'
        fields = p.get(key) if isinstance(p, dict) else None
        if not (isinstance(fields, list) and all(isinstance(f, str) for f in fields)):
            out |= {'title', 'body'}  # unknown scope: fail closed
        else:
            out |= {'title', 'body'} & set(fields)
    return out


def _release_comment_max_id(conn, rel_id):
    row = conn.execute('SELECT payload FROM task_events WHERE id=?', (rel_id,)).fetchone()
    if not row or not row['payload']:
        return None
    try:
        v = json.loads(row['payload']).get('_released_comment_max_id')
    except Exception:
        return None
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def unresolved_hold(conn, task, decision_only=False):
    """Round 10 (t_fa49d830): enforcement-grade hold check.

    Independent of block_kind and lane. Returns the held subject when an
    explicit recorded hold (title, body, comment or stop reason) has not been
    answered by a trusted release, else None. Material recorded after the
    latest trusted release (a new stop, a new comment, a text edit) is
    protected again. Same-second comments fail closed.
    """
    rel = conn.execute(
        'SELECT id, created_at FROM task_events WHERE task_id=? AND kind IN (%s) '
        'ORDER BY id DESC LIMIT 1' % ','.join('?' * len(RELEASE_EVENTS)),
        (task.id, *RELEASE_EVENTS)).fetchone()
    rel_id = rel['id'] if rel else 0
    texts = []
    rescan = {'title', 'body'} if rel is None else _text_fields_to_rescan(conn, task, rel_id)
    edited = bool(rescan)
    if 'title' in rescan:
        texts.append(str(task.title or ''))
    if 'body' in rescan:
        texts.append(str(task.body or ''))
    stop = conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND id>? AND kind IN (%s) '
        'ORDER BY id DESC LIMIT 1' % ','.join('?' * len(STOP_EVENTS)),
        (task.id, rel_id, *STOP_EVENTS)).fetchone()
    reason = ''
    if stop and stop['payload']:
        try:
            reason = str(json.loads(stop['payload']).get('reason') or '')
        except Exception:
            reason = str(stop['payload'])
        if decision_only:
            # The stop reason keeps the full hold grammar (see classify_hold).
            for sentence in _sentences(reason):
                held = _hold_in_sentence(sentence)
                if held:
                    return held
        else:
            texts.append(reason)
    if stop and task.block_kind and not decision_only:
        # A typed stop since the last release keeps the r9 keyword guard.
        m = PROTECTED.search(_norm(f"{task.title or ''} {reason}"))
        if m:
            return m.group(0)
    rows = conn.execute(
        'SELECT id, body, created_at FROM task_comments WHERE task_id=? ORDER BY id',
        (task.id,)).fetchall()
    max_id = _release_comment_max_id(conn, rel_id) if rel is not None else None
    if max_id is not None:
        # Round 12 (t_fa49d830): bind post-release comments by durable row
        # identity, never by event count / tail alignment, so inline
        # producers without a 'commented' event (ancestor invalidation)
        # cannot displace a real hold. Answered historical comments stay
        # answered.
        # Round 13: the identity boundary is independent of title/body
        # edits. Edited text is re-scanned above; an unrelated edit must not
        # re-open legitimately answered historical comments.
        rows = [r for r in rows if r['id'] > max_id]
    elif rel is not None and not edited:
        # Legacy release without identity: fail closed - every comment
        # at or after the release second, plus the last N rows by the
        # commented-event count.
        n = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND id>? AND kind='commented'",
            (task.id, rel_id)).fetchone()[0]
        tail = {r['id'] for r in rows[len(rows) - min(n, len(rows)):]} if n else set()
        rows = [r for r in rows
                if r['id'] in tail or (r['created_at'] or 0) >= rel['created_at']]
    texts += [r['body'] or '' for r in rows]
    check = _decision_in_sentence if decision_only else _hold_in_sentence
    for text in texts:
        for sentence in _sentences(text):
            held = check(sentence)
            if held:
                return held
    return None


def classify_hold(conn, task, decision_only=False):
    """Separate the four kinds of stop. Returns (kind, plain_reason).

    granted     - ordinary work the user already authorised; peers may resume
    technical   - access/tooling failure; peers may resume or retire it
    review      - an independent check is in flight; never self-cleared
    protected   - a genuinely new consequential user decision
    """
    reason = _last_block_reason(conn, task.id)
    text = _norm(f"{task.title or ''} {reason}")
    if task.status == 'review':
        return 'review', 'An independent check is in progress.'
    if decision_only:
        # t_a9f7e29e: for reversible retirement, a protected word in the
        # title or a scope disclaimer is not a pending decision. A block
        # reason that names a protected action AND a pending decision is.
        # The latest stop reason is why the card stopped, so it keeps the
        # full hold grammar (only disclaimers/non-event reports are dropped).
        for sentence in _sentences(reason):
            held = _hold_in_sentence(sentence)
            if held:
                return 'protected', f'Explicit recorded hold: needs Chris to decide on {held.lower()}.'
    elif PROTECTED.search(text):
        m = PROTECTED.search(text).group(0)
        return 'protected', f'Needs Chris to decide: this involves {m.lower()}.'
    held = _explicit_hold_text(conn, task, decision_only)
    if held:
        return 'protected', f'Explicit recorded hold: needs Chris to decide on {held.lower()}.'
    if task.block_kind in ('capability', 'transient'):
        return 'technical', 'Stopped on a technical access or tooling failure, not missing consent.'
    return 'granted', 'Ordinary work already authorised by the original request.'


def _proof(args):
    from agent.delegation_context import is_delegated_child_process_context
    from gateway.gus_worker_authority import current
    from gateway.josh_board_authority import admitted_board, restricted
    if is_delegated_child_process_context():
        raise PermissionError('Delegated children must return results to their parent')
    worker = current()
    from hermes_cli.board_owner_manage import member_controller_scope
    member = member_controller_scope()
    if member is not None:
        if worker is not None or getattr(args, 'board', None) != member['board']:
            raise PermissionError('Member management outside authenticated board')
        if args.kanban_action not in {'block','archive','restore','specify','resume-triage','unblock','unlink','recover_review','repair_review_reference'}:
            raise PermissionError('Unsupported member board action')
        # The exact signed operation/board/target is rechecked in the mutation transaction.
        return None
    if getattr(args, 'owner_continuation', False):
        # An operator/chief-of-staff continuation is not peer authority.
        # Never derive it from the supplied comment author or a --by value.
        if (worker is not None or restricted()
                or os.environ.get('HERMES_SESSION_SOURCE') == 'kanban'
                or os.environ.get('HERMES_PROFILE', 'default') not in ('', 'default')):
            raise PermissionError('Owner continuation is unavailable to task workers and member profiles')
        if args.kanban_action != 'resume-triage':
            raise PermissionError('Owner continuation applies only to same-card resume-triage')
    if worker is None:
        # Member sessions cannot use this administrative escape hatch.
        if restricted() or os.environ.get('HERMES_SESSION_SOURCE') == 'kanban':
            raise PermissionError('Native GUS administrative assignment required')
        return None
    if args.kanban_action == 'specify' and (getattr(args, 'all_triage', False) or not getattr(args, 'task_id', None)):
        # t_1cc0200c: one named triage card at a time; never a blanket sweep.
        raise PermissionError('Administrative specify takes exactly one task id, never --all')
    board = admitted_board(getattr(args, 'board', None))
    if board != worker['board']:
        raise PermissionError('Administrative CLI board mismatch')
    import yaml
    from hermes_constants import get_hermes_home
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import resolve_toolset
    config = yaml.safe_load((Path(get_hermes_home()) / 'config.yaml').read_text())
    enabled = _get_platform_tools(config, 'cli', include_default_mcp_servers=False)
    disabled = config.get('agent', {}).get('disabled_toolsets', [])
    names = set().union(*(set(resolve_toolset(x)) for x in enabled if x not in disabled))
    if 'terminal' not in names:
        raise PermissionError('Existing native terminal capability required')
    action = args.kanban_action
    if action not in READS | MUTATIONS:
        raise PermissionError('Operation is outside the bounded administrative CLI; use native tools')
    if getattr(args, 'purge_ids', None) or getattr(args, 'force', False):
        raise PermissionError('Purge and forced transitions are unavailable to workers')
    if action in MUTATIONS and not str(getattr(args, 'reason', '') or '').strip():
        raise PermissionError('Administrative changes require --reason with recorded evidence')
    args.board = board
    if action == 'unblock':
        args.preserve_failure_history = True
    return worker


def _assignment(conn, worker):
    from hermes_cli import kanban_db as kb
    own = kb.get_task(conn, worker['task'])
    if (own is None or own.assignee != worker['profile'] or own.status != 'running'
            or not own.current_run_id or not own.worker_pid):
        raise PermissionError('Administrative native assignment is no longer running')
    run = conn.execute('SELECT profile,status,worker_admission_contract FROM task_runs WHERE id=? AND task_id=?', (own.current_run_id, own.id)).fetchone()
    contract = json.loads(run['worker_admission_contract'] or '{}') if run else {}
    available = contract.get('skill_dependencies', {}).get('tool_schema', {}).get('available_tools', [])
    if (run is None or run['profile'] != worker['profile'] or run['status'] != 'running'
            or contract.get('profile') != worker['profile'] or contract.get('task') != own.id
            or 'terminal' not in available):
        raise PermissionError('Current admitted worker must already expose terminal')
    # Phase-specific review never becomes administrative maker authority.
    row = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (own.id,)).fetchone()
    if row is not None and row['phase'] == 'review':
        raise PermissionError('Reviewer cannot mutate other tasks through administrative CLI')


@contextmanager
def admission(args):
    worker = _proof(args)
    if worker is not None:
        from hermes_cli import kanban_db as kb
        with kb.connect_closing() as conn:
            _assignment(conn, worker)
    token = _active.set(worker)
    owner_token = _owner_continuation.set(bool(getattr(args, 'owner_continuation', False)))
    try:
        yield
    finally:
        _owner_continuation.reset(owner_token)
        _active.reset(token)


def owner_continuation_active():
    return _owner_continuation.get()


def _validate_unlink(conn, pair, worker):
    from hermes_cli import kanban_db as kb
    # t_f283b52b: the child's owner may drop a nonessential scheduling
    # edge; QA/review/privacy edges and active children are preserved.
    parent_id, child_id = pair
    child = kb.get_task(conn, child_id)
    parent = kb.get_task(conn, parent_id)
    if child is None or parent is None:
        raise PermissionError('Unlink needs an existing parent and child')
    if child.id == worker['task'] or parent.id == worker['task']:
        raise PermissionError('Administrative CLI cannot change its own assignment')
    if child.assignee != worker['profile']:
        raise PermissionError('Only the child task owner can remove its scheduling dependency')
    if child.tenant and PRIVATE_TENANT.search(child.tenant):
        raise PermissionError('Private or creator-lane task is outside administrative cleanup scope')
    import time as _t
    if (child.current_run_id or child.worker_pid or child.claim_lock or child.claim_expires
            or child.status in {'running', 'review', 'done', 'archived'}
            or conn.execute('SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL', (child_id,)).fetchone()
            or conn.execute('SELECT 1 FROM target_leases WHERE task_id=? AND expires_at>=?', (child_id, int(_t.time()))).fetchone()):
        raise PermissionError('Active tasks cannot be changed by administrative cleanup')
    if (parent.assignee in REVIEW_PROFILES or PROTECTED_EDGE.search(parent.title or '')):
        raise PermissionError('QA, review and privacy dependencies are preserved')
    return True


def validate_target(conn, task_id, operation, reason=None):
    """Must run within the supported mutator's write transaction."""
    from hermes_cli.board_owner_manage import validate_bound_target
    validate_bound_target(conn, task_id, operation)
    if operation == 'archive':
        # Shared boundary (t_fa49d830): owner/core callers get the same
        # reason requirement before the worker-only checks below.
        require_retirement_reason(reason)
    worker = _active.get()
    if worker is None:
        return False
    from hermes_cli import kanban_db as kb
    _assignment(conn, worker)
    if operation == 'unlink':
        return _validate_unlink(conn, task_id, worker)
    task = kb.get_task(conn, task_id)
    if task is None or task.id == worker['task']:
        raise PermissionError('Administrative CLI cannot change its own assignment or a missing task')
    # Self-clear prohibition: the profile doing the work never clears its own stop.
    if task.assignee and task.assignee == worker['profile'] and operation in {'unblock', 'promote', 'release'}:
        raise PermissionError('The assigned worker cannot clear its own stop; a different agent must')
    if task.tenant and PRIVATE_TENANT.search(task.tenant):
        raise PermissionError('Private or creator-lane task is outside administrative cleanup scope')
    if operation == 'restore':
        if task.status != 'archived':
            raise PermissionError('Only archived tasks can be restored')
        return True
    if operation in {'resume-triage', 'resume-qa'}:
        # t_f283b52b: same-card triage continuation. State/evidence binding
        # (exact stop event, later comment, no newer hold) is enforced by
        # kanban_db.resume_triage_task for every caller; peer-only limits here.
        if task.assignee and task.assignee == worker['profile']:
            raise PermissionError('The assigned worker cannot clear its own stop; a different agent must')
        if task.current_run_id or task.worker_pid or task.claim_lock:
            raise PermissionError('Active tasks cannot be changed by administrative cleanup')
        if operation == 'resume-qa':
            from gateway.qa_continuation import validate as validate_qa
            validate_qa(conn, task)
        if task.status != ('blocked' if operation == 'resume-qa' else 'triage'):
            raise PermissionError('Only idle triage cards or scoped QA blocks can be resumed')
        goal = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        if goal and goal['phase'] == 'review':
            raise PermissionError('Preserve active review and scheduled holds')
        from hermes_cli.kanban_db import _gate_preserved_only
        text = f"{task.title or ''}. {_last_block_reason(conn, task_id)}"
        for m in NEVER_PEER_RESUMABLE.finditer(_norm(text)):
            # Negated/preserved mentions ("no spend", "nothing published")
            # are not pending decisions; any affirmative mention refuses.
            if not _gate_preserved_only(text, m.group(0)):
                raise PermissionError(f'Needs Chris to decide: this involves {m.group(0).lower()}; peers cannot resume it.')
        if not kb._parents_satisfied(conn, task_id):
            raise PermissionError('Dependencies must be satisfied without force')
        return True
    if operation == 'release':
        # Ending a restore-only pause is a resume: same active/review/hold
        # guards as promote. Dependency gating stays with recompute_ready.
        if task.current_run_id or task.worker_pid or task.claim_lock:
            raise PermissionError('Active tasks cannot be changed by administrative cleanup')
        if task.status in {'review', 'running', 'scheduled'}:
            raise PermissionError('Preserve active review and scheduled holds')
        goal = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
        if goal and goal['phase'] == 'review':
            raise PermissionError('Preserve active review and scheduled holds')
        kind, plain = classify_hold(conn, task)
        if kind == 'protected':
            raise PermissionError(plain)
        return True
    if task.current_run_id or task.worker_pid or task.claim_lock:
        raise PermissionError('Active tasks cannot be changed by administrative cleanup')
    goal = conn.execute('SELECT phase FROM owner_cell_goals WHERE task_id=?', (task_id,)).fetchone()
    # Only a review that is actually happening is protected. An idle triage
    # card that merely carries a review-phase goal is a stale projection.
    if task.status in {'review', 'running', 'scheduled'}:
        raise PermissionError('Preserve active review and scheduled holds')
    if goal and goal['phase'] == 'review' and operation != 'archive':
        raise PermissionError('Preserve active review and scheduled holds')
    # t_a9f7e29e: archival (reversible, never completion) of a card that is
    # not a typed needs_input decision only honours pending decisions, not
    # scope disclaimers or protected words in the title. Every resume path
    # keeps the full classifier.
    archive_lenient = operation == 'archive' and task.block_kind != 'needs_input'
    kind, plain = classify_hold(conn, task, decision_only=archive_lenient)
    if operation in {'unblock', 'promote'} and kind == 'protected':
        # No approval laundering: protected decisions are never resumed by a
        # peer, whatever the block_kind and wherever the hold was recorded.
        raise PermissionError(plain)
    if operation == 'archive':
        # Archive is reversible removal from active view, never completion.
        # Protected obligations stay visible until Chris decides them.
        # Round 9: triage (loop-routed) and typed dependency waits are not
        # answered decisions either.
        if kind == 'protected' and (task.status in {'blocked', 'triage'} or task.block_kind):
            raise PermissionError(plain + ' It stays visible until then.')
        # Round 10: an unanswered explicit hold protects todo/ready cards too,
        # independent of block_kind.
        held = unresolved_hold(conn, task, decision_only=archive_lenient) if task.status != 'done' else None
        if held:
            raise PermissionError(f'Explicit recorded hold: needs Chris to decide on {held.lower()}. It stays visible until then.')
        if task.status not in {'triage', 'todo', 'ready', 'done', 'blocked'}:
            raise PermissionError('Task is not eligible for reversible archival')
        for child_id in kb.child_ids(conn, task_id):
            child = kb.get_task(conn, child_id)
            if child and child.status not in {'done', 'archived'}:
                raise PermissionError('Archival would change a live dependency; preserve it')
    elif operation == 'specify':
        # Guarded triage -> todo. Status-only; dependency gating stays with
        # recompute_ready, so open parents keep the card in todo.
        if task.status != 'triage':
            raise PermissionError('Only triage cards can be specified')
        if task.assignee and task.assignee == worker['profile']:
            raise PermissionError('The assigned worker cannot clear its own stop; a different agent must')
        if kind == 'protected':
            raise PermissionError(plain)
    elif operation in {'promote', 'unblock'}:
        if task.status == 'triage' and operation == 'unblock':
            raise PermissionError('Use promote for triage cards')
        if not kb._parents_satisfied(conn, task_id):
            raise PermissionError('Dependencies must be satisfied without force')
        if operation == 'unblock' and task.status != 'blocked':
            raise PermissionError('Only resolved dependency blocks can be resumed')
    else:
        raise PermissionError('Unsupported administrative mutation')
    return True


def active():
    return _active.get() is not None
