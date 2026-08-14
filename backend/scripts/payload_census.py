"""Every indicator the registry expects, against what actually arrives.

Written after the WEO loader spent its whole life writing rows nothing read.
Nineteen editions parsed, sixteen thousand rows landed, every count looked
right, and not one value reached a payload — because the loader's target codes
were not registry keys and nothing anywhere compared the two.

That failure is invisible by construction: `build_evidence_payload` omits an
indicator with no observation, so "this loader is broken" and "this country has
no reading for that series" produce byte-identical payloads. The only way to see
it is to ask what was *expected* and diff.

Read the STORE column first. An indicator with rows in `indicator_series` and
nothing in the payload is a wiring bug of the kind this script exists to find.
An indicator absent from both is just a series nobody collects for this country.

    python -m backend.scripts.payload_census [ISO2] [YYYY-MM-DD]
"""
import collections
import datetime
import sys

from dotenv import load_dotenv

load_dotenv("backend/.env")

from backend.util import constants
from backend.utils import data_retrieval as dr
from backend.data_upsert import data_push

_BLOCKS = ("friction_inputs", "uncertainty_inputs",
           "information_inputs", "edge_inputs")


def census(iso2: str, as_of: datetime.date, vintage: bool = True) -> dict:
    """One country at one anchor: expected, stored, delivered."""
    panel = dr.query_macro_panel(iso2)
    series = data_push.read_indicator_series(iso2)
    recent = data_push.read_recent_indicators(iso2)
    payload = dr.build_evidence_payload(
        iso2, as_of=as_of, panel=panel, series=series, recent=recent,
        fx_regimes=constants.FX_REGIMES, elections=constants.ELECTIONS,
        vintage_as_of=as_of if vintage else None)

    delivered = {}
    for block in _BLOCKS:
        for label, value in (payload.get(block) or {}).items():
            if isinstance(value, dict) and "period" in value:
                delivered[label] = value

    rows = []
    for code, spec in constants.INDICATOR_REGISTRY.items():
        label = str(spec["label"])
        stored = len(series.get(code) or [])
        in_panel = bool(spec.get("panel_col")) and not panel.empty \
            and str(spec["panel_col"]) in panel.columns
        got = delivered.get(label)
        rows.append({
            "code": code, "label": label, "source": str(spec.get("source")),
            "ledger": str(spec.get("ledger")), "stored": stored,
            "panel": in_panel, "delivered": got is not None,
            "period": (got or {}).get("period"),
            "as_of": (got or {}).get("as_of"),
            "value_source": (got or {}).get("source"),
        })
    return {"rows": rows, "delivered": len(delivered),
            "expected": len(constants.INDICATOR_REGISTRY)}


def main() -> None:
    iso2 = (sys.argv[1] if len(sys.argv) > 1 else "PT").upper()
    as_of = (datetime.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2
             else datetime.date.today())
    result = census(iso2, as_of)

    by_source = collections.defaultdict(list)
    for row in result["rows"]:
        by_source[row["source"]].append(row)

    print(f"\n=== payload census: {iso2} @ {as_of} ===")
    print(f"{'':2}{'indicator':44} {'store':>6} {'panel':>6} {'sent':>5}  period")
    suspect = []
    for source in sorted(by_source):
        print(f"\n-- {source} --")
        for r in sorted(by_source[source], key=lambda x: x["label"]):
            mark = "OK " if r["delivered"] else "   "
            if r["delivered"]:
                where = f"{r['period']} via {r['value_source']}"
            else:
                where = "ABSENT"
                if r["stored"] or r["panel"]:
                    mark, where = "!! ", "ABSENT but data exists"
                    suspect.append(r)
            print(f"{mark}{r['label'][:44]:44} {r['stored']:>6} "
                  f"{'yes' if r['panel'] else '-':>6} "
                  f"{'yes' if r['delivered'] else 'no':>5}  {where}")

    print(f"\ndelivered {result['delivered']} of {result['expected']} registry indicators")
    if suspect:
        print(f"\n!! {len(suspect)} indicator(s) have stored data and reach no payload —")
        print("   this is the shape of the WEO bug. Check the code is a registry key,")
        print("   that the period parses, and that the vintage bound admits it:")
        for r in suspect:
            print(f"     {r['code']:22} stored={r['stored']:<5} panel={r['panel']}")
    else:
        print("\nno indicator has stored data that fails to reach the payload.")


if __name__ == "__main__":
    main()
