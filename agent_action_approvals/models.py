"""The durable approval queue.

``ActionApproval`` is one row = one bounded action an agent wants to take,
sitting in a persisted state machine until a human (or an auto-approval policy)
decides it. Durability is the point: an in-process ``interrupt()`` dies with the
process and forgets every pending action; a database row survives a restart, a
redeploy, and the week the operator was on holiday.

The model is deliberately domain-agnostic. ``action_type`` is a free string and
``action_payload`` is opaque JSON. YOUR application defines what "merge_pr" or
"send_email" means and registers an executor for it (see ``registry``). This
model knows only: an action is pending, someone decided it, an executor ran it,
here is the result.
"""
from __future__ import annotations

import hashlib
import uuid as uuid_lib

from django.db import models
from django.utils import timezone


class InvalidTransition(ValueError):
    """Raised when a transition starts from the wrong state: the row was already
    decided, already executed, or another process moved it first. Subclasses
    ``ValueError`` so older ``except ValueError`` handlers keep working."""


class FingerprintMismatch(Exception):
    """Raised by ``approve`` when the action's fingerprint at approval time does
    not match the fingerprint captured at enqueue, meaning the thing being approved
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

    # What the agent wants to do. Free-form: your app's vocabulary, not ours.
    action_type = models.CharField(max_length=64, db_index=True)
    action_payload = models.JSONField(default=dict, blank=True)
    title = models.CharField(max_length=255)

    # Free-form identifier of who/what enqueued it (an agent session id, a user,
    # a daemon name). Not a FK: this package does not own your identity model.
    requested_by = models.CharField(max_length=128, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    auto_approved = models.BooleanField(default=False)

    # Binds the approval to the exact reviewed content. If set at enqueue,
    # ``approve`` refuses when a different fingerprint is presented. That is the
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
    # Each transition is a compare-and-swap in the database: one UPDATE ... WHERE
    # status = <expected>. Two processes holding the same PENDING row cannot both
    # win; the loser gets InvalidTransition and nothing about the row changes.
    # Checking ``self.status`` in memory alone is not enough, because each worker
    # loaded its own copy and both copies say PENDING.

    def approve(self, by: str, *, expected_fingerprint: str | None = None,
                auto: bool = False) -> None:
        """Move PENDING -> APPROVED.

        If this approval carries a ``diff_fingerprint`` (captured at enqueue) and
        ``expected_fingerprint`` is supplied, they must match. Otherwise the
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
        self._transition(
            self.PENDING, "approve",
            status=self.APPROVED, auto_approved=auto,
            decided_by=by, decided_at=timezone.now(),
        )

    def decline(self, by: str, reason: str = "") -> None:
        """Move PENDING -> DECLINED."""
        self._require(self.PENDING, "decline")
        fields = dict(status=self.DECLINED, decided_by=by, decided_at=timezone.now())
        if reason:
            fields["result"] = {**(self.result or {}), "decline_reason": reason}
        self._transition(self.PENDING, "decline", **fields)

    def time_out(self, reason: str = "expired before a decision") -> None:
        """Move PENDING -> TIMED_OUT. Races with ``approve``/``decline`` the same
        way they race with each other: exactly one of them wins."""
        self._require(self.PENDING, "time out")
        self._transition(
            self.PENDING, "time out",
            status=self.TIMED_OUT, decided_by="timeout", decided_at=timezone.now(),
            result={**(self.result or {}), "timeout_reason": reason},
        )

    @classmethod
    def expire_pending(cls, older_than) -> int:
        """Time out every PENDING row created more than ``older_than`` (a
        ``timedelta``) ago. One UPDATE filtered on status, so a row approved a
        moment earlier is not touched. Returns the number of rows timed out.
        Call it from a periodic task; this package does not schedule anything."""
        cutoff = timezone.now() - older_than
        return cls.objects.filter(status=cls.PENDING, created_at__lt=cutoff).update(
            status=cls.TIMED_OUT, decided_by="timeout", decided_at=timezone.now(),
        )

    def mark_executing(self) -> None:
        """Move APPROVED -> EXECUTING. The registry calls this before it runs the
        executor. Because it is a compare-and-swap, it is also the claim: of two
        workers handed the same approval, only one gets past this line."""
        self._require(self.APPROVED, "mark_executing")
        self._transition(self.APPROVED, "mark_executing",
                         status=self.EXECUTING, executing_at=timezone.now())

    def mark_done(self, result: dict | None = None) -> None:
        self._require(self.EXECUTING, "mark_done")
        fields = {"status": self.DONE}
        if result is not None:
            fields["result"] = result
        self._transition(self.EXECUTING, "mark_done", **fields)

    def mark_failed(self, result: dict | None = None) -> None:
        self._require(self.EXECUTING, "mark_failed")
        fields = {"status": self.FAILED}
        if result is not None:
            fields["result"] = result
        self._transition(self.EXECUTING, "mark_failed", **fields)

    def _require(self, expected: str, action: str) -> None:
        if self.status != expected:
            raise InvalidTransition(
                f"cannot {action} approval {self.uuid}: status is "
                f"{self.status!r}, expected {expected!r}"
            )

    def _transition(self, expected: str, action: str, **fields) -> None:
        updated = type(self).objects.filter(pk=self.pk).update(**fields)
        if updated != 1:
            # Another process moved the row first. Reload so this instance stops
            # lying about the state, then refuse.
            self.refresh_from_db()
            raise InvalidTransition(
                f"cannot {action} approval {self.uuid}: status is now "
                f"{self.status!r} (changed by another process), expected {expected!r}"
            )
        for name, value in fields.items():
            setattr(self, name, value)
