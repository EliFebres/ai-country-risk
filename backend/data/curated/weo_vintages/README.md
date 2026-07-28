# WEO forecast vintages

One file per IMF World Economic Outlook release. `metrics.forecast_instability`
differences consecutive vintages' forecasts of the *same* target year to measure
how much the IMF keeps changing its mind about a country — uncertainty about the
order, measured rather than inferred from articles.

## Filename

`weo_<YYYY><MM>.csv` — e.g. `weo_202604.csv` for the April 2026 WEO.

**The vintage date comes from the filename.** Do not rename files after dropping
them; the loader has no other way to order the vintages, and mis-ordered
vintages produce revisions with the wrong sign.

## Schema

```csv
country_iso2,target_year,value
PT,2027,1.8
```

| Column | Meaning |
|---|---|
| `country_iso2` | ISO-3166-1 alpha-2, uppercase. |
| `target_year` | The year being forecast, `YYYY`. |
| `value` | Real GDP growth forecast for `target_year`, percent, as of this vintage. |

## Source and cadence

- **Where:** IMF World Economic Outlook database. Past releases stay available in
  the WEO archive, so a back-history of vintages can be assembled in one sitting.
- **Cadence:** twice yearly, April and October.
- **Minimum useful:** two vintages. With one, there is no revision to measure and
  `forecast_instability` returns None.

This directory ships with no vintage files — only this spec.
