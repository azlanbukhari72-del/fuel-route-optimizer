import random
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, connection, transaction

from planner.importer import ImportFileError, canonical, import_stations, load_places
from planner.models import FuelStation, Place
from planner.names import lookup_keys, normalize_place

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


@pytest.fixture
def places(db):
    Place.objects.bulk_create(
        [
            Place(state="OK", name_key="big cabin", name="Big Cabin", latitude=D("36.54"), longitude=D("-95.22")),
            Place(state="AZ", name_key="gila bend", name="Gila Bend", latitude=D("32.95"), longitude=D("-112.71")),
            Place(state="TX", name_key="denver city", name="Denver City", latitude=D("32.96"), longitude=D("-102.83")),
        ]
    )


def write(tmp_path, body, name="fuel.csv"):
    p = tmp_path / name
    p.write_text(HEADER + body, encoding="utf-8")
    return p


BODY = (
    '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283",Big Cabin,OK,307,3.00733333\n'
    "20,PILOT TRAVEL CENTER #1243,I-8,Gila Bend  ,AZ,930,3.899\n"
    "20,PILOT #1243,I-8,Gila Bend,AZ,930,3.799\n"
    "20,PILOT #1243,I-8,Gila Bend,AZ,930,3.799\n"  # exact duplicate
    "30,SOME PLACE,Hwy 1,Nowhereville,AZ,1,3.5\n"  # unknown city -> null coords
    "31,MAPLE STOP,Hwy 2,Toronto,ON,1,3.5\n"  # Canada
    "32,BAD PRICE,Hwy 3,Gila Bend,AZ,1,abc\n"
    "33,ZERO PRICE,Hwy 3,Gila Bend,AZ,1,0\n"
    ",NO ID,Hwy 3,Gila Bend,AZ,1,3.1\n"
    "34,DENVER CITY STOP,Hwy 4,Denver City,TX,5,3.2\n"
)


def test_import_counts_and_rules(tmp_path, places):
    stats = import_stations(write(tmp_path, BODY))
    assert stats.rows_read == 10
    assert stats.canadian_skipped == 1
    assert stats.invalid_skipped == {"bad_price": 2, "bad_id": 1}
    assert stats.duplicates_merged == 2  # 3 rows of id 20 -> 1 station
    assert (stats.created, stats.updated, stats.deleted) == (4, 0, 0)
    assert (stats.geocoded, stats.unmatched) == (3, 1)

    s20 = FuelStation.objects.get(opis_id=20)
    assert s20.price == D("3.7990")  # lowest of the duplicate price rows
    assert s20.city == "Gila Bend"  # whitespace collapsed
    assert s20.name == "PILOT #1243"  # most frequent variant (2 of 3 rows)
    assert FuelStation.objects.get(opis_id=7).price == D("3.0073")  # 8 dp -> 4 dp
    assert FuelStation.objects.get(opis_id=30).latitude is None
    assert FuelStation.objects.get(opis_id=34).latitude == D("32.96")  # "Denver City" keeps 'city'
    assert not FuelStation.objects.filter(opis_id=31).exists()


def test_reimport_is_noop_and_keeps_timestamps(tmp_path, places):
    f = write(tmp_path, BODY)
    import_stations(f)
    stamps = dict(FuelStation.objects.values_list("opis_id", "updated_at"))
    stats = import_stations(f)
    assert (stats.created, stats.updated, stats.deleted) == (0, 0, 0)
    assert stats.unchanged == 4
    assert dict(FuelStation.objects.values_list("opis_id", "updated_at")) == stamps


def test_snapshot_semantics_update_and_delete(tmp_path, places):
    import_stations(write(tmp_path, BODY))
    second = write(
        tmp_path,
        '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283",Big Cabin,OK,307,2.5\n'
        "34,DENVER CITY STOP,Hwy 4,Denver City,TX,5,3.2\n",
        "second.csv",
    )
    stats = import_stations(second)
    assert (stats.created, stats.updated, stats.unchanged, stats.deleted) == (0, 1, 1, 2)
    assert set(FuelStation.objects.values_list("opis_id", flat=True)) == {7, 34}
    assert FuelStation.objects.get(opis_id=7).price == D("2.5000")


def test_canonical_name_deterministic_for_any_row_order():
    variants = ["PILOT TRAVEL CENTER #1243", "PILOT #1243"]  # tie -> lexicographically smallest
    for seed in range(5):
        shuffled = variants[:]
        random.Random(seed).shuffle(shuffled)
        assert canonical(shuffled) == "PILOT #1243"
    assert canonical(["B", "A", "B"]) == "B"


def test_missing_columns_and_missing_file(tmp_path, db):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ImportFileError):
        import_stations(bad)
    with pytest.raises(ImportFileError):
        import_stations(tmp_path / "nope.csv")


def test_db_constraints(places):
    ok = dict(name="n", address="a", city="c", state="AZ", rack_id=1, updated_at="2026-01-01T00:00:00Z")
    with pytest.raises(IntegrityError), transaction.atomic():
        FuelStation.objects.create(opis_id=1, price=D("0"), **ok)
    with pytest.raises(IntegrityError), transaction.atomic():
        FuelStation.objects.create(opis_id=2, price=D("3"), latitude=D("30"), **ok)  # lat without lng
    FuelStation.objects.create(opis_id=3, price=D("3"), **ok)
    with pytest.raises(IntegrityError), transaction.atomic():
        FuelStation.objects.create(opis_id=3, price=D("3"), **ok)  # unique opis_id
    with pytest.raises(IntegrityError), transaction.atomic():
        Place.objects.create(state="OK", name_key="big cabin", name="dup", latitude=D("36"), longitude=D("-95"))


def test_indexes_exist(db):
    with connection.cursor() as c:
        c.execute("select indexname from pg_indexes where tablename = 'planner_fuelstation'")
        names = {r[0] for r in c.fetchall()}
    assert "station_lat_lng_idx" in names
    with connection.cursor() as c:
        c.execute("select indexname from pg_indexes where tablename = 'planner_place'")
        assert any("place_state_key_uniq" in r[0] for r in c.fetchall())


def test_load_places_idempotent_and_snapshot(tmp_path, db):
    f = tmp_path / "p.csv"
    f.write_text("state,name_key,name,latitude,longitude\nOK,tulsa,Tulsa,36.15,-95.99\nAZ,yuma,Yuma,32.69,-114.62\n")
    assert load_places(f)["new"] == 2
    assert load_places(f)["new"] == 0
    f.write_text("state,name_key,name,latitude,longitude\nOK,tulsa,Tulsa,36.20,-95.99\n")
    r = load_places(f)
    assert r["deleted"] == 1 and Place.objects.get(state="OK", name_key="tulsa").latitude == D("36.20")


def test_committed_places_file_has_unique_keys():
    import csv
    from pathlib import Path

    rows = list(csv.DictReader(open(Path(__file__).parent.parent / "data" / "us_places.csv", encoding="utf-8")))
    keys = [(r["state"], r["name_key"]) for r in rows]
    assert len(rows) > 40000 and len(keys) == len(set(keys))
    assert all(r["name_key"] == normalize_place(r["name"]) for r in rows[:5000:50])


def test_lookup_keys():
    assert lookup_keys("Denver City")[:2] == ["denver city", "denver"]
    assert lookup_keys("Saint Louis")[0] == "st louis"
    assert lookup_keys("  New   York  ")[0] == "new york"
