"""Unified AI Brain layer: one abstraction over cloud APIs and local CLI runtimes."""

from app.brain.contracts import (
    Brain,
    BrainError,
    BrainEvent,
    BrainHealth,
    Capabilities,
    Capability,
    ErrorKind,
    EventKind,
    HealthStatus,
    ModelInfo,
    SessionRef,
    Usage,
)
# `probe` (the function) is deliberately *not* re-exported here: binding it on the
# package would shadow the `app.brain.probe` submodule for every importer.
from app.brain.probe import ProbeResult, probe_settings
from app.brain.registry import (
    BrainDescriptor,
    BrainKind,
    BrainLocation,
    cloud_providers,
    descriptors,
    get,
    local_agents,
    register,
    registered_ids,
    resolve,
)

__all__ = [
    "Brain",
    "BrainDescriptor",
    "BrainError",
    "BrainEvent",
    "BrainHealth",
    "BrainKind",
    "BrainLocation",
    "Capabilities",
    "Capability",
    "ErrorKind",
    "EventKind",
    "HealthStatus",
    "ModelInfo",
    "ProbeResult",
    "SessionRef",
    "Usage",
    "cloud_providers",
    "descriptors",
    "get",
    "local_agents",
    "probe_settings",
    "register",
    "registered_ids",
    "resolve",
]
