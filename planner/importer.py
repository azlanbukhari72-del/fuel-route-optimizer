"""Loading reference places and the fuel-price snapshot into PostgreSQL."""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import FuelStation, Place
from .names import lookup_keys

CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT", "NT", "NU", "PE", "NL"}
REQUIRED_COLUMNS = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
}
PRICE_QUANT = Decimal("0.0001")


class ImportFileError(Exception):
    pass


@dataclass
class ImportStats:
    rows_read: int = 0
    canadian_skipped: int = 0
    invalid_skipped: Counter = field(default_factory=Counter)
    duplicates_merged: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    geocoded: int = 0
    unmatched: int = 0
    unmatched_cities: list = field(default_factory=list)

    def summary(self) -> str:
        invalid = sum(self.invalid_skipped.values())
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.invalid_skipped.items())) or "-"
        return (
            f"rows read: {self.rows_read}\n"
            f"skipped (Canada): {self.canadian_skipped}\n"
            f"skipped (invalid): {invalid} [{reasons}]\n"
            f"duplicate rows merged: {self.duplicates_merged}\n"
            f"stations created/updated/unchanged/deleted: "
            f"{self.created}/{self.updated}/{self.unchanged}/{self.deleted}\n"
            f"geocoded: {self.geocoded}, unmatched (no coordinates): {self.unmatched}"
        )


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def choose_price(prices: list[Decimal]) -> Decimal:
    """Interpretation, not fact: several rows for one station are alternative price points and
    the LOWEST is the price a buyer could obtain. Change here if that reading is wrong."""
    return min(prices)


def canonical(values: list[str]) -> str:
    """Most frequent variant; ties -> lexicographically smallest (casefolded, then raw).
    Independent of row order, so re-imports are deterministic."""
    counts = Counter(values)
    return min(counts, key=lambda v: (-counts[v], v.casefold(), v))


def load_places(path: Path) -> dict:
    with Path(path).open(newline="", encoding="utf-8") as f:
        rows = [
            Place(
                state=r["state"],
                name_key=r["name_key"],
                name=r["name"][:120],
                latitude=Decimal(r["latitude"]),
                longitude=Decimal(r["longitude"]),
            )
            for r in csv.DictReader(f)
        ]
    with transaction.atomic():
        before = Place.objects.count()
        for i in range(0, len(rows), 5000):
            Place.objects.bulk_create(
                rows[i : i + 5000],
                update_conflicts=True,
                unique_fields=["state", "name_key"],
                update_fields=["name", "latitude", "longitude"],
            )
        keep = {(p.state, p.name_key) for p in rows}
        stale = [
            pk
            for pk, s, k in Place.objects.values_list("pk", "state", "name_key")
            if (s, k) not in keep
        ]
        Place.objects.filter(pk__in=stale).delete()
    return {
        "places": len(rows),
        "new": max(0, Place.objects.count() - before),
        "deleted": len(stale),
    }


def _parse_rows(path: Path, stats: ImportStats) -> dict[int, dict]:
    groups: dict[int, dict] = defaultdict(
        lambda: {"names": [], "prices": [], "addr": [], "city": [], "state": [], "rack": []}
    )
    try:
        f = Path(path).open(newline="", encoding="utf-8-sig")
    except OSError as e:
        raise ImportFileError(f"cannot read {path}: {e}") from e
    with f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ImportFileError(f"missing columns: {sorted(missing)}")
        for row in reader:
            stats.rows_read += 1
            state = _clean(row["State"]).upper()
            if state in CANADIAN_PROVINCES:
                stats.canadian_skipped += 1
                continue
            try:
                opis_id = int(_clean(row["OPIS Truckstop ID"]))
            except ValueError:
                stats.invalid_skipped["bad_id"] += 1
                continue
            try:
                price = Decimal(_clean(row["Retail Price"]))
            except InvalidOperation:
                stats.invalid_skipped["bad_price"] += 1
                continue
            price = (
                price.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP) if price.is_finite() else price
            )
            if not price.is_finite() or not (0 < price < 20):
                stats.invalid_skipped["bad_price"] += 1
                continue
            name, addr, city = (
                _clean(row["Truckstop Name"]),
                _clean(row["Address"]),
                _clean(row["City"]),
            )
            if not (name and city and len(state) == 2):
                stats.invalid_skipped["missing_field"] += 1
                continue
            try:
                rack = int(_clean(row["Rack ID"]))
            except ValueError:
                stats.invalid_skipped["bad_rack"] += 1
                continue
            g = groups[opis_id]
            g["names"].append(name)
            g["prices"].append(price)
            g["addr"].append(addr)
            g["city"].append(city)
            g["state"].append(state)
            g["rack"].append(rack)
    return groups


def import_stations(path: Path) -> ImportStats:
    """Import the fuel CSV as a COMPLETE SNAPSHOT: upsert by opis_id, delete stations absent
    from the file, all in one transaction. Re-importing the same file is a no-op."""
    stats = ImportStats()
    groups = _parse_rows(path, stats)
    stats.duplicates_merged = (
        stats.rows_read - stats.canadian_skipped - sum(stats.invalid_skipped.values()) - len(groups)
    )
    states = {canonical(g["state"]) for g in groups.values()}
    places = {
        (s, k): (lat, lng)
        for s, k, lat, lng in Place.objects.filter(state__in=states).values_list(
            "state", "name_key", "latitude", "longitude"
        )
    }
    now = timezone.now()
    desired: dict[int, dict] = {}
    for opis_id, g in groups.items():
        state = canonical(g["state"])
        city = canonical(g["city"])
        coords = next(
            (places[(state, k)] for k in lookup_keys(city) if (state, k) in places), (None, None)
        )
        if coords[0] is None:
            stats.unmatched += 1
            stats.unmatched_cities.append((city, state))
        else:
            stats.geocoded += 1
        desired[opis_id] = {
            "name": canonical(g["names"])[:200],
            "address": canonical(g["addr"])[:200],
            "city": city[:100],
            "state": state,
            "rack_id": int(canonical([str(r) for r in g["rack"]])),
            "price": choose_price(g["prices"]),
            "latitude": coords[0],
            "longitude": coords[1],
        }

    compared = ("name", "address", "city", "state", "rack_id", "price", "latitude", "longitude")
    with transaction.atomic():
        existing = {s.opis_id: s for s in FuelStation.objects.all()}
        to_create, to_update = [], []
        for opis_id, values in desired.items():
            obj = existing.get(opis_id)
            if obj is None:
                to_create.append(FuelStation(opis_id=opis_id, updated_at=now, **values))
            elif any(getattr(obj, f) != values[f] for f in compared):
                for f in compared:
                    setattr(obj, f, values[f])
                obj.updated_at = now
                to_update.append(obj)
            else:
                stats.unchanged += 1
        FuelStation.objects.bulk_create(to_create, batch_size=2000)
        FuelStation.objects.bulk_update(to_update, [*compared, "updated_at"], batch_size=2000)
        gone = set(existing) - set(desired)
        FuelStation.objects.filter(opis_id__in=gone).delete()
        stats.created, stats.updated, stats.deleted = len(to_create), len(to_update), len(gone)
    return stats
