"""Pure place-name normalisation (no Django) shared by the model layer, importer and build script."""

import re

_SUFFIX = re.compile(
    r"\s+(city and borough|city|town|village|CDP|borough|municipality|urban county|township|"
    r"plantation|\(balance\))$",
    re.I,
)


def normalize_place(name: str) -> str:
    """Canonical lookup key for a place name (same rule used to build data/us_places.csv)."""
    s = name.strip().lower()
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bmount\b", "mt", s)
    s = re.sub(r"\bfort\b", "ft", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def display_name(name: str) -> str:
    return _SUFFIX.sub("", name.strip())


def place_key(name: str) -> str:
    """Key stored in Place.name_key: designator ("city", "CDP", ...) stripped, then normalized."""
    return normalize_place(display_name(name))


def lookup_keys(name: str) -> list[str]:
    """Candidate Place.name_key values for a user/CSV city name, best match first.

    The raw form first ("Denver City" is a town whose stored key keeps "city"), then the
    designator-stripped form, then space-less variants ("De Kalb" vs "DeKalb").
    """
    full, stripped = normalize_place(name), place_key(name)
    keys = [full, stripped, full.replace(" ", ""), stripped.replace(" ", "")]
    return list(dict.fromkeys(k for k in keys if k))
