"""Turn an empty Postgres into a working system.

The acceptance test for the whole no-files-on-disk rule: clone the repo, fill
`.env`, run this, and every macro number the scorer needs is present. No
downloads to do by hand, no templates to fill in, no Parquet to copy.

Six steps, each idempotent and each guarded on its own, so a step that fails
does not cost the ones after it and a re-run picks up where it stopped:

1. **schema**    — the ten tables and their indexes
2. **roster**    — the 48 countries, their map positions and structural facts
3. **weo**       — IMF WEO editions, fetched if absent, loaded per vintage
4. **panels**    — World Bank annuals for every country
5. **ledgers**   — the extra WB codes, BIS bulk files, and curated values
6. **imf**       — IMF monthly and quarterly prints

**Article harvesting is not here, deliberately.** It takes days of somebody
else's rate limit and only matters for rebuilding history, so it stays a
separate command. Everything above is minutes to a couple of hours.

    python backend/main.py bootstrap                 # everything
    python backend/main.py bootstrap --only schema roster
    python backend/main.py bootstrap --dry-run       # report, change nothing
    python backend/main.py bootstrap --check         # what is present now
"""

import argparse
import datetime
import logging
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / "backend" / ".env")

from backend.data_fetching import country_data_fetch, curated_loader  # noqa: E402
from backend.data_fetching.vintage import weo  # noqa: E402
from backend.data_upsert import data_push, schema  # noqa: E402
from backend.util import constants, pipeline  # noqa: E402

logger = logging.getLogger("bootstrap")


def step_schema() -> str:
    """Create the ten tables. A no-op against a database that already has them."""
    with data_push._transaction() as cur:
        created = schema.create_all(cur)
    return f"created {len(created)} table(s): {', '.join(created) or 'none, all present'}"


def step_roster() -> str:
    """Seed the country roster and the structural facts block.

    Structural facts are shipped as data in the repo rather than as a template
    somebody fills in: they are hand-researched, cited values, and a clone that
    started with an empty file would score forty-eight countries without the
    one block masking cannot replace.
    """
    data_push.upsert_countries(constants.COUNTRY_ROSTER)
    facts = curated_loader.load_structural_facts()
    if facts:
        data_push.upsert_structural_facts(facts)
    return (f"{len(constants.COUNTRY_ROSTER)} countries, "
            f"{len(facts)} with a structural block")


def step_weo() -> str:
    """Load every IMF WEO edition, fetching the files first if they are absent.

    Each edition is its own vintage, stamped with its own publication date, so
    a 2018 anchor reads the April-2018 estimate rather than today's revision of
    it. That is the macro half of the no-future rule and it cannot be
    reconstructed after the fact.
    """
    editions = sorted(weo.VINTAGE_DIR.glob("*.xls")) if weo.VINTAGE_DIR.exists() else []
    fetched = 0
    if not editions:
        from backend.data_fetching.vintage import fetch_editions
        fetched = fetch_editions.fetch_all()
        editions = sorted(weo.VINTAGE_DIR.glob("*.xls"))

    rows = weo.load_all([c["iso2"] for c in constants.COUNTRY_ROSTER])
    if rows:
        data_push.upsert_indicator_series(rows)
    return (f"{len(editions)} edition(s) on disk ({fetched} fetched), "
            f"{len(rows)} observation(s) loaded")


def step_panels() -> str:
    """World Bank annuals for every rostered country."""
    country_data_fetch.backfill_missing_panels()
    return _series_summary()


def step_ledgers() -> str:
    """The extra World Bank codes, the BIS bulk files, and curated values."""
    pipeline.refresh_ledger_sources()
    return _series_summary()


def step_imf() -> str:
    """IMF monthly and quarterly prints."""
    pipeline.refresh_imf_indicators()
    return _series_summary()


def _series_summary() -> str:
    with data_push._transaction() as cur:
        cur.execute("SELECT count(*), count(DISTINCT country_iso2), "
                    "       count(DISTINCT indicator_code) FROM indicator_series")
        rows, countries, codes = cur.fetchone()
    return f"{rows:,} observations, {countries} countries, {codes} indicators"


STEPS = (
    ("schema", step_schema),
    ("roster", step_roster),
    ("weo", step_weo),
    ("panels", step_panels),
    ("ledgers", step_ledgers),
    ("imf", step_imf),
)


def check() -> dict:
    """What is present, and what a scorer would still be missing."""
    with data_push._transaction() as cur:
        report = schema.verify(cur)
        cur.execute("""
            SELECT count(DISTINCT indicator_code) FROM indicator_series
        """)
        codes = cur.fetchone()[0]
        cur.execute("""
            SELECT count(*) FROM country WHERE structural IS NOT NULL
        """)
        structural = cur.fetchone()[0]
        cur.execute("""
            SELECT count(DISTINCT source) FROM indicator_series
             WHERE source LIKE 'IMF WEO%'
        """)
        editions = cur.fetchone()[0]
    report["indicator_codes_present"] = codes
    report["indicator_codes_expected"] = len(constants.INDICATOR_REGISTRY)
    report["countries_with_structural"] = structural
    report["weo_editions_loaded"] = editions
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="+", choices=[n for n, _ in STEPS],
                        help="run a subset, in the order given here")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the steps and report the current state")
    parser.add_argument("--check", action="store_true",
                        help="report what is present, run nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [bootstrap] %(levelname)s %(message)s")

    if args.check:
        for key, value in check().items():
            if key in ("counts", "present"):
                continue
            print(f"  {key:28} {value}")
        with data_push._transaction() as cur:
            for name in schema.table_names():
                cur.execute(f'SELECT count(*) FROM "{name}"')
                print(f"  {name:28} {cur.fetchone()[0]:>10,} rows")
        return

    chosen = [(n, f) for n, f in STEPS if not args.only or n in args.only]
    if args.dry_run:
        print("would run, in order:")
        for name, fn in chosen:
            print(f"  {name:10} {(fn.__doc__ or '').splitlines()[0]}")
        print("\ncurrent state:")
        for key, value in check().items():
            if key not in ("counts", "present"):
                print(f"  {key:28} {value}")
        return

    started = datetime.datetime.now()
    failures = []
    for name, fn in chosen:
        step_started = datetime.datetime.now()
        try:
            outcome = fn()
            elapsed = (datetime.datetime.now() - step_started).total_seconds()
            logger.info("%-8s OK  %s  (%.1fs)", name, outcome, elapsed)
        except Exception as exc:  # noqa: BLE001 - one bad step must not stop the rest
            failures.append(name)
            logger.exception("%-8s FAILED: %s", name, exc)

    total = (datetime.datetime.now() - started).total_seconds()
    logger.info("bootstrap finished in %.1fs; %d step(s) failed", total, len(failures))
    if failures:
        logger.error("re-run to retry: python backend/main.py bootstrap --only %s",
                     " ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
