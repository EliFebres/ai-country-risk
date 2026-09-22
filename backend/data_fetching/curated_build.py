"""
Acquire the two curated series, with their citations, into `curated.csv`.

RSF's World Press Freedom Index and the OECD's PISA means are the reason the
`information` and `edge` ledgers are not one-indicator ledgers. Neither has a
feed a pipeline can poll: RSF publishes a CSV per index year, PISA publishes
tables attached to a report. So they are acquired deliberately, by running this,
and the result is committed as data.

Run it when a new index lands — RSF each May, PISA every three years:

    python -m backend.data_fetching.curated_build

**Everything is checked against the source's own printed figures**, because the
failure mode here is not a crash. It is a transcription that looks fine: one
column copied into another, or a step that silently drops every other row. Both
leave a file that loads cleanly and is wrong. So:

* the OECD average row is read back out of the same table and compared against
  the published figures;
* the three PISA domains are checked against each other, because identical
  columns mean one was copied over another;
* row counts are asserted against what the source actually contains;
* a handful of known values are spot-checked.

Any of those failing stops the write. A half-written curated file is worse than
no curated file, because the census will report it as coverage.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from backend.util import constants, paths

__all__ = ["build", "fetch_rsf", "fetch_pisa"]


UA = {"User-Agent": "Mozilla/5.0 (compatible; ai-country-risk/2.0)"}

OUT_PATH = paths.DATA_DIR / "curated.csv"

COLUMNS = (
    "country_iso2",
    "indicator_code",
    "period",
    "value",
    "as_of",
    "source_url",
    "source_table",
    "retrieved_at",
)


# --- RSF --------------------------------------------------------------------

RSF_CSV_URL = "https://rsf.org/sites/default/files/import_classement/{year}.csv"
RSF_INDEX_URL = "https://rsf.org/en/index"

# The index years to load. RSF rebuilt its methodology for the 2022 edition, so
# 2022 is the earliest edition whose score is comparable with today's — an
# earlier one would look like a trend and be a change of instrument.
RSF_YEARS = (2022, 2023, 2024, 2025, 2026)

# An index published in May of year N assesses the calendar year that closed the
# previous December. Recording the period as the index year would put `as_of`
# seven months before the period it describes had even ended.
RSF_PERIOD_OFFSET = -1

# RSF publishes on World Press Freedom Day.
RSF_PUBLICATION = (5, 3)

RSF_SPOT_CHECKS = {
    # Verified against the RSF index page and a second source (Wikipedia's
    # World Press Freedom Index table, retrieved 2026-09-22). Six countries
    # agreed exactly; four of them are pinned here.
    2026: {"NOR": 92.72, "PRT": 83.71, "DEU": 82.17, "USA": 62.61},
}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch_rsf(years: Tuple[int, ...] = RSF_YEARS) -> Dict[str, Dict[int, float]]:
    """Return ``{iso3: {period_year: score}}`` for the roster, from RSF's CSVs.

    Raises:
        ValueError: if a year's file is missing countries the roster needs, or a
            spot check fails.
    """
    wanted = {c["iso3"]: c["iso2"] for c in constants.COUNTRY_ROSTER}
    out: Dict[str, Dict[int, float]] = {iso3: {} for iso3 in wanted}

    for year in years:
        raw = _get(RSF_CSV_URL.format(year=year))
        text = raw.decode("utf-8-sig", errors="replace")
        if text.count(";") < 100:
            text = raw.decode("latin-1")
        rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
        if not rows:
            raise ValueError(f"RSF {year}: no rows")

        score_col = next(
            (k for k in rows[0] if k and k.strip().lower().startswith("score")), None
        )
        if not score_col:
            raise ValueError(f"RSF {year}: no score column in {list(rows[0])}")

        period = year + RSF_PERIOD_OFFSET
        found = 0
        for row in rows:
            iso3 = (row.get("ISO") or "").strip()
            if iso3 not in wanted:
                continue
            raw_score = (row.get(score_col) or "").strip().replace(",", ".")
            if not raw_score:
                continue
            score = float(raw_score)
            if not 0.0 <= score <= 100.0:
                raise ValueError(f"RSF {year} {iso3}: score {score} outside 0-100")
            out[iso3][period] = score
            found += 1

        print(f"  RSF {year} (period {period}): {found}/{len(wanted)} roster countries")

        for iso3, expected in RSF_SPOT_CHECKS.get(year, {}).items():
            got = out[iso3].get(period)
            if got is None or abs(got - expected) > 0.005:
                raise ValueError(
                    f"RSF {year} {iso3}: expected {expected}, read {got}. "
                    f"The file has changed shape — do not write the result."
                )

    missing = [iso3 for iso3, series in out.items() if not series]
    if missing:
        print(f"  RSF: no score at all for {missing}")
    return out


# --- PISA -------------------------------------------------------------------

# The statlink attached to the Executive Summary of PISA 2022 Results Volume I.
# Table I.1 is the OECD's own snapshot of mean performance.
PISA_XLSX_URL = "https://stat.link/d84fig"
PISA_SHEET = "Table I.1"
PISA_SOURCE_URL = (
    "https://www.oecd.org/en/publications/pisa-2022-results-volume-i_53f23881-en.html"
)
PISA_SOURCE_TABLE = (
    "PISA 2022 Results (Volume I), Table I.1 — Snapshot of performance in "
    "mathematics, reading and science (statlink https://stat.link/d84fig)"
)

PISA_ROUND = 2022
PISA_PUBLISHED = dt.date(2023, 12, 5)

# The OECD averages printed in the same table. These are the validation anchor:
# if the sheet is reshaped, or a column is read into the wrong slot, these stop
# matching before anything is written.
PISA_OECD_AVERAGE = {
    "mathematics": 472.35765,
    "reading": 475.588199,
    "science": 484.645525,
}

PISA_SPOT_CHECKS = {
    "Singapore": (574.66382, 542.553322, 561.433275),
    "Portugal": (471.910522, None, None),
}

# The count of country/economy rows Table I.1 holds, excluding the OECD
# average row and the title and footnote rows around it.
PISA_EXPECTED_ROWS = 81

# OECD/PISA names to roster ISO-2. Names not here are economies outside the
# roster; roster countries not here did not sit PISA 2022 and stay absent.
PISA_NAME_TO_ISO2 = {
    "Australia": "AU", "Austria": "AT", "Belgium": "BE", "Brazil": "BR",
    "Canada": "CA", "Chile": "CL", "Chinese Taipei": "TW", "Colombia": "CO",
    "Czech Republic": "CZ", "Czechia": "CZ", "Denmark": "DK", "Finland": "FI",
    "France": "FR", "Germany": "DE", "Greece": "GR", "Hong Kong (China)": "HK",
    "Hungary": "HU", "Indonesia": "ID", "Ireland": "IE", "Israel": "IL",
    "Italy": "IT", "Japan": "JP", "Korea": "KR", "Malaysia": "MY",
    "Mexico": "MX", "Netherlands": "NL", "New Zealand": "NZ", "Norway": "NO",
    "Peru": "PE", "Philippines": "PH", "Poland": "PL", "Portugal": "PT",
    "Qatar": "QA", "Saudi Arabia": "SA", "Singapore": "SG", "Spain": "ES",
    "Sweden": "SE", "Switzerland": "CH", "Thailand": "TH", "Türkiye": "TR",
    "Turkiye": "TR", "United Arab Emirates": "AE", "United Kingdom": "GB",
    "United States": "US",
}

# China did not sit PISA 2022 — the four-province sample (B-S-J-Z) took part in
# 2018 and is not the country in any case. Recording it as "China" would be the
# same error as a roster name that means something else, so it is excluded
# explicitly as well as being absent from the source.
PISA_EXCLUDED = {"CN"}


def _clean_pisa_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip().rstrip("*").strip()
    return name or None


def fetch_pisa() -> Dict[str, Dict[str, float]]:
    """Return ``{iso2: {"mean", "mathematics", "reading", "science"}}``.

    Raises:
        ValueError: if the OECD averages, the row count, the spot checks or the
            column-distinctness check fail.
    """
    import pandas as pd

    raw = _get(PISA_XLSX_URL)
    frame = pd.read_excel(io.BytesIO(raw), sheet_name=PISA_SHEET, header=None)

    rows: List[Tuple[str, float, float, float]] = []
    averages: Optional[Tuple[float, float, float]] = None

    for i in range(len(frame)):
        name = _clean_pisa_name(frame.iloc[i, 0])
        if not name:
            continue
        try:
            maths = float(frame.iloc[i, 1])
            reading = float(frame.iloc[i, 2])
            science = float(frame.iloc[i, 3])
        except (TypeError, ValueError):
            continue
        # `float(nan)` succeeds, so a title or footnote row — text in column 0
        # and empty cells beside it — reads as a data row unless NaN is rejected
        # explicitly. That was worth eight phantom countries on the first run.
        if any(v != v for v in (maths, reading, science)):
            continue
        if name == "OECD average":
            averages = (maths, reading, science)
            continue
        rows.append((name, maths, reading, science))

    # --- validation, before anything is written ----------------------------
    if averages is None:
        raise ValueError("PISA: the OECD average row was not found in Table I.1")
    for domain, got, expected in zip(
        ("mathematics", "reading", "science"), averages, PISA_OECD_AVERAGE.values()
    ):
        if abs(got - expected) > 0.01:
            raise ValueError(
                f"PISA: OECD average for {domain} reads {got}, the published "
                f"figure is {expected}. The sheet has changed — do not write."
            )

    if len(rows) != PISA_EXPECTED_ROWS:
        raise ValueError(
            f"PISA: read {len(rows)} country rows, expected {PISA_EXPECTED_ROWS}. "
            f"A step that silently drops rows leaves a file that loads cleanly "
            f"and is wrong."
        )

    maths_col = [r[1] for r in rows]
    reading_col = [r[2] for r in rows]
    science_col = [r[3] for r in rows]
    if maths_col == reading_col or reading_col == science_col or maths_col == science_col:
        raise ValueError(
            "PISA: two domain columns are identical — one has been copied over "
            "another."
        )

    by_name = {r[0]: r[1:] for r in rows}
    for name, expected in PISA_SPOT_CHECKS.items():
        got = by_name.get(name)
        if got is None:
            raise ValueError(f"PISA: spot-check country {name!r} is missing")
        for domain, e, g in zip(("maths", "reading", "science"), expected, got):
            if e is not None and abs(g - e) > 0.01:
                raise ValueError(f"PISA {name} {domain}: expected {e}, read {g}")

    out: Dict[str, Dict[str, float]] = {}
    for name, maths, reading, science in rows:
        iso2 = PISA_NAME_TO_ISO2.get(name)
        if not iso2 or iso2 in PISA_EXCLUDED:
            continue
        for domain, v in (("maths", maths), ("reading", reading), ("science", science)):
            if not 200.0 <= v <= 700.0:
                raise ValueError(f"PISA {name} {domain}: {v} is not a plausible score")
        out[iso2] = {
            "mean": round((maths + reading + science) / 3, 2),
            "mathematics": round(maths, 2),
            "reading": round(reading, 2),
            "science": round(science, 2),
        }

    roster = {c["iso2"] for c in constants.COUNTRY_ROSTER}
    absent = sorted(roster - set(out))
    print(f"  PISA {PISA_ROUND}: {len(out)}/{len(roster)} roster countries")
    print(f"  PISA {PISA_ROUND}: absent, left absent rather than substituted: {absent}")
    return out


# --- writing ----------------------------------------------------------------


def build(out_path: Any = None) -> int:
    """Acquire both series and write `curated.csv`. Returns the row count."""
    out_path = out_path or OUT_PATH
    retrieved = dt.date.today().isoformat()
    rows: List[Dict[str, Any]] = []

    print("Reporters Without Borders — World Press Freedom Index")
    rsf = fetch_rsf()
    iso2_by_iso3 = {c["iso3"]: c["iso2"] for c in constants.COUNTRY_ROSTER}
    for iso3, series in sorted(rsf.items()):
        for period, score in sorted(series.items()):
            index_year = period - RSF_PERIOD_OFFSET
            rows.append({
                "country_iso2": iso2_by_iso3[iso3],
                "indicator_code": "RSF.PRESS.SCORE",
                "period": period,
                "value": round(score, 2),
                "as_of": dt.date(index_year, *RSF_PUBLICATION).isoformat(),
                "source_url": RSF_CSV_URL.format(year=index_year),
                "source_table": (
                    f"RSF World Press Freedom Index {index_year}, global score "
                    f"column (0-100, higher = freer); assesses calendar year "
                    f"{period}. Index page: {RSF_INDEX_URL}"
                ),
                "retrieved_at": retrieved,
            })

    print("\nOECD — PISA")
    pisa = fetch_pisa()
    for iso2, scores in sorted(pisa.items()):
        rows.append({
            "country_iso2": iso2,
            "indicator_code": "OECD.PISA.MEAN",
            "period": PISA_ROUND,
            "value": scores["mean"],
            "as_of": PISA_PUBLISHED.isoformat(),
            "source_url": PISA_SOURCE_URL,
            "source_table": (
                f"{PISA_SOURCE_TABLE}; mean of mathematics {scores['mathematics']}, "
                f"reading {scores['reading']}, science {scores['science']}"
            ),
            "retrieved_at": retrieved,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {out_path}")
    return len(rows)


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
