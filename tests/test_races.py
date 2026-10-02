"""Races between processes, simulated with two independently loaded copies of
the same row (what two workers or two web requests actually hold).

Before the compare-and-swap transitions, every test in this file failed: each
copy checked its own in-memory ``status``, both saw PENDING (or APPROVED), and
both writes landed. The executor ran twice.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from agent_action_approvals import ActionApproval, ExecutorRegistry, InvalidTransition


def _pending(**kw) -> ActionApproval:
    base = dict(action_type="merge_pr", title="t", action_payload={})
    base.update(kw)
    return ActionApproval.objects.create(**base)


def _two_copies(a: ActionApproval):
    return ActionApproval.objects.get(pk=a.pk), ActionApproval.objects.get(pk=a.pk)


class DoubleDecision(TestCase):
    def test_approve_then_stale_decline_is_refused(self):
        w1, w2 = _two_copies(_pending())
        w1.approve(by="alice")
        with self.assertRaises(InvalidTransition):
            w2.decline(by="bob")
        row = ActionApproval.objects.get(pk=w1.pk)
        self.assertEqual(row.status, ActionApproval.APPROVED)
        self.assertEqual(row.decided_by, "alice")

    def test_two_stale_approvals_only_one_wins(self):
        w1, w2 = _two_copies(_pending())
        w1.approve(by="alice")
        with self.assertRaises(InvalidTransition):
            w2.approve(by="auto", auto=True)
        row = ActionApproval.objects.get(pk=w1.pk)
        self.assertEqual(row.decided_by, "alice")
        self.assertFalse(row.auto_approved)

    def test_loser_instance_is_refreshed_to_the_real_state(self):
        w1, w2 = _two_copies(_pending())
        w1.decline(by="bob", reason="no")
        with self.assertRaises(InvalidTransition):
            w2.approve(by="alice")
        self.assertEqual(w2.status, ActionApproval.DECLINED)

    def test_invalid_transition_is_still_a_value_error(self):
        self.assertTrue(issubclass(InvalidTransition, ValueError))


class DoubleExecute(TestCase):
    def test_two_workers_with_the_same_approval_run_the_executor_once(self):
        runs = []
        reg = ExecutorRegistry()
        reg.register("merge_pr", lambda ap: runs.append(ap.pk) or {"ok": True})
        a = _pending()
        a.approve(by="alice")
        w1, w2 = _two_copies(a)
        reg.execute(w1)
        with self.assertRaises(InvalidTransition):
            reg.execute(w2)
        self.assertEqual(runs, [a.pk])
        self.assertEqual(ActionApproval.objects.get(pk=a.pk).status, ActionApproval.DONE)


class Expiry(TestCase):
    def test_time_out_moves_pending_to_timed_out(self):
        a = _pending()
        a.time_out(reason="nobody answered")
        a.refresh_from_db()
        self.assertEqual(a.status, ActionApproval.TIMED_OUT)
        self.assertEqual(a.result["timeout_reason"], "nobody answered")
        self.assertIn(a.status, ActionApproval.TERMINAL)

    def test_cannot_approve_after_time_out(self):
        w1, w2 = _two_copies(_pending())
        w1.time_out()
        with self.assertRaises(InvalidTransition):
            w2.approve(by="alice")
        self.assertEqual(ActionApproval.objects.get(pk=w1.pk).status, ActionApproval.TIMED_OUT)

    def test_time_out_loses_to_an_earlier_approve(self):
        w1, w2 = _two_copies(_pending())
        w1.approve(by="alice")
        with self.assertRaises(InvalidTransition):
            w2.time_out()
        self.assertEqual(ActionApproval.objects.get(pk=w1.pk).status, ActionApproval.APPROVED)

    def test_expire_pending_only_touches_old_pending_rows(self):
        old = _pending(title="old")
        old_approved = _pending(title="old but approved")
        old_approved.approve(by="alice")
        fresh = _pending(title="fresh")
        two_days_ago = timezone.now() - timedelta(days=2)
        ActionApproval.objects.filter(pk__in=[old.pk, old_approved.pk]).update(created_at=two_days_ago)

        n = ActionApproval.expire_pending(older_than=timedelta(days=1))

        self.assertEqual(n, 1)
        self.assertEqual(ActionApproval.objects.get(pk=old.pk).status, ActionApproval.TIMED_OUT)
        self.assertEqual(ActionApproval.objects.get(pk=old_approved.pk).status, ActionApproval.APPROVED)
        self.assertEqual(ActionApproval.objects.get(pk=fresh.pk).status, ActionApproval.PENDING)

    def test_a_row_held_in_memory_cannot_be_approved_after_bulk_expiry(self):
        a = _pending()
        held = ActionApproval.objects.get(pk=a.pk)  # e.g. an approve form opened yesterday
        ActionApproval.objects.filter(pk=a.pk).update(created_at=timezone.now() - timedelta(days=2))
        ActionApproval.expire_pending(older_than=timedelta(days=1))
        with self.assertRaises(InvalidTransition):
            held.approve(by="alice")
