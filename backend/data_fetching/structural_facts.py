"""
The structural facts the framework needs before it reads a single number.

A 7% policy rate means something different in a country that sets its own rate
than in one that imports Frankfurt's. A current-account deficit means something
different under a currency board than under a float. The scorer is given these
first, so the rest of the payload is read against them rather than against a
default assumption about how countries work.

Three facts, from two kinds of source:

* **Region** and **income group** come from the World Bank's own country
  endpoint. They are classifications the World Bank maintains and revises, so
  they are fetched rather than typed — a hand-copied income group is a fact that
  silently goes stale the year a country is reclassified.

* **Monetary regime** is declared in `constants.MONETARY_REGIME`. There is no
  machine-readable source that says what a currency board is; the IMF's own
  classification is published annually as a PDF table. It is a small, slow-moving
  judgement, so it is written down once with its reasoning rather than scraped.

Taiwan is not a World Bank member and is absent from the endpoint. It gets its
region and income group from the declared fallback rather than being dropped —
a country with no structural facts at all would have the rest of its payload
read against nothing.

    python -m backend.data_fetching.structural_facts
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional

from backend.util import constants, paths

__all__ = ["FACTS_PATH", "fetch", "load", "for_country"]


FACTS_PATH = paths.DATA_DIR / "structural_facts.json"

WB_COUNTRY_URL = "https://api.worldbank.org/v2/country?format=json&per_page=400"

SOURCE_NOTE = (
    "Region and income group: World Bank country classifications, "
    "https://api.worldbank.org/v2/country. Monetary regime: declared in "
    "backend/util/constants.py::MONETARY_REGIME."
)

# Countries the World Bank does not classify. Stated rather than silently
# missing, with the reason, so nobody later reads the gap as an error.
FALLBACKS: Dict[str, Dict[str, str]] = {
    "TW": {
        "region": "East Asia & Pacific",
        "income_group": "High income",
        "classification_source": (
            "not a World Bank member; region and income group assigned here to "
            "match the World Bank's own categories for comparable economies"
        ),
    },
}


def fetch(out_path: Any = None) -> Dict[str, Dict[str, Any]]:
    """Fetch region and income group, merge the declared regime, and write JSON."""
    out_path = out_path or FACTS_PATH

    req = urllib.request.Request(
        WB_COUNTRY_URL, headers={"User-Agent": "ai-country-risk/2.0"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    if not isinstance(body, list) or len(body) < 2:
        raise ValueError("World Bank country endpoint returned an unexpected shape")

    wb = {c["iso2Code"]: c for c in body[1] if c.get("iso2Code")}

    facts: Dict[str, Dict[str, Any]] = {}
    missing = []
    for entry in constants.COUNTRY_ROSTER:
        iso2 = entry["iso2"]
        regime = constants.MONETARY_REGIME.get(iso2)
        if regime is None:
            raise ValueError(
                f"{iso2} has no entry in constants.MONETARY_REGIME. A country "
                f"whose monetary regime is unstated has its whole payload read "
                f"against a guess."
            )

        row = wb.get(iso2)
        if row and (row.get("region") or {}).get("value") not in (None, "", "Aggregates"):
            region = row["region"]["value"]
            income = (row.get("incomeLevel") or {}).get("value") or "unknown"
            source = "World Bank country classification"
        elif iso2 in FALLBACKS:
            region = FALLBACKS[iso2]["region"]
            income = FALLBACKS[iso2]["income_group"]
            source = FALLBACKS[iso2]["classification_source"]
            missing.append(iso2)
        else:
            raise ValueError(
                f"{iso2} is not in the World Bank country endpoint and has no "
                f"declared fallback. Add one to FALLBACKS with its reason."
            )

        facts[iso2] = {
            "name": entry["name"],
            "tier": entry["tier"],
            "region": region,
            "income_group": income,
            "classification_source": source,
            **regime,
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"_source": SOURCE_NOTE, "countries": facts}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Wrote structural facts for {len(facts)} countries to {out_path}")
    if missing:
        print(f"  used the declared fallback for: {missing}")
    return facts


def load(path: Any = None) -> Dict[str, Dict[str, Any]]:
    """Read the stored facts. Empty if the file is absent."""
    path = path or FACTS_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("countries", {})
    except FileNotFoundError:
        return {}


def for_country(iso2: str, path: Any = None) -> Optional[Dict[str, Any]]:
    """One country's structural facts, or None."""
    return load(path).get((iso2 or "").upper())


if __name__ == "__main__":
    fetch()
