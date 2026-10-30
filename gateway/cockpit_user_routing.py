"""Canonical, fail-closed routing for authenticated Agent OS cockpit users.

The cockpit is a shared-knowledge surface with audience-isolated work and
notifications.  This module is deliberately small and configuration-free: the
mapping is reviewed source policy, not an environment-variable best guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


COCKPIT_USER_BOARDS = {
    "chris": "chris-home",
    "connor": "connor-home",
    "joshgarcia": "josh-home",
}
COCKPIT_SESSION_PREFIX = "agent-os:cockpit:"
COCKPIT_SOURCE_SURFACE = "agent-os:cockpit:voice"
COCKPIT_RETURN_SURFACE = "agent-os:cockpit:voice-callback"

_COCKPIT_USER_RE = re.compile(r"[a-z0-9._-]{1,40}")
_BOARD_USERS = {board: user for user, board in COCKPIT_USER_BOARDS.items()}


class CockpitRoutingError(ValueError):
    """Raised before mutation when a cockpit lane cannot be proven."""


@dataclass(frozen=True)
class CockpitLane:
    user: str
    board: str
    session_key: str
    audience: str


def normalize_cockpit_user(value: Any) -> Optional[str]:
    """Return a syntactically valid normalized user, otherwise ``None``."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not _COCKPIT_USER_RE.fullmatch(normalized):
        return None
    return normalized


def is_cockpit_session_key(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("agent-os:cockpit")


def cockpit_user_from_session_key(value: Any) -> Optional[str]:
    """Parse a cockpit key, failing closed for malformed/unknown identities."""
    if not is_cockpit_session_key(value):
        return None
    raw = str(value).strip()
    if not raw.startswith(COCKPIT_SESSION_PREFIX):
        raise CockpitRoutingError("cockpit session key is missing a user")
    user = normalize_cockpit_user(raw[len(COCKPIT_SESSION_PREFIX) :])
    if user is None or user not in COCKPIT_USER_BOARDS:
        raise CockpitRoutingError("cockpit session key has an unknown or malformed user")
    return user


def resolve_cockpit_lane(value: Any, *, board: Optional[str] = None) -> CockpitLane:
    """Resolve a configured user or cockpit session key to its canonical lane."""
    if is_cockpit_session_key(value):
        user = cockpit_user_from_session_key(value)
    else:
        user = normalize_cockpit_user(value)
    if user is None or user not in COCKPIT_USER_BOARDS:
        raise CockpitRoutingError("missing, malformed, or unknown cockpit user")
    expected_board = COCKPIT_USER_BOARDS[user]
    if board is not None and str(board).strip() != expected_board:
        # Instructive on purpose: the model consuming this error must learn the
        # correct move (drop the board arg) instead of concluding the user's
        # permissions are broken and reporting a dead end to a human.
        raise CockpitRoutingError(
            f"cockpit user {user!r} is not allowed to write board {board!r}; "
            f"their work routes to {expected_board!r} automatically — retry "
            "WITHOUT the board argument (cockpit tasks never need one)"
        )
    return CockpitLane(
        user=user,
        board=expected_board,
        session_key=f"{COCKPIT_SESSION_PREFIX}{user}",
        audience=user,
    )


def resolve_cockpit_board(value: Any, *, board: Optional[str] = None) -> str:
    return resolve_cockpit_lane(value, board=board).board


def bind_cockpit_principal(
    principal: Any,
    *,
    session_key: Optional[str] = None,
    requesting_user: Optional[str] = None,
    board: Optional[str] = None,
    audience: Optional[str] = None,
) -> CockpitLane:
    """Bind caller-selected cockpit fields to authenticated ``principal``.

    The authenticated API principal is the sole authority.  Caller fields are
    accepted only as redundant assertions of the canonical lane; they can
    never select a different user, board, audience, or memory/session scope.
    Unknown principals (including agent identities such as Ava or Photon) fail
    closed before any session or board mutation.
    """
    lane = resolve_cockpit_lane(principal)
    supplied = {
        "session key": session_key,
        "requesting user": requesting_user,
        "board": board,
        "audience": audience,
    }
    expected = {
        "session key": lane.session_key,
        "requesting user": lane.user,
        "board": lane.board,
        "audience": lane.audience,
    }
    for label, raw in supplied.items():
        if raw is None:
            continue
        value = str(raw).strip()
        if value != expected[label]:
            raise CockpitRoutingError(
                f"cockpit {label} does not match the authenticated principal"
            )
    return lane


def audience_for_task_metadata(
    metadata: Optional[Mapping[str, Any]], *, board: Optional[str]
) -> str:
    """Resolve and cross-check a report-back audience.

    Known cockpit boards map to their configured user for legacy Chris records
    and early Connor records that predate explicit stamping.  Other explicit
    boards remain legacy/non-cockpit and retain the historical Chris audience.
    Any cockpit-shaped metadata, however, must agree in every field.
    """
    meta = dict(metadata or {})
    board_value = str(board or "").strip()
    requesting = normalize_cockpit_user(meta.get("requesting_user"))
    audience = normalize_cockpit_user(meta.get("audience"))
    raw_session_key = meta.get("source_session_key")
    session_user = cockpit_user_from_session_key(raw_session_key)
    cockpit_board = str(meta.get("cockpit_board") or "").strip()
    cockpit_surface = any(
        str(meta.get(key) or "").startswith("agent-os:cockpit")
        for key in ("source_surface", "return_surface")
    )
    cockpit_shaped = bool(
        requesting
        or audience
        or is_cockpit_session_key(raw_session_key)
        or cockpit_board
        or cockpit_surface
        or board_value in _BOARD_USERS
    )

    candidates = {value for value in (requesting, audience, session_user) if value}
    if len(candidates) > 1:
        raise CockpitRoutingError("cockpit requesting user, audience, and session disagree")

    if candidates:
        lane = resolve_cockpit_lane(next(iter(candidates)), board=board_value or None)
        if cockpit_board and cockpit_board != lane.board:
            raise CockpitRoutingError("cockpit metadata board disagrees with the user lane")
        return lane.audience

    if cockpit_shaped:
        # Report-back compatibility: historical cockpit tasks already carry
        # source/return surfaces but not requesting_user.  Their explicit known
        # board is the only permitted legacy identity source.
        if board_value in _BOARD_USERS:
            if cockpit_board and cockpit_board != board_value:
                raise CockpitRoutingError("cockpit metadata board disagrees with the task board")
            return _BOARD_USERS[board_value]
        raise CockpitRoutingError("cockpit-originated work is missing a trusted user")

    return "chris"


def cockpit_user_for_board(board: Any) -> Optional[str]:
    return _BOARD_USERS.get(str(board or "").strip())
