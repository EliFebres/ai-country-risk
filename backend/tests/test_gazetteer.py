"""The deterministic half of masking.

Two failure modes, and they pull in opposite directions. Mask too little and
the country is still named, which makes the whole masked series meaningless.
Mask too much and ordinary English is mangled — "real GDP" becoming "the local
currency GDP" — which teaches the scorer that something is wrong with the text
and is a louder signal than the name it was hiding.

So roughly half of these tests assert that things are masked and the other half
assert that things are left alone.

No network, no model, no database.
"""

import pytest

from backend.utils.history import config
from backend.utils.history.masking import gazetteer as gz

ROSTER = config.PILOT_ROSTER


class TestTheCountryDisappears:
    @pytest.mark.parametrize("iso2,text", [
        ("TR", "Turkey raised rates as the Turkish lira fell."),
        ("BR", "Brazil's president met Brazilian lawmakers in Brasília."),
        ("PT", "Portugal's parliament met in Lisbon."),
        ("KR", "South Korea declared martial law; the Bank of Korea responded."),
        ("US", "The United States and American allies met in Washington."),
    ])
    def test_no_form_of_the_name_survives(self, iso2, text):
        assert gz.scan(gz.mask(text, iso2), [iso2]) == []

    def test_the_role_survives_the_name(self):
        # The scorer still has to know a central bank did it.
        out = gz.mask("The Central Bank of Turkey held rates.", "TR")
        assert "central bank" in out.lower()
        assert "Turkey" not in out

    def test_an_institution_is_masked_before_the_country_inside_it(self):
        # "Bank of Korea" contains "Korea"; shortest-first would leave
        # "Bank of the country", which names the country just as loudly.
        out = gz.mask("The Bank of Korea met.", "KR")
        assert out == "The the central bank met."

    def test_the_longest_name_wins(self):
        assert "United States" not in gz.mask("the United States of America", "US")


class TestNumbersAndMagnitudesSurvive:
    """Masking is about identity, not evidence."""

    def test_the_numbers_are_untouched(self):
        out = gz.mask("Turkish inflation hit 85.5% in October 2022.", "TR")
        assert "85.5%" in out and "October 2022" in out

    def test_a_currency_amount_keeps_its_figure(self):
        out = gz.mask("The Turkish lira fell to 18.6 per dollar.", "TR")
        assert "18.6" in out and "lira" not in out.lower()


class TestOrdinaryEnglishIsNotMangled:
    """Every case here is a word that is also a country's currency or name."""

    def test_real_gdp_is_not_a_currency(self):
        text = "Real GDP grew 2.1% and in real terms wages fell."
        assert gz.mask(text, "BR") == text

    def test_winning_an_election_is_not_a_currency(self):
        text = "The party won the election and won again in 2024."
        assert gz.mask(text, "KR") == text

    def test_the_unambiguous_currency_forms_are_still_masked(self):
        assert "reais" not in gz.mask("It cost 400 reais.", "BR")
        assert "Korean won" not in gz.mask("The Korean won slipped.", "KR")

    def test_try_the_verb_survives_but_TRY_the_code_does_not(self):
        # Case-sensitivity earns its keep here: TRY is the lira's ISO code and
        # also an extremely common English verb.
        assert gz.mask("Officials will try again.", "TR") == "Officials will try again."
        assert "TRY" not in gz.mask("Quoted in TRY terms.", "TR")

    def test_us_the_pronoun_survives_but_US_the_country_does_not(self):
        assert gz.mask("It told us to wait.", "US") == "It told us to wait."
        assert "US" not in gz.mask("US tariffs rose.", "US")

    def test_latin_america_does_not_become_latin_the_country(self):
        # "America" inside a continent name, which appears constantly in the
        # coverage a US run reads.
        out = gz.mask("Latin America and South America traded with the United States.", "US")
        assert "Latin the country" not in out
        assert "the region" in out


class TestRegionStaysCoarse:
    def test_a_neighbour_is_a_neighbour_not_the_country(self):
        # Masking "North Korea" to "the country" would be both wrong and a
        # louder giveaway than the name it replaced.
        out = gz.mask("North Korea fired a missile; South Korea responded.", "KR")
        assert "a neighbouring country" in out
        assert "North the country" not in out
        assert gz.scan(out, ["KR"]) == []


class TestScanIsTheBackstop:
    def test_a_clean_payload_scans_clean(self):
        assert gz.scan("The central bank raised rates by 200bp.", ROSTER) == []

    def test_scan_catches_a_country_the_mask_pass_was_not_run_for(self):
        # An article about Turkey inside a Portugal bundle is still a leak: the
        # probe can rule countries in or out by elimination.
        assert gz.scan("Portugal and Turkey both raised rates.", ROSTER)

    def test_scan_reports_what_it_found(self):
        found = gz.scan("Brazil devalued.", ROSTER)
        assert "Brazil" in found

    def test_an_empty_payload_is_clean(self):
        assert gz.scan("", ROSTER) == [] and gz.mask("", "TR") == ""


class TestTheMapItself:
    def test_every_pilot_country_has_a_gazetteer(self):
        assert set(gz.COUNTRIES) == set(ROSTER)

    def test_every_category_has_a_role(self):
        for iso2, entry in gz.COUNTRIES.items():
            assert set(entry) <= set(gz.ROLES), iso2

    def test_every_country_has_a_name_and_a_central_bank(self):
        for iso2, entry in gz.COUNTRIES.items():
            assert entry.get("names") and entry.get("central_bank"), iso2

    def test_forms_are_ordered_longest_first(self):
        lengths = [len(form) for form in gz.terms("KR")]
        assert lengths == sorted(lengths, reverse=True)

    def test_the_version_is_stamped(self):
        # The digest cache keys on masked content hashes, so a silently
        # improved gazetteer would serve digests of differently-masked text.
        assert gz.MASK_MAP_VERSION

    def test_mentions_finds_a_country_by_any_form(self):
        assert gz.mentions("The Fed raised rates.", "US")
        assert gz.mentions("Ankara responded.", "TR")
        assert not gz.mentions("The central bank raised rates.", "TR")
