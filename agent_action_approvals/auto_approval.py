"""The auto-approval policy, a default-DENY, fail-closed, kill-switched
classifier for the narrow lane of actions safe to approve without a human.

The whole value of this queue is that a human sees each action. Auto-approval is
the deliberate, dangerous exception, so it is built to be as hard as possible to
widen by accident:

  * DEFAULT DENY. An action_type with no registered classifier is never
    auto-approved. You opt a lane IN, one at a time; you cannot forget to opt one
    out.
  * FAIL CLOSED. Any exception in a classifier (a network error, a missing
    field, a bad response) resolves to DENY, never approve. The unsafe default
    is the safe one.
  * A MASTER KILL-SWITCH, off by default. Until an env var is explicitly set,
    the whole policy is inert and denies everything. So the feature ships dark:
    merging this code changes nothing until someone consciously turns it on.

You register a classifier per lane: ``fn(approval) -> (should_approve, reason)``.
Keep each one an allowlist: prove the action is inside a safe box, return False
for anything you can't prove. ``examples/`` has a docs-only-PR classifier that
fetches the changed files from the GitHub API (never trusting the payload) and
requires green CI.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Tuple

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

# A classifier returns (should_auto_approve, human-readable reason).
Classifier = Callable[["object"], Tuple[bool, str]]


class AutoApprovalPolicy:
    """A registry of per-action-type auto-approval classifiers, behind a
    kill-switch, that defaults to DENY and fails closed."""

    def __init__(self, kill_switch_env: str = "AGENT_ACTION_AUTO_APPROVE_ENABLED") -> None:
        self.kill_switch_env = kill_switch_env
        self._classifiers: dict[str, Classifier] = {}

    def enabled(self) -> bool:
        """The master switch. Off (absent/empty/falsey) means the whole policy is
        inert (every action denied), which is how it ships."""
        return os.environ.get(self.kill_switch_env, "").strip().lower() in _TRUTHY

    def register(self, action_type: str, fn: Classifier) -> None:
        if action_type in self._classifiers:
            raise ValueError(f"classifier already registered for {action_type!r}")
        self._classifiers[action_type] = fn

    def classifier(self, action_type: str) -> Callable[[Classifier], Classifier]:
        """Decorator form: ``@policy.classifier('merge_pr')``."""
        def deco(fn: Classifier) -> Classifier:
            self.register(action_type, fn)
            return fn
        return deco

    def classify(self, approval) -> Tuple[bool, str]:
        """Decide whether ``approval`` may be auto-approved. Returns
        (should_approve, reason). Default DENY; fail closed on any error."""
        if not self.enabled():
            return False, "auto-approval disabled (kill-switch off)"

        fn = self._classifiers.get(approval.action_type)
        if fn is None:
            # No registered lane for this type -> deny. This is the default-DENY
            # spine: you cannot auto-approve a type you never opted in.
            return False, f"no auto-approval lane for {approval.action_type!r}"

        try:
            should, reason = fn(approval)
        except Exception:  # noqa: BLE001 (any classifier error must fail CLOSED)
            logger.exception(
                "auto-approval classifier for %s raised, failing closed (approval %s)",
                approval.action_type, getattr(approval, "uuid", "?"),
            )
            return False, "classifier error -> fail-closed"

        return bool(should), reason
