from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.importer import ImportFileError, load_places


class Command(BaseCommand):
    help = (
        "Load Census Gazetteer places (data/us_places.csv) used for City, ST lookups and geocoding."
        " Snapshot semantics: places absent from the file are deleted."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "file", nargs="?", default=str(settings.BASE_DIR / "data" / "us_places.csv")
        )
        parser.add_argument(
            "--min-rows", type=int, default=10000, help="refuse files with fewer places"
        )
        parser.add_argument(
            "--force", action="store_true", help="skip the size checks (empty files stay refused)"
        )

    def handle(self, *args, file, min_rows, force, **options):
        if not Path(file).exists():
            raise CommandError(f"file not found: {file}")
        try:
            result = load_places(Path(file), min_rows=min_rows, force=force)
        except ImportFileError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(
            self.style.SUCCESS(
                f"places in file: {result['places']}, new: {result['new']}, "
                f"deleted: {result['deleted']}"
            )
        )
