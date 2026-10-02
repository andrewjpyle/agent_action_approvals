#!/usr/bin/env python
"""Acme Docs demo project (SAMPLE DATA, fictional company). See README.md here."""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acme_demo.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
