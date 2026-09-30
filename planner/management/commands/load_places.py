from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.importer import load_places


class Command(BaseCommand):
    help = "Load Census Gazetteer places (data/us_places.csv) used for City, ST lookups and geocoding."

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", default=str(settings.BASE_DIR / "data" / "us_places.csv"))

    def handle(self, *args, file, **options):
        if not Path(file).exists():
            raise CommandError(f"file not found: {file}")
        result = load_places(Path(file))
        self.stdout.write(self.style.SUCCESS(f"places in file: {result['places']}, new: {result['new']}, deleted: {result['deleted']}"))
