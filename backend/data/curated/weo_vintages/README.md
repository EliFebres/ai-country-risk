# WEO vintages — drop the editions here

Each file is one IMF *World Economic Outlook* edition, kept as its own vintage
so a historical snapshot is scored on the numbers that existed at the time.

This matters more than it looks. A future-dated news article is obvious the
moment anybody checks. A **revised** GDP figure is not: the IMF's estimate of
2017 growth published in April 2018 differs from the one published in October
2018, and from today's, and all three are the same-looking number in the same
column. Without these files a 2018 snapshot is scored on 2026's revisions of
2018, and nothing in the output would ever reveal it.

## Naming

    YYYY-MM.xls        e.g. 2018-04.xls, 2018-10.xls

`YYYY-MM` is the **edition**, not the download date. The loader reads the
vintage from the filename and ignores everything else about the file, so a
misnamed file is a silently wrong vintage — the one mistake here that does real
damage.

WEO publishes twice a year, in **April** and **October**. For the pilot window
that is:

    2016-10  2017-04  2017-10  2018-04  2018-10  2019-04  2019-10
    2020-04  2020-10  2021-04  2021-10  2022-04  2022-10  2023-04  2023-10
    2024-04  2024-10  2025-04  2025-10  2026-04

Editions before `2016-10` are not needed: the pilot starts 2016-08-03 and the
rule is "newest vintage not after the anchor", so `2016-10` covers the first
weeks via the edition before it only if you also drop `2016-04`. Adding it is
one more file and makes the first two months honest — worth doing.

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
than the level. See `SUBJECTS` in `backend/utils/history/vintage/weo.py`.

Only **historical** columns: years up to and including the edition's own year.
The WEO's forward columns are projections, and a 2018 edition's guess at 2020 is
not a fact about 2020. Loading them would put the IMF's forecast into the
evidence payload as if it were an observation.

## Running it

    python -m backend.utils.history.run weo

Idempotent — `indicator_series` is keyed on
`(country_iso2, indicator_code, freq, period)` with `as_of` carrying the
vintage, so re-running re-writes the same rows.

## If this folder is empty

The pilot still runs. Every historical payload then uses as-published-latest
annual macro, which the row's own `vintage_scheme` stamp confesses, and the
run logs a warning saying so. It is a real degradation, not a blocker — but it
is the degradation that is hardest to see later, so prefer dropping the files.
