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
#
# The target has to be a key of `constants.INDICATOR_REGISTRY`. The builder
# resolves registry codes and nothing else, so a plausible-looking World Bank
# code that is not in the registry loads cleanly and is never read — which is
# how this table spent its whole life mapping all five subjects onto codes no
# ledger requests. Check the registry before adding a row here.
SUBJECTS: Dict[str, str] = {
    "PCPIPCH": "CPI.YOY",                 # inflation, average consumer prices, %
    # The four that got their own registry entries rather than being pointed at
    # a World Bank code that nearly means the same thing. `WEO.`-prefixed so the
    # source is unambiguous: these are edition-vintaged and the World Bank's
    # versions are not, and quietly merging the two would throw away the
    # revision history that is the whole reason for loading editions.
    "NGDP_RPCH": "WEO.NGDP_RPCH",         # real GDP growth, % (aggregate)
    "GGXWDG_NGDP": "WEO.GGXWDG_NGDP",     # general government gross debt, % of GDP
    "GGXCNL_NGDP": "WEO.GGXCNL_NGDP",     # general government net lending, % of GDP
    "BCA_NGDPD": "WEO.BCA_NGDPD",         # current account balance, % of GDP
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
        except (UnicodeDecodeError, UnicodeError, csv.Error):
            # csv.Error belongs here and its absence cost the newer half of the
            # archive. Decoding a single-byte edition as UTF-16 does not raise:
            # it succeeds and yields characters that contain no newline at all,
            # so csv sees one field of eight million characters and raises
            # "field larger than field limit" — from the csv module, not the
            # codec. That escaped this loop, so the file never reached latin-1,
            # and every edition the IMF ships in a single-byte encoding was
            # reported as unreadable. The UTF-16 ones parsed on the first try
            # and hid it.
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


def _estimates_start_after(row: Dict[str, str],
                           vintage: datetime.date) -> int:
    """The last year this row states as fact, from the file's own column.

    Every edition ships ``Estimates Start After`` and it is per row — per country
    per subject — because the IMF does not have actuals for every country at the
    same date. It is the only authoritative answer to "is this cell an
    observation or a forecast", and it was not being read.

    Falls back to the year before the edition, loudly. Every edition checked
    (2016-04, 2020-10, 2025-04) states exactly that for every roster country, so
    the fallback matches observed reality — but a silent fallback is how the
    original bug survived, so this one says something.
    """
    raw = (row.get("Estimates Start After") or "").strip()
    if raw.isdigit():
        return int(raw)
    logger.warning("[weo] %s %s: no readable 'Estimates Start After' (%r); "
                   "falling back to %d", row.get("ISO"),
                   row.get("WEO Subject Code"), raw, vintage.year - 1)
    return vintage.year - 1


def read_edition(path: pathlib.Path, roster: List[str]) -> List[Dict[str, Any]]:
    """One WEO edition as ``indicator_series`` rows.

    Only *historical* columns are read. The WEO's forward columns are
    projections, and a projection of 2020 published in 2018 is not a fact about
    2020; loading them would put the IMF's forecast into the evidence payload as
    though it were an observation.

    Where the history stops is the file's own ``Estimates Start After``, per
    row, not the edition's year. That distinction is the whole of this function's
    correctness and it was wrong in the obvious direction: an edition published
    in April of year Y does not have Y's actuals — the year has not happened.
    Every edition therefore carried exactly one forecast year loaded as fact,
    and `2020-10` says so out loud, with TUR NGDP_RPCH estimated after 2019
    while the old rule admitted 2020.

    It mattered less than it looks, because ``_resolve`` takes the newest vintage
    not after the anchor and by then a later edition usually supplies the actual.
    It matters exactly when the later edition is missing, which is the tail of
    the archive — the anchors nearest today.

    Args:
        path: an edition file named ``YYYY-MM.xls``.
        roster: ISO2 codes to keep.

    Returns:
        Rows for ``data_push.upsert_indicator_series``, stamped with the
        edition date as ``as_of``. Projections are dropped rather than marked:
        nothing downstream can carry a marker today, and a forecast the payload
        cannot label is a forecast the model reads as an observation.
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
        last_actual = _estimates_start_after(row, vintage)
        for column, raw in row.items():
            if not column or not column.strip().isdigit():
                continue
            year = int(column.strip())
            if year > last_actual:
                continue                     # a projection, not an observation
            value = _number(raw)
            if value is None:
                continue
            out.append({
                "country_iso2": iso2,
                "indicator_code": SUBJECTS[subject],
                "freq": "A",
                # A bare year, because that is what every other annual source
                # writes and what `data_retrieval._period_to_date` parses. Dated
                # periods ("2017-12-31") round-trip through the database fine and
                # are then dropped by the payload builder, which is worse than
                # failing: the editions load, the row counts look right, and not
                # one WEO value ever reaches a score.
                "period": str(year),
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
    # The whole roster, not the pilot five. These series now carry four of the
    # registry's indicators, so a country missing from here is a country scored
    # without its debt ratio, live as well as historically. The files hold every
    # country in the world; restricting the load saved nothing.
    roster = roster or [entry["iso2"] for entry in constants.COUNTRY_ROSTER]
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
