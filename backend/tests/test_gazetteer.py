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
from backend.utils.masking import gazetteer as gz

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


class TestRegionsIdentifyToo:
    """What the live run of 2026-08-03 found and nothing else could.

    That snapshot passed the integrity scan with zero flagged tokens and the
    probe named Portugal at 0.9 confidence anyway, citing the Douro Valley and
    the Algarve. A wine region is not a name, a demonym, a currency or a
    capital, so no check in the project was looking at it.
    """

    def test_the_two_forms_that_actually_leaked(self):
        masked = gz.mask("Tourism in the Algarve rose and Douro Valley exports grew.", "PT")
        assert "Algarve" not in masked
        assert "Douro" not in masked

    def test_every_pilot_country_carries_regions(self):
        for iso2 in ROSTER:
            assert gz.COUNTRIES[iso2].get("regions"), iso2

    def test_numbers_survive_region_masking(self):
        # The whole point of masking: identity goes, evidence stays.
        masked = gz.mask("Algarve tourism revenue rose 8.5% to 4.2bn.", "PT")
        assert "8.5%" in masked and "4.2bn" in masked

    def test_a_region_that_is_ordinary_english_is_left_alone(self):
        # Masking "the South" or "the West" damages far more sentences than it
        # protects, and a corpus that reads as broken is its own leak.
        for form in ("the South", "the West", "the North", "the East"):
            assert form not in gz.terms("US")

    def test_a_company_that_shares_a_regions_name_survives(self):
        assert "Amazon Web Services" in gz.mask("Amazon Web Services revenue rose.", "BR")
        assert "the region" in gz.mask("Deforestation in the Amazon rainforest fell.", "BR")

    def test_a_multiword_region_is_consumed_whole(self):
        # "the Korean Peninsula" left to the name patterns becomes "the the
        # country Peninsula" — broken, and louder than the name it replaced.
        masked = gz.mask("Tension on the Korean Peninsula rose.", "KR")
        assert "Peninsula" not in masked and "Korea" not in masked

    def test_a_region_masked_payload_passes_the_scan(self):
        text = "The Algarve and the Douro Valley drew record visitors."
        assert gz.scan(gz.mask(text, "PT"), ROSTER) == []


class TestCurrencySymbols:
    """A probe run named "R$" as its reason for identifying Brazil."""

    def test_a_symbol_flush_against_its_digits_is_masked(self):
        # The case the word-boundary lookahead could never match.
        assert "R$" not in gz.mask("The price hit R$4,200 per tonne.", "BR")
        assert "€" not in gz.mask("Exports reached €2.1bn.", "PT")
        assert "₺" not in gz.mask("The lira fell to ₺18.5.", "TR")
        assert "₩" not in gz.mask("Revenue was ₩1.2tn.", "KR")

    def test_the_number_survives_the_symbol(self):
        assert "4,200" in gz.mask("The price hit R$4,200 per tonne.", "BR")
        assert "2.1bn" in gz.mask("Exports reached €2.1bn.", "PT")

    def test_the_replacement_supplies_the_space_the_symbol_did_not(self):
        # "the local currency4,200" reads as damaged, which is its own leak.
        assert "the local currency 4,200" in gz.mask("hit R$4,200 today", "BR")

    def test_dropping_the_lookahead_did_not_break_dotted_forms(self):
        # "U.S." must not match inside "U.S.S.R." and leave "the countryS.R.".
        assert "U.S.S.R." in gz.mask("The U.S.S.R. collapsed.", "US")

    def test_ordinary_english_still_survives(self):
        assert gz.mask("Real GDP rose and the party won.", "BR") == \
            "Real GDP rose and the party won."


class TestTheMapItself:
    def test_every_roster_country_has_a_gazetteer(self):
        # Not just the pilot five: the daily run masks all forty-eight, and a
        # country with no entry would be scored named without anyone noticing.
        assert set(gz.COUNTRIES) == set(gz.DEFAULT_ROSTER)
        assert len(gz.COUNTRIES) == 48

    def test_every_category_has_a_role(self):
        for iso2, entry in gz.COUNTRIES.items():
            assert set(entry) <= set(gz.ROLES), iso2

    def test_every_pilot_country_is_curated_to_its_central_bank(self):
        for iso2 in ROSTER:
            entry = gz.COUNTRIES[iso2]
            assert entry.get("names") and entry.get("central_bank"), iso2

    def test_every_country_has_a_name_and_a_currency(self):
        # The thin tier's floor. Anything below this is not masking.
        for iso2, entry in gz.COUNTRIES.items():
            assert entry.get("names") and entry.get("currency"), iso2

    def test_the_thin_tier_carries_a_demonym_or_a_multiword_name(self):
        # "Japanese" and "New Zealand" identify a country as surely as its name.
        for iso2 in gz.THIN:
            entry = gz.COUNTRIES[iso2]
            assert entry.get("demonyms") or " " in entry["names"][0], iso2

    def test_ambiguous_currency_codes_are_left_out(self):
        # "PEN" is a pen and "COP" is a police officer; masking them would
        # damage text nowhere near a country name.
        assert "PEN" not in gz.terms("PE") and "COP" not in gz.terms("CO")
        assert "Peruvian Sol" in gz.terms("PE")

    def test_plural_currency_names_are_masked(self):
        # A live run found "euros" surviving. It narrows forty-eight countries
        # to twelve, which is most of the way to naming one.
        assert "euro" not in gz.mask("traders held euros", "PT")
        assert "Dollar" not in gz.mask("held Australian Dollars", "AU")

    def test_iso_codes_are_not_pluralised(self):
        assert "EURs" not in gz.terms("PT")

    def test_a_thin_country_masks_its_own_forms(self):
        masked = gz.mask("Tokyo raised the Japanese Yen against Japan.", "JP")
        assert gz.scan(masked, ["JP"]) == []

    def test_forms_are_ordered_longest_first(self):
        lengths = [len(form) for form in gz.terms("KR")]
        assert lengths == sorted(lengths, reverse=True)

    def test_the_version_is_stamped(self):
        # The digest cache keys on masked content hashes, so a silently
        # improved gazetteer would serve digests of differently-masked text.
        assert gz.MASK_MAP_VERSION

class TestEveryoneElseFlattens:
    """The foreign pass, without which the gate fires on every real snapshot.

    A story about Portugal that mentions Germany is ordinary; a gate that
    refuses to send it is a gate somebody turns off.
    """

    def test_another_roster_country_becomes_a_flat_role(self):
        masked = gz.mask_foreign("Germany and Japan disagreed.", "PT")
        assert "Germany" not in masked and "Japan" not in masked
        assert masked.count(gz.ROLES["foreign"]) == 2

    def test_the_scored_country_survives_the_foreign_pass(self):
        # It has already had its own, richer pass; this one must not touch it.
        assert gz.mask_foreign("Portugal met Germany.", "PT").startswith("Portugal")

    def test_the_scored_country_keeps_its_specific_roles(self):
        masked = gz.mask_foreign(gz.mask("The Bank of Korea met Germany.", "KR"), "KR")
        assert "the central bank" in masked

    def test_a_swallowed_article_leaves_readable_text(self):
        # "the Bank of Korea" must not become "the another country" — text that
        # reads as damaged says something was removed.
        assert gz.mask_foreign("the Bank of Korea held rates.", "PT") == \
            "another country held rates."

    def test_the_longest_form_wins_inside_the_alternation(self):
        # Python's `|` is leftmost-first, so "Korea" listed before "North Korea"
        # would leave "North another country" behind.
        assert "North" not in gz.mask_foreign("North Korea tested a missile.", "PT")

    def test_a_masked_snapshot_passes_the_scan(self):
        text = "Turkey, Brazil and the UK met in Tokyo; Lisbon abstained."
        masked = gz.mask_foreign(gz.mask(text, "PT"), "PT")
        assert gz.scan(masked, gz.DEFAULT_ROSTER) == []

    def test_a_custom_roster_does_not_poison_the_default(self):
        assert "Germany" in gz.mask_foreign("Germany spoke.", "PT", ["PT", "BR"])
        assert "Germany" not in gz.mask_foreign("Germany spoke.", "PT")


class TestTheMapItselfContinued:
    def test_mentions_finds_a_country_by_any_form(self):
        assert gz.mentions("The Fed raised rates.", "US")
        assert gz.mentions("Ankara responded.", "TR")
        assert not gz.mentions("The central bank raised rates.", "TR")
