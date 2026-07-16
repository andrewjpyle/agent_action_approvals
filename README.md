# agent_action_approvals

**Let an AI agent merge a PR without letting it merge *any* PR.**

The question every team wiring up an autonomous agent hits: how do you let it take real actions — merge a pull request, unblock a deploy, send an email — while keeping a human in the loop for the ones that matter? This is a small, durable Django app that answers it:

- an agent **enqueues** a bounded action as a row,
- a human (or a narrow auto-approval lane) **decides** it,
- an **executor** runs the approved action out-of-band and records the result.

```python
from agent_action_approvals import ActionApproval, ExecutorRegistry

# The agent enqueues — nothing happens yet.
approval = ActionApproval.objects.create(
    action_type="merge_pr",
    title="Merge #1342: docs typo fix",
    action_payload={"owner": "acme", "repo": "web", "pr_number": 1342},
    requested_by="agent-session-abc123",
)

# ...later, a human approves from your UI...
approval.approve(by="alice@acme.com")

# ...and a worker runs the registered executor.
registry = ExecutorRegistry()
registry.register("merge_pr", merge_pr_executor)
registry.execute(approval)   # EXECUTING → DONE, result stored
```

## Why durable, and why that's the point

The closest primitives are in-process: LangGraph's `interrupt()`, a Temporal signal, a raw `input()`. They all die with the process — a redeploy, a crash, or an agent that finishes its run drops every pending action on the floor. This is a **database row**. It survives a restart, it's queryable (`ActionApproval.objects.filter(status="pending")`), and the operator can approve it three days later from a phone. The durability *is* the feature.

## The safety contract

An approval queue is a safety control, so every default here is the safe one.

**The state machine can't be lied to.** Every transition guards its starting state and stamps the audit fields (`decided_by`, `decided_at`, `executing_at`). You can't approve a declined row, you can't execute twice, and a double-decision race (two operators, or an operator racing an auto-policy) loses cleanly instead of running the action twice.

**A crashed executor becomes a visible FAILED row, not a lost action.** `registry.execute()` drives `EXECUTING → DONE/FAILED` around your executor and never re-raises — an exception in an out-of-band worker is invisible, but a `FAILED` row with the traceback in `result` is not. A half-finished action is *visibly* half-finished.

**The fingerprint catches a bait-and-switch.** If you set `diff_fingerprint` at enqueue (say, a hash of a PR's changed files), `approve(expected_fingerprint=...)` refuses when the two differ — the PR gained a code file since it was reviewed, so the thing being approved is no longer the thing a human looked at:

```python
approval = ActionApproval.objects.create(
    ..., diff_fingerprint=fingerprint(*sorted(changed_paths)),
)
# at approve time, re-fetch and re-hash:
approval.approve(by="alice", expected_fingerprint=fingerprint(*sorted(current_paths)))
# → FingerprintMismatch if the PR changed since review
```

## Auto-approval: default-DENY, fail-closed, ships dark

Auto-approval is the deliberate, dangerous exception to "a human sees each action," so it's built to be as hard as possible to widen by accident:

```python
from agent_action_approvals import AutoApprovalPolicy

policy = AutoApprovalPolicy()                          # kill-switch OFF by default
policy.register("merge_pr", classify_docs_only)        # opt ONE lane in

should, reason = policy.classify(approval)
if should:
    approval.approve(by="auto", auto=True)
```

- **Default DENY.** An `action_type` with no registered classifier is never auto-approved. You opt lanes in one at a time; you can't forget to opt one out.
- **Fail closed.** Any exception in a classifier — a network blip, a missing field — resolves to *deny*. The unsafe default is the safe one.
- **A master kill-switch, off by default.** Until you set `AGENT_ACTION_AUTO_APPROVE_ENABLED`, the whole policy is inert and denies everything. Merging this code changes nothing until someone consciously turns it on.

`examples/docs_only_merge.py` is a complete worked lane: it auto-approves a PR only when the changed files (fetched *independently* from the GitHub API, never trusted from the payload) are all docs and CI is green — and denies everything else.

## Install

```bash
pip install agent-action-approvals   # or vendor the agent_action_approvals/ dir
```

Add to `INSTALLED_APPS` and migrate:

```python
INSTALLED_APPS = [..., "agent_action_approvals"]
```
```bash
python manage.py migrate agent_action_approvals
```

The **core app has zero dependencies** beyond Django. The GitHub example opts into `requests` on its own, so the library never forces it on you.

Run the executor from wherever you run background work — a Celery task, an RQ job, a thread — so an HTTP approve returns immediately:

```python
@shared_task
def execute_approval(approval_id):
    registry.execute(ActionApproval.objects.get(pk=approval_id))
```

## Tests

```bash
python runtests.py     # needs `django` importable; wires an in-memory sqlite app
```

18 tests. The largest group is the safety contract — default-DENY, fail-closed, the kill-switch, the fingerprint catch, crash-to-FAILED — because those are the properties that decide whether the queue is a real gate or just a speed bump.

## License

MIT
