"""Tests for agent_action_approvals.

Django TestCase (needs the model + migration). Run via ../runtests.py.

The load-bearing tests are the SAFETY ones: default-DENY, fail-closed, the
kill-switch, the fingerprint catch, and crash-to-FAILED. A durable approval queue
that quietly approves, or quietly loses a crashed action, is worse than none.
"""
from __future__ import annotations

import os
from unittest import mock

from django.test import TestCase

from agent_action_approvals import (
    ActionApproval, FingerprintMismatch, ExecutorRegistry, AutoApprovalPolicy,
)
from agent_action_approvals.models import fingerprint


def _pending(**kw) -> ActionApproval:
    base = dict(action_type="merge_pr", title="t", action_payload={})
    base.update(kw)
    return ActionApproval.objects.create(**base)


class StateMachine(TestCase):
    def test_approve_moves_pending_to_approved_and_stamps_audit(self):
        a = _pending()
        a.approve(by="alice")
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.APPROVED)
        self.assertEqual(a.decided_by, "alice")
        self.assertIsNotNone(a.decided_at)
        self.assertFalse(a.auto_approved)

    def test_decline_records_reason(self):
        a = _pending()
        a.decline(by="bob", reason="not now")
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.DECLINED)
        self.assertEqual(a.result["decline_reason"], "not now")

    def test_cannot_approve_a_non_pending_row(self):
        a = _pending()
        a.decline(by="bob")
        with self.assertRaises(ValueError):
            a.approve(by="alice")  # already declined — guards the double-decision race

    def test_cannot_double_execute(self):
        a = _pending()
        a.approve(by="alice")
        a.mark_executing()
        a.mark_done({"ok": True})
        with self.assertRaises(ValueError):
            a.mark_executing()  # DONE is terminal


class Fingerprint(TestCase):
    def test_matching_fingerprint_approves(self):
        fp = fingerprint("docs/a.md", "docs/b.md")
        a = _pending(diff_fingerprint=fp)
        a.approve(by="alice", expected_fingerprint=fp)  # no raise
        self.assertEqual(a.status, ActionApproval.APPROVED)

    def test_changed_fingerprint_refuses(self):
        a = _pending(diff_fingerprint=fingerprint("docs/a.md"))
        with self.assertRaises(FingerprintMismatch):
            # The PR gained a code file since it was reviewed → different fp.
            a.approve(by="alice", expected_fingerprint=fingerprint("docs/a.md", "src/x.py"))
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.PENDING)  # unchanged — not approved

    def test_no_expected_fingerprint_skips_the_check(self):
        # A caller that doesn't fingerprint gets the old behavior — the check is
        # opt-in per approval, not forced.
        a = _pending(diff_fingerprint=fingerprint("docs/a.md"))
        a.approve(by="alice")  # no expected_fingerprint → no comparison
        self.assertEqual(a.status, ActionApproval.APPROVED)


class Registry(TestCase):
    def test_executor_runs_and_records_result(self):
        reg = ExecutorRegistry()
        reg.register("merge_pr", lambda ap: {"ok": True, "merged": ap.action_payload["pr"]})
        a = _pending(action_payload={"pr": 42})
        a.approve(by="alice")
        result = reg.execute(a)
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.DONE)
        self.assertEqual(result, {"ok": True, "merged": 42})
        self.assertEqual(a.result["merged"], 42)

    def test_a_crashing_executor_marks_the_row_FAILED_not_the_worker(self):
        """The point of the registry: an out-of-band raise is invisible, so it must
        become a visible FAILED row instead of a lost action."""
        reg = ExecutorRegistry()

        def boom(ap):
            raise RuntimeError("kaboom")

        reg.register("merge_pr", boom)
        a = _pending()
        a.approve(by="alice")
        result = reg.execute(a)  # must NOT raise
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.FAILED)
        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["error"])
        self.assertIn("Traceback", result["traceback"])

    def test_missing_executor_fails_the_row_visibly(self):
        reg = ExecutorRegistry()  # nothing registered
        a = _pending(action_type="send_email")
        a.approve(by="alice")
        result = reg.execute(a)
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.FAILED)
        self.assertIn("no executor", result["error"])

    def test_double_registration_is_rejected(self):
        reg = ExecutorRegistry()
        reg.register("x", lambda a: {})
        with self.assertRaises(ValueError):
            reg.register("x", lambda a: {})

    def test_decorator_form(self):
        reg = ExecutorRegistry()

        @reg.executor("ping")
        def _ping(ap):
            return {"pong": True}

        self.assertTrue(reg.has("ping"))


class AutoApproval(TestCase):
    """The safety spine. Every default here must be the safe one."""

    def _policy_with_lane(self):
        pol = AutoApprovalPolicy(kill_switch_env="TEST_AUTO_ENABLED")
        pol.register("merge_pr", lambda ap: (True, "docs-only + green"))
        return pol

    def test_kill_switch_off_denies_everything(self):
        pol = self._policy_with_lane()  # env not set
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_AUTO_ENABLED", None)
            should, reason = pol.classify(_pending())
        self.assertFalse(should)
        self.assertIn("kill-switch", reason)

    def test_enabled_but_no_lane_still_denies(self):
        pol = self._policy_with_lane()
        with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": "1"}):
            should, reason = pol.classify(_pending(action_type="unblock_gate"))
        self.assertFalse(should)                 # default DENY for unregistered types
        self.assertIn("no auto-approval lane", reason)

    def test_registered_lane_can_approve_when_enabled(self):
        pol = self._policy_with_lane()
        with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": "1"}):
            should, reason = pol.classify(_pending(action_type="merge_pr"))
        self.assertTrue(should)

    def test_a_raising_classifier_fails_closed(self):
        pol = AutoApprovalPolicy(kill_switch_env="TEST_AUTO_ENABLED")

        def explode(ap):
            raise RuntimeError("github down")

        pol.register("merge_pr", explode)
        with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": "1"}):
            should, reason = pol.classify(_pending(action_type="merge_pr"))
        self.assertFalse(should)                 # error → DENY, never approve
        self.assertIn("fail-closed", reason)

    def test_a_classifier_returning_false_denies(self):
        pol = AutoApprovalPolicy(kill_switch_env="TEST_AUTO_ENABLED")
        pol.register("merge_pr", lambda ap: (False, "not docs-only: src/x.py"))
        with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": "1"}):
            should, reason = pol.classify(_pending(action_type="merge_pr"))
        self.assertFalse(should)
        self.assertIn("not docs-only", reason)

    def test_truthy_variants_enable(self):
        pol = self._policy_with_lane()
        for v in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": v}):
                self.assertTrue(pol.enabled(), v)
        for v in ("0", "false", "no", "off", ""):
            with mock.patch.dict(os.environ, {"TEST_AUTO_ENABLED": v}):
                self.assertFalse(pol.enabled(), v)
