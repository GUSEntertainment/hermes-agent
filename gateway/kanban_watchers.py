"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Per-tick work lives in
``kanban_watchers_notifier`` / ``kanban_watchers_dispatcher``; shared plumbing
in ``kanban_watchers_common``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from agent.i18n import t
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _gc_retention_days,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import MAX_SEND_FAILURES, TERMINAL_KINDS
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)

_HELD_DELIVERY_BACKOFF: dict = {}


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_GC_INTERVAL_SECONDS = 3600.0
_HEALTH_WINDOW = 6


_LOCAL_PATH_RE = re.compile(
    r"(?<![\\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\\s,;]+|"
    r"[A-Za-z]:\\\\[^\\s,;]+)"
)


def _safe_review_reason(value: Any, limit: int = 160) -> str:
    from agent.redact import redact_sensitive_text
    reason = redact_sensitive_text("" if value is None else str(value), force=True, redact_url_credentials=True)
    reason = _LOCAL_PATH_RE.sub("[local path]", reason)
    reason = " ".join(reason.split())
    return reason[:limit - 1].rstrip() + "…" if len(reason) > limit else reason


_UNSAFE_ROOT_PROFILE_MARKERS = (
    "Kanban worker spawn refused:",
    "resolves to the root Hermes home",
    "interactive default gateway",
)


def _safe_block_notification_reason(value: Any, limit: int = 160) -> str:
    """Return a concise blocked-task reason for external notifications."""
    raw = "" if value is None else str(value)
    if all(marker in raw for marker in _UNSAFE_ROOT_PROFILE_MARKERS):
        return (
            "unsafe default worker profile; reassign this card to a named "
            "specialist profile"
        )
    return _safe_review_reason(raw, limit)


def _is_unsafe_root_profile_block_event(ev: Any) -> bool:
    payload = getattr(ev, "payload", None)
    if not isinstance(payload, dict):
        return False
    raw = str(payload.get("reason") or "")
    return all(marker in raw for marker in _UNSAFE_ROOT_PROFILE_MARKERS)


def _session_id_matches_subscription(task: Any, sub: dict) -> bool:
    session_id = str(getattr(task, "session_id", "") or "")
    if not session_id:
        return False
    parts = session_id.split(":")
    if len(parts) < 5 or parts[0] != "agent" or parts[1] != "main":
        return False
    if parts[2] != str(sub.get("platform") or ""):
        return False
    if parts[4] != str(sub.get("chat_id") or ""):
        return False
    subscribed_thread = str(sub.get("thread_id") or "")
    if subscribed_thread and subscribed_thread not in parts[5:]:
        return False
    return True


def _has_exact_notification_origin(task: Any, sub: dict) -> bool:
    metadata = sub.get("delivery_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    task_id = str(sub.get("task_id") or "")
    origin_task = str(metadata.get("_kanban_subscription_origin_task_id") or "")
    if origin_task:
        return origin_task == task_id and not metadata.get("_kanban_inherited_subscription")
    return _session_id_matches_subscription(task, sub)


def _should_suppress_worker_machinery_notice(task: Any, sub: dict, ev: Any) -> bool:
    """Suppress internal worker guard noise on non-origin subscriptions."""
    if not _is_unsafe_root_profile_block_event(ev):
        return False
    metadata = sub.get("delivery_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("_kanban_inherited_authorized") is True:
        return False
    return not _has_exact_notification_origin(task, sub)


# 2026-10-05 (Chris): member/owner chats (iMessage via Photon, the Margot
# app) never see board plumbing. Only a completed result reaches them, as
# one natural message plus a PDF/Desk document. Operator Telegram lanes keep
# the existing per-event notices.
MEMBER_CHAT_PLATFORMS = frozenset({"photon", "api_server"})
_PLUMBING_RE = re.compile(
    r"\bt_[0-9a-f]{6,}\b|\bkanban\b|\[[\w-]+\]|@[\w-]+|\b(?:worker|reviewer|review|board|card|"
    r"handoff|subscription|profile|runtime|workspace|preflight|digest|sha256)\b|/Users/\S+|~/\S+",
    re.I,
)


def is_member_chat(sub: dict) -> bool:
    return str(sub.get("platform") or "").lower() in MEMBER_CHAT_PLATFORMS


def member_completion_text(title: str, has_file: bool) -> str:
    """One short human line. Never IDs, board tags, assignees or worker text."""
    clean = _PLUMBING_RE.sub("", str(title or "")).strip(" -—:.")
    clean = re.sub(r"\s+", " ", clean)[:120].rstrip(" ,;:-—…")
    if has_file:
        return f"All done — “{clean}” is ready. I've attached it as a PDF." if clean else "All done — I've attached it as a PDF."
    return f"All done — “{clean}” is finished." if clean else "All done — that's finished."


def _external_delivery_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove notifier-internal provenance keys before adapter delivery."""
    return {
        key: value
        for key, value in metadata.items()
        if not str(key).startswith("_kanban_")
    }


_AD_FAILED_UNTIL: dict = {}  # (board, task_id) -> epoch; failed auto-decompose cards are skipped for 1h

def _resolve_command_boards(config: Any) -> tuple[str, ...] | None:
    """Resolve the shared command-board allowlist without an implicit board."""
    from hermes_cli.command_boards import resolve_command_boards

    return resolve_command_boards(config, default="")


def _resolve_adaptive_controller(kanban_cfg: Any):
    """Return an opt-in adaptive controller; malformed enabled config is fatal."""
    if not isinstance(kanban_cfg, dict):
        return None
    raw = kanban_cfg.get("adaptive_concurrency")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("kanban.adaptive_concurrency must be a mapping")
    enabled = raw.get("enabled", False)
    if enabled is not True:
        if enabled not in (False, None):
            raise ValueError("kanban.adaptive_concurrency.enabled must be boolean")
        return None

    def _positive(name: str, default: int | None = None) -> int | None:
        value = raw.get(name, default)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"kanban.adaptive_concurrency.{name} must be a positive integer")
        return value

    from gateway.kanban_admission import AdaptiveAdmissionController

    recovery_samples = _positive("recovery_samples", 2)
    growth_step = _positive("growth_step", 2)
    assert recovery_samples is not None and growth_step is not None
    return AdaptiveAdmissionController(
        maximum_capacity=_positive("maximum_capacity"),
        recovery_samples=recovery_samples,
        growth_step=growth_step,
    )


def _resolve_report_back_settings(config: Any) -> Optional[dict[str, Any]]:
    """Resolve the explicit trusted report-back gate and dependency closure."""
    if not isinstance(config, dict):
        return None
    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        return None
    raw = kanban_cfg.get("report_back")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("kanban.report_back must be a mapping")
    enabled = raw.get("enabled", False)
    if enabled is not True:
        if enabled not in (False, None):
            raise ValueError("kanban.report_back.enabled must be boolean")
        return None
    dry_run = raw.get("dry_run", False)
    principal_registry = raw.get("principal_registry")
    department_registry = raw.get("department_registry")
    command_boards = _resolve_command_boards(config)
    if not isinstance(dry_run, bool):
        raise ValueError("kanban.report_back.dry_run must be boolean")
    if not isinstance(principal_registry, dict):
        raise ValueError("kanban.report_back principal registry is missing or malformed")
    if not isinstance(department_registry, dict):
        raise ValueError("kanban.report_back department registry is missing or malformed")
    if not command_boards:
        raise ValueError("kanban.report_back requires explicit command_boards")
    return {
        "dry_run": dry_run,
        "principal_registry": principal_registry,
        "department_registry": department_registry,
        "command_boards": frozenset(command_boards),
    }


def _wake_scope_id(adapter: Any, sub: dict) -> Optional[str]:
    """Return the tenant scope (Slack workspace) a subscription's wake keys to.

    ``build_session_key()`` includes ``SessionSource.scope_id`` on platforms
    where one bot serves several isolated tenants, so a wake source must carry
    the same scope as inbound messages from that chat to resolve to the same
    session.

    The subscription's persisted ``delivery_metadata`` wins over the adapter's
    live chat → scope mapping, because it records the scope the subscription was
    created from; the mapping only covers rows that stored no metadata. ``None``
    means the chat has no scope, which is what an unscoped platform's key
    contains.
    """
    delivery_meta = sub.get("delivery_metadata")
    if isinstance(delivery_meta, dict):
        for key in ("scope_id", "slack_team_id", "team_id"):
            value = delivery_meta.get(key)
            if value:
                return str(value)
    resolver = getattr(adapter, "scope_id_for_chat", None)
    if callable(resolver):
        try:
            resolved = resolver(str(sub.get("chat_id") or ""))
        except Exception as exc:
            logger.debug("kanban notifier: scope lookup failed for chat %s: %s", sub.get("chat_id"), exc, exc_info=True)
            return None
        if resolved:
            return str(resolved)
    return None



def _pin_first_call(kb, fn):
    """Run *fn* with env-pin-first board resolution (machine flow over enumerated boards)."""
    def _wrapped():
        with kb.pin_first_board_resolution():
            return fn()
    return _wrapped

class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        Per subscription, claims ``task_events`` newer than the stored cursor
        (kinds in TERMINAL_KINDS), sends one message per event, then advances
        the cursor. The subscription is removed only when the task is
        ``archived``: ``done`` is reversible, so the cursor — not unsubscribing
        — is the dedup mechanism (unsub-on-terminal dropped users when the
        dispatcher respawned a crashed task). All SQLite work runs in a thread;
        one tick's failure never stops the next.
        """
        try:
            from hermes_cli.config import load_config as _load_config

            cfg = _load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); continuing enabled", exc)
            kanban_cfg = {}
        if not kanban_cfg.get("notify_in_gateway", True):
            logger.info("kanban notifier: disabled via config kanban.notify_in_gateway=false")
            return

        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        sub_fail_counts: dict[tuple, int] = getattr(self, "_kanban_sub_fail_counts", {})
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name()
        self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC: subs survive ``done``, so boards that never
        # archive would accumulate rows scanned every tick. One DELETE per
        # board, at startup (0 → first tick) and at most hourly.
        _gc_next_at = 0.0

        while self._running:
            try:
                # Trusted report-back is independently opt-in and re-read each
                # tick.  An explicitly enabled but incomplete dependency set is
                # a fail-closed state: no Desk materialization, platform send,
                # or cursor claim/finalization is allowed for the tick.
                _report_back_settings = None
                _attention_config = {}
                _report_back_error: Optional[str] = None
                try:
                    from hermes_cli.config import load_config as _load_report_cfg

                    _attention_config = _load_report_cfg() or {}
                    _report_back_settings = _resolve_report_back_settings(_attention_config)
                except ValueError as exc:
                    _report_back_error = str(exc)
                except Exception as exc:
                    # Privacy must be monotonic across preprocessing failures:
                    # an unreadable config cannot prove the feature is off, so
                    # suppress claims/sends for this tick rather than falling
                    # through to the raw legacy formatter.
                    _report_back_error = f"config unavailable: {exc}"

                _gc_due = time.monotonic() >= _gc_next_at
                _retention = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    _retention = _gc_retention_days()

                def _collect():
                    deliveries: list[dict] = []
                    include_unowned = self._owns_kanban_dispatcher_lock()
                    notifier_profiles = {notifier_profile}
                    notifier_profiles.update(
                        str(profile).strip()
                        for profile in getattr(self, "_profile_adapters", {})
                        if str(profile).strip()
                    )
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    # Widen to every platform any secondary profile has live,
                    # not just the default profile's. This is only a coarse
                    # pre-filter to skip claiming events for subs nobody can
                    # possibly deliver — the precise per-profile check (via
                    # gateway/authz_mixin.py::_authorization_adapter, which
                    # forbids default-profile fallback) still runs at delivery
                    # time below, rewinding the claim if it resolves to None.
                    # Without this, a subscription owned by a secondary
                    # profile on a platform the DEFAULT profile never
                    # connected (e.g. beta owns discord, default doesn't) was
                    # dropped here before ever being claimed — no rewind
                    # applies to an unclaimed event, so it silently never
                    # retries.
                    for _profile_adapter_map in getattr(self, "_profile_adapters", {}).values():
                        active_platforms.update(
                            getattr(platform, "value", str(platform)).lower()
                            for platform in _profile_adapter_map.keys()
                        )
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        # Zero-subscription early exit: probe the board with a
                        # cheap read-only connection BEFORE the writable
                        # `connect()`. A board with no subscriptions has
                        # nothing to notify, and the writable open (schema
                        # init/migration on first open, WAL/-shm sidecars,
                        # checkpoint traffic) is exactly the per-tick cost
                        # this skip avoids.
                        try:
                            if _kb.count_notify_subs(
                                board=slug,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                            ) == 0 and not (
                                (_attention_config.get("kanban") or {}).get("owner_attention", {}).get("enabled") is True
                                and (_kb.count_notify_subs(board=slug, platform="api_server") > 0
                                     or __import__('gateway.owner_attention_binding',fromlist=['count_sources']).count_sources(slug) > 0)
                            ):
                                logger.debug(
                                    "kanban notifier: board %s has no subscriptions owned by %s; skipping open",
                                    slug, sorted(notifier_profiles),
                                )
                                continue
                        except Exception as exc:
                            logger.debug(
                                "kanban notifier: read-only subscription probe failed "
                                "for board %s (%s); falling back to writable open",
                                slug, exc,
                            )
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            if _gc_due:
                                # Hourly (plus once at startup) stale-sub GC:
                                # drop subscriptions for tasks that have been
                                # ``done`` untouched past the retention
                                # window. Best-effort — a failed sweep never
                                # blocks delivery; the next hourly gate
                                # retries it.
                                try:
                                    _purged = _kb.purge_stale_done_notify_subs(
                                        conn,
                                        max_age_days=_gc_retention_days,
                                    )
                                    if _purged:
                                        logger.info(
                                            "kanban notifier: purged %d stale done-task subscription(s) on board %s (retention %dd)",
                                            _purged, slug, _gc_retention_days,
                                        )
                                except Exception as _gc_exc:
                                    logger.debug(
                                        "kanban notifier: stale-sub GC failed for board %s: %s",
                                        slug, _gc_exc,
                                    )
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            # App returns are durable local replies, not sends.
                            # Multiplexed API profiles need not own push adapters.
                            from gateway.owner_app_returns import pump as pump_owner_app_returns
                            pump_owner_app_returns(conn)
                            subs = _kb.list_notify_subs(
                                conn,
                                notifier_profiles=notifier_profiles,
                                include_unowned=include_unowned,
                                include_internal_metadata=True,
                            )
                            from gateway.owner_attention import collect as collect_owner_attention, settings as owner_attention_settings
                            if _report_back_error is None and owner_attention_settings(_attention_config):
                                # API-native owner profiles need not own a push adapter.
                                # This widens only passive scoped inbox collection.
                                from gateway.kanban_delivery_receipts import identity as notification_identity
                                attention_subs = {notification_identity(sub): sub for sub in subs}
                                for api_sub in _kb.list_notify_subs(conn, include_internal_metadata=True):
                                    if api_sub.get("platform") == "api_server":
                                        attention_subs[notification_identity(api_sub)] = api_sub
                                from gateway.owner_attention_binding import subscriptions as bound_attention_subscriptions
                                for bound_sub in bound_attention_subscriptions(conn):
                                    if bound_sub.get("platform") == "api_server" or bound_sub.get("notifier_profile") in notifier_profiles:
                                        attention_subs[notification_identity(bound_sub)] = bound_sub
                                for attention in collect_owner_attention(conn, list(attention_subs.values()), _attention_config):
                                    deliveries.append({"attention": attention, "board": slug})
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                try:
                                    if _report_back_error is not None:
                                        logger.warning(
                                            "kanban report-back dependencies failed closed "
                                            "on board %s; notification claim suppressed: %s",
                                            slug, _report_back_error,
                                        )
                                        continue
                                    owner_profile = sub.get("notifier_profile") or None
                                    if owner_profile and owner_profile != notifier_profile:
                                        _owner_adapters = getattr(self, "_profile_adapters", {}).get(owner_profile)
                                        if not _owner_adapters:
                                            logger.debug(
                                                "kanban notifier: subscription for %s owned by profile %s; current profile %s has no adapter for it, skipping",
                                                sub.get("task_id"), owner_profile, notifier_profile,
                                            )
                                            continue
                                    platform = (sub.get("platform") or "").lower()
                                    if platform == 'api_server' and owner_profile == 'joshgarcia':
                                        source=conn.execute('SELECT binding_json FROM task_return_intents WHERE task_id=?',(sub['task_id'],)).fetchone()
                                        if source and json.loads(source[0]).get('kind')=='owner-delegation-return-v1':
                                            continue  # persisted by the local app-return pump
                                    if platform not in active_platforms:
                                        logger.debug(
                                            "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                            sub.get("task_id"), platform or "<missing>",
                                        )
                                        continue
                                    from gateway.secondary_documents import current as secondary_current
                                    secondary_document = secondary_current(conn, sub)
                                    from gateway.kanban_delivery_receipts import stage_and_recover
                                    # 2026-09-26 (Chris): the receipts layer only resolves its own
                                    # pending intent via its deliver(); when the agent wake is
                                    # the delivery (wake_agent subs), a staged intent stays
                                    # "pending" and resurrects the completed event every tick
                                    # (t_1513e6fd: 40+ wakes). Only stage for pure text-notify subs.
                                    _dm = str(sub.get("delivery_mode") or "")
                                    recoverable = stage_and_recover(conn, sub) if (_dm == "notify" and platform != "api_server") else []
                                    old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    if recoverable:
                                        events = sorted({event.id:event for event in [*events,*recoverable]}.values(), key=lambda event:event.id)
                                    if secondary_document:
                                        events = [event for event in events if event.kind == "completed"]
                                    from gateway.owner_attention import owns_event as attention_owns_event
                                    events = [event for event in events if not attention_owns_event(
                                        conn, sub, event, _attention_config)]
                                    if not events:
                                        continue
                                    task = _kb.get_task(conn, sub["task_id"])
                                    trusted_report_back = False
                                    trusted_report_back_enabled = False
                                    trusted_decisions: dict[int, Any] = {}
                                    if (
                                        not secondary_document
                                        and _report_back_settings is not None
                                        and slug in _report_back_settings["command_boards"]
                                        and task is not None
                                    ):
                                        trusted_report_back_enabled = True
                                        is_root = conn.execute(
                                            "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1",
                                            (sub["task_id"],),
                                        ).fetchone() is None
                                        if is_root:
                                            delivery_metadata = sub.get("delivery_metadata")
                                            authenticated_principal = (
                                                delivery_metadata.get("authenticated_principal")
                                                if isinstance(delivery_metadata, dict)
                                                else None
                                            )
                                            try:
                                                from gateway.kanban_report_back import (
                                                    record_report_back_dry_run,
                                                )

                                                for event in events:
                                                    decision = record_report_back_dry_run(
                                                        conn,
                                                        task=task,
                                                        event=event,
                                                        board=slug,
                                                        authenticated_principal=authenticated_principal,
                                                        principal_registry=_report_back_settings[
                                                            "principal_registry"
                                                        ],
                                                        department_registry=_report_back_settings[
                                                            "department_registry"
                                                        ],
                                                        dry_run=_report_back_settings["dry_run"],
                                                    )
                                                    trusted_report_back = (
                                                        trusted_report_back
                                                        or decision is not None
                                                    )
                                                    if decision is not None:
                                                        trusted_decisions[event.id] = decision
                                            except ValueError as exc:
                                                _kb.rewind_notify_cursor(
                                                    conn,
                                                    task_id=sub["task_id"],
                                                    platform=sub["platform"],
                                                    chat_id=sub["chat_id"],
                                                    thread_id=sub.get("thread_id") or "",
                                                    claimed_cursor=cursor,
                                                    old_cursor=old_cursor,
                                                )
                                                logger.warning(
                                                    "kanban report-back validation failed "
                                                    "closed for %s on board %s: %s",
                                                    sub["task_id"], slug, exc,
                                                )
                                                continue
                                            except Exception as exc:
                                                _kb.rewind_notify_cursor(
                                                    conn,
                                                    task_id=sub["task_id"],
                                                    platform=sub["platform"],
                                                    chat_id=sub["chat_id"],
                                                    thread_id=sub.get("thread_id") or "",
                                                    claimed_cursor=cursor,
                                                    old_cursor=old_cursor,
                                                )
                                                logger.warning(
                                                    "kanban report-back materialization failed "
                                                    "closed for %s on board %s: %s",
                                                    sub["task_id"], slug, exc,
                                                )
                                                continue
                                    logger.debug(
                                        "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                        len(events), sub["task_id"], slug, old_cursor, cursor,
                                    )
                                    deliveries.append({
                                        "sub": sub,
                                        "old_cursor": old_cursor,
                                        "cursor": cursor,
                                        "events": events,
                                        "task": task,
                                        "board": slug,
                                        "trusted_report_back": trusted_report_back,
                                        "trusted_report_back_enabled": trusted_report_back_enabled,
                                        "trusted_decisions": trusted_decisions,
                                    })
                                except Exception as sub_exc:
                                    # Isolate per-subscription failures so one
                                    # bad subscription cannot block delivery for
                                    # all other subscriptions in this tick.
                                    logger.warning(
                                        "kanban notifier: subscription for %s on board %s failed: %s",
                                        sub.get("task_id"), slug, sub_exc,
                                    )
                        finally:
                            conn.close()
                    return deliveries

                def _collect_pinned():
                    # Board slugs here are list_boards() iteration variables, not caller intent:
                    # resolve env-pin-first like upstream's _Collector (v0.22.5), or a
                    # HERMES_KANBAN_DB-pinned gateway reads empty per-slug DBs and never delivers.
                    with _kb.pin_first_board_resolution():
                        return _collect()

                deliveries = await asyncio.to_thread(_collect_pinned)
                for d in deliveries:
                    if "attention" in d:
                        from gateway.owner_attention import deliver as deliver_owner_attention
                        attention = d["attention"]
                        source = attention["sub"]
                        try:
                            platform = _Platform(source["platform"])
                            adapter = self._authorization_adapter(platform, source.get("notifier_profile") or None)
                            with _kb.pin_first_board_resolution():
                                conn = _kb.connect(board=d["board"])
                            try:
                                await deliver_owner_attention(conn, attention, adapter, _attention_config)
                            finally:
                                conn.close()
                        except (PermissionError, ValueError):
                            pass  # missing/revoked original source never falls back
                        continue
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    trusted_decisions = d.get("trusted_decisions") or {}

                    def _approved_trusted_decision(decision: Any) -> bool:
                        if not getattr(decision, "telegram_allowed", False):
                            return False
                        if platform_str != "telegram":
                            return False
                        destination = getattr(decision, "telegram_destination", {})
                        if not isinstance(destination, dict):
                            return False
                        if str(destination.get("chat_id")) != str(sub.get("chat_id")):
                            return False
                        topic = destination.get("topic_id")
                        subscribed_topic = sub.get("thread_id") or ""
                        return topic in (None, "", subscribed_topic) or str(topic) == str(
                            subscribed_topic
                        )

                    if d.get("trusted_report_back_enabled") and not any(
                        _approved_trusted_decision(decision)
                        for decision in trusted_decisions.values()
                    ):
                        # Trusted report-back owns the whole external-delivery
                        # decision on configured command boards. Descendants,
                        # Desk-only roots, and subscriptions that are not the
                        # registry destination are intentionally ACKed here;
                        # they must never fall through to the legacy formatter.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = self._authorization_adapter(plat, sub_profile or None)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await _to_thread_process_service(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    # Per-subscription failure-counter key. Hoisted out of the
                    # event loop: the wake self-post path (in the loop's
                    # ``else`` clause) needs it even when every event in the
                    # claim was skipped before reaching the send site.
                    sub_key = (
                        sub["task_id"], sub["platform"],
                        sub["chat_id"], sub.get("thread_id") or "",
                    )
                    mode = sub.get("delivery_mode") or "notify"
                    wake_agent = mode in ("notify+wake", "wake")
                    # 2026-09-25 (Chris): when Margot is woken she is the
                    # delivery; never also push raw "[board] @who Kanban t_x"
                    # machinery text into a human chat.
                    send_passive = not wake_agent
                    # Worker handoff carried into the synthetic wake turn below
                    # (#70752): without it the woken creator only sees
                    # "Task X completed" and re-decomposes work that already
                    # exists on the board.
                    wake_handoff = ""
                    wake_review_detail = ""
                    allowed_wake_kinds: set[str] = set()
                    member_chat = is_member_chat(sub)
                    for ev in d["events"]:
                        kind = ev.kind
                        msg = ""
                        if member_chat and kind != "completed":
                            # Internal transitions stay internal for members;
                            # the cursor still advances past them below.
                            continue
                        if _should_suppress_worker_machinery_notice(task, sub, ev):
                            logger.debug(
                                "kanban notifier: suppressed inherited unsafe-profile "
                                "notice for %s on %s/%s",
                                sub["task_id"],
                                platform_str,
                                sub["chat_id"],
                            )
                            continue
                        if d.get("trusted_report_back_enabled"):
                            decision = trusted_decisions.get(ev.id)
                            if not _approved_trusted_decision(decision):
                                continue
                            assert decision is not None
                            # The policy decision is the sole content source in
                            # trusted mode. Never re-read raw summary/result/
                            # reason fields after privacy preprocessing.
                            msg = str(decision.redacted_summary)
                            wake_handoff = msg
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if d.get("trusted_report_back_enabled"):
                            pass
                        elif kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                                wake_handoff = h
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                                wake_handoff = r
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason_text = _safe_block_notification_reason(
                                    ev.payload.get("reason")
                                )
                                if reason_text:
                                    reason = f": {reason_text}"
                            msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                # [v0.21.5 upstream wording] plain-language crash notice.
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} — its worker stopped "
                                f"unexpectedly; it will be retried automatically."
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {board_tag}{tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        elif kind == "status":
                            new_status = ""
                            if ev.payload and ev.payload.get("status"):
                                new_status = str(ev.payload["status"])
                            msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
                        elif kind == "review_requested":
                            # Implementation complete; task moved to the
                            # first-class review lane. Wake the origin thread.
                            handoff = ""
                            if ev.payload and ev.payload.get("summary"):
                                summary = str(ev.payload["summary"])
                                handoff = f"\n{summary[:200]}"
                                # Carry the worker's handoff into the wake turn
                                # like ``completed`` does: a reviewer woken with
                                # a bare "ready for review" has to re-read the
                                # board to learn what was implemented.
                                lines = summary.strip().splitlines()
                                wake_handoff = (
                                    lines[0][:200] if lines else summary[:200]
                                )
                            msg = (
                                f"👀 {board_tag}{tag}Kanban {sub['task_id']} ready for review"
                                f" — {title}{handoff}"
                            )
                        elif kind == "changes_requested":
                            payload = ev.payload or {}
                            reason = _safe_review_reason(payload.get("reason"))
                            reviewer = _safe_review_reason(payload.get("reviewer"), 48)
                            implementer = _safe_review_reason(payload.get("implementer"), 48)
                            reason_text = reason or "reviewer feedback requires changes"
                            provenance = ""
                            if reviewer:
                                provenance += f" — reviewer @{reviewer}"
                            if implementer:
                                provenance += f" → implementer @{implementer}"
                            msg = (
                                f"🛑 {board_tag}Kanban {sub['task_id']} review requested "
                                f"changes/BLOCK: {reason_text}{provenance}"
                            )
                            wake_review_detail = reason_text
                        elif kind == "block_loop_detected":
                            # A task re-blocked for the same cause past the
                            # recurrence limit and was routed to `triage` for a
                            # human decision. This is the ONE transition that
                            # exists to force human attention, yet it emits no
                            # `blocked`/`status` event — so before adding it to
                            # TERMINAL_KINDS it produced zero notification and
                            # the task stalled in triage silently. Ping loudly.
                            reason = ""
                            recurrences = None
                            if ev.payload:
                                if ev.payload.get("reason"):
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                                recurrences = ev.payload.get("recurrences")
                            rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
                            msg = (
                                f"🛑 {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
                                f" — needs a human decision{rc}{reason}"
                            )
                        else:
                            # archived / unblocked are claimed by TERMINAL_KINDS
                            # (so the cursor advances past them and they can't
                            # wedge a later completed/blocked event behind an
                            # unclaimed row) but are intentionally SILENT: an
                            # archive needs no user ping, and unblocked is an
                            # internal transition. They are also excluded from
                            # _WAKE_KINDS below, so they never wake the creator.
                            continue
                        allowed_wake_kinds.add(kind)
                        delivery_metadata = sub.get("delivery_metadata")
                        metadata: dict[str, Any] = (
                            dict(delivery_metadata)
                            if isinstance(delivery_metadata, dict)
                            else {}
                        )
                        metadata = _external_delivery_metadata(metadata)

                        if sub.get("thread_id") and not metadata.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        # Adapters with no push channel (the API server —
                        # ``supports_async_delivery = False``) can NEVER
                        # satisfy a text-send: ``send()`` always reports
                        # SendResult(success=False) by design (see
                        # ApiServerAdapter.send()). Treating that as a
                        # delivery failure would rewind/drop the subscription
                        # forever and — because the wake dispatch below lives
                        # in this loop's ``else`` clause — would also make the
                        # wake-on-completion path (the actual fix for the
                        # api_server wrong-session bug) unreachable. So for
                        # non-push adapters, skip the doomed send attempt
                        # entirely: there is nothing to text-notify, the
                        # creator is woken via the self-post below instead.
                        from gateway.wake import adapter_supports_push

                        if not adapter_supports_push(adapter) and wake_agent:
                            logger.debug(
                                "kanban notifier: adapter %s has no push "
                                "channel; skipping text ping for %s, relying "
                                "on wake self-post instead",
                                platform_str, sub["task_id"],
                            )
                            # Do NOT reset the failure counter here: on this
                            # path the wake self-post below IS the delivery,
                            # so the counter is resolved (reset or bumped) by
                            # the self-post outcome, not by skipping the send.
                            continue
                        if not send_passive:
                            # Wake-only subscriptions intentionally skip the
                            # visible platform message. The retained wake path
                            # below is the sole delivery — the failure counter
                            # is resolved (reset or bumped) by the wake
                            # outcome there, not by skipping the send here.
                            continue
                        if kind == "completed":
                            from gateway.kanban_delivery_receipts import deliver, DeliveryHeld, record_problem
                            if member_chat and not d.get("trusted_report_back_enabled"):
                                # Human line only (finalized in deliver() once
                                # the approved file is known); never raw
                                # worker text, IDs or board tags.
                                msg = member_completion_text(
                                    task.title if task else "", False,
                                )
                            _hk = (board_slug, sub["task_id"], ev.id)
                            _hb = _HELD_DELIVERY_BACKOFF.get(_hk)
                            if _hb and time.monotonic() < _hb[0]:
                                break
                            with _kb.pin_first_board_resolution():
                                receipt_conn = _kb.connect(board=board_slug)
                            try:
                                await deliver(receipt_conn, sub, ev.id, msg, adapter, metadata,
                                              allow_artifacts=not d.get("trusted_report_back_enabled"))
                            except DeliveryHeld as exc:
                                record_problem(receipt_conn, sub, ev.id, str(exc))
                                _n = (_hb[1] + 1) if _hb else 1
                                _HELD_DELIVERY_BACKOFF[_hk] = (time.monotonic() + min(1800, 5 * 2 ** _n), _n)
                                logger.warning("kanban result delivery requires reconciliation for %s event %s: %s", sub["task_id"], ev.id, exc)
                                break
                            finally:
                                receipt_conn.close()
                            continue
                        try:
                            _send_res = await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            # A SendResult(success=False) without an exception
                            # (returned by push-capable adapters on a genuine
                            # transient failure) must count as a FAILED
                            # delivery — otherwise the cursor advances and the
                            # event is permanently lost. Adapters returning
                            # None (or anything non-SendResult shaped) keep
                            # the legacy "no exception == delivered" contract.
                            if getattr(_send_res, "success", True) is False:
                                raise RuntimeError(
                                    "adapter send() reported failure: "
                                    f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                                )
                            try:
                                from gateway.mirror import find_owner_dm_session, mirror_proactive_message
                                _dm_sid = find_owner_dm_session(platform_str, str(sub["chat_id"]), sub.get("thread_id"))
                                if _dm_sid:
                                    mirror_proactive_message(
                                        platform_str, str(sub["chat_id"]), msg,
                                        source="delegated work update",
                                        ref=f"task {sub['task_id']} ({kind})",
                                        thread_id=sub.get("thread_id"), session_id=_dm_sid,
                                    )
                            except Exception:
                                pass
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Fires on ``completed`` and (v0.21.5 upstream)
                            # ``review_requested`` — whose payload names the
                            # durable staged copies — never on retries.
                            if kind in ("completed", "review_requested"):
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            if d.get("trusted_report_back"):
                                # The pre-send cursor claim is the durable
                                # at-most-once intent for trusted report-back.
                                # Adapter exceptions can be ambiguous (the
                                # remote may have accepted the message), so
                                # never rewind and risk a second visible send.
                                logger.warning(
                                    "kanban notifier: trusted report-back send "
                                    "ended ambiguously for %s on %s; preserving "
                                    "claimed cursor to prevent duplicate delivery: %s",
                                    sub["task_id"], platform_str, exc,
                                )
                                sub_fail_counts.pop(sub_key, None)
                                break
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await _to_thread_process_service(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All text pings delivered (or intentionally skipped
                        # for non-push adapters, whose delivery is the wake
                        # self-post below). Whether the cursor may advance now
                        # depends on the adapter class:
                        #
                        # * push-capable: the text send WAS the delivery, so
                        #   advance immediately (pre-existing behavior); the
                        #   wake injection below stays best-effort.
                        # * non-push (api_server): the wake self-post IS the
                        #   delivery. Advancing first would let a failed /
                        #   retry-exhausted self-post (swallowed by the
                        #   best-effort except) permanently lose the event.
                        #   So the self-post runs FIRST and the cursor only
                        #   advances after it succeeds — a failure rewinds the
                        #   claim exactly like a failed send() above, so the
                        #   next tick retries.
                        task_terminal = task and task.status == "archived"
                        # Kinds that hand a decision back to the origin, so the
                        # origin has to take a turn. ``review_requested`` (the
                        # implementation is done and waits for a reviewer),
                        # ``changes_requested`` (a reviewer BLOCKed and work
                        # returns to the implementer) and ``block_loop_detected``
                        # (routed to triage) belong here for the same reason
                        # ``blocked`` does. ``status`` / ``archived`` /
                        # ``unblocked`` stay out: bookkeeping.
                        # 2026-09-25 (Chris): wake only on outcomes a human
                        # cares about. crashed/timed_out are retried by the
                        # dispatcher; review/changes cycles are internal.
                        _WAKE_KINDS = (
                            "completed", "gave_up", "blocked",
                            "block_loop_detected",
                        )
                        _wake_kinds = (
                            {kind for kind in allowed_wake_kinds if kind in _WAKE_KINDS}
                            if wake_agent
                            else set()
                        )
                        from gateway.wake import adapter_supports_push as _adapter_push_ok

                        _is_push_adapter = _adapter_push_ok(adapter)
                        _session_key = ""
                        _synth = ""
                        if _wake_kinds:
                            if _is_push_adapter:
                                _session_key = getattr(task, "session_id", None) or ""
                            else:
                                # Non-push (api_server) wakes go to the
                                # subscription's delivery destination —
                                # sub["chat_id"] IS the raw session id the
                                # subscriber registered with. task.session_id
                                # is worker/creator provenance and may point
                                # at a WORKER session for child tasks with
                                # inherited subscriptions; falling back to it
                                # only when chat_id is empty (legacy rows).
                                _session_key = (
                                    sub["chat_id"]
                                    or getattr(task, "session_id", None)
                                    or ""
                                )
                        if _wake_kinds:
                            _title = (task.title if task else sub["task_id"])[:120]
                            _assignee = task.assignee if task else ""
                            _parts = []
                            if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                            if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                            if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                            if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                            if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                            if "review_requested" in _wake_kinds: _parts.append(t("gateway.kanban.wake.review_requested"))
                            if "changes_requested" in _wake_kinds: _parts.append(t("gateway.kanban.wake.changes_requested"))
                            if "block_loop_detected" in _wake_kinds: _parts.append(t("gateway.kanban.wake.block_loop_detected"))
                            _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                            _synth = t(
                                "gateway.kanban.wake.message",
                                task_id=sub["task_id"],
                                status=_status,
                                title=_title,
                                assignee=_assignee,
                                board=board_slug,
                            )
                            # Graph-safe wake turn (#70752): carry the worker's
                            # completion handoff into the synthetic turn and
                            # label it as an automatic notification so the woken
                            # creator inspects the board instead of
                            # re-decomposing work that already exists.
                            if wake_handoff:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.handoff",
                                    summary=wake_handoff,
                                )
                            if wake_review_detail:
                                _synth += "\n" + t(
                                    "gateway.kanban.wake.review_detail",
                                    reason=wake_review_detail,
                                )
                            _synth += "\n\n" + t(
                                "gateway.kanban.wake.guidance"
                            )

                        if not _is_push_adapter and _wake_kinds and _session_key:
                            # Wake self-post IS the delivery on this path —
                            # it must succeed BEFORE the cursor advances.
                            from gateway.wake import deliver_wake

                            try:
                                await deliver_wake(
                                    adapter,
                                    text=_synth,
                                    session_id=_session_key,
                                )
                                logger.info(
                                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                )
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                if d.get("trusted_report_back"):
                                    logger.warning(
                                        "kanban notifier: trusted report-back wake "
                                        "ended ambiguously for %s; preserving claimed "
                                        "cursor to prevent duplicate delivery: %s",
                                        sub["task_id"], _wk_err,
                                    )
                                    sub_fail_counts.pop(sub_key, None)
                                    continue
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake self-post failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the self-post — the event
                                    # is NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        async def _push_wake() -> None:
                            """Wake the creator session behind a push adapter.

                            Shared by the wake-only (pre-advance, delivery)
                            and notify+wake (post-advance, best-effort)
                            branches below; raises on failure so the caller
                            decides whether to rewind or merely log.
                            """
                            from gateway.session import SessionSource
                            from gateway.wake import deliver_wake
                            # Rebuild the creator's real session scope from
                            # the chat_type persisted on the subscription
                            # row (#56580). build_session_key() keys DMs
                            # (":dm:<chat_id>") on a wholly different shape
                            # from group/thread, so the old hardcoded
                            # "group" mis-routed DM/thread creators into a
                            # fresh session. Legacy rows written before the
                            # column existed may still carry chat_type in
                            # delivery_metadata (#60600 rows) — fall back
                            # to that, then to "group" (the historical
                            # default that suits the dashboard/group flows).
                            # handle_message() get_or_create_session's the
                            # target, so a mismatch only ever degrades to a
                            # fresh session, never an exception.
                            _chat_type = str(sub.get("chat_type") or "").strip()
                            if not _chat_type:
                                _delivery_meta = sub.get("delivery_metadata")
                                if isinstance(_delivery_meta, dict):
                                    _chat_type = str(
                                        _delivery_meta.get("chat_type") or ""
                                    ).strip()
                            _chat_type = _chat_type or "group"
                            _source = SessionSource(
                                platform=plat,
                                chat_id=sub["chat_id"],
                                chat_type=_chat_type,
                                thread_id=sub.get("thread_id") or None,
                                user_id=sub.get("user_id"),
                                user_id_alt=sub.get("user_id_alt"),
                                profile=sub_profile or None,
                                scope_id=_wake_scope_id(adapter, sub),
                            )
                            # deliver_wake preserves the synthetic
                            # MessageEvent/handle_message path for
                            # push-capable adapters (the non-push /
                            # self-post branch is handled BEFORE the
                            # cursor advance above).
                            await deliver_wake(
                                adapter,
                                text=_synth,
                                session_id=_session_key,
                                source=_source,
                            )
                            logger.info(
                                "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                            )

                        if _is_push_adapter and not send_passive and _wake_kinds:
                            # Wake-only (delivery_mode='wake') push sub: the
                            # text ping was intentionally skipped above, so
                            # the wake IS the sole delivery. It must succeed
                            # BEFORE the cursor advances — advancing first
                            # would let a failed wake (previously swallowed
                            # by the best-effort except below) permanently
                            # lose the event. Mirrors the non-push
                            # (api_server) self-post ordering above.
                            try:
                                await _push_wake()
                                sub_fail_counts.pop(sub_key, None)
                            except Exception as _wk_err:
                                if d.get("trusted_report_back"):
                                    logger.warning(
                                        "kanban notifier: trusted report-back wake "
                                        "ended ambiguously for %s; preserving claimed "
                                        "cursor to prevent duplicate delivery: %s",
                                        sub["task_id"], _wk_err,
                                    )
                                    sub_fail_counts.pop(sub_key, None)
                                    continue
                                fails = sub_fail_counts.get(sub_key, 0) + 1
                                sub_fail_counts[sub_key] = fails
                                logger.warning(
                                    "kanban notifier: wake-only delivery failed "
                                    "for %s (attempt %d/%d): %s",
                                    sub["task_id"], fails,
                                    MAX_SEND_FAILURES, _wk_err, exc_info=True,
                                )
                                if fails >= MAX_SEND_FAILURES:
                                    logger.warning(
                                        "kanban notifier: dropping subscription "
                                        "%s on %s after %d consecutive wake failures",
                                        sub["task_id"], platform_str, fails,
                                    )
                                    await _to_thread_process_service(self._kanban_unsub, sub, board_slug)
                                    sub_fail_counts.pop(sub_key, None)
                                else:
                                    # Rewind the pre-send claim so the next
                                    # tick retries the wake — the event is
                                    # NOT lost.
                                    await _to_thread_process_service(
                                        self._kanban_rewind,
                                        sub,
                                        d["cursor"],
                                        d.get("old_cursor", 0),
                                        board_slug,
                                    )
                                continue

                        # Delivery complete (text ping for push adapters, wake
                        # self-post for non-push, wake injection for wake-only
                        # push subs): advance cursor. The cursor is the dedup
                        # mechanism — it prevents re-delivery of the same
                        # event on subsequent ticks.
                        await _to_thread_process_service(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        if not _is_push_adapter:
                            # Nothing left to deliver on this path (the wake,
                            # if any, already succeeded above).
                            sub_fail_counts.pop(sub_key, None)
                        # Unsubscribe only on archive. Completion (``done``)
                        # remains reversible: controllers reopen completed
                        # work for review corrections and continuation. The
                        # retained cursor prevents replay while preserving the
                        # original delivery and wake ownership for that cycle.
                        if _is_push_adapter and send_passive and _wake_kinds:
                            # notify+wake: the text ping above was the
                            # delivery and the cursor has advanced; the wake
                            # injection stays best-effort.
                            try:
                                await _push_wake()
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await _to_thread_process_service(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            await self._sleep_between_ticks(interval)

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> None:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        # Cursor writes are machine flow: the sub's board slug must resolve
        # through the env pin on a dispatcher-pinned box (same as the notifier
        # tick), or advance/rewind land on a per-slug DB the notifier never
        # reads and cursors silently reset.
        with _kb.pin_first_board_resolution():
            conn = _kbc.connect(board=board)
            try:
                getattr(_kbn, op)(
                    conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "", **extra,
                )
            finally:
                conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        """
        raw_paths: list[str] = []
        prose_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                prose_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            prose_paths += adapter.extract_local_files(str(task.result))[0]
        # A staged copy and the scratch original it was copied from are the
        # same deliverable; on a review handoff the original still exists, so
        # prose mentions of it must not upload the file a second time.
        staged_names = {os.path.basename(p) for p in raw_paths}
        raw_paths += [p for p in prose_paths if os.path.basename(p) not in staged_names]
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    def _kanban_dispatcher_boot(self) -> Optional[tuple]:
        """Resolve config, kanban_db and the singleton lock; None when the dispatcher must not run.

        Config is read once at boot (restart to apply), except the auto-decompose
        toggle which is re-read every tick. The env var is an escape hatch to
        disable without editing YAML.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return None
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return None
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return None
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info("kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false")
            return None
        command_boards = _resolve_command_boards(cfg)
        try:
            adaptive_controller = _resolve_adaptive_controller(kanban_cfg)
        except ValueError as exc:
            logger.error(
                "kanban dispatcher: adaptive concurrency config failed closed: %s",
                exc,
            )
            return
        adaptive_enabled = adaptive_controller is not None

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return None

        # Single-dispatcher backstop (see _acquire_singleton_lock). The lock
        # lives at the machine-global kanban root, so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info("kanban dispatcher: another gateway already holds the dispatcher "
                        "lock (%s); this gateway will NOT dispatch.", _lock_path)
            return None
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            if adaptive_enabled:
                logger.error(
                    "kanban dispatcher: adaptive concurrency requires the "
                    "machine-global singleton lock; dispatch disabled"
                )
                return
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        return _load_config, _kb, kanban_cfg, command_boards, adaptive_controller

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` (default True); when false the
        loop exits and an external `hermes kanban daemon` is expected. Each
        tick runs :func:`kanban_db_dispatch.dispatch_once` in a thread; one tick's
        failure never stops the next. Shutdown: ``self._running`` is checked
        between ticks and the in-flight ``to_thread`` returns on its own.
        """
        boot = self._kanban_dispatcher_boot()
        if boot is None:
            return
        _load_config, _kb, kanban_cfg, command_boards, adaptive_controller = boot
        adaptive_enabled = adaptive_controller is not None
        settings = _resolve_dispatcher_settings(kanban_cfg, _kb)
        interval = settings.interval

        # Initial delay so adapters are wired before workers spawn (matches the notifier).
        await asyncio.sleep(5)

        # Health telemetry (mirrors `_cmd_daemon`): warn when the ready queue
        # is non-empty but spawns are 0 for N consecutive ticks — usually a
        # broken PATH, missing venv, or credential loss.
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(
            slug: str, admission_cap: Optional[int] = None,
        ) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                board_max_in_progress = settings.max_in_progress
                if admission_cap is not None:
                    board_max_in_progress = (
                        admission_cap
                        if board_max_in_progress is None
                        else min(board_max_in_progress, admission_cap)
                    )
                result = _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=settings.max_spawn,
                    max_in_progress=board_max_in_progress,
                    failure_limit=settings.failure_limit,
                    stale_timeout_seconds=settings.stale_timeout_seconds,
                    default_assignee=settings.default_assignee,
                    max_in_progress_per_profile=settings.max_in_progress_per_profile,
                    reconcile_orphans=settings.reconcile_orphans,
                )
                from hermes_cli.kanban_completion import policy, write_tick_receipt
                write_tick_receipt(conn, slug, result, policy())
                return result
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Dispatch selected boards with one optional host-wide allowance."""
            if command_boards is not None:
                slugs = command_boards
            else:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                slugs = tuple(
                    b.get("slug") or _kb.DEFAULT_BOARD for b in boards
                )
            if not adaptive_enabled or adaptive_controller is None:
                return [(slug, _tick_once_for_board(slug)) for slug in slugs]

            from gateway.kanban_admission import (
                dispatch_with_global_budget,
                sample_host_pressure,
            )

            # Resolve every board's active count before taking the one host
            # pressure sample or spawning anything.  A malformed count aborts
            # the whole tick rather than granting another board excess budget.
            active_by_board: dict[str, int] = {}
            for slug in slugs:
                conn = _kb.connect(board=slug)
                try:
                    raw_count = conn.execute(
                        "SELECT COUNT(*) FROM tasks "
                        "WHERE status = 'running' "
                        "OR (status = 'review' AND claim_lock IS NOT NULL)"
                    ).fetchone()[0]
                    if (
                        not isinstance(raw_count, int)
                        or isinstance(raw_count, bool)
                        or raw_count < 0
                    ):
                        raise ValueError("malformed active worker count")
                    active_by_board[slug] = raw_count
                finally:
                    conn.close()
            decision = adaptive_controller.update(
                sample_host_pressure(),
                active_workers=sum(active_by_board.values()),
            )
            return dispatch_with_global_budget(
                slugs,
                total_capacity=decision.capacity,
                new_admissions=decision.new_admissions,
                active_count=active_by_board.__getitem__,
                dispatch_board=lambda slug, cap: _tick_once_for_board(slug, cap),
            )

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            # Only probe the review column when autonomous review dispatch is
            # actually on. With ``review_dispatch`` off (the default — no
            # sdlc-review agent), a task parked in 'review' is "correctly idle"
            # waiting for a human, not a stuck dispatcher; probing it here would
            # fire a false "dispatcher stuck" warning that never clears. Shares
            # the exact gate the dispatcher uses so the two can't drift.
            _review_probe = _kb.review_dispatch_enabled()
            if command_boards is not None:
                boards = [{"slug": slug} for slug in command_boards]
            else:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _review_probe and _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            if command_boards is not None:
                boards = [{"slug": slug} for slug in command_boards]
            else:
                try:
                    boards = _kb.list_boards(include_archived=False)
                except Exception:
                    boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    _now_ad = time.time()
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        if _AD_FAILED_UNTIL.get((slug, tid), 0) > _now_ad:
                            continue  # recently failed: don't let one card block the queue
                        try:
                            # Private owner-delegated cards (Josh/Connor) are hash-pinned; rewriting
                            # their body breaks the binding and blocks them forever. Promote as-is.
                            with _kb.connect_closing(board=slug) as _pc:
                                _realm = _pc.execute("SELECT security_realm FROM tasks WHERE id=?", (tid,)).fetchone()
                                if _realm and str(_realm[0] or "").startswith("owner:"):
                                    _pc.execute("UPDATE tasks SET status='todo' WHERE id=? AND status='triage'", (tid,))
                                    _pc.commit()
                                    continue
                        except Exception:
                            pass
                        attempted += 1
                        try:
                            # Multiplexed gateway: the dispatcher thread has no secret scope, so the
                            # decomposer's LLM call raised UnscopedSecretError on every card. Run it
                            # under the default (owner) profile scope, like a normal owner turn.
                            from gateway.run import _profile_runtime_scope as _prs
                            from hermes_constants import get_hermes_home as _ghh
                            with _prs(_ghh()):
                                outcome = _decomp.decompose_task(
                                    tid, author="auto-decomposer",
                                )
                        except Exception:
                            _AD_FAILED_UNTIL[(slug, tid)] = time.time() + 3600
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s (retry in 1h)",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            _AD_FAILED_UNTIL[(slug, tid)] = time.time() + 3600
                            logger.warning(
                                "kanban auto-decompose [%s]: %s skipped (retry in 1h): %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        from gateway.kanban_dispatch_wake import CommittedTransitions, wait_for_transition
        transition_observer = CommittedTransitions()
        def _wake_paths():
            if command_boards is not None:
                return [_kb.kanban_db_path(slug) for slug in command_boards]
            return [_kb.kanban_db_path(b.get("slug") or _kb.DEFAULT_BOARD)
                    for b in _kb.list_boards(include_archived=False)]
        # Prime before dispatch, not afterwards: a worker may commit during a tick.
        try:
            await _to_thread_process_service(transition_observer.changed, _wake_paths())
        except Exception:
            pass
        next_periodic = time.monotonic()
        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            tick_started = time.monotonic()
            periodic_tick = tick_started >= next_periodic
            try:
                # Reap zombies before per-board work so a board DB failure
                # cannot block cleanup of unrelated workers.
                from hermes_cli import kanban_db_dispatch as _kbd
                pids = await _to_thread_process_service(_kbd.reap_worker_zombies)
                if pids:
                    logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Emergency stop (`hermes pause`): no auto-decompose or
                # dispatch while paused; running workers finish naturally.
                if not _kanban_dispatch_allowed():
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live so disabling it
                    # takes effect on the next tick, not on restart.
                    _ad_enabled, _ad_per_tick = _resolve_auto_decompose_settings(_load_config)
                    # See #49638.
                    if _ad_enabled and periodic_tick:
                        await _to_thread_process_service(_auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(_pin_first_call(_kb, _tick_once))
                    any_spawned = _log_spawn_results(results)
                    ready_pending = await _to_thread_process_service(_pin_first_call(_kb, _ready_nonempty))
                    bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0
                now = int(time.time())
                if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:
                    held = _kbd.describe_suppression(res for _slug, res in (results or []))
                    logger.warning(
                        "kanban dispatcher stuck: ready queue non-empty for "
                        "%d consecutive ticks but 0 workers spawned.%s Check "
                        "profile health (venv, PATH, credentials) and "
                        "`hermes kanban list --status ready`.",
                        bad_ticks, f" Last tick held back: {held}." if held else "",
                    )
                    last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            if periodic_tick:
                next_periodic = time.monotonic() + interval
            # Wake only the existing dispatcher. Hints never claim tasks and
            # every tick repeats the same pause, admission and concurrency gates.
            await wait_for_transition(transition_observer, _wake_paths,
                                      lambda: self._running,
                                      periodic_deadline=next_periodic,
                                      last_tick=tick_started)

        self._release_kanban_dispatcher_lock()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Callable  # noqa: F401,E402
from contextvars import Context  # noqa: F401,E402
import re  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    't': ('agent.i18n', 't'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
