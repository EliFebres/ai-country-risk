# WEO vintages — drop the editions here

Each file is one IMF *World Economic Outlook* edition, kept as its own vintage.
They are the only source of the live payload's four `WEO.*` indicators; the
live run reads the newest edition, and the older ones keep the revision history
in `indicator_series`. They were collected for the retired historical backfill
(`docs/historical-ratings-postmortem.md`), which read the edition that existed
at each past date.

## Naming

    YYYY-MM.xls        e.g. 2018-04.xls, 2018-10.xls

`YYYY-MM` is the **edition**, not the download date. The loader reads the
vintage from the filename and ignores everything else about the file, so a
misnamed file is a silently wrong vintage — the one mistake here that does real
damage.

WEO publishes twice a year, in **April** and **October**. On disk:

    2016-04  2016-10  2017-04  2017-10  2018-04  2018-10  2019-04  2019-10
    2020-04  2020-10  2021-04  2021-10  2022-04  2022-10  2023-04  2023-10
    2024-04  2024-10  2025-04  2025-10  2026-04

**All twenty-one are present**, and all twenty-one are loaded. The last two
(`2025-10`, `2026-04`) had to be fetched by hand: the WEO database moved to
data.imf.org in October 2025 and the legacy path the fetch script uses was never
backfilled, so `weo_fetch.py` still cannot reach them. If a future edition
is likewise unreachable, download it from the site and drop it here under the
naming rule above — the loader neither knows nor cares how the file arrived.

## Where to download

<https://www.imf.org/en/Publications/WEO/weo-database> → choose the edition →
**"By Countries"** or the *entire dataset* → download the **"WEO data"** file.

The archive of past editions is linked from the same page under *WEO Databases*.
Direct URLs follow this shape and are the quickest route:

    https://www.imf.org/-/media/Files/Publications/WEO/WEO-Database/2018/WEOOct2018all.ashx

Save each one as `YYYY-MM.xls` in this folder.

## The file is not a spreadsheet

The IMF calls it `.xls`, but it is a **tab-delimited text file**. Opening it in
Excel or LibreOffice shows a format-mismatch warning; that is expected and the
file is fine. Do not "fix" it by re-saving as real xlsx — the loader reads it
with the standard library's `csv` module precisely because it is text, and this
project pins no xlsx parser.

Encodings vary by edition (older ones are UTF-16, newer ones Latin-1). The
loader tries both, so no conversion is needed.

## What gets loaded

Only a handful of series — the ones where the *revision* is the story rather
than the level. See `SUBJECTS` in `backend/data_fetching/weo.py`.

All five now map onto a key of `constants.INDICATOR_REGISTRY` and therefore
reach a score: inflation on `CPI.YOY`, and real GDP growth, gross government
debt, general government net lending and the current account on their own
`WEO.`-prefixed codes. The prefix is deliberate — these are edition-vintaged and
the World Bank's versions of nearly the same series are not, so quietly merging
the two would throw away the revision history that is the whole reason for
loading editions at all.

### Known per-edition quirks

- **`2020-04` carries four subjects, not five** — the April 2020 edition was
  published with curtailed coverage, and gross government debt is absent from
  it (the file is 1.7 MB against ~9 MB for its neighbours). This is the IMF's
  own gap, not a truncated download. The "newest vintage ≤ as_of" rule resolves
  per indicator, so an anchor between April and October 2020 falls back to the
  `2019-10` debt figure rather than losing it.
- **`2020-10` onward are UTF-16LE without a BOM** and around 19–20 MB. The
  loader handles them; do not "fix" the encoding.
- **A few rows carry the edition's own year as history.** `2025-04` states 2025
  actuals for Singapore, and `2020-10` for India and Egypt, because their fiscal
  years close before the edition goes to press. That is the file's own per-row
  `Estimates Start After` talking, not a projection leaking in, and the loader
  is right to keep it.

Only **historical** columns: years up to and including the edition's own year.
The WEO's forward columns are projections, and a 2018 edition's guess at 2020 is
not a fact about 2020. Loading them would put the IMF's forecast into the
evidence payload as if it were an observation.

## Running it

    python backend/main.py bootstrap        # loads every edition here
    python backend/main.py weo-fetch        # fetches what the IMF still serves

Idempotent — `indicator_series` is keyed on
`(country_iso2, indicator_code, freq, period, as_of)`, so every edition's copy of
a year coexists with the others instead of the newest load overwriting them all.
`as_of` is in the key precisely because the vintages are the point.

## If this folder is empty

The run still scores. Every payload then lacks the four `WEO.*` indicators,
the run logs a warning saying so, and `payload_health` records them as
`no row`.
