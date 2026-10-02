from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class Migrations(TestCase):
    def test_models_and_migrations_agree(self):
        # Fails if a model change shipped without its migration.
        call_command("makemigrations", "agent_action_approvals", "--check", "--dry-run",
                     stdout=StringIO())
