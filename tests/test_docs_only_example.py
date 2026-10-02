"""Tests for examples/docs_only_merge.py against a fake ``requests`` module.

No network. The fake answers the three GitHub calls the example makes and
records every request so the tests can check what would have been sent.
"""
from __future__ import annotations

import sys
import types
from unittest import mock

from django.test import TestCase

from agent_action_approvals import ActionApproval
from examples.docs_only_merge import classify_docs_only, execute_merge


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _fake_requests(files, *, head_sha="abc123", mergeable="clean", merge_status=200):
    """files: a list of GitHub file dicts. Paginated 100 per page like the API."""
    calls = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        if url.endswith("/files"):
            page = params["page"]
            return _Resp(200, files[(page - 1) * 100: page * 100])
        return _Resp(200, {"mergeable_state": mergeable, "head": {"sha": head_sha}})

    def put(url, headers=None, json=None, timeout=None):
        calls.append(("PUT", url, json))
        return _Resp(merge_status, {"merged": merge_status == 200})

    mod = types.ModuleType("requests")
    mod.get, mod.put, mod.calls = get, put, calls
    return mod


def _approval():
    return ActionApproval.objects.create(
        action_type="merge_pr", title="docs",
        action_payload={"owner": "acme", "repo": "web", "pr_number": 7},
    )


class DocsOnlyClassifier(TestCase):
    def _classify(self, fake, approval):
        with mock.patch.dict(sys.modules, {"requests": fake}):
            return classify_docs_only(approval)

    def test_docs_only_pr_is_approved_and_the_binding_is_persisted(self):
        a = _approval()
        should, _ = self._classify(_fake_requests([{"filename": "README.md"}]), a)
        self.assertTrue(should)
        row = ActionApproval.objects.get(pk=a.pk)
        self.assertNotEqual(row.diff_fingerprint, "")
        self.assertEqual(row.action_payload["reviewed_head_sha"], "abc123")

    def test_a_code_file_is_denied(self):
        should, reason = self._classify(
            _fake_requests([{"filename": "README.md"}, {"filename": "src/app.py"}]), _approval())
        self.assertFalse(should)
        self.assertIn("src/app.py", reason)

    def test_renaming_code_to_a_doc_name_is_denied(self):
        files = [{"filename": "notes.md", "previous_filename": "src/app.py", "status": "renamed"}]
        should, reason = self._classify(_fake_requests(files), _approval())
        self.assertFalse(should)
        self.assertIn("src/app.py", reason)

    def test_non_doc_file_under_docs_dir_is_denied(self):
        should, _ = self._classify(_fake_requests([{"filename": "docs/conf.py"}]), _approval())
        self.assertFalse(should)

    def test_a_pr_too_big_to_list_fully_is_denied(self):
        # 500 doc files fill every page the example reads; file 501 is code.
        files = [{"filename": f"d{i}.md"} for i in range(500)] + [{"filename": "src/x.py"}]
        should, reason = self._classify(_fake_requests(files), _approval())
        self.assertFalse(should)
        self.assertIn("or more files", reason)

    def test_red_ci_is_denied(self):
        should, _ = self._classify(
            _fake_requests([{"filename": "README.md"}], mergeable="blocked"), _approval())
        self.assertFalse(should)


class DocsOnlyExecutor(TestCase):
    def test_merge_is_pinned_to_the_reviewed_head_sha(self):
        a = _approval()
        fake = _fake_requests([{"filename": "README.md"}], head_sha="feedbeef")
        with mock.patch.dict(sys.modules, {"requests": fake}):
            classify_docs_only(a)
            a.refresh_from_db()
            result = execute_merge(a)
        self.assertTrue(result["ok"])
        method, _, body = fake.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(body["sha"], "feedbeef")

    def test_refuses_to_merge_without_a_reviewed_sha(self):
        fake = _fake_requests([])
        with mock.patch.dict(sys.modules, {"requests": fake}):
            result = execute_merge(_approval())
        self.assertFalse(result["ok"])
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])
