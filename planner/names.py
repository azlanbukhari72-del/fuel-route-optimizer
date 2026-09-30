"""Pure place-name normalisation (no Django), shared by importer, lookups and build script."""

import re

_SUFFIX = re.compile(
    r"\s+(city and borough|city|town|village|CDP|borough|municipality|urban county|township|"
    r"plantation)$",
    re.I,
)
_BALANCE = re.compile(r"\s*\(balance\)$", re.I)
_CONSOLIDATED = re.compile(
    r"\(balance\)|metropolitan government|metro government|consolidated government|"
    r"unified government|urban county",
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
    """Drop a trailing "(balance)" then ONE designator: "Indianapolis city (balance)" ->
    "Indianapolis", but "Kansas City city" -> "Kansas City" (never strip a real name part)."""
    return _SUFFIX.sub("", _BALANCE.sub("", name.strip()))


def place_key(name: str) -> str:
    """Key stored in Place.name_key: designator ("city", "CDP", ...) stripped, then normalized."""
    return normalize_place(display_name(name))


def alias_names(raw: str) -> list[str]:
    """Extra lookup names for consolidated governments: "Lexington-Fayette" -> "Lexington"."""
    if not _CONSOLIDATED.search(raw):
        return []
    base = _CONSOLIDATED.sub("", raw).strip()
    first = re.split(r"[-/]", display_name(base))[0].strip()
    return [first] if first and first != display_name(raw) else []


def lookup_keys(name: str) -> list[str]:
    """Candidate Place.name_key values for a user/CSV city name, best match first.

    The raw form first ("Denver City" is a town whose stored key keeps "city"), then the
    designator-stripped form, then space-less variants ("De Kalb" vs "DeKalb"), and finally
    "<name> city" ("Boise" -> "Boise City").
    """
    full, stripped = normalize_place(name), place_key(name)
    keys = [full, stripped, full.replace(" ", ""), stripped.replace(" ", ""), f"{stripped} city"]
    return list(dict.fromkeys(k for k in keys if k))
