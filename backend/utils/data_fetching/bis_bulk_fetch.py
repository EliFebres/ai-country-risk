"""BIS bulk statistics: monthly policy rates and USD exchange rates.

The BIS publishes its full datasets as no-auth zipped CSVs. Two of them cover
gaps nothing else in this project fills:

* **WS_CBPOL** — central bank policy rates, which turn measured inflation into a
  *real* policy rate.
* **WS_XRU** — US dollar exchange rates, which give the monthly series FX
  volatility is computed from. The IMF's SDMX ``ER`` dataflow returns no series
  for the roster's country keys, so this is the working source rather than a
  preference.

Both are read with the standard library — ``zipfile`` and ``csv`` — over a
``requests`` download, so this adds no dependency. The "flat" variants are used
on purpose: they are long-format (one row per observation) where the "col"
variants put every period in its own column, mixing daily and monthly headers in
one row and making the parse far easier to get subtly wrong.

Each file is a whole-dataset download covering every country, so a run fetches
once and slices per country rather than making one request per country.

Every failure degrades to no rows with a logged warning: BIS being unreachable
must cost the run its policy rates, not its scores.
"""

import csv
import datetime as _dt
import io
import logging
import re
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from backend.utils import constants
from backend.utils.data_upsert import data_push

logger = logging.getLogger(__name__)

_BULK_ROOT = "https://data.bis.org/static/bulk"
_HEADERS = {"User-Agent": "AI-Country-Risk-Dashboard/1.0 (+bis-bulk)"}
_TIMEOUT = 300  # seconds; WS_XRU is ~10 MB

# BIS flat-CSV columns carry a "CODE:Label" header form.
_COL_FREQ = "FREQ:Frequency"
_COL_AREA = "REF_AREA:Reference area"
_COL_PERIOD = "TIME_PERIOD:Time period or range"
_COL_VALUE = "OBS_VALUE:Observation Value"
_COL_CURRENCY = "CURRENCY:Currency"
_COL_COLLECTION = "COLLECTION:Collection"

_MONTHLY_PERIOD = re.compile(r"^\d{4}-\d{2}$")

# Only the recent tail matters for the daily run: 24 months of returns for FX
# volatility, and a current level for the policy rate. Keeping five years bounds
# the write while leaving room for a longer window later.
#
# That longer window is now the History Machine's, and it is a parameter rather
# than a bigger default: the backfill wants a decade, the daily run wants a
# small write, and the flat CSV holds the whole history either way.
_KEEP_MONTHS = 60

# A series whose newest observation is older than this is discontinued, not
# stale, and is dropped entirely.
#
# This is not hypothetical: BIS still publishes Portugal's national policy rate,
# which ends in 1998-12 because Portugal joined the euro. Storing it would put a
# 3.25% escudo-era rate in the payload and hand `real_policy_rate` a number that
# looks current and means nothing. The payload's staleness stamp would say
# 10,000 days, but a derived metric computed from it carries no such warning.
#
# Two years is comfortably longer than any real publication lag on a monthly
# series and far shorter than any discontinuation.
_MAX_STALE_MONTHS = 24

_DATASETS = {
    "BIS.POLICY.RATE": {"file": "WS_CBPOL_csv_flat.zip", "source": "BIS CBPOL"},
    "BIS.FX.USD": {"file": "WS_XRU_csv_flat.zip", "source": "BIS XRU"},
}


def _leading_code(cell: Optional[str]) -> str:
    """Extract the code from a BIS ``'M: Monthly'`` style cell.

    BIS flat CSVs put code and label in one field separated by a colon. Only the
    code half is stable enough to match on.
    """
    if not cell:
        return ""
    return cell.split(":", 1)[0].strip()


def download_flat_csv(filename: str) -> List[Dict[str, str]]:
    """Download and unzip one BIS bulk flat CSV.

    Args:
        filename: the zip's name under the BIS bulk root, e.g.
            ``'WS_CBPOL_csv_flat.zip'``.

    Returns:
        One dict per data row, keyed by the CSV's own header names. Empty on any
        network, zip, or decode failure — all logged.
    """
    url = f"{_BULK_ROOT}/{filename}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[bis] %s -> HTTP %s", filename, resp.status_code)
            return []
        archive = zipfile.ZipFile(io.BytesIO(resp.content))
        member = archive.namelist()[0]
        with archive.open(member) as handle:
            # BIS ships these with a BOM and non-ASCII footnote text; replace
            # rather than raise, since a mangled footnote is irrelevant and the
            # numeric columns are plain ASCII.
            text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace")
            return list(csv.DictReader(text))
    except Exception as exc:  # noqa: BLE001 - any failure degrades to no rows
        logger.warning("[bis] %s failed: %s", filename, exc)
        return []


def _monthly_rows_by_area(rows: Iterable[Dict[str, str]]) -> Dict[str, List[Tuple[str, float, str, str]]]:
    """Group a flat CSV's monthly observations by reference area.

    Returns:
        ``{iso2: [(period, value, currency, collection), ...]}`` unsorted. Rows
        that are not monthly, not numeric, or not a ``YYYY-MM`` period are
        dropped — BIS mixes daily and monthly series in one file.
    """
    grouped: Dict[str, List[Tuple[str, float, str, str]]] = {}
    for row in rows:
        if _leading_code(row.get(_COL_FREQ)) != "M":
            continue
        period = (row.get(_COL_PERIOD) or "").strip()
        if not _MONTHLY_PERIOD.match(period):
            continue
        area = _leading_code(row.get(_COL_AREA))
        if not area:
            continue
        raw = (row.get(_COL_VALUE) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        grouped.setdefault(area, []).append((
            period, value,
            _leading_code(row.get(_COL_CURRENCY)),
            _leading_code(row.get(_COL_COLLECTION)),
        ))
    return grouped


def _newest_variant(observations: List[Tuple[str, float, str, str]]) -> List[Tuple[str, float]]:
    """Pick one coherent series when a country has several.

    A country can appear under more than one (currency, collection) pair — an
    escudo series alongside a euro one, a period-average alongside an
    end-of-period. Mixing them would produce a volatility measuring the switch
    rather than the currency, so the variant carrying the newest observation
    wins and the rest are dropped.

    Returns:
        ``[(period, value), ...]`` ascending, for the winning variant only.
    """
    by_variant: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
    for period, value, currency, collection in observations:
        by_variant.setdefault((currency, collection), []).append((period, value))
    if not by_variant:
        return []

    winner = max(by_variant.values(), key=lambda obs: max(p for p, _ in obs))
    winner.sort(key=lambda pair: pair[0])
    return winner


def _cutoff_period(as_of: _dt.date) -> str:
    """The oldest ``YYYY-MM`` a series may end on and still count as live."""
    months = as_of.year * 12 + (as_of.month - 1) - _MAX_STALE_MONTHS
    return f"{months // 12:04d}-{months % 12 + 1:02d}"


def fetch_dataset_rows(code: str, *, as_of: Optional[_dt.date] = None,
                       keep_months: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch one BIS dataset as ``indicator_series`` rows for the whole roster.

    Args:
        code: registry id, either ``'BIS.POLICY.RATE'`` or ``'BIS.FX.USD'``.
        as_of: date to stamp values with. Defaults to today.
        keep_months: how much history to keep per country. Defaults to
            :data:`_KEEP_MONTHS`; the historical backfill passes a decade. The
            download is the same either way — this only bounds what is written.

    Returns:
        Rows ready for ``data_push.upsert_indicator_series``, restricted to the
        roster and to the most recent ``keep_months`` observations per country.
        Empty if the download failed or the code is unknown.
    """
    spec = _DATASETS.get(code)
    if not spec:
        logger.warning("[bis] unknown dataset code %r", code)
        return []

    stamp = as_of or _dt.date.today()
    grouped = _monthly_rows_by_area(download_flat_csv(str(spec["file"])))
    if not grouped:
        return []

    cutoff = _cutoff_period(stamp)
    roster = {c["iso2"] for c in constants.COUNTRY_ROSTER}
    rows: List[Dict[str, Any]] = []
    discontinued: List[str] = []

    for iso2 in sorted(roster & set(grouped)):
        series = _newest_variant(grouped[iso2])
        if not series or series[-1][0] < cutoff:
            discontinued.append(f"{iso2}@{series[-1][0]}" if series else iso2)
            continue
        for period, value in series[-(keep_months or _KEEP_MONTHS):]:
            rows.append({
                "country_iso2": iso2,
                "indicator_code": code,
                "freq": "M",
                "period": period,
                "value": value,
                "as_of": stamp,
                "source": str(spec["source"]),
            })

    # Both absences are real and worth naming rather than silently dropping:
    # euro-area members share the ECB rate and have no national series at all,
    # and several more have one that stopped when they joined.
    missing = sorted(roster - set(grouped))
    if missing:
        logger.info("[bis] %s: no series for %d rostered countries: %s",
                    code, len(missing), ",".join(missing))
    if discontinued:
        logger.info("[bis] %s: dropped %d discontinued series (last obs before %s): %s",
                    code, len(discontinued), cutoff, ",".join(discontinued))
    return rows


def refresh_bis_series() -> None:
    """Refresh both BIS datasets into ``indicator_series``.

    Each dataset is downloaded once for the whole roster. A failure in one
    dataset does not stop the other.
    """
    stamp = _dt.date.today()
    for code in _DATASETS:
        try:
            rows = fetch_dataset_rows(code, as_of=stamp)
            if rows:
                data_push.upsert_indicator_series(rows)
            logger.info("[bis] %s: %d observations", code, len(rows))
        except Exception:
            logger.exception("[bis] %s ERROR", code)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    for _code in _DATASETS:
        _rows = fetch_dataset_rows(_code)
        _countries = sorted({r["country_iso2"] for r in _rows})
        print(f"{_code}: {len(_rows)} rows, {len(_countries)} countries")
        for _r in _rows[-3:]:
            print("   ", _r)
