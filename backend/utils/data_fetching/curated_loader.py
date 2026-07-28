"""Loader for the hand-maintained files in ``backend/data/curated``.

Some of what the three ledgers need has no free, stable, no-auth API: statutory
tax rates, press freedom scores, currency regimes, WEO forecast vintages. Those
arrive as files an operator downloads and drops into ``backend/data/curated``,
documented file by file in the README there.

The loader's contract is three rules, and the asymmetry between them is the
point:

* **Absent files are silent.** Every curated file is expected to be missing
  until someone fills it. Warning on each one every run would train the operator
  to ignore the log.
* **Malformed files are loud** — they raise. A file that is *present* is a file
  someone meant to be used, so a wrong column or an unparseable number is a
  mistake to surface immediately, not to degrade into silently-missing evidence
  that looks exactly like the absent case.
* **Header-only files are neither.** They load zero rows and are not an error;
  that is the shipped state of every template.

Raising is safe because the pipeline calls this behind a ``try``: the operator
sees the failure in the log and the run still scores. Loud at the source,
isolated at the boundary.

Numeric series go to ``indicator_series`` under their registry ids. The two
lookup files (currency regimes, election dates) are not numeric series and are
read directly by the payload builder.
"""

import csv
import datetime as _dt
import logging
import pathlib
import re
from typing import Any, Dict, List, Optional

import yaml

from backend.utils import constants

logger = logging.getLogger(__name__)

CURATED_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "curated"
WEO_DIR = CURATED_DIR / "weo_vintages"

# The common-shape CSVs: filename -> (registry id, frequency).
# Adding a source is one line here plus one entry in INDICATOR_REGISTRY.
_CURATED_SERIES: Dict[str, tuple] = {
    "statutory_rates.csv":            ("STAT.TAX.TOP.RATE", "A"),
    "press_freedom_rsf.csv":          ("RSF.PRESS.SCORE", "A"),
    "informal_economy.csv":           ("INFORMAL.PCT.GDP", "A"),
    "open_budget_survey.csv":         ("OBS.SCORE", "A"),
    "un_egdi.csv":                    ("UN.EGDI", "A"),
    "oecd_tax_wedge.csv":             ("OECD.TAX.WEDGE", "A"),
    "unwpp_old_age_projection.csv":   ("UNWPP.DPND.OL.PROJ", "A"),
    "wui_quarterly.csv":              ("WUI.INDEX", "Q"),
    "policy_rates.csv":               ("BIS.POLICY.RATE", "M"),
    "reserves_monthly.csv":           ("RESERVES.USD", "M"),
}

_COMMON_COLUMNS = ["country_iso2", "period", "value"]
_WEO_COLUMNS = ["country_iso2", "target_year", "value"]

_PERIOD_PATTERNS = {
    "A": re.compile(r"^\d{4}$"),
    "Q": re.compile(r"^\d{4}Q[1-4]$"),
    "M": re.compile(r"^\d{4}-(0[1-9]|1[0-2])$"),
}

_WEO_FILENAME = re.compile(r"^weo_(\d{4})(\d{2})\.csv$")


class CuratedFileError(ValueError):
    """A curated file is present but cannot be trusted.

    Carries the path and the specific problem so the operator can fix the file
    without reading the loader.
    """


def _roster_iso2() -> set:
    """The ISO-2 codes a curated row may refer to."""
    return {c["iso2"] for c in constants.COUNTRY_ROSTER}


def _file_as_of(path: pathlib.Path) -> _dt.date:
    """When we learned a curated file's contents: its modification time.

    A hand-dropped file has no publication date we can read, and its mtime is
    the honest answer to "when did this become known to us" — which is exactly
    what ``indicator_series.as_of`` means. The payload turns it into the
    staleness the model sees.
    """
    return _dt.date.fromtimestamp(path.stat().st_mtime)


def _read_csv(path: pathlib.Path, expected: List[str]) -> List[Dict[str, str]]:
    """Read a curated CSV, validating its header.

    Raises:
        CuratedFileError: if the file is unreadable or its columns are not
            exactly ``expected``, in that order.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if [h.strip() for h in header] != expected:
                raise CuratedFileError(
                    f"{path.name}: expected columns {expected}, got {header}. "
                    f"See backend/data/curated/README.md."
                )
            return list(reader)
    except CuratedFileError:
        raise
    except OSError as exc:
        raise CuratedFileError(f"{path.name}: cannot read ({exc})") from exc


def _parse_common_rows(
    path: pathlib.Path,
    rows: List[Dict[str, str]],
    *,
    code: str,
    freq: str,
    source: str,
    as_of: _dt.date,
) -> List[Dict[str, Any]]:
    """Validate and convert common-shape rows into ``indicator_series`` rows.

    Off-roster countries are skipped with a logged count — a published dataset
    covering 190 countries is not malformed for being wider than this project.
    Everything else wrong raises.

    Raises:
        CuratedFileError: on a bad period format or an unparseable value.
    """
    roster = _roster_iso2()
    pattern = _PERIOD_PATTERNS[freq]
    out: List[Dict[str, Any]] = []
    off_roster = 0

    for line_no, row in enumerate(rows, start=2):  # start=2: line 1 is the header
        iso2 = (row.get("country_iso2") or "").strip().upper()
        period = (row.get("period") or "").strip()
        raw = (row.get("value") or "").strip()

        if not iso2 and not period and not raw:
            continue  # a trailing blank line is not an error
        if iso2 not in roster:
            off_roster += 1
            continue
        if not pattern.match(period):
            raise CuratedFileError(
                f"{path.name} line {line_no}: period {period!r} is not valid for "
                f"frequency {freq!r} (expected {pattern.pattern})."
            )

        value: Optional[float] = None
        if raw:
            try:
                value = float(raw)
            except ValueError as exc:
                raise CuratedFileError(
                    f"{path.name} line {line_no}: value {raw!r} is not a number."
                ) from exc

        out.append({
            "country_iso2": iso2,
            "indicator_code": code,
            "freq": freq,
            "period": period,
            "value": value,          # blank stays NULL: "reported unavailable"
            "as_of": as_of,
            "source": source,
        })

    if off_roster:
        logger.info("[curated] %s: skipped %d off-roster row(s)", path.name, off_roster)
    return out


def load_curated_series(directory: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    """Load every common-shape curated CSV into ``indicator_series`` rows.

    Args:
        directory: where to look. Defaults to :data:`CURATED_DIR`; overridable
            so tests can point at a fixture folder.

    Returns:
        Rows ready for ``data_push.upsert_indicator_series``, across all files
        present. Empty when every file is absent or header-only — the shipped
        state.

    Raises:
        CuratedFileError: if a file that exists is malformed.
    """
    root = directory or CURATED_DIR
    rows: List[Dict[str, Any]] = []

    for filename, (code, freq) in _CURATED_SERIES.items():
        path = root / filename
        if not path.exists():
            continue  # expected until filled; silence is the contract

        spec = constants.INDICATOR_REGISTRY.get(code)
        source = str(spec["source"]) if spec else f"curated:{filename}"
        parsed = _parse_common_rows(
            path, _read_csv(path, _COMMON_COLUMNS),
            code=code, freq=freq, source=source, as_of=_file_as_of(path),
        )
        if parsed:
            logger.info("[curated] %s: %d row(s)", filename, len(parsed))
        rows.extend(parsed)

    return rows


def _load_yaml(path: pathlib.Path) -> Optional[Dict]:
    """Read a curated YAML file, or None if it is absent.

    Raises:
        CuratedFileError: if the file exists but is not a YAML mapping.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise CuratedFileError(f"{path.name}: cannot parse ({exc})") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise CuratedFileError(f"{path.name}: expected a YAML mapping, got {type(loaded).__name__}.")
    return loaded


def load_fx_regimes(directory: Optional[pathlib.Path] = None) -> Dict[str, str]:
    """Load the currency regime lookup.

    Returns:
        ``{iso2: regime}`` where regime is ``peg``, ``managed`` or ``float``.
        Empty when the file is absent or has no entries, in which case
        ``metrics.suppressed_vol_flag`` returns None for everyone — "we have no
        regime file" rather than "everything floats".

    Raises:
        CuratedFileError: if the file exists but is malformed, or names a regime
            outside the documented three.
    """
    root = directory or CURATED_DIR
    path = root / "fx_regimes.yaml"
    loaded = _load_yaml(path)
    if loaded is None:
        return {}

    regimes = loaded.get("regimes") or {}
    if not isinstance(regimes, dict):
        raise CuratedFileError(f"{path.name}: `regimes` must be a mapping of iso2 -> regime.")

    valid = {"peg", "managed", "float"}
    out: Dict[str, str] = {}
    for iso2, regime in regimes.items():
        normalized = str(regime).strip().lower()
        if normalized not in valid:
            raise CuratedFileError(
                f"{path.name}: {iso2} has regime {regime!r}; expected one of {sorted(valid)}."
            )
        out[str(iso2).strip().upper()] = normalized
    return out


def load_election_calendar(directory: Optional[pathlib.Path] = None) -> Dict[str, List[Dict[str, str]]]:
    """Load the scheduled-election lookup.

    Returns:
        ``{iso2: [{"date": "YYYY-MM-DD", "kind": str}, ...]}`` sorted by date.
        Empty when the file is absent or has no entries.

    Raises:
        CuratedFileError: if the file exists but is malformed, or holds an
            unparseable date.
    """
    root = directory or CURATED_DIR
    path = root / "election_calendar.yaml"
    loaded = _load_yaml(path)
    if loaded is None:
        return {}

    elections = loaded.get("elections") or {}
    if not isinstance(elections, dict):
        raise CuratedFileError(f"{path.name}: `elections` must be a mapping of iso2 -> list.")

    out: Dict[str, List[Dict[str, str]]] = {}
    for iso2, entries in elections.items():
        if not isinstance(entries, list):
            raise CuratedFileError(f"{path.name}: {iso2} must map to a list of elections.")
        parsed: List[Dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or "date" not in entry:
                raise CuratedFileError(f"{path.name}: {iso2} has an entry without a `date`.")
            try:
                when = _dt.date.fromisoformat(str(entry["date"])[:10])
            except ValueError as exc:
                raise CuratedFileError(
                    f"{path.name}: {iso2} has unparseable date {entry['date']!r}."
                ) from exc
            parsed.append({"date": when.isoformat(), "kind": str(entry.get("kind") or "unspecified")})
        parsed.sort(key=lambda e: e["date"])
        out[str(iso2).strip().upper()] = parsed
    return out


def load_reference_constants(directory: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Load the frozen reference scalars.

    Returns:
        The parsed mapping, or ``{}`` when the file is absent. The only key in
        use is ``rome_reference_ratio``, which is null until computed once from a
        filled ``statutory_rates.csv``.

    Raises:
        CuratedFileError: if the file exists but is not a YAML mapping, or the
            ratio is present and not a number.
    """
    root = directory or CURATED_DIR
    loaded = _load_yaml(root / "reference_constants.yaml")
    if loaded is None:
        return {}

    ratio = loaded.get("rome_reference_ratio")
    if ratio is not None and not isinstance(ratio, (int, float)):
        raise CuratedFileError(
            f"reference_constants.yaml: rome_reference_ratio must be a number or null, "
            f"got {ratio!r}."
        )
    return loaded


def load_weo_revisions(directory: Optional[pathlib.Path] = None) -> Dict[str, List[float]]:
    """Load WEO vintages and difference them into per-country revisions.

    Vintages are ordered by the date in their filename, then consecutive
    vintages' forecasts of the *same* target year are differenced. The result is
    what ``metrics.forecast_instability`` averages.

    Args:
        directory: where to look. Defaults to :data:`WEO_DIR`.

    Returns:
        ``{iso2: [revision, ...]}`` across all target years, in no particular
        order — the consumer takes a mean of absolute values. Empty when fewer
        than two vintages are present, since one vintage has nothing to revise.

    Raises:
        CuratedFileError: if a vintage file is malformed.
    """
    root = directory or WEO_DIR
    if not root.exists():
        return {}

    vintages: List[tuple] = []
    for path in sorted(root.glob("weo_*.csv")):
        match = _WEO_FILENAME.match(path.name)
        if not match:
            raise CuratedFileError(
                f"{path.name}: WEO vintage files must be named weo_<YYYY><MM>.csv — "
                f"the vintage order comes from the filename."
            )
        vintages.append((match.group(1) + match.group(2), path))

    if len(vintages) < 2:
        return {}

    roster = _roster_iso2()
    # {(iso2, target_year): {vintage_tag: value}}
    forecasts: Dict[tuple, Dict[str, float]] = {}
    for tag, path in vintages:
        for line_no, row in enumerate(_read_csv(path, _WEO_COLUMNS), start=2):
            iso2 = (row.get("country_iso2") or "").strip().upper()
            target = (row.get("target_year") or "").strip()
            raw = (row.get("value") or "").strip()
            if not iso2 and not target and not raw:
                continue
            if iso2 not in roster:
                continue
            if not _PERIOD_PATTERNS["A"].match(target):
                raise CuratedFileError(
                    f"{path.name} line {line_no}: target_year {target!r} is not a YYYY year."
                )
            if not raw:
                continue
            try:
                forecasts.setdefault((iso2, target), {})[tag] = float(raw)
            except ValueError as exc:
                raise CuratedFileError(
                    f"{path.name} line {line_no}: value {raw!r} is not a number."
                ) from exc

    out: Dict[str, List[float]] = {}
    ordered_tags = [tag for tag, _ in vintages]
    for (iso2, _target), by_vintage in forecasts.items():
        present = [by_vintage[tag] for tag in ordered_tags if tag in by_vintage]
        for earlier, later in zip(present, present[1:]):
            out.setdefault(iso2, []).append(round(later - earlier, 4))
    return out


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    print("series rows:      ", len(load_curated_series()))
    print("fx regimes:       ", load_fx_regimes())
    print("elections:        ", load_election_calendar())
    print("reference consts: ", load_reference_constants())
    print("weo revisions:    ", load_weo_revisions())
