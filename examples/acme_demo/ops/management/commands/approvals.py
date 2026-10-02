"""``manage.py approvals <verb>``: drive the approval queue from a shell.

Each call is its own process, so running these in sequence shows the queue
surviving between processes. SAMPLE DATA, fictional company.
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from agent_action_approvals import ActionApproval, FingerprintMismatch, InvalidTransition
from agent_action_approvals.models import fingerprint
from ops.acme import FAKE_PULLS, policy, registry


def _row(a):
    extra = ""
    if a.decided_by:
        extra += f" decided_by={a.decided_by}"
    if a.status == a.EXECUTING and a.executing_at:
        extra += " executing_since=" + a.executing_at.strftime("%H:%M:%SZ")
    if a.auto_approved:
        extra += " auto"
    if a.result:
        r = {k: v for k, v in a.result.items() if k != "traceback"}
        if "traceback" in a.result:
            r["traceback"] = f"<{len(a.result['traceback'].splitlines())} lines>"
        extra += f" result={json.dumps(r, sort_keys=True)}"
    return f"  #{a.pk:<3} {a.status:<9} {a.action_type:<10} {a.title}{extra}"


class Command(BaseCommand):
    help = "Drive the agent action approval queue (Acme Docs demo)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="verb", required=True)
        e = sub.add_parser("enqueue")
        e.add_argument("action_type")
        e.add_argument("title")
        e.add_argument("--pr", type=int)
        e.add_argument("--by", default="agent-session-7f3a")
        e.add_argument("--fingerprint-pr", action="store_true",
                       help="bind the approval to the PR's current file list")
        e.add_argument("--backdate-hours", type=int, default=0,
                       help="demo only: pretend the row was enqueued this long ago")
        sub.add_parser("list")
        ap = sub.add_parser("approve")
        ap.add_argument("id", type=int)
        ap.add_argument("--by", required=True)
        ap.add_argument("--check-fingerprint", action="store_true")
        d = sub.add_parser("decline")
        d.add_argument("id", type=int)
        d.add_argument("--by", required=True)
        d.add_argument("--reason", default="")
        sub.add_parser("work")
        r = sub.add_parser("race-decide")
        r.add_argument("id", type=int)
        w = sub.add_parser("race-work")
        w.add_argument("id", type=int)
        x = sub.add_parser("expire")
        x.add_argument("--older-than-hours", type=int, required=True)
        au = sub.add_parser("auto")
        au.add_argument("id", type=int)

    def handle(self, *args, verb, **o):
        getattr(self, "do_" + verb.replace("-", "_"))(**o)

    def _get(self, pk):
        try:
            return ActionApproval.objects.get(pk=pk)
        except ActionApproval.DoesNotExist:
            raise CommandError(f"no approval #{pk}")

    def do_enqueue(self, action_type, title, pr, by, fingerprint_pr, backdate_hours, **_):
        payload = {"pr_number": pr} if pr else {}
        fp = fingerprint(*sorted(FAKE_PULLS[pr]["files"])) if fingerprint_pr else ""
        a = ActionApproval.objects.create(action_type=action_type, title=title,
                                          action_payload=payload, requested_by=by,
                                          diff_fingerprint=fp)
        if backdate_hours:
            ActionApproval.objects.filter(pk=a.pk).update(
                created_at=timezone.now() - timedelta(hours=backdate_hours))
        note = f" (fingerprint {fp})" if fp else ""
        note += f" (backdated {backdate_hours}h)" if backdate_hours else ""
        print(f"  enqueued #{a.pk} {action_type}: {title}{note}. Nothing has run.")

    def do_list(self, **_):
        for a in ActionApproval.objects.order_by("pk"):
            print(_row(a))

    def do_approve(self, id, by, check_fingerprint, **_):
        a = self._get(id)
        expected = None
        if check_fingerprint:
            expected = fingerprint(*sorted(FAKE_PULLS[a.action_payload["pr_number"]]["files"]))
        try:
            a.approve(by=by, expected_fingerprint=expected)
        except FingerprintMismatch as exc:
            print(f"  REFUSED: {exc}")
            return
        except InvalidTransition as exc:
            print(f"  REFUSED: {exc}")
            return
        print(f"  #{a.pk} approved by {by}. Still nothing has run; a worker picks it up.")

    def do_decline(self, id, by, reason, **_):
        a = self._get(id)
        a.decline(by=by, reason=reason)
        print(f"  #{a.pk} declined by {by}. It will never run.")

    def do_work(self, **_):
        todo = list(ActionApproval.objects.filter(status=ActionApproval.APPROVED).order_by("pk"))
        if not todo:
            print("  worker: nothing approved to run")
        for a in todo:
            print(f"  worker: running #{a.pk} {a.action_type}", flush=True)
            result = registry.execute(a)
            print(f"  worker: #{a.pk} -> {a.status} (ok={result.get('ok')})")

    def do_race_decide(self, id, **_):
        # Two copies of one PENDING row, like two web requests loaded a second apart.
        alice_copy = self._get(id)
        bot_copy = self._get(id)
        print(f"  both copies loaded: alice sees {alice_copy.status}, bot sees {bot_copy.status}")
        alice_copy.decline(by="alice@acme.example", reason="CI is red")
        print("  alice declines: ok")
        try:
            bot_copy.approve(by="auto-policy", auto=True)
            print("  bot approves: ok  <- both decisions landed")
        except InvalidTransition as exc:
            print(f"  bot approves: REFUSED ({exc})")

    def do_race_work(self, id, **_):
        # Two workers handed the same approved row (a retried job, a double enqueue).
        w1, w2 = self._get(id), self._get(id)
        print(f"  both workers loaded #{id}: {w1.status} / {w2.status}")
        for name, copy in (("worker-1", w1), ("worker-2", w2)):
            try:
                registry.execute(copy)
                print(f"  {name}: executed")
            except InvalidTransition as exc:
                print(f"  {name}: REFUSED to run ({exc})")

    def do_expire(self, older_than_hours, **_):
        n = ActionApproval.expire_pending(older_than=timedelta(hours=older_than_hours))
        print(f"  timed out {n} pending approval(s) older than {older_than_hours}h")

    def do_auto(self, id, **_):
        a = self._get(id)
        should, reason = policy.classify(a)
        print(f"  policy says: {'APPROVE' if should else 'DENY'} ({reason})")
        if should:
            a.approve(by="auto-policy", auto=True)
            print(f"  #{a.pk} auto-approved")
