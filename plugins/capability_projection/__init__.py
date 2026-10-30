"""Least-privilege capability projection for Kanban worker admission."""

from .admission import TaskAdmission, preflight_legacy_metadata, preflight_task
from .policy import (
    ApprovalError,
    CapabilityContract,
    CapabilityProfile,
    HmacApprovalVerifier,
    RoutingResult,
    RoutingStatus,
)
from .resolver import CapabilityInventory, load_capability_inventory, profile_config_fingerprint

__all__ = [
    "ApprovalError",
    "CapabilityContract",
    "CapabilityInventory",
    "CapabilityProfile",
    "HmacApprovalVerifier",
    "RoutingResult",
    "RoutingStatus",
    "TaskAdmission",
    "load_capability_inventory",
    "preflight_legacy_metadata",
    "preflight_task",
    "profile_config_fingerprint",
]
