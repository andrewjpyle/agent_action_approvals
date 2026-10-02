# Acme Docs demo (SAMPLE DATA, fictional company)

A tiny Django project that drives the approval queue from the shell. Acme Docs does not exist; its
pull requests live in a Python dict (`ops/acme.py`), and its executors print what they would do.
No network, no secrets.

```bash
pip install .                         # from the repo root
sh examples/acme_demo/run_demo.sh     # from anywhere
```

`run_demo.sh` deletes and recreates `demo.sqlite3`, then runs each step as its own
`python manage.py approvals ...` process, so the queue has to survive between processes. Step 9 kills
the worker with `os._exit(137)` mid-executor to show what a crash leaves behind.

`TRANSCRIPT.txt` is the unedited output of one run. The README graphics are built from the same run.

| Verb | What it does |
|---|---|
| `enqueue <type> <title> [--pr N] [--fingerprint-pr]` | the agent's side: add a pending row |
| `approve <id> --by <who> [--check-fingerprint]` / `decline <id> --by <who>` | the human's side |
| `work` | a worker: run every approved row through the registry |
| `race-decide <id>` / `race-work <id>` | load two copies of one row and let them collide |
| `expire --older-than-hours N` | time out stale pending rows |
| `auto <id>` | ask the auto-approval policy (kill switch: `ACME_AUTO_APPROVE_ENABLED`) |
| `list` | print the queue |
