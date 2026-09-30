"""Build data/us_places.csv from the US Census Gazetteer (public domain).

    python scripts/build_us_places.py            # downloads the two source files

Deterministic collision rule per (state, name_key): lowest rank (incorporated place=0, CDP=1,
county subdivision=2), then LARGEST land area, then lowest GEOID. Land area is only a proxy for
"the place the user meant"; the Gazetteer has no population. See README "Known limitations".
"""

import csv
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from planner.names import display_name, place_key  # noqa: E402

BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/"
FILES = {"places": "2023_Gaz_place_national.zip", "cousubs": "2023_Gaz_cousubs_national.zip"}
OUT = Path(__file__).resolve().parent.parent / "data" / "us_places.csv"


def rows(kind: str):
    raw = urllib.request.urlopen(BASE + FILES[kind], timeout=60).read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    raw = z.read(z.namelist()[0])
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        lines = raw.decode("latin-1").splitlines()
    header = [h.strip() for h in lines[0].split("\t")]
    for line in lines[1:]:
        yield dict(zip(header, [x.strip() for x in line.split("\t")], strict=False))


def main() -> None:
    cands = []
    for d in rows("places"):
        rank = 1 if d["LSAD"] == "57" else 0  # 57 = CDP
        cands.append((rank, -float(d["ALAND_SQMI"]), d["GEOID"], d))
    for d in rows("cousubs"):
        if d["NAME"].endswith("CCD") or "not defined" in d["NAME"]:
            continue
        cands.append((2, -float(d["ALAND_SQMI"]), d["GEOID"], d))
    best: dict[tuple[str, str], dict] = {}
    for _, _, _, d in sorted(cands, key=lambda c: c[:3]):
        best.setdefault((d["USPS"], place_key(d["NAME"])), d)
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["state", "name_key", "name", "latitude", "longitude"])
        for (state, key), d in sorted(best.items()):
            lat, lng = float(d["INTPTLAT"]), float(d["INTPTLONG"])
            if not key or not (24 <= lat <= 50 and -125 <= lng <= -66):  # contiguous US only
                continue
            w.writerow([state, key, display_name(d["NAME"]), d["INTPTLAT"], d["INTPTLONG"]])
    print(f"wrote {sum(1 for _ in OUT.open(encoding='utf-8')) - 1} places to {OUT}")


if __name__ == "__main__":
    main()
