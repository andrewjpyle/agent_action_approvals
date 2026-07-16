#!/usr/bin/env python3
"""Standalone test runner for agent_action_approvals.

The app needs Django configured (settings, app registry, an in-memory DB for the
migration), which a bare ``unittest discover`` can't provide. This wires a
minimal settings module and runs the suite with Django's own test runner — so the
package is testable without a host project.

    python runtests.py          # needs `django` importable (e.g. the backend venv)

The OSS rail invokes this via scripts/oss/run_django_package_tests.py inside the
backend venv, where Django is on the path.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # make `agent_action_approvals` and `tests` importable

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def main() -> int:
    import django
    from django.test.utils import get_runner
    from django.conf import settings

    django.setup()
    runner_cls = get_runner(settings)
    runner = runner_cls(verbosity=2)
    failures = runner.run_tests(["tests"])
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
