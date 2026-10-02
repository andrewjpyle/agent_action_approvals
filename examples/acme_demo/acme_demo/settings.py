"""Settings for the Acme Docs demo (SAMPLE DATA, fictional company).

A sqlite FILE, not :memory:, so every manage.py call is a separate process
reading the same durable queue. That is the point of the demo.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "demo-only-not-a-secret"
DEBUG = True
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "agent_action_approvals",
    "ops",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("ACME_DEMO_DB", str(BASE_DIR / "demo.sqlite3")),
    }
}
