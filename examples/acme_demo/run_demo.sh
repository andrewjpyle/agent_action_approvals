#!/bin/sh
# The full approval lifecycle, one separate process per step.
# SAMPLE DATA, fictional company (Acme Docs). No network, no secrets.
# Run from this directory after `pip install` of the repo root.
set -u
cd "$(dirname "$0")"
export ACME_DEMO_DB="${ACME_DEMO_DB:-demo.sqlite3}"
rm -f "$ACME_DEMO_DB"

step() { printf '\n== %s\n' "$1"; }
run() {
  printf '$ python manage.py approvals'
  for a in "$@"; do
    case "$a" in *" "*|*"#"*|*";"*) printf ' "%s"' "$a" ;; *) printf ' %s' "$a" ;; esac
  done
  printf '\n'
  python manage.py approvals "$@"
}

python manage.py migrate --verbosity 0

step "1. An agent enqueues actions. Each is a row; nothing runs."
run enqueue merge_pr "Merge #101: install guide typo" --pr 101
run enqueue merge_pr "Merge #102: billing fix + API docs" --pr 102
run enqueue send_email "Email 40 customers about the outage"
run list

step "2. A human decides, in a later process. The queue survived the restart."
run approve 1 --by alice@acme.example
run decline 2 --by alice@acme.example --reason "touches billing; needs review"
run approve 3 --by alice@acme.example

step "3. A worker runs only what was approved."
run work
run list

step "4. Two deciders race on one row. Exactly one wins."
run enqueue merge_pr "Merge #103: changelog" --pr 103
run race-decide 4

step "5. Two workers get the same approved row. The executor runs once."
run enqueue merge_pr "Merge #101 again (retried job)" --pr 101
run approve 5 --by alice@acme.example
run race-work 5

step "6. Bait-and-switch: the PR gains a code file after review."
run enqueue merge_pr "Merge #101: install guide" --pr 101 --fingerprint-pr
printf '(a commit adding src/deploy.py lands on PR #101)\n'
export ACME_PUSHED_FILE=101:src/deploy.py
run approve 6 --by alice@acme.example --check-fingerprint
unset ACME_PUSHED_FILE
run list

step "7. Expiry: stale requests time out instead of waiting forever."
run enqueue merge_pr "Merge #101: from last week" --pr 101 --backdate-hours 30
run expire --older-than-hours 24
run approve 7 --by alice@acme.example

step "8. Auto-approval ships dark, then opens exactly one lane."
run enqueue merge_pr "Merge #101: docs only" --pr 101
run enqueue merge_pr "Merge #102: has code" --pr 102
run auto 8
printf '$ export ACME_AUTO_APPROVE_ENABLED=1\n'
export ACME_AUTO_APPROVE_ENABLED=1
run auto 8
run auto 9
run work
unset ACME_AUTO_APPROVE_ENABLED

step "9. A worker process dies mid-execution (no exception, no cleanup)."
run enqueue merge_pr "Merge #101: final docs pass" --pr 101
run approve 10 --by alice@acme.example
export ACME_DIE_MID_EXECUTION=1
run work
printf '(worker exited with status %s)\n' "$?"
unset ACME_DIE_MID_EXECUTION
run work
run list

rm -f "$ACME_DEMO_DB"
