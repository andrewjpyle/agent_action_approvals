"""Worked example: an auto-approval lane for docs-only PRs, and its executor.

This is the ONE lane it's usually safe to auto-approve: a pull request whose
changed files are all documentation, and whose CI is green. It demonstrates the
two rules that keep an auto-approval classifier honest:

  1. Verify INDEPENDENTLY. The changed-file list is fetched from the GitHub API,
     never read from the approval payload — an agent that could set its own
     "these are all docs" flag would defeat the gate.
  2. Prove the safe case; deny everything else. Any file that isn't docs, any
     non-2xx response, any exception → return False. Allowlist, not blocklist.

Wire it up:

    from agent_action_approvals import AutoApprovalPolicy, ExecutorRegistry
    from examples.docs_only_merge import classify_docs_only, execute_merge

    policy = AutoApprovalPolicy()                 # kill-switch off by default
    policy.register("merge_pr", classify_docs_only)

    registry = ExecutorRegistry()
    registry.register("merge_pr", execute_merge)

Requires the `requests` package and a GITHUB_TOKEN env var. Kept out of the core
package precisely because the core has zero dependencies; this example opts into
`requests` so the library doesn't.
"""
from __future__ import annotations

import os

from agent_action_approvals.models import fingerprint

_GH_API = "https://api.github.com"
_DOC_SUFFIXES = (".md", ".mdx", ".txt", ".rst")
_PER_PAGE = 100
_MAX_PAGES = 5


def _headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _is_docs_path(path: str) -> bool:
    p = (path or "").strip().lower()
    return bool(p) and (p.endswith(_DOC_SUFFIXES) or p.startswith("docs/"))


def classify_docs_only(approval) -> tuple[bool, str]:
    """Auto-approve a merge_pr only if every changed file is docs AND CI is green.
    Fails closed on any error. Sets approval.diff_fingerprint to the reviewed set.
    """
    import requests  # local import: the core library must stay dependency-free

    payload = approval.action_payload or {}
    owner, repo, pr = payload.get("owner"), payload.get("repo"), payload.get("pr_number")
    if not (owner and repo and pr):
        return False, "missing owner/repo/pr_number → fail-closed"

    try:
        paths: list[str] = []
        for page in range(1, _MAX_PAGES + 1):
            r = requests.get(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr}/files",
                headers=_headers(), params={"per_page": _PER_PAGE, "page": page}, timeout=30,
            )
            if not (200 <= r.status_code < 300):
                return False, "github error → fail-closed"
            batch = r.json() or []
            paths.extend(f.get("filename", "") for f in batch)
            if len(batch) < _PER_PAGE:
                break

        if not paths:
            return False, "no files → deny"
        for p in paths:
            if not _is_docs_path(p):
                return False, f"not docs-only: {p}"

        pr_resp = requests.get(f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr}",
                               headers=_headers(), timeout=30)
        if not (200 <= pr_resp.status_code < 300):
            return False, "github error → fail-closed"
        if (pr_resp.json() or {}).get("mergeable_state") != "clean":
            return False, "checks not green / not mergeable"
    except Exception as exc:  # noqa: BLE001 — network/parse error → fail closed
        return False, f"github call failed → fail-closed: {exc}"

    # Bind the approval to exactly what we evaluated.
    approval.diff_fingerprint = fingerprint(*sorted(paths))
    return True, "docs-only + mergeable"


def execute_merge(approval) -> dict:
    """Merge the PR named in the payload. Runs only after approval."""
    import requests

    payload = approval.action_payload or {}
    owner, repo, pr = payload["owner"], payload["repo"], payload["pr_number"]
    r = requests.put(
        f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr}/merge",
        headers=_headers(), json={"merge_method": "squash"}, timeout=30,
    )
    ok = 200 <= r.status_code < 300
    return {"ok": ok, "status_code": r.status_code, "response": r.json() if ok else r.text}
