"""manage.py seed_scenarios <file.csv> — load version-controlled scenario
definitions into the models (design §5, §8)."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from mockvendor import seed


class Command(BaseCommand):
    help = "Seed mock scenarios from a scenario-library CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--run-id", default="", help="Namespace seeded rows (parallel isolation).")

    def handle(self, *args, **opts):
        path = opts["csv_path"]
        try:
            result = seed.seed_scenarios_csv(path, run_id=opts["run_id"])
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {result['scenarios']} scenario(s) across {result['endpoints']} endpoint(s)."
        ))
