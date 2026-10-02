"""agent_action_approvals: a durable, human-in-the-loop approval queue for
letting an AI agent take a bounded action without letting it take *any* action.

Public API (lazily re-exported so importing this package during Django's
app-loading phase does not touch the model registry before it is ready):

    from agent_action_approvals import (
        ActionApproval,        # the durable queue model
        FingerprintMismatch,   # raised when the action changed between enqueue and approve
        InvalidTransition,     # raised when a transition loses a race or starts from the wrong state
        ExecutorRegistry,      # action_type -> callable dispatch
        AutoApprovalPolicy,    # default-DENY, fail-closed, kill-switched classifier
    )
"""

__all__ = [
    "ActionApproval",
    "FingerprintMismatch",
    "InvalidTransition",
    "ExecutorRegistry",
    "AutoApprovalPolicy",
]


def __getattr__(name):
    # PEP 562 lazy import. The model must not be imported at package-init time:
    # Django imports this package while loading apps, before the model registry
    # is ready, and eagerly importing models.py there raises AppRegistryNotReady.
    if name in ("ActionApproval", "FingerprintMismatch", "InvalidTransition"):
        from . import models
        return getattr(models, name)
    if name == "ExecutorRegistry":
        from .registry import ExecutorRegistry
        return ExecutorRegistry
    if name == "AutoApprovalPolicy":
        from .auto_approval import AutoApprovalPolicy
        return AutoApprovalPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
