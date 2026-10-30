"""Blocked work: equip the assigned agent first, reassign only as last resort.

t_be56cfc0 shipped automatic pick-up; t_96d87a50 (Chris): "I obviously want the
agents who are assigned to do the work. If the agent can't do the work, the main
step should be giving them the access or tools they need, not dishing it off to
another agent."  Order, on every board, inside that board owner's own scope:

1. Keep the assigned agent; read the recorded unmet requirement
   (``capability_preflight_blocked.unmet``).
2. Internal setup gap -> add it to THAT agent's profile (backup first, minimal
   text insertion, logged to Agent-Shared/knowledge/fixes-log.md), then requeue
   the same agent with a note naming what it now has.  Setup gaps: missing
   built-in toolset, MCP server already configured in the agent's own profile
   but not enabled, shared skill missing from the profile, read path inside our
   own workspace (Chris boards only), installed Chris-side profile not enrolled
   in the worker roster (Chris boards only).
3. Gaps setup cannot close (credential, account login/browser session, MCP
   auth, network/server grants, missing input) are never auto-granted: the card
   becomes ``needs_input`` with one plain ask to Chris, once.  Reassignment
   happens only when the assignee is not a real worker profile (human identity
   or profile not installed) and the board has a head in its own scope.
4. Never widen across owners: non-Chris boards only receive generic toolsets
   and shared skills -- never paths, roster enrollment, MCP servers or
   credentials.  Holds, needs_input and member-private routes are untouched.
5. Julia's Hermes profile (~/.hermes/profiles/julia) is a real worker.

Agent-reported capability/transient blocks stay with the same-owner lane
(``auto_same_owner_recovery``); they are never handed to another agent.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from hermes_cli import kanban_db as kb

ACTOR = 'blocked-lane-recovery'
EVENT = 'auto_lane_recovery'
COOLDOWN = 600
ROOT = Path.home() / '.hermes'
AGENT_SHARED = Path('/Users/Chris/.openclaw/workspace/Agent-Shared')
FIXES_LOG = AGENT_SHARED / 'knowledge' / 'fixes-log.md'
REGISTRY = AGENT_SHARED / 'roles' / 'role-registry.yaml'
# Board -> owner. Unknown boards get the most restrictive (non-Chris) treatment.
OWNER = {'chris-home': 'chris', 'chris': 'chris', 'josh-home': 'josh',
         'connor-home': 'connor', 'britt-home': 'britt', 'britt': 'britt'}
OWNER_NAME = {'chris': 'Chris', 'josh': 'Josh', 'connor': 'Connor', 'britt': 'Britt'}
# Last-resort head per board, inside that board's own scope only.
FALLBACK = {'chris-home': 'scarlett', 'chris': 'scarlett'}
# Human / owner identities, never worker lanes. Julia's Hermes profile IS a worker.
# t_9ac68588: no global non-worker list; board ownership decides admission.
# Toolsets any owner's agent may receive (no Chris data/accounts behind them).
GENERIC_TOOLSETS = frozenset({'terminal', 'file', 'kanban', 'skills', 'todo', 'memory', 'vision',
                              'code_execution', 'delegation', 'web', 'clarify', 'session_search'})
# Unmet codes setup cannot close: one plain ask to Chris, never auto-granted.
GRANT_CODES = {
    'authenticated_browser_phase_required': 'sign in to the account the browser step needs',
    'mcp_connection_or_auth_unavailable': 'connect or re-authorize the {r} integration',
    'terminal_network_denied': 'allow outbound network for this agent',
    'localhost_listen_denied': 'allow this agent to run a local server',
    'missing_input': 'provide the missing input {r}',
    'missing_command': 'install the command {r} on the Mac',
}


def _payload(raw):
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def is_worker_profile(name, root=None):
    root = Path(root) if root else ROOT
    if not name:
        return False
    return (root / 'profiles' / name / 'config.yaml').is_file()


def _registry_owner_boards():
    # t_10f94d4e: registered members' boards are owner boards, never Chris's.
    try:
        from gateway.member_registry import members
        for p, r in members().items():
            short = r.get('short') or p
            OWNER.setdefault(r['board'], short)
            OWNER_NAME.setdefault(short, short.capitalize())
    except Exception:
        pass


_registry_owner_boards()


def profile_owner(name, root=None):
    # Member-created agents carry their verified owner in config (owner_agent.owner).
    if name.startswith('owner-agent-'):
        try:
            import yaml
            cfg = yaml.safe_load(((Path(root) if root else ROOT) / 'profiles' / name / 'config.yaml').read_text()) or {}
            return ((cfg.get('owner_agent') or {}).get('owner')) or 'unknown'
        except Exception:
            return 'unknown'
    if name.startswith('josh-agent-') or 'joshgarcia' in name:
        return 'josh'
    try:
        from gateway.member_registry import owner_of_profile, members
        p = owner_of_profile(name)
        if p:
            return members()[p].get('short') or p
    except Exception:
        pass
    if 'connor' in name:
        return 'connor'
    if name.startswith('britt'):
        return 'britt'
    return 'chris'


def _last_stop(conn, task_id):
    return conn.execute("SELECT id,kind,payload,created_at FROM task_events WHERE task_id=? AND kind IN "
                        "('blocked','capability_preflight_blocked','triaged','escalated','block_loop_detected') "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def _since_assigned(conn, task_id):
    return conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=? AND kind='assigned'",
                        (task_id,)).fetchone()[0]


def _private_route(task):
    tenant = task['tenant'] or ''
    return (tenant.startswith('app:private:') or tenant.startswith('member:')
            or bool(task['owner_profile']) or bool(task['runtime_profile']))


def _held(conn, task):
    from gateway.gus_admin_cli import classify_hold
    from hermes_cli.auto_same_owner_recovery import complete_record_hold
    kind, why = classify_hold(conn, kb.get_task(conn, task['id']))
    if kind != 'technical':
        return why
    return complete_record_hold(conn, task['id'])


# ---------------------------------------------------------------- classify

def _shared_skill(name, shared):
    for skill in Path(shared).rglob('SKILL.md'):
        if skill.parent.name == name:
            return skill.parent
    return None


def _valid_toolset(name):
    if name in ('all', '*'):
        return False
    try:
        from toolsets import validate_toolset
        return bool(validate_toolset(name))
    except Exception:
        return name in GENERIC_TOOLSETS


def setup_plan(board, assignee, unmet, root=None, shared=None, verified_owner=None):
    """Classify a recorded unmet requirement -> (fixes, asks, refused). No writes.

    ``verified_owner`` is passed only by the authenticated member entrypoint after
    it proved ``assignee`` is in that member's own roster (t_735766e7)."""
    import yaml
    root = Path(root) if root else ROOT
    shared = Path(shared) if shared else root / 'skills'
    owner = verified_owner or OWNER.get(board, 'other')
    if verified_owner == 'chris':
        raise PermissionError('Member entrypoint never carries Chris-side scope')
    home = root / 'profiles' / assignee
    cfg = yaml.safe_load((home / 'config.yaml').read_text()) or {}
    fixes, asks, refused = [], [], []
    if verified_owner is None and profile_owner(assignee, root) != owner:
        return fixes, asks, [f'{assignee} is not in the {board} owner scope']
    for issue in unmet.get('issues') or []:
        code, res = issue.get('code'), str(issue.get('resource') or '')
        if code == 'missing_toolset':
            if not _valid_toolset(res):
                refused.append(f'unknown toolset {res}')
            elif owner != 'chris' and res not in GENERIC_TOOLSETS:
                refused.append(f'toolset {res} is not shared across owners')
            else:
                fixes.append(('toolset', res))
        elif code == 'missing_mcp_server':
            if owner == 'chris' and isinstance((cfg.get('mcp_servers') or {}).get(res), dict):
                fixes.append(('toolset', res))  # installed for this agent already; enable it
            else:
                asks.append(f'set up the {res} integration for {assignee}')
        elif code == 'path_outside_workspace':
            p = Path(res).expanduser().resolve()
            ours = any(p == a or a in p.parents for a in (root.resolve(), AGENT_SHARED))
            own = home.resolve()
            mine = p == own or own in p.parents  # the agent's own workspace only
            if (owner == 'chris' and ours or owner != 'chris' and mine) \
                    and res in (unmet.get('read_paths') or [res]) \
                    and res not in (unmet.get('write_paths') or []):
                fixes.append(('read_root', str(p)))
            else:
                refused.append(f'path {res} is outside {assignee} scope')
        elif code in GRANT_CODES:
            asks.append(GRANT_CODES[code].format(r=res))
        elif code:
            refused.append(f'{code} {res}'.strip())
    have = {s.parent.name for s in (home / 'skills').rglob('SKILL.md')} if (home / 'skills').is_dir() else set()
    for name in unmet.get('skills') or []:
        if name not in have:
            if _shared_skill(name, shared) is not None:
                fixes.append(('skill', name))
            else:
                refused.append(f'skill {name} is not a shared skill')
    if unmet.get('profile_installed') and unmet.get('enrolled_worker') is False \
            and unmet.get('hermes_worker_lane') is not False:
        if owner == 'chris':
            fixes.append(('enroll', assignee))
        else:
            refused.append(f'{assignee} roster enrollment is Chris-side only')
    return fixes, asks, refused


# ---------------------------------------------------------------- setup

_KEY = re.compile(r'^(\s*)([^\s#:-][^:]*):\s*(\[\])?\s*(#.*)?$')


def _indent(line):
    return len(line) - len(line.lstrip())


def _insert_list_item(text, keys, value):
    """Append ``- value`` to the block list at ``keys``; None if not found."""
    lines = text.split('\n')
    lo, hi, depth, at = 0, len(lines), None, None
    for key in keys:
        at, child = None, None
        for i in range(lo, hi):
            s = lines[i]
            if not s.strip() or s.lstrip().startswith('#'):
                continue
            if depth is not None and _indent(s) <= depth:
                hi = i
                break
            if child is None:
                child = _indent(s)
            m = _KEY.match(s)
            if m and _indent(s) == child and m.group(2).strip() == key:
                at = i
                break
        if at is None:
            return None
        depth, lo, hi = _indent(lines[at]), at + 1, len(lines)
    m = _KEY.match(lines[at])
    ind = m.group(1)
    if m.group(3):
        lines[at] = f'{ind}{m.group(2)}:'
        lines.insert(at + 1, f'{ind}- {value}')
        return '\n'.join(lines)
    j, item = at + 1, None
    while j < len(lines) and lines[j].strip().startswith('- ') and _indent(lines[j]) >= len(ind):
        item = lines[j][:_indent(lines[j])]
        j += 1
    if item is None:
        return None
    lines.insert(j, f'{item}- {value}')
    return '\n'.join(lines)


def _edit_yaml(path, edits, stamp, verify):
    """Backup, minimal text edit, verify parse; restore on any mismatch."""
    import yaml
    original = path.read_text()
    before = yaml.safe_load(original) or {}
    text = original
    for keys, value in edits:
        new = _insert_list_item(text, keys, value)
        if new is not None:
            text = new
    if text == original:
        return None
    backup = path.with_name(path.name + f'.bak-t_96d87a50-{stamp}')
    shutil.copy2(path, backup)
    path.write_text(text)
    try:
        after = yaml.safe_load(text) or {}
        if set(after) != set(before) or not verify(after):
            raise ValueError('verification failed')
    except Exception:
        shutil.copy2(backup, path)
        return None
    return backup


def apply_setup(assignee, fixes, root=None, registry=None, shared=None, stamp=None):
    import yaml
    root = Path(root) if root else ROOT
    registry = Path(registry) if registry else REGISTRY
    shared = Path(shared) if shared else root / 'skills'
    stamp = stamp or time.strftime('%Y%m%d%H%M%S')
    cfg_path = root / 'profiles' / assignee / 'config.yaml'
    done = []
    for kind, value in fixes:
        if kind == 'toolset':
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            cli = (cfg.get('platform_toolsets') or {}).get('cli')
            edits = []
            if isinstance(cli, list) and value not in cli:
                edits.append((['platform_toolsets', 'cli'], value))
            if isinstance(cfg.get('toolsets'), list) and value not in cfg['toolsets']:
                edits.append((['toolsets'], value))
            def ok(c, v=value):
                lst = (c.get('platform_toolsets') or {}).get('cli')
                return v in (lst if isinstance(lst, list) else c.get('toolsets') or [])
            if edits and _edit_yaml(cfg_path, edits, stamp, ok):
                done.append(f'the {value} toolset')
        elif kind == 'read_root':
            def ok(c, v=value):
                return v in ((((c.get('security') or {}).get('workspace_confinement') or {})
                              .get('read_only_roots')) or [])
            if _edit_yaml(cfg_path, [(['security', 'workspace_confinement', 'read_only_roots'], value)], stamp, ok):
                done.append(f'read access to {value}')
        elif kind == 'skill':
            src = _shared_skill(value, shared)
            if src is None:
                continue
            dest = root / 'profiles' / assignee / 'skills' / src.relative_to(shared)
            if not dest.exists():
                shutil.copytree(src, dest)
                done.append(f'the {value} skill')
        elif kind == 'enroll':
            r = (yaml.safe_load(registry.read_text()) or {}).get('profile_roster') or {}
            edits = [(['profile_roster', k], value) for k in ('canonical', 'department_makers')
                     if value not in (r.get(k) or [])]
            def ok(c, v=value):
                rr = c.get('profile_roster') or {}
                return v in (rr.get('canonical') or []) and v in (rr.get('department_makers') or [])
            if edits and _edit_yaml(registry, edits, stamp, ok):
                done.append('worker roster enrollment')
    return done


def log_fix(task_id, board, assignee, done, path=None):
    path = Path(path) if path else FIXES_LOG
    with open(path, 'a') as fh:
        fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} — blocked-lane-recovery {board}/{task_id}\n"
                 f"- Gave {assignee} {', '.join(done)} (backup *.bak-t_96d87a50-*); requeued the same agent.\n")


# ---------------------------------------------------------------- plan

def plan(conn, board, now=None, root=None, shared=None):
    now = int(time.time()) if now is None else now
    fallback = FALLBACK.get(board)
    out = []
    for task in conn.execute("SELECT * FROM tasks WHERE status='blocked' AND block_kind IN ('capability','transient') "
                             "AND claim_lock IS NULL ORDER BY id").fetchall():
        tid, assignee = task['id'], task['assignee']
        stop = _last_stop(conn, tid)
        if stop is None:
            out.append((tid, 'skip', 'no recorded stop')); continue
        held = _held(conn, task)
        if held:
            out.append((tid, 'hold', held)); continue
        if _private_route(task):
            out.append((tid, 'skip', 'member-private/owner-bound route')); continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND id>?",
                        (tid, EVENT, stop['id'])).fetchone():
            out.append((tid, 'skip', 'already acted on this stop')); continue
        if stop['kind'] != 'capability_preflight_blocked':
            out.append((tid, 'defer', 'same-owner recovery lane; assigned agent keeps the card')); continue
        if not is_worker_profile(assignee, root):
            if fallback and fallback != assignee:
                out.append((tid, 'reassign', {'to': fallback, 'stop': stop['id'],
                                              'why': f'{assignee} is not a Hermes worker profile (last resort)'}))
            else:
                out.append((tid, 'skip', 'non-worker assignee and no head in this board scope'))
            continue
        if now - stop['created_at'] < COOLDOWN:
            out.append((tid, 'wait', 'cooldown')); continue
        unmet = _payload(stop['payload']).get('unmet') or {}
        try:
            fixes, asks, refused = setup_plan(board, assignee, unmet, root, shared)
        except Exception as exc:
            out.append((tid, 'skip', f'setup plan failed: {type(exc).__name__}')); continue
        if asks:
            if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND payload LIKE ?",
                            (tid, EVENT, '%"action": "ask"%')).fetchone():
                out.append((tid, 'skip', 'Chris already asked once')); continue
            out.append((tid, 'ask', {'stop': stop['id'], 'asks': asks,
                                     'why': 'needs a grant that setup cannot make'})); continue
        if fixes:
            out.append((tid, 'equip', {'stop': stop['id'], 'fixes': fixes, 'refused': refused,
                                       'why': 'internal setup gap'})); continue
        prior = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=? AND id>?",
                             (tid, EVENT, _since_assigned(conn, tid))).fetchone()[0]
        if prior == 0 and not refused:
            out.append((tid, 'requeue', {'stop': stop['id'],
                                         'why': 'admission failed without a recorded setup gap; one same-agent retry'}))
        else:
            out.append((tid, 'skip', 'assigned agent kept; no setup fix available' +
                        (f" ({'; '.join(refused)})" if refused else '')))
    return out


def _last_failure(conn, task_id, stop_id):
    p = _payload(conn.execute('SELECT payload FROM task_events WHERE id=?', (stop_id,)).fetchone()[0])
    parts = [str(p.get('reason') or '')]
    if p.get('unmet'):
        parts.append('unmet: ' + json.dumps(p['unmet'], sort_keys=True))
    return ' '.join(' '.join(parts).split())[:900]


def ask(conn, task_id, detail, who='Chris'):
    """Credential/account gap: never auto-granted; one plain ask to the board owner."""
    task = kb.get_task(conn, task_id)
    text = f"{task.assignee} needs you to " + '; '.join(dict.fromkeys(detail['asks'])) + '.'
    with kb.write_txn(conn):
        cur = conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=? AND status='blocked' "
                           "AND claim_lock IS NULL", (task_id,))
        if cur.rowcount != 1:
            return None
        kb._append_event(conn, task_id, EVENT, {'actor': ACTOR, 'action': 'ask', 'from': task.assignee,
                                                'to': task.assignee, 'why': detail['why'], 'ask': text,
                                                'stop_event': detail['stop'], 'approval_granted': False,
                                                'owner_asked': who, 'status': 'blocked'})
    kb.add_comment(conn, task_id, ACTOR, f"Needs {who}: {text} Nothing was granted automatically; "
                   f"{task.assignee} keeps the card.")
    return 'blocked'


def apply(conn, task_id, action, detail, note=None):
    from hermes_cli import owner_work_start as ows
    from hermes_cli import owner_work_preparation as prep
    task = prep._task(conn, task_id)
    prep._idle(conn, task)
    previous = task['assignee']
    failure = _last_failure(conn, task_id, detail['stop'])
    if action == 'reassign':
        if not kb.assign_task(conn, task_id, detail['to']):
            return None
    who = detail.get('to') or previous
    if note:
        instruction = (f"Automatic recovery: your profile now has {note}, added for this card. "
                       f"Previous admission failure: {failure} Continue the original task yourself. "
                       "All recorded holds and approval gates remain in force.")
    else:
        instruction = (f"Automatic recovery ({detail['why']}). Last failure: {failure} "
                       "Do not repeat that attempt: take a different authorized path (another tool, account, "
                       "script or lane we already have). All recorded holds and approval gates remain in force.")
    instruction = ' '.join(instruction.split())[:2000]
    with kb.write_txn(conn):
        cur = conn.execute('SELECT status,assignee,claim_lock FROM tasks WHERE id=?', (task_id,)).fetchone()
        if cur['status'] != 'blocked' or cur['claim_lock'] is not None or cur['assignee'] != who:
            return None
        target = kb._landing_status_after_parents(conn, task_id)
        binding = ows._binding(conn, task_id)
        order = {'instruction': instruction, 'recovery': True, 'phase': 'execute',
                 'binding_digest': binding, 'plan_digest': None}
        order['revision'] = prep._hash({'instruction': instruction, 'binding': binding, 'plan_digest': None})
        budget = ows._budget(prep._task(conn, task_id)); budget['work_order'] = order
        conn.execute('UPDATE tasks SET status=?,block_kind=NULL,worker_budget_json=? WHERE id=?',
                     (target, json.dumps(budget, sort_keys=True), task_id))
        latest = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_comments WHERE task_id=?', (task_id,)).fetchone()[0]
        last_run = conn.execute('SELECT COALESCE(MAX(id),0) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0]
        common = {'version': 1, 'actor': ACTOR, 'work_order': order, 'latest_comment_id': latest,
                  'prior_run_id': last_run, 'preserved_stop': {'event_id': detail['stop'], 'reason': failure},
                  'status': target, 'automatic': True, 'approval_granted': False,
                  'action_restrictions_preserved': True}
        kb._append_event(conn, task_id, ows.EVENT, common)
        kb._append_event(conn, task_id, EVENT, {'actor': ACTOR, 'action': action, 'from': previous,
                                                'to': who, 'why': detail['why'], 'stop_event': detail['stop'],
                                                'equipped': note, 'status': target})
    if note:
        kb.add_comment(conn, task_id, ACTOR, f"{who} now has {note}; requeued to {who}.")
    return target


def run(conn, board, now=None, dry_run=False, root=None, registry=None, shared=None, fixes_log=None):
    report = []
    for tid, verdict, detail in plan(conn, board, now, root, shared):
        if verdict not in ('reassign', 'requeue', 'equip', 'ask'):
            report.append({'task': tid, 'action': verdict, 'detail': detail}); continue
        status, extra = None, {}
        if not dry_run:
            try:
                if verdict == 'ask':
                    status = ask(conn, tid, detail, OWNER_NAME.get(OWNER.get(board), 'the board owner'))
                elif verdict == 'equip':
                    assignee = kb.get_task(conn, tid).assignee
                    done = apply_setup(assignee, detail['fixes'], root, registry, shared)
                    if not done:
                        report.append({'task': tid, 'action': 'skip', 'detail': 'setup change did not apply'}); continue
                    log_fix(tid, board, assignee, done, fixes_log)
                    extra['equipped'] = done
                    status = apply(conn, tid, 'equip', detail, note=', '.join(done))
                else:
                    status = apply(conn, tid, verdict, detail)
            except Exception as exc:  # one card never stops the sweep
                report.append({'task': tid, 'action': 'error', 'detail': type(exc).__name__ + ': ' + str(exc)[:200]})
                continue
        report.append({'task': tid, 'action': verdict if (status or dry_run) else 'raced',
                       'to': detail.get('to'), 'why': detail['why'], 'status': status,
                       **({'asks': detail['asks']} if verdict == 'ask' else {}),
                       **({'fixes': detail['fixes']} if verdict == 'equip' else {}), **extra})
    return report


def run_member(conn, board, task_id, owner, roster, root=None, shared=None, fixes_log=None):
    """Authenticated member's main agent: equip-first recovery for ONE card on its own board.

    ``owner``/``roster``/``board`` come from the caller's verified enrollment, never the model.
    Only the member's own roster agents; generic toolsets, shared skills, MCP already in that
    agent's own profile, and paths inside that agent's own home. Grants -> one ask to the owner.
    """
    if owner in (None, '', 'chris', 'default'):
        raise PermissionError('Member recovery is for enrolled non-Chris owners only')
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        raise ValueError('Unknown task in your workspace')
    assignee = task['assignee']
    # t_f8d88c0b: a card in review/done/running is assigned to its reviewer or
    # still working; report its live status instead of a false roster error.
    if task['status'] != 'blocked' or task['claim_lock'] is not None:
        return {'task': task_id, 'action': 'skip', 'status': task['status'],
                'detail': f"card is {task['status']}, not blocked; nothing to repair"}
    if assignee not in set(roster):
        raise PermissionError('Assigned agent is not in your own roster')
    stop = _last_stop(conn, task_id)
    if stop is None or stop['kind'] != 'capability_preflight_blocked':
        return {'task': task_id, 'action': 'skip', 'detail': 'no recorded setup gap; use own_work_manage work/unblock'}
    held = _held(conn, task)
    if held:
        return {'task': task_id, 'action': 'hold', 'detail': held}
    unmet = _payload(stop['payload']).get('unmet') or {}
    fixes, asks, refused = setup_plan(board, assignee, unmet, root, shared, verified_owner=owner)
    # Member scope: never roster enrollment; MCP only if already in that agent's own profile.
    fixes = [f for f in fixes if f[0] in ('toolset', 'skill', 'read_root')]
    who = OWNER_NAME.get(owner, owner.capitalize())
    if asks:
        detail = {'stop': stop['id'], 'asks': asks, 'why': 'needs a grant that setup cannot make'}
        return {'task': task_id, 'action': 'ask', 'asks': asks, 'status': ask(conn, task_id, detail, who)}
    if not fixes:
        return {'task': task_id, 'action': 'skip', 'detail': 'no setup fix in your scope', 'refused': refused}
    done = apply_setup(assignee, fixes, root, None, shared)
    if not done:
        return {'task': task_id, 'action': 'skip', 'detail': 'setup change did not apply'}
    log_fix(task_id, board, assignee, done, fixes_log)
    detail = {'stop': stop['id'], 'why': 'internal setup gap (owner agent)'}
    return {'task': task_id, 'action': 'equip', 'equipped': done, 'refused': refused,
            'status': apply(conn, task_id, 'equip', detail, note=', '.join(done))}


if __name__ == '__main__':
    import sys
    dry = '--dry-run' in sys.argv
    board = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--board=')), 'chris-home')
    db = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--db=')), None)
    ctx = kb.connect_closing(Path(db), board=board) if db else kb.connect_closing(board=board)
    with ctx as c:
        rep = run(c, board, dry_run=dry)
    acted = [r for r in rep if r['action'] in ('reassign', 'requeue', 'equip', 'ask', 'error')]
    print(json.dumps(rep if '-v' in sys.argv else acted, indent=1))
