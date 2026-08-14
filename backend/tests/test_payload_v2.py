"""Characterization tests for ``data_retrieval.build_evidence_payload``.

This is the payload the scoring model actually reads, so a defect here is
invisible in exactly the way that matters: the JSON still looks well-formed, the
model still returns a confident score, and nobody can tell it was reasoning
about a two-year-old inflation number.

Four properties carry that risk and are pinned below.

* **Freshest value wins.** An indicator can live in the annual panel, the
  monthly latest-print table, and the series store at once. If the resolution
  picks wrong, the model scores Argentina's crisis on last year's annual
  average — which is the concrete failure the whole payload rewrite exists to
  fix.
* **Everything is stamped.** ``as_of`` and ``staleness_days`` are what let the
  model discount an old reading. Measured against the snapshot's ``as_of``,
  never the clock, so re-running an old date reports the staleness that was
  true then.
* **Missing means missing.** Absent indicators are absent from the JSON, not
  padded with nulls the model has to interpret.
* **Windows respect frequency.** A "36-month volatility" computed over ten
  annual values and six monthly ones is a number with no meaning, and the
  resolved series deliberately mixes both.

No network and no database: every store is passed in.
"""

import datetime as _dt

import pandas as pd
import pytest

from backend.utils import data_retrieval as dr


AS_OF = _dt.date(2026, 7, 27)


def panel(**columns) -> pd.DataFrame:
    """A parquet-shaped panel: a `year` column plus one column per indicator."""
    years = columns.pop("years", [2022, 2023, 2024, 2025])
    return pd.DataFrame({"year": years, **columns})


def series_rows(code, values, *, freq="M", start="2026-01", as_of=AS_OF, source="IMF CPI"):
    """`indicator_series` rows for one indicator, consecutive periods."""
    rows = []
    if freq == "M":
        year, month = (int(p) for p in start.split("-"))
        for value in values:
            rows.append({"period": f"{year:04d}-{month:02d}", "freq": "M", "value": value,
                         "as_of": as_of, "source": source})
            month += 1
            if month > 12:
                month, year = 1, year + 1
    else:
        for offset, value in enumerate(values):
            rows.append({"period": str(int(start) + offset), "freq": freq, "value": value,
                         "as_of": as_of, "source": source})
    return {code: rows}


def build(**kwargs):
    """Build a payload for PT with sensible empty defaults."""
    kwargs.setdefault("panel", pd.DataFrame())
    return dr.build_evidence_payload("PT", as_of=AS_OF, **kwargs)


class TestShape:
    def test_has_every_ledger_section(self):
        payload = build()
        for key in ("_meta", "friction_inputs", "uncertainty_inputs",
                    "information_inputs", "edge_inputs", "computed"):
            assert key in payload

    def test_meta_carries_country_asof_and_vintage(self):
        meta = build()["_meta"]
        assert meta["country"] == "PT"
        assert meta["as_of"] == "2026-07-27"
        assert meta["vintage_scheme"] == "as-published-latest"

    def test_indicators_land_in_their_declared_ledger(self):
        payload = build(
            panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]),
            series={**series_rows("IQ.SPI.OVRL", [64.0], freq="A", start="2024",
                                  source="World Bank SPI"),
                    **series_rows("IC.BUS.NDNS.ZS", [5.1], freq="A", start="2024",
                                  source="World Bank WDI"),
                    **series_rows("SE.XPD.TOTL.GD.ZS", [4.9], freq="A", start="2024",
                                  source="World Bank WDI")},
        )
        assert "Inflation (% y/y)" in payload["uncertainty_inputs"]
        assert "Statistical performance (0–100)" in payload["information_inputs"]
        assert "New business density (per 1,000 working-age)" in payload["edge_inputs"]
        assert "Government education spending (% GDP)" in payload["edge_inputs"]


class TestFreshestValueWins:
    def test_monthly_series_beats_the_annual_panel(self):
        # The headline case: the panel says 2.34 for 2025, the monthly series
        # says 3.8 for 2026-06. The model must see 3.8.
        payload = build(
            panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
            series=series_rows("CPI.YOY", [3.3, 3.5, 3.8], start="2026-04"),
        )
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.8
        assert entry["period"] == "2026-06"
        assert entry["freq"] == "M"

    def test_recent_indicator_also_competes(self):
        payload = build(
            panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
            recent={"Inflation (% y/y)": {
                "value": 3.9, "period": _dt.date(2026, 6, 30), "freq": "M",
                "unit": "% y/y", "source": "IMF",
            }},
        )
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.9
        assert entry["freq"] == "M"

    def test_same_period_resolves_to_the_newer_as_of(self):
        # Two stores hold the same period; the one we learned more recently wins.
        stale = {"period": "2026-06", "freq": "M", "value": 3.0,
                 "as_of": _dt.date(2026, 7, 1), "source": "stale"}
        fresh = {"period": "2026-06", "freq": "M", "value": 3.8,
                 "as_of": _dt.date(2026, 7, 20), "source": "fresh"}
        payload = build(series={"CPI.YOY": [stale, fresh]})
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["value"] == 3.8
        assert entry["source"] == "fresh"

    def test_an_older_monthly_print_does_not_beat_a_newer_annual(self):
        # Resolution is by the period covered, not by frequency rank.
        payload = build(
            panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0], years=[2023, 2024, 2025, 2026]),
            series=series_rows("CPI.YOY", [9.9], start="2024-01"),
        )
        assert payload["uncertainty_inputs"]["Inflation (% y/y)"]["value"] == 4.0


class TestStalenessStamps:
    def test_every_entry_carries_the_full_provenance(self):
        payload = build(panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]))
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        for key in ("value", "period", "freq", "as_of", "staleness_days", "source", "unit"):
            assert key in entry, key

    def test_staleness_is_measured_against_as_of_not_the_clock(self):
        payload = build(panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]))
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["as_of"] == "2025-12-31"
        assert entry["staleness_days"] == (AS_OF - _dt.date(2025, 12, 31)).days

    def test_rerunning_an_old_date_reports_that_date_s_staleness(self):
        # The property that makes a historical backfill honest.
        old = dr.build_evidence_payload(
            "PT", as_of=_dt.date(2026, 1, 31), panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]),
        )
        assert old["uncertainty_inputs"]["Inflation (% y/y)"]["staleness_days"] == 31

    def test_staleness_measures_the_reading_not_the_fetch(self):
        # The bug this guards: `indicator_series.as_of` is the FETCH date, so a
        # 2020 HCI vintage pulled today has as_of = today. Measuring staleness
        # against that would tell the model a six-year-old number is current.
        # Staleness must come from the period the value describes.
        #
        # HCI is also the reason a large staleness must never mean "drop it":
        # the index updates on an irregular multi-year cadence and enters as
        # slow structure. Old is the honest reading, not a defect.
        payload = build(series=series_rows(
            "HD.HCI.OVRL", [0.78], freq="A", start="2020",
            as_of=AS_OF, source="World Bank Human Capital Project",
        ))
        entry = payload["edge_inputs"]["Human Capital Index (0–1)"]
        assert entry["value"] == 0.78                  # present, not filtered out
        assert entry["as_of"] == "2026-07-27"          # when we learned it
        assert entry["staleness_days"] > 2000          # how old the reading is
        assert entry["staleness_days"] == (AS_OF - _dt.date(2020, 12, 31)).days
        # One vintage in the window: no neighbour close enough to trend against.
        assert "trend_1y" not in entry and "trend_5y" not in entry

    def test_a_monthly_print_is_stale_by_its_period_end(self):
        payload = build(series=series_rows("CPI.YOY", [3.8], start="2026-06"))
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["staleness_days"] == (AS_OF - _dt.date(2026, 6, 30)).days

    def test_the_two_dates_are_reported_separately(self):
        # Both facts are useful and they are not the same fact.
        payload = build(series=series_rows("CPI.YOY", [3.8], start="2026-06"))
        entry = payload["uncertainty_inputs"]["Inflation (% y/y)"]
        assert entry["as_of"] != f"{AS_OF - _dt.timedelta(days=entry['staleness_days'])}"


class TestMissingSeriesAreOmitted:
    def test_empty_stores_produce_empty_sections(self):
        payload = build()
        assert payload["friction_inputs"] == {}
        assert payload["information_inputs"] == {}
        assert payload["edge_inputs"] == {}

    def test_absent_indicator_has_no_key_at_all(self):
        payload = build(panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]))
        assert "Top statutory tax rate (%)" not in payload["friction_inputs"]

    def test_no_null_padding_anywhere_in_the_sections(self):
        payload = build(panel=panel(INFLATION=[1.0, 2.0, 3.0, 4.0]))
        for section in ("friction_inputs", "information_inputs", "edge_inputs"):
            for entry in payload[section].values():
                assert entry.get("value") is not None

    def test_an_unregistered_code_is_ignored_entirely(self):
        # Rows outlive their registry entry: retiring an indicator leaves its
        # history in `indicator_series` until someone deletes it, and the
        # resolver walks the registry rather than the store precisely so an
        # orphan cannot surface. Also covers the ledger-less denominator case.
        payload = build(series=series_rows("ZZ.NOT.REGISTERED", [10_600_000.0],
                                           freq="A", start="2024", source="World Bank WDI"))
        assert payload["friction_inputs"] == {}
        assert payload["information_inputs"] == {}
        assert payload["edge_inputs"] == {}
        # Uncertainty always carries the computed flag; nothing else got in.
        assert set(payload["uncertainty_inputs"]) == {"suppressed_vol_flag"}


class TestComputedBlock:
    def test_conversion_loss_and_extraction(self):
        payload = build(series={
            **series_rows("GOV_WGI_GE.EST", [1.2], freq="A", start="2024",
                          source="World Bank WGI"),
            **series_rows("GC.TAX.TOTL.GD.ZS", [34.0], freq="A", start="2024",
                          source="World Bank WDI"),
        }, panel=panel(POL_CORRUPTION=[0.18, 0.18, 0.18, 0.18]))
        assert payload["computed"]["conversion_loss"] == pytest.approx(0.22)
        assert payload["computed"]["frictional_extraction"] == pytest.approx(7.48)

    def test_real_policy_rate_uses_the_freshest_cpi(self):
        payload = build(
            panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
            series={**series_rows("CPI.YOY", [3.8], start="2026-06"),
                    **series_rows("BIS.POLICY.RATE", [2.15], start="2026-06",
                                  source="BIS CBPOL")},
        )
        # 2.15 − 3.8 (the monthly print), not 2.15 − 2.34 (the annual).
        assert payload["computed"]["real_policy_rate"] == pytest.approx(-1.65)

    def test_absent_metrics_are_omitted_from_computed(self):
        assert build()["computed"].get("frictional_extraction") is None

    def test_precommitted_share_is_marked_partial(self):
        # No free source for social protection; the metric must say so rather
        # than impute the missing half.
        payload = build(series=series_rows("GC.XPN.INTP.RV.ZS", [5.4], freq="A",
                                           start="2024", source="World Bank WDI"))
        assert payload["computed"]["precommitted_share"] == {"value": 5.4, "partial": True}


class TestFrequencyAwareWindows:
    def _mixed_cpi(self):
        """An annual history behind 30 monthly prints — what resolution produces."""
        rows = series_rows("CPI.YOY", [3.0 + 0.02 * i for i in range(30)], start="2024-01")
        return rows

    def test_volatility_ignores_the_annual_tail(self, ):
        # Annual values of 7.8/4.3/2.4 sitting behind flat monthly prints would
        # inflate a naive stdev by an order of magnitude.
        with_annual = build(panel=panel(INFLATION=[7.8, 4.3, 2.4, 2.34]),
                            series=self._mixed_cpi())
        without = build(series=self._mixed_cpi())
        assert (with_annual["computed"]["cpi_volatility_36m"]
                == without["computed"]["cpi_volatility_36m"])

    def test_thin_monthly_history_reports_no_volatility(self):
        # Six prints cannot support a 36-month window; absent beats invented.
        payload = build(series=series_rows("CPI.YOY", [3.0] * 6, start="2026-01"))
        assert "cpi_volatility_36m" not in payload["computed"]


class TestSuppressedVolFlag:
    def _managed(self, regime):
        return build(
            series={
                # A defended peg: near-zero movement, reserves draining.
                **series_rows("BIS.FX.USD", [0.92 + 0.00002 * (i % 3) for i in range(30)],
                              start="2024-01", source="BIS XRU"),
                **series_rows("RESERVES.USD", [3.0e10 - 1e8 * i for i in range(30)],
                              start="2024-01", source="IMF IRFCL (manual)"),
            },
            fx_regimes={"PT": regime},
        )

    def test_always_present_even_when_undecidable(self):
        # The model must be able to tell "not suppressed" from "we can't say".
        entry = build()["uncertainty_inputs"]["suppressed_vol_flag"]
        assert entry["value"] is None
        assert "null means one of the three inputs is unavailable" in entry["note"]

    def test_fires_for_a_defended_peg_bleeding_reserves(self):
        entry = self._managed("managed")["uncertainty_inputs"]["suppressed_vol_flag"]
        assert entry["value"] is True
        assert entry["reserves_trend_6m"] < 0

    def test_same_numbers_under_a_float_do_not_fire(self):
        assert self._managed("float")["uncertainty_inputs"]["suppressed_vol_flag"]["value"] is False

    def test_missing_regime_file_is_null_not_false(self):
        # Same volatility and reserve inputs as the firing case, but no regime
        # file: an unfilled fx_regimes.yaml must not read as "everything floats".
        payload = build(
            series={
                **series_rows("BIS.FX.USD", [0.92 + 0.00002 * (i % 3) for i in range(30)],
                              start="2024-01", source="BIS XRU"),
                **series_rows("RESERVES.USD", [3.0e10 - 1e8 * i for i in range(30)],
                              start="2024-01", source="IMF IRFCL (manual)"),
            },
            fx_regimes={},
        )
        entry = payload["uncertainty_inputs"]["suppressed_vol_flag"]
        assert entry["value"] is None
        assert entry["regime"] is None
        # The inputs it *does* have are still reported, so the model can see
        # that only the regime was missing.
        assert entry["fx_volatility_24m"] is not None


class TestTheLoaderToPayloadContract:
    """The assertion whose absence let the WEO loader run inert for its whole life.

    Nineteen editions parsed, sixteen thousand rows written, correct-looking
    counts in every log — and no value ever reached a payload, because the
    loader's target codes were not registry keys and its periods were dates
    where the reader expects bare years. Every existing test asserted the
    loader's own output shape. None asserted that a written row is a read row.

    A missing indicator is omitted from the payload rather than nulled, so a
    broken loader and a country with no data are byte-identical. This closes
    that: for every registry indicator, a stored row must arrive.
    """

    def test_every_registry_indicator_can_reach_the_payload(self):
        from backend.utils import constants
        missing = []
        for code, spec in constants.INDICATOR_REGISTRY.items():
            freq = str(spec["freq"])
            if freq == "M":
                rows = series_rows(code, [1.5, 2.5, 3.5], freq="M", start="2026-04")
            elif freq == "Q":
                rows = {code: [{"period": f"2025Q{q}", "freq": "Q", "value": 1.0 + q,
                                "as_of": AS_OF, "source": str(spec["source"])}
                               for q in (1, 2, 3)]}
            else:
                rows = series_rows(code, [1.5, 2.5, 3.5], freq=freq, start="2023")
            payload = build(series=rows)
            labels = set()
            for block in ("friction_inputs", "uncertainty_inputs",
                          "information_inputs", "edge_inputs"):
                labels |= set((payload.get(block) or {}).keys())
            if str(spec["label"]) not in labels:
                missing.append(code)
        assert not missing, (
            f"{len(missing)} indicator(s) accept a stored row and never appear in "
            f"a payload: {missing}")

    def test_a_dated_period_is_rejected_rather_than_silently_dropped(self):
        """The exact WEO defect: an annual period written as a date.

        `_period_to_date` returns None for "2023-12-31" at freq A, and the row
        vanishes. Pinned so the shape is at least visible in a test rather than
        only in a payload nobody diffed.
        """
        good = build(series={"CPI.YOY": [{"period": "2023", "freq": "A", "value": 1.0,
                                          "as_of": AS_OF, "source": "IMF WEO 2024-04"}]})
        bad = build(series={"CPI.YOY": [{"period": "2023-12-31", "freq": "A", "value": 1.0,
                                         "as_of": AS_OF, "source": "IMF WEO 2024-04"}]})
        assert any("Inflation" in k for k in (good.get("uncertainty_inputs") or {}))
        assert not any("Inflation" in k for k in (bad.get("uncertainty_inputs") or {}))

    def test_the_weo_block_arrives_with_its_edition_as_the_source(self):
        from backend.utils import constants
        rows = {}
        for code in ("WEO.NGDP_RPCH", "WEO.GGXWDG_NGDP",
                     "WEO.GGXCNL_NGDP", "WEO.BCA_NGDPD"):
            rows.update(series_rows(code, [1.0, 2.0], freq="A", start="2023",
                                    source="IMF WEO 2025-04"))
        payload = build(series=rows)
        seen = {}
        for block in ("friction_inputs", "uncertainty_inputs"):
            seen.update(payload.get(block) or {})
        for code in ("WEO.NGDP_RPCH", "WEO.GGXWDG_NGDP",
                     "WEO.GGXCNL_NGDP", "WEO.BCA_NGDPD"):
            label = str(constants.INDICATOR_REGISTRY[code]["label"])
            assert label in seen, f"{code} did not reach the payload"
            assert seen[label]["source"] == "IMF WEO 2025-04"


class TestTokenBudget:
    def test_a_fully_populated_country_stays_within_budget(self):
        # Every registry indicator present with a full monthly history — the
        # worst realistic case.
        from backend.utils import constants
        series = {}
        for code, spec in constants.INDICATOR_REGISTRY.items():
            if spec["freq"] == "M":
                series.update(series_rows(code, [1.0 + 0.01 * i for i in range(30)],
                                          start="2024-01", source=str(spec["source"])))
            else:
                series.update(series_rows(code, [1.0 + i for i in range(12)], freq="A",
                                          start="2014", source=str(spec["source"])))
        payload = build(series=series)
        import json
        estimated = len(json.dumps(payload, ensure_ascii=False)) // dr._CHARS_PER_TOKEN
        assert estimated <= dr._TOKEN_BUDGET, f"payload is ~{estimated} tokens"

    def test_long_history_is_limited_to_two_indicators(self):
        from backend.utils import constants
        series = {code: series_rows(code, [1.0 + i for i in range(12)], freq="A",
                                    start="2014", source=str(spec["source"]))[code]
                  for code, spec in constants.INDICATOR_REGISTRY.items()
                  if spec["freq"] == "A"}
        payload = build(series=series)
        with_history = [
            label
            for section in ("friction_inputs", "uncertainty_inputs",
                            "information_inputs", "edge_inputs")
            for label, entry in payload[section].items()
            if isinstance(entry, dict) and "history" in entry
        ]
        assert len(with_history) <= len(dr._LONG_HISTORY_CODES)


class TestElections:
    def test_next_scheduled_election_is_the_first_on_or_after_as_of(self):
        payload = build(elections={"PT": [
            {"date": "2024-03-10", "kind": "legislative"},
            {"date": "2026-10-04", "kind": "legislative"},
            {"date": "2027-01-10", "kind": "presidential"},
        ]})
        assert payload["_meta"]["next_scheduled_election"] == {
            "date": "2026-10-04", "kind": "legislative",
        }

    def test_no_calendar_is_null_not_omitted(self):
        assert build()["_meta"]["next_scheduled_election"] is None
