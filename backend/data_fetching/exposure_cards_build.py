"""
Build `backend/llm/exposure_cards.json`: what each roster country is exposed to.

The relevance gate's spillover test (T5) asks whether an article about another
country reaches this one through a main trade partner, a main export, a
neighbour or a security rival. The model cannot judge that without knowing the
exposures, so each country carries a card.

Partners, exports and land borders come from the CIA World Factbook, via
github.com/factbook/factbook.json. The edition is the date of the last change to
the data files, and it is recorded on every card. Sea neighbours and
security rivals are not in that data. They are written here by hand, and only
where they matter: a rival is a state in a live military or coercive dispute with
the country, at most three.

The repository is read from a partial clone (history, and only the blobs that
are checked out), so each file's edition comes from `git log` rather than the
GitHub API, whose unauthenticated limit is below two builds.

Refresh yearly (deferred.md §18).

    python -m backend.data_fetching.exposure_cards_build [--clone DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from backend.util import constants, paths

REPO = "https://github.com/factbook/factbook.json.git"

OUT = paths.PROJECT_ROOT / "backend" / "llm" / "exposure_cards.json"

# Roster ISO-2 -> factbook region folder and GEC code.
FACTBOOK_PATH = {
    "AU": "australia-oceania/as", "AT": "europe/au", "BE": "europe/be",
    "CA": "north-america/ca", "DK": "europe/da", "FI": "europe/fi", "FR": "europe/fr",
    "DE": "europe/gm", "HK": "east-n-southeast-asia/hk", "IE": "europe/ei",
    "IL": "middle-east/is", "IT": "europe/it", "JP": "east-n-southeast-asia/ja",
    "NL": "europe/nl", "NZ": "australia-oceania/nz", "NO": "europe/no", "PT": "europe/po",
    "SG": "east-n-southeast-asia/sn", "ES": "europe/sp", "SE": "europe/sw",
    "CH": "europe/sz", "GB": "europe/uk", "US": "north-america/us",
    "BR": "south-america/br", "CL": "south-america/ci", "CN": "east-n-southeast-asia/ch",
    "CO": "south-america/co", "CZ": "europe/ez", "EG": "africa/eg", "GR": "europe/gr",
    "HU": "europe/hu", "IN": "south-asia/in", "ID": "east-n-southeast-asia/id",
    "KW": "middle-east/ku", "MY": "east-n-southeast-asia/my", "MX": "north-america/mx",
    "PE": "south-america/pe", "PH": "east-n-southeast-asia/rp", "PL": "europe/pl",
    "QA": "middle-east/qa", "SA": "middle-east/sa", "ZA": "africa/sf",
    "KR": "east-n-southeast-asia/ks", "TW": "east-n-southeast-asia/tw",
    "TH": "east-n-southeast-asia/th", "TR": "middle-east/tu", "AE": "middle-east/ae",
    "RU": "central-asia/rs",
}

# Written by hand: neighbours the land-border data leaves out. Mostly across a
# sea; Hong Kong's border with mainland China is not in its factbook entry.
HAND_NEIGHBOURS = {
    "HK": ["China"], "TW": ["China"], "KW": ["Iran"], "JP": ["China", "Russia", "North Korea", "South Korea"],
    "SG": ["Malaysia", "Indonesia"], "AU": ["Indonesia", "Papua New Guinea"],
    "NZ": ["Australia"], "GB": ["France"], "PH": ["China", "Taiwan", "Malaysia", "Indonesia"],
    "QA": ["Iran", "Bahrain"], "AE": ["Iran"], "SA": ["Iran"], "KR": ["Japan", "China"],
    "DK": ["Sweden"], "SE": ["Denmark"],
}

# Written by hand: states in a live military or coercive dispute, at most three.
SECURITY_RIVALS = {
    "TW": ["China"], "KR": ["North Korea"], "JP": ["China", "North Korea", "Russia"],
    "PL": ["Russia", "Belarus"], "FI": ["Russia"], "NO": ["Russia"], "SE": ["Russia"],
    "DK": ["Russia"], "IL": ["Iran"], "KW": ["Iran", "Iraq"], "SA": ["Iran"],
    "AE": ["Iran"], "QA": ["Iran"], "IN": ["China", "Pakistan"],
    "CN": ["United States", "India", "Japan"], "RU": ["Ukraine", "United States"],
    "PH": ["China"], "GR": ["Turkey"], "TR": ["Greece"], "US": ["China", "Russia", "Iran"],
    "MY": ["China"],
}

# The factbook's short forms, spelled out so they match how articles name them.
NAMES = {"USA": "United States", "US": "United States", "UK": "United Kingdom",
         "UAE": "United Arab Emirates", "S. Korea": "South Korea", "Czech Republic": "Czechia",
         "Korea, South": "South Korea", "Korea, North": "North Korea",
         "Congo, Democratic Republic of the": "DR Congo", "Burma": "Myanmar",
         "Turkey (Turkiye)": "Turkey", "Czechia": "Czechia"}


def _name(raw: str) -> str:
    raw = raw.strip()
    return NAMES.get(raw, raw)


def _partners(text: str, n: int = 5) -> List[str]:
    """'China 25%, India 13%, ... (2023)' -> ['China', 'India', ...]."""
    body = re.sub(r"\(\d{4}\)\s*$", "", text or "").strip()
    out = []
    for part in body.split(","):
        name = re.sub(r"\s*\d+(\.\d+)?%\s*$", "", part).strip()
        if name:
            out.append(_name(name))
    return out[:n]


def _commodities(text: str, n: int = 3) -> List[str]:
    body = re.sub(r"\(\d{4}\)\s*$", "", text or "").strip()
    return [p.strip() for p in body.split(",") if p.strip()][:n]


def _borders(geo: Dict) -> List[str]:
    text = ((geo.get("Land boundaries") or {}).get("border countries") or {}).get("text", "")
    out = []
    for part in re.split(r";", text):
        name = re.sub(r"\s*\(\d+\)", "", part)
        name = re.sub(r"\s*[\d,.]+\s*km.*$", "", name).strip()
        if name:
            out.append(_name(name))
    return out


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(clone), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def build(clone: Optional[Path] = None) -> Dict[str, Dict]:
    if clone is None:
        clone = Path(tempfile.mkdtemp(prefix="factbook-")) / "factbook.json"
    if not (clone / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", REPO, str(clone)], check=True)

    cards: Dict[str, Dict] = {}
    editions = set()
    for c in constants.COUNTRY_ROSTER:
        iso2 = c["iso2"]
        path = FACTBOOK_PATH[iso2] + ".json"
        sha, date = _git(clone, "log", "-1", "--format=%H %cs", "--", path).split()
        editions.add(date)
        data = json.loads((clone / path).read_text(encoding="utf-8"))
        econ, geo = data.get("Economy", {}), data.get("Geography", {})
        neighbours = _borders(geo)
        neighbours += [m for m in HAND_NEIGHBOURS.get(iso2, []) if m not in neighbours]
        cards[iso2] = {
            "export_partners": _partners((econ.get("Exports - partners") or {}).get("text", "")),
            "import_partners": _partners((econ.get("Imports - partners") or {}).get("text", "")),
            "main_exports": _commodities((econ.get("Exports - commodities") or {}).get("text", "")),
            "neighbours": neighbours,
            "security_rivals": SECURITY_RIVALS.get(iso2, []),
            "source": f"CIA World Factbook via github.com/factbook/factbook.json, "
                      f"edition {date} (commit {sha[:12]}); sea neighbours and "
                      f"security rivals written by hand",
        }
    OUT.write_text(json.dumps(cards, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[cards] wrote {len(cards)} cards to {OUT} (editions: {sorted(editions)})")
    return cards


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clone", type=Path, help="an existing clone of factbook.json")
    build(ap.parse_args().clone)


if __name__ == "__main__":
    main()
