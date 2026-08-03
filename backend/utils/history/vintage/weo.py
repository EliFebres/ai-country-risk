"""Per-edition IMF World Economic Outlook, so a 2018 score sees 2018's numbers.

The macro half of the no-future rule, and the quieter half. A future-dated
article is obvious once anybody looks; a *revised* GDP figure looks exactly like
an unrevised one. The IMF publishes its estimate of 2017 growth in April 2018,
revises it in October 2018, and revises it again for years afterwards. Scoring
April 2018 on today's number is scoring on eight years of hindsight, and nothing
in the output would ever show it.

Each WEO edition is therefore loaded as its own vintage, stamped with the
edition's own date, and ``data_retrieval._resolve`` picks the newest vintage not
after the snapshot's anchor.

**The file format is not a spreadsheet.** The IMF ships `WEOOct2018all.xls`, and
it is a tab-delimited text file with an .xls extension — opening it in a
spreadsheet program shows a format warning for exactly this reason. That is
convenient here: no xlsx parser is pinned in this project, and this reads with
the standard library's ``csv`` module. Encoding varies by edition (older ones
are UTF-16, newer ones Latin-1), so both are tried.

Eli drops the editions into ``backend/data/curated/weo_vintages/``; see the
README there for the download URLs and the naming rule.
"""

import csv
import datetime
import logging
import pathlib
import re
from typing import Any, Dict, Iterator, List, Optional

from backend.utils import constants

logger = logging.getLogger(__name__)

VINTAGE_DIR = (pathlib.Path(__file__).resolve().parents[4]
               / "backend" / "data" / "curated" / "weo_vintages")

# WEO subject codes worth carrying, mapped onto this project's indicator codes.
# Deliberately short: the panel already holds the World Bank's version of most
# series, and what the vintages are for is the handful where the *revision* is
# the story rather than the level.
SUBJECTS: Dict[str, str] = {
    "NGDP_RPCH": "NY.GDP.MKTP.KD.ZG",     # real GDP growth, %
    "PCPIPCH": "FP.CPI.TOTL.ZG",          # inflation, average consumer prices, %
    "GGXWDG_NGDP": "GC.DOD.TOTL.GD.ZS",   # general government gross debt, % of GDP
    "GGXCNL_NGDP": "GC.NLD.TOTL.GD.ZS",   # general government net lending, % of GDP
    "BCA_NGDPD": "BN.CAB.XOKA.GD.ZS",     # current account balance, % of GDP
}

# "2018-04.xls" / "2018-10.xls" — the edition, not the file's own mtime.
_EDITION_RE = re.compile(r"^(\d{4})-(\d{2})")

# Editions publish in April and October. The date stamped on every row is the
# first of the publication month: it is what a reader could have known, and
# being a few days early is safer than a few days late.
_ENCODINGS = ("utf-16", "utf-16-le", "latin-1")


def edition_date(path: pathlib.Path) -> Optional[datetime.date]:
    """The vintage date a file's name declares, or None if it does not."""
    match = _EDITION_RE.match(path.stem)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return datetime.date(year, month, 1)


def _rows(path: pathlib.Path) -> Iterator[Dict[str, str]]:
    """Every row of one edition, whichever encoding it happens to use.

    Falls through the encodings rather than guessing from the filename: the IMF
    changed format mid-decade and the change is not announced anywhere in the
    name.
    """
    for encoding in _ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                if not reader.fieldnames or "WEO Subject Code" not in reader.fieldnames:
                    continue
                yield from reader
                return
        except (UnicodeDecodeError, UnicodeError):
            continue
    logger.warning("[weo] %s: no encoding produced a WEO table; skipping", path.name)


def _number(raw: str) -> Optional[float]:
    """One WEO cell as a float, or None.

    The file uses "n/a" and "--" for absent, and thousands separators for large
    values. Absent is absent — never zero, never carried forward.
    """
    text = (raw or "").strip().replace(",", "")
    if not text or text in ("n/a", "--", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_edition(path: pathlib.Path, roster: List[str]) -> List[Dict[str, Any]]:
    """One WEO edition as ``indicator_series`` rows.

    Only *historical* columns are read — years up to and including the edition's
    own year. The WEO's forward columns are projections, and a projection of
    2020 published in 2018 is not a fact about 2020; loading them would put the
    IMF's forecast into the evidence payload as though it were an observation.

    Args:
        path: an edition file named ``YYYY-MM.xls``.
        roster: ISO2 codes to keep.

    Returns:
        Rows for ``data_push.upsert_indicator_series``, stamped with the
        edition date as ``as_of``.
    """
    vintage = edition_date(path)
    if vintage is None:
        logger.warning("[weo] %s: name is not YYYY-MM; skipping", path.name)
        return []

    iso3_to_iso2 = {v: k for k, v in constants.ISO3_BY_ISO2.items()}
    wanted = {iso3 for iso3, iso2 in iso3_to_iso2.items() if iso2 in roster}
    out: List[Dict[str, Any]] = []

    for row in _rows(path):
        iso3 = (row.get("ISO") or "").strip()
        subject = (row.get("WEO Subject Code") or "").strip()
        if iso3 not in wanted or subject not in SUBJECTS:
            continue
        iso2 = iso3_to_iso2[iso3]
        for column, raw in row.items():
            if not column or not column.strip().isdigit():
                continue
            year = int(column.strip())
            if year > vintage.year:
                continue                     # a projection, not an observation
            value = _number(raw)
            if value is None:
                continue
            out.append({
                "country_iso2": iso2,
                "indicator_code": SUBJECTS[subject],
                "freq": "A",
                "period": datetime.date(year, 12, 31),
                "value": value,
                "as_of": vintage,
                "source": f"IMF WEO {vintage:%Y-%m}",
                "vintage_scheme": "as-published-edition",
            })

    logger.info("[weo] %s: %d row(s) for %d country/ies", path.name, len(out), len(roster))
    return out


def load_all(roster: Optional[List[str]] = None,
             directory: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    """Every edition in the vintage directory, oldest first.

    An empty directory returns an empty list with a loud log rather than
    raising: the pilot can run without vintages, it just runs on
    as-published-latest macro and has to say so in its stamps.
    """
    from backend.utils.history import config
    roster = roster or config.PILOT_ROSTER
    directory = directory or VINTAGE_DIR

    if not directory.exists():
        logger.warning("[weo] %s does not exist — no macro vintages will be loaded, "
                       "and every historical payload will use as-published-latest "
                       "annual data. See the README in that folder.", directory)
        return []

    files = sorted(p for p in directory.iterdir()
                   if p.is_file() and _EDITION_RE.match(p.stem))
    if not files:
        logger.warning("[weo] no YYYY-MM.* files in %s", directory)
        return []

    rows: List[Dict[str, Any]] = []
    for path in files:
        try:
            rows += read_edition(path, roster)
        except Exception:  # noqa: BLE001
            # One malformed edition costs its own vintage, not the other twenty.
            logger.exception("[weo] %s failed; continuing", path.name)
    return rows
