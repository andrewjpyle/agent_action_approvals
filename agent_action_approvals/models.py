"""The durable approval queue.

``ActionApproval`` is one row = one bounded action an agent wants to take,
sitting in a persisted state machine until a human (or an auto-approval policy)
decides it. Durability is the point: an in-process ``interrupt()`` dies with the
process and forgets every pending action; a database row survives a restart, a
redeploy, and the week the operator was on holiday.

The model is deliberately domain-agnostic. ``action_type`` is a free string and
``action_payload`` is opaque JSON — YOUR application defines what "merge_pr" or
"send_email" means and registers an executor for it (see ``registry``). This
model knows only: an action is pending, someone decided it, an executor ran it,
here is the result.
"""
from __future__ import annotations

import hashlib
import uuid as uuid_lib

from django.db import models
from django.utils import timezone


class FingerprintMismatch(Exception):
    """Raised by ``approve`` when the action's fingerprint at approval time does
    not match the fingerprint captured at enqueue — i.e. the thing being approved
    is no longer the thing that was reviewed."""


def fingerprint(*parts: str) -> str:
    """Short, stable sha256 over the given strings. Use it to bind an approval to
    the exact content that was reviewed (e.g. the sorted changed-file paths of a
    PR), so a later change is caught at approve time rather than silently executed.
    """
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class ActionApproval(models.Model):
    """A bounded action awaiting a decision before it executes."""

    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    DECLINED = "declined"
    TIMED_OUT = "timed_out"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (EXECUTING, "Executing"),
        (DONE, "Done"),
        (FAILED, "Failed"),
        (DECLINED, "Declined"),
        (TIMED_OUT, "Timed out"),
    ]
    # The states from which nothing more should happen. Guards against a
    # double-decision race (two operators, or an operator + an auto-policy).
    TERMINAL = frozenset({DONE, FAILED, DECLINED, TIMED_OUT})

    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False, db_index=True)

    # What the agent wants to do. Free-form — your app's vocabulary, not ours.
    action_type = models.CharField(max_length=64, db_index=True)
    action_payload = models.JSONField(default=dict, blank=True)
    title = models.CharField(max_length=255)

    # Free-form identifier of who/what enqueued it (an agent session id, a user,
    # a daemon name). Not a FK — this package does not own your identity model.
    requested_by = models.CharField(max_length=128, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    auto_approved = models.BooleanField(default=False)

    # Binds the approval to the exact reviewed content. If set at enqueue,
    # ``approve`` refuses when a different fingerprint is presented — the
    # "the diff changed between enqueue and approve" catch.
    diff_fingerprint = models.CharField(max_length=128, blank=True)

    decided_by = models.CharField(max_length=128, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    executing_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["action_type", "-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"A{self.pk}: {self.action_type} {self.title[:50]} ({self.status})"

    # --- state transitions --------------------------------------------------
    # Each transition guards on the current state and stamps the audit fields,
    # so the row is always a truthful record of what happened and when.

    def approve(self, by: str, *, expected_fingerprint: str | None = None,
                auto: bool = False) -> None:
        """Move PENDING -> APPROVED.

        If this approval carries a ``diff_fingerprint`` (captured at enqueue) and
        ``expected_fingerprint`` is supplied, they must match — otherwise the
        action changed since it was reviewed and we raise ``FingerprintMismatch``
        rather than approve a thing nobody looked at.
        """
        self._require(self.PENDING, "approve")
        if self.diff_fingerprint and expected_fingerprint is not None:
            if expected_fingerprint != self.diff_fingerprint:
                raise FingerprintMismatch(
                    f"approval {self.uuid}: fingerprint changed since enqueue "
                    f"({self.diff_fingerprint!r} -> {expected_fingerprint!r})"
                )
        self.status = self.APPROVED
        self.auto_approved = auto
        self.decided_by = by
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "auto_approved", "decided_by", "decided_at"])

    def decline(self, by: str, reason: str = "") -> None:
        """Move PENDING -> DECLINED."""
        self._require(self.PENDING, "decline")
        self.status = self.DECLINED
        self.decided_by = by
        self.decided_at = timezone.now()
        if reason:
            self.result = {**(self.result or {}), "decline_reason": reason}
        self.save(update_fields=["status", "decided_by", "decided_at", "result"])

    def mark_executing(self) -> None:
        """Move APPROVED -> EXECUTING. Called by the registry before it runs the
        executor, so a crashed executor leaves a visible EXECUTING row rather than
        a silent one still marked APPROVED."""
        self._require(self.APPROVED, "mark_executing")
        self.status = self.EXECUTING
        self.executing_at = timezone.now()
        self.save(update_fields=["status", "executing_at"])

    def mark_done(self, result: dict | None = None) -> None:
        self._require(self.EXECUTING, "mark_done")
        self.status = self.DONE
        if result is not None:
            self.result = result
        self.save(update_fields=["status", "result"])

    def mark_failed(self, result: dict | None = None) -> None:
        self._require(self.EXECUTING, "mark_failed")
        self.status = self.FAILED
        if result is not None:
            self.result = result
        self.save(update_fields=["status", "result"])

    def _require(self, expected: str, action: str) -> None:
        if self.status != expected:
            raise ValueError(
                f"cannot {action} approval {self.uuid}: status is "
                f"{self.status!r}, expected {expected!r}"
            )
