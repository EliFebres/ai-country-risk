"""Download every IMF WEO edition the pilot window needs, live or from the archive.

A one-off script, not a runtime import: nothing in the backend imports this. It
adds no dependency — ``requests`` is already pinned, and the Wayback constants
and user agent come from the modules that already talk to those services.

**The IMF moved the WEO archive and did not leave forwarding addresses.** Three
things are true at once as of 2026-08:

* the ``/-/media/Files/Publications/WEO/WEO-Database/`` path — the one the
  open-source ``weo-reader`` project resolves, and the one the folder README
  documents — now returns **403 Access Denied** from Akamai for every edition.
  Not an IP block: other ``/-/media/`` files serve fine. That folder specifically
  is gone.
* the classic ``/external/pubs/ft/weo/{YYYY}/{01|02}/weodata/`` path still serves
  a handful of pre-2021 editions, and 302s the rest to a page that also 403s.
* everything else is on the **Wayback Machine**, which captured the ``.ashx``
  files while they were live and serves the original bytes back.

So this tries live URLs first and falls back to the archive. Reaching for
web.archive.org for a *macro dataset* looks odd until you notice it is the same
move ``history/wayback.py`` already makes for article bodies, for the same
reason: the vintage matters more than the source, and a 2018 edition is a
historical artefact whether the IMF still hosts it or not.

**Validation is the point.** imf.org answers a dead path with 200 and an HTML
page often enough that "the download succeeded" means nothing. A file is kept
only if ``weo.read_edition`` — the same function the loader will call — returns
rows from it. Everything else is deleted, and the editions still missing are
printed at the end.

Usage:
    python -m backend.scripts.fetch_weo_vintages
    python -m backend.scripts.fetch_weo_vintages --force      # re-fetch what exists
    python -m backend.scripts.fetch_weo_vintages --no-archive # live URLs only
"""

import argparse
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from backend.util import http  # noqa: E402
from backend.news_fetching import wayback  # noqa: E402
from backend.util import config  # noqa: E402
from backend.data_fetching.vintage import weo  # noqa: E402

_MONTHS = {4: "Apr", 10: "Oct"}

# April editions live under `01`, October under `02`, in the classic path's own
# numbering. Not the calendar month — this is the WEO issue number.
_ISSUE = {4: "01", 10: "02"}

_MEDIA = "https://www.imf.org/-/media/Files/Publications/WEO/WEO-Database"
_CLASSIC = "https://www.imf.org/external/pubs/ft/weo"

# A real WEO edition is megabytes. The archive holds truncated captures of the
# same URLs — a 147KB "capture" of a 2.8MB file — and they decode far enough to
# look plausible. Cheaper to skip them by size than to parse each one.
_MIN_BYTES = 500_000

# How many captures of one URL to actually download before giving up on it.
#
# Bounded because the first version was not, and it cost an hour: four URL
# shapes times twenty listed captures times a three-minute timeout is a
# four-hour worst case for a single edition that does not exist. If the two
# oldest sound captures both fail to fetch, a third is not going to save it.
_MAX_CAPTURE_TRIES = 2

# Long enough for a 9MB file over a slow archive replay, short enough that a
# hung connection is not mistaken for a download.
_TIMEOUT = 90


def urls(year: int, month: int) -> list:
    """Every path shape this edition has ever lived at, likeliest first.

    The classic path first because it is the one still serving real files; the
    media shapes after it because they are what the archive captured.
    """
    mon = _MONTHS[month]
    return [
        f"{_CLASSIC}/{year}/{_ISSUE[month]}/weodata/WEO{mon}{year}all.xls",
        f"{_MEDIA}/{year}/WEO{mon}{year}all.ashx",
        f"{_MEDIA}/{year}/{mon}/WEO{mon}{year}all.ashx",
        f"{_MEDIA}/{year}/{month:02d}/WEO{mon}{year}all.ashx",
    ]


def editions(start_year: int, end_year: int) -> list:
    """Every (year, month) the pilot window needs, oldest first.

    Starts at the April before ``PILOT_START``'s year: the selection rule is
    "newest vintage not after the anchor", so the earliest anchors need an
    edition that precedes them or they have no vintage at all.
    """
    return [(year, month)
            for year in range(start_year, end_year + 1)
            for month in (4, 10)]


def valid(path: pathlib.Path) -> bool:
    """Does the loader get rows out of this file?

    The only validation that means anything, because it covers the whole chain
    at once: the bytes decode under one of the encodings ``weo._rows`` tries,
    the table is tab-delimited, the header carries "WEO Subject Code", and at
    least one roster country's subject row parses. An HTML error page fails
    every one of them.

    Deliberately not a hardcoded encoding check — the README documents older
    editions as UTF-16 and newer as Latin-1, so pinning either would reject half
    the archive as corrupt.
    """
    try:
        return bool(weo.read_edition(path, config.PILOT_ROSTER))
    except Exception:  # noqa: BLE001
        return False


def _keep(body: bytes, target: pathlib.Path, note: str) -> bool:
    """Write, validate, and delete if it does not parse.

    A file that exists and does not parse is worse than a missing one: the
    folder reads as populated forever after, and ``load_all`` logs the failure
    once and moves on.
    """
    if len(body) < _MIN_BYTES:
        print(f"  {target.name}: {len(body):,} bytes from {note} — too small to be an edition")
        return False
    target.write_bytes(body)
    if valid(target):
        print(f"  {target.name}: {len(body):,} bytes from {note}")
        return True
    target.unlink()
    print(f"  {target.name}: {len(body):,} bytes from {note} did not parse as a WEO table")
    return False


def _live(url: str) -> bytes:
    """The file as the IMF serves it today, or empty on any refusal."""
    try:
        response = requests.get(url, headers={"User-Agent": http.PROJECT_UA},
                                timeout=_TIMEOUT)
    except requests.RequestException:
        return b""
    # A dead classic path answers 200 with an HTML interstitial, so the content
    # type is load-bearing here rather than decorative.
    if response.status_code != 200 or "html" in response.headers.get("content-type", ""):
        return b""
    return response.content


def _archived(url: str) -> bytes:
    """The earliest sound Wayback capture of this URL, or empty.

    Earliest rather than newest on purpose. Later captures of these files are
    increasingly the IMF's own error pages, captured after the move; the first
    one is the closest thing to the edition as published.
    """
    try:
        rows = requests.get(wayback._CDX, timeout=_TIMEOUT, headers={"User-Agent": http.PROJECT_UA},
                            params={"url": url, "output": "json", "limit": 20,
                                    "filter": "statuscode:200"}).json()
    except (requests.RequestException, ValueError):
        return b""
    if len(rows) < 2:
        return b""

    header, captures = rows[0], rows[1:]
    stamp, length = header.index("timestamp"), header.index("length")
    sound = [r for r in sorted(captures, key=lambda r: r[stamp])
             if int(r[length] or 0) >= _MIN_BYTES]
    for row in sound[:_MAX_CAPTURE_TRIES]:
        try:
            response = requests.get(wayback._CAPTURE.format(timestamp=row[stamp], url=url),
                                    headers={"User-Agent": http.PROJECT_UA}, timeout=_TIMEOUT)
        except requests.RequestException:
            continue
        if response.status_code == 200:
            return response.content
    return b""


def fetch(year: int, month: int, directory: pathlib.Path, force: bool,
          archive: bool) -> bool:
    """One edition, live then archived, across every known URL shape."""
    target = directory / f"{year}-{month:02d}.xls"
    if target.exists() and not force:
        print(f"  {target.name}: already here")
        return True

    candidates = urls(year, month)
    for url in candidates:
        body = _live(url)
        if body and _keep(body, target, f"live {url.split('/')[5]}"):
            return True

    if not archive:
        return False

    for url in candidates:
        body = _archived(url)
        if body and _keep(body, target, "the Wayback Machine"):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="re-download editions already on disk")
    parser.add_argument("--no-archive", action="store_true",
                        help="live URLs only; do not fall back to web.archive.org")
    parser.add_argument("--dir", type=pathlib.Path, default=weo.VINTAGE_DIR)
    args = parser.parse_args()

    args.dir.mkdir(parents=True, exist_ok=True)
    start, end = int(config.PILOT_START[:4]), 2026

    print(f"WEO editions {start}-04 .. {end}-10 -> {args.dir}\n")
    missing = [(y, m) for y, m in editions(start, end)
               if not fetch(y, m, args.dir, args.force, not args.no_archive)]

    have = sorted(p.name for p in args.dir.iterdir() if p.suffix == ".xls")
    print(f"\n{len(have)} edition(s) on disk: {', '.join(p[:-4] for p in have) or 'none'}")
    if missing:
        print(f"\n{len(missing)} edition(s) need manual retrieval:")
        for year, month in missing:
            print(f"  {year}-{month:02d}.xls")
        print("\nThe WEO database moved to data.imf.org in October 2025 and the legacy\n"
              "media path now 403s. Download from\n"
              "  https://www.imf.org/en/Publications/SPROLLS/world-economic-outlook-databases\n"
              f"in a browser and save each as YYYY-MM.xls in {args.dir}")
    print("\nThen: python -m backend.util.run weo")


if __name__ == "__main__":
    main()
