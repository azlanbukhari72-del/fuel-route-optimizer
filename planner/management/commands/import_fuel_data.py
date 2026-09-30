from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from planner.importer import ImportFileError, import_stations


class Command(BaseCommand):
    help = (
        "Import the fuel-price CSV as a complete snapshot (upsert by OPIS id; stations missing "
        "from the file are deleted). Safe to re-run. Run load_places first."
    )

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument(
            "--min-rows", type=int, default=1000, help="refuse files with fewer usable stations"
        )
        parser.add_argument(
            "--force", action="store_true", help="skip the size checks (empty files stay refused)"
        )
        parser.add_argument(
            "--show-unmatched", action="store_true", help="list cities without coordinates"
        )

    def handle(self, *args, file, show_unmatched, min_rows, force, **options):
        try:
            stats = import_stations(Path(file), min_rows=min_rows, force=force)
        except ImportFileError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(stats.summary())
        if show_unmatched:
            for city, state in sorted(set(stats.unmatched_cities)):
                self.stdout.write(f"  unmatched: {city}, {state}")
