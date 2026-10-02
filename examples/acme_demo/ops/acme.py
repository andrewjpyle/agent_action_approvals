"""Acme Docs wiring: SAMPLE DATA, fictional company, no network.

FAKE_PULLS stands in for the GitHub API. The executors print what they would do
and return a result; nothing leaves this machine.
"""
import os

from agent_action_approvals import AutoApprovalPolicy, ExecutorRegistry

# Pull requests in Acme's (fictional) docs repo, as an API would report them.
FAKE_PULLS = {
    101: {"files": ["docs/install.md", "README.md"], "ci": "green"},
    102: {"files": ["docs/api.md", "src/billing.py"], "ci": "green"},
    103: {"files": ["CHANGELOG.md"], "ci": "red"},
}

# Demo only: ACME_PUSHED_FILE="101:src/deploy.py" simulates a commit pushed to a
# PR after it was enqueued, so a later process sees a different file list.
if os.environ.get("ACME_PUSHED_FILE"):
    _pr, _path = os.environ["ACME_PUSHED_FILE"].split(":", 1)
    FAKE_PULLS[int(_pr)]["files"].append(_path)

registry = ExecutorRegistry()


@registry.executor("merge_pr")
def merge_pr(approval):
    pr = approval.action_payload["pr_number"]
    if os.environ.get("ACME_DIE_MID_EXECUTION") == "1":
        print(f"  executor: merging PR #{pr} ... worker process killed (simulated crash)", flush=True)
        os._exit(137)  # no exception, no cleanup: what a SIGKILL or OOM looks like
    print(f"  executor: merged PR #{pr} (simulated, no network)")
    return {"ok": True, "merged_pr": pr}


@registry.executor("send_email")
def send_email(approval):
    raise ConnectionError("SMTP relay refused the connection (simulated)")


policy = AutoApprovalPolicy(kill_switch_env="ACME_AUTO_APPROVE_ENABLED")


@policy.classifier("merge_pr")
def docs_only(approval):
    # Look the PR up independently; never trust a "docs only" flag in the payload.
    pr = FAKE_PULLS.get(approval.action_payload.get("pr_number"))
    if pr is None:
        return False, "unknown PR"
    code = [f for f in pr["files"] if not f.endswith((".md", ".rst", ".txt"))]
    if code:
        return False, f"not docs-only: {code[0]}"
    if pr["ci"] != "green":
        return False, "CI is not green"
    return True, "docs-only + CI green"
