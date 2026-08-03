"""Which words give a country away, and what each becomes instead.

The masked run asks the scorer to judge a country it cannot name. That only
means anything if the country really is unnameable, so this module is the
deterministic first pass: every surface form that identifies a roster country,
mapped to the functional role it plays.

Two tiers, deliberately uneven. The five pilot countries are hand-curated down
to their statistics offices, because they are the ones whose masking gets
measured. The other forty-three get a **thin** entry — name, demonym, capital,
currency — assembled from the roster and from ``babel``, which already knows
every territory's name and currency and does not need a second copy of that
list maintained here. What the thin tier misses is exactly what ``rewrite.py``'s
model pass exists to catch, and the gap between the two tiers is one of the
things the probe measures.

Three properties the replacements are chosen for:

* **Numbers survive.** "inflation hit 85.5%" stays "inflation hit 85.5%".
  Masking is about identity, not about evidence — a masked run that also lost
  the magnitudes would be measuring the wrong thing entirely.
* **Roles survive.** "the Central Bank of the Republic of Türkiye" becomes
  "the central bank", not a hole. The scorer still needs to know that a central
  bank did the thing.
* **Region stays coarse.** A country becomes "the country"; its neighbours
  become "a neighbouring country". Turning "North Korea" into "the country"
  would be both wrong and a giveaway.

This is a first pass, not the whole job. It cannot mask what it does not list —
the politicians, parties and companies of ten years of news, which change every
election. Those are what ``rewrite.py``'s model pass is for, and what
``scan`` exists to catch before anything is sent.

``MASK_MAP_VERSION`` is stamped into every masked manifest. Change the data,
change the version: the digest cache keys on masked content hashes, and a
silently-improved gazetteer would serve digests of differently-masked text.
"""

import re
from typing import Dict, Iterable, List, Optional, Tuple

from babel import Locale, numbers

from backend.utils import constants

# Bump on any change to COUNTRIES, THIN or ROLES. Stamped into every masked
# run's manifest so a score can always be traced to the mask map that produced
# it.
MASK_MAP_VERSION = "g3"

# What each category of proper noun becomes. The scorer reads these, so they
# read as English rather than as placeholders — "the central bank" carries the
# institutional meaning that "[INSTITUTION_4]" throws away.
ROLES: Dict[str, str] = {
    "names": "the country",
    "demonyms": "the country's",
    "people": "the country's citizens",
    "currency": "the local currency",
    "capital": "the capital",
    "cities": "a major city",
    "central_bank": "the central bank",
    "statistics_office": "the national statistics office",
    "neighbors": "a neighbouring country",
    "regions": "the region",
    # Every roster country that is *not* the one being scored. A payload naming
    # a different roster country is the same leak wearing a hat — it lets the
    # probe rule countries out by elimination — but it is not the scored
    # country's central bank either, so it collapses to one flat role.
    "foreign": "another country",
}

# Deliberately NOT masked, though each names a currency of a pilot country:
# "real", "won" and bare "dollar"-less usages are ordinary English words far
# more often than they are money. Masking "real" would turn "real GDP" into
# "the local currency GDP" and "in real terms" into nonsense; masking "won"
# would turn "the party won the election" into gibberish. A masked corpus that
# reads as damaged teaches the scorer that something is wrong with the text,
# which is a worse leak than the currency name it was hiding.
#
# The unambiguous forms of both currencies ARE masked ("Brazilian real",
# "reais", "BRL", "Korean won", "KRW"), and anything these miss is what the
# model rewrite pass and the identifiability probe exist to measure.
UNMASKED_BY_DESIGN: Tuple[str, ...] = ("real", "won")

# Surface forms per country. Ordered within a category longest-first at build
# time, so "United States of America" is consumed before "United States" and
# "South Korean" before "Korea" — a shorter form matching first would leave a
# fragment behind, and a fragment is a leak.
COUNTRIES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "US": {
        "names": ("United States of America", "the United States", "United States",
                  "U.S.A.", "USA", "U.S.", "US", "America"),
        "demonyms": ("American",),
        "people": ("Americans",),
        "currency": ("U.S. dollar", "US dollar", "dollar", "USD"),
        "capital": ("Washington, D.C.", "Washington D.C.", "Washington DC", "Washington"),
        "cities": ("New York City", "New York", "Los Angeles", "Chicago", "Houston",
                   "San Francisco", "Boston", "Philadelphia", "Atlanta"),
        "central_bank": ("Federal Reserve Board", "Federal Reserve", "the Fed", "FOMC"),
        "statistics_office": ("Bureau of Labor Statistics", "BLS",
                              "Bureau of Economic Analysis", "Census Bureau"),
        # Listed so "America" cannot eat the continent it is part of. Without
        # these, "Latin America" masks to "Latin the country" — a corruption
        # that appears constantly in exactly the coverage the US run reads.
        "regions": ("Latin America", "South America", "North America",
                    "Central America"),
    },
    "TR": {
        "names": ("Republic of Türkiye", "Republic of Turkiye", "Turkey", "Türkiye",
                  "Turkiye"),
        "demonyms": ("Turkish",),
        "people": ("Turks",),
        "currency": ("Turkish lira", "lira", "TRY"),
        "capital": ("Ankara",),
        "cities": ("Istanbul", "Izmir", "İzmir", "Bursa", "Antalya", "Adana"),
        "central_bank": ("Central Bank of the Republic of Türkiye",
                         "Central Bank of the Republic of Turkey",
                         "Central Bank of Turkey", "CBRT"),
        "statistics_office": ("Turkish Statistical Institute", "TurkStat", "TÜİK", "TUIK"),
    },
    "BR": {
        "names": ("Federative Republic of Brazil", "Brazil", "Brasil"),
        "demonyms": ("Brazilian",),
        "people": ("Brazilians",),
        # No bare "real" — see UNMASKED_BY_DESIGN.
        "currency": ("Brazilian real", "reais", "BRL"),
        "capital": ("Brasília", "Brasilia"),
        "cities": ("São Paulo", "Sao Paulo", "Rio de Janeiro", "Belo Horizonte",
                   "Salvador", "Curitiba", "Recife", "Porto Alegre"),
        "central_bank": ("Banco Central do Brasil", "Central Bank of Brazil", "BCB",
                         "Copom", "COPOM"),
        "statistics_office": ("Brazilian Institute of Geography and Statistics", "IBGE"),
    },
    "PT": {
        "names": ("Portuguese Republic", "Portugal"),
        "demonyms": ("Portuguese",),
        "currency": ("euro", "EUR"),
        "capital": ("Lisbon", "Lisboa"),
        "cities": ("Porto", "Oporto", "Braga", "Coimbra", "Faro", "Funchal"),
        "central_bank": ("Banco de Portugal", "Bank of Portugal"),
        "statistics_office": ("Instituto Nacional de Estatística",
                              "Statistics Portugal", "INE"),
    },
    "KR": {
        "names": ("Republic of Korea", "South Korea", "Korea"),
        "demonyms": ("South Korean", "Korean"),
        "people": ("South Koreans", "Koreans"),
        # No bare "won" — see UNMASKED_BY_DESIGN.
        "currency": ("South Korean won", "Korean won", "KRW"),
        "capital": ("Seoul",),
        "cities": ("Busan", "Pusan", "Incheon", "Daegu", "Daejeon", "Gwangju", "Ulsan"),
        "central_bank": ("Bank of Korea", "BOK"),
        "statistics_office": ("Statistics Korea", "KOSTAT", "Kostat"),
        # Listed before the names above at build time, so "North Korea" is never
        # eaten by "Korea" and turned into "North the country" — which would be
        # both wrong and a louder giveaway than the name it replaced.
        "neighbors": ("North Korea", "Democratic People's Republic of Korea", "DPRK",
                      "Pyongyang", "North Korean"),
    },
}

# --- The thin tier: the other forty-three -----------------------------------
# Demonym, the plural noun for the people, the capital, and any alias a
# newsroom uses more often than the roster's own name. Everything else a thin
# entry needs — the country name and the currency — comes from `babel` below,
# because that list is already maintained by somebody else and a second copy
# here would rot.
#
# Written out rather than derived: no library ships demonyms, and guessing them
# from the name ("Netherlands" → "Netherlandish") produces exactly the kind of
# almost-right text that teaches a scorer something is wrong with the corpus.
#
# Cities beyond the capital are deliberately absent. A capital is named in
# nearly every political story about a country; a second city is named in few,
# and the model rewrite pass covers what this misses.
THIN: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "AU": {"demonyms": ("Australian",), "people": ("Australians",), "capital": ("Canberra",)},
    "AT": {"demonyms": ("Austrian",), "people": ("Austrians",), "capital": ("Vienna",)},
    "BE": {"demonyms": ("Belgian",), "people": ("Belgians",), "capital": ("Brussels",)},
    "CA": {"demonyms": ("Canadian",), "people": ("Canadians",), "capital": ("Ottawa",)},
    "DK": {"demonyms": ("Danish",), "people": ("Danes",), "capital": ("Copenhagen",)},
    "FI": {"demonyms": ("Finnish",), "people": ("Finns",), "capital": ("Helsinki",)},
    "FR": {"demonyms": ("French",), "people": ("Frenchmen",), "capital": ("Paris",)},
    "DE": {"demonyms": ("German",), "people": ("Germans",), "capital": ("Berlin",)},
    "HK": {"demonyms": ("Hong Kong",), "people": ("Hong Kongers",),
           "names": ("Hong Kong SAR China", "Hong Kong SAR", "Hong Kong", "Hongkong")},
    "IE": {"demonyms": ("Irish",), "people": ("Irishmen",), "capital": ("Dublin",)},
    "IL": {"demonyms": ("Israeli",), "people": ("Israelis",),
           "capital": ("Jerusalem", "Tel Aviv")},
    "IT": {"demonyms": ("Italian",), "people": ("Italians",), "capital": ("Rome",)},
    "JP": {"demonyms": ("Japanese",), "capital": ("Tokyo",)},
    "NL": {"demonyms": ("Dutch",), "people": ("Dutchmen",),
           "names": ("the Netherlands", "Netherlands", "Holland"),
           "capital": ("Amsterdam", "The Hague")},
    "NZ": {"names": ("New Zealand",), "people": ("New Zealanders",),
           "capital": ("Wellington",)},
    "NO": {"demonyms": ("Norwegian",), "people": ("Norwegians",), "capital": ("Oslo",)},
    "SG": {"demonyms": ("Singaporean",), "people": ("Singaporeans",)},
    "ES": {"demonyms": ("Spanish",), "people": ("Spaniards",), "capital": ("Madrid",)},
    "SE": {"demonyms": ("Swedish",), "people": ("Swedes",), "capital": ("Stockholm",)},
    "CH": {"demonyms": ("Swiss",), "capital": ("Bern", "Berne")},
    "GB": {"demonyms": ("British",), "people": ("Britons",), "capital": ("London",),
           "names": ("the United Kingdom", "United Kingdom", "Great Britain",
                     "Britain", "U.K.", "UK", "England", "Scotland", "Wales",
                     "Northern Ireland")},
    "CL": {"demonyms": ("Chilean",), "people": ("Chileans",), "capital": ("Santiago",)},
    "CN": {"demonyms": ("Chinese",), "capital": ("Beijing", "Peking"),
           "names": ("People's Republic of China", "mainland China", "China", "PRC")},
    "CO": {"demonyms": ("Colombian",), "people": ("Colombians",),
           "capital": ("Bogotá", "Bogota")},
    "CZ": {"demonyms": ("Czech",), "people": ("Czechs",), "capital": ("Prague",),
           "names": ("the Czech Republic", "Czech Republic", "Czechia")},
    "EG": {"demonyms": ("Egyptian",), "people": ("Egyptians",), "capital": ("Cairo",)},
    "GR": {"demonyms": ("Greek",), "people": ("Greeks",), "capital": ("Athens",)},
    "HU": {"demonyms": ("Hungarian",), "people": ("Hungarians",), "capital": ("Budapest",)},
    "IN": {"demonyms": ("Indian",), "people": ("Indians",), "capital": ("New Delhi", "Delhi")},
    "ID": {"demonyms": ("Indonesian",), "people": ("Indonesians",), "capital": ("Jakarta",)},
    "KW": {"demonyms": ("Kuwaiti",), "people": ("Kuwaitis",), "capital": ("Kuwait City",)},
    "MY": {"demonyms": ("Malaysian",), "people": ("Malaysians",),
           "capital": ("Kuala Lumpur",)},
    "MX": {"demonyms": ("Mexican",), "people": ("Mexicans",), "capital": ("Mexico City",)},
    "PE": {"demonyms": ("Peruvian",), "people": ("Peruvians",), "capital": ("Lima",)},
    "PH": {"demonyms": ("Filipino", "Philippine"), "people": ("Filipinos",),
           "capital": ("Manila",), "names": ("the Philippines", "Philippines")},
    "PL": {"demonyms": ("Polish",), "people": ("Poles",), "capital": ("Warsaw",)},
    "QA": {"demonyms": ("Qatari",), "people": ("Qataris",), "capital": ("Doha",)},
    "SA": {"demonyms": ("Saudi",), "people": ("Saudis",), "capital": ("Riyadh",),
           "names": ("Kingdom of Saudi Arabia", "Saudi Arabia", "KSA")},
    "ZA": {"demonyms": ("South African",), "people": ("South Africans",),
           "capital": ("Pretoria", "Cape Town", "Johannesburg")},
    "TW": {"demonyms": ("Taiwanese",), "capital": ("Taipei",),
           "names": ("Republic of China", "Chinese Taipei", "Taiwan")},
    "TH": {"demonyms": ("Thai",), "people": ("Thais",), "capital": ("Bangkok",)},
    "AE": {"demonyms": ("Emirati",), "people": ("Emiratis",),
           "capital": ("Abu Dhabi", "Dubai"),
           "names": ("United Arab Emirates", "the UAE", "UAE", "U.A.E.")},
    "RU": {"demonyms": ("Russian",), "people": ("Russians",), "capital": ("Moscow",),
           "names": ("Russian Federation", "Russia")},
}

# ISO currency codes that read as ordinary uppercase English or as a common
# acronym, and are therefore left out of the thin tier entirely. "PEN" is a pen,
# "COP" is a police officer and a climate summit, "PHP" is a programming
# language, "SAR" is search-and-rescue *and* "special administrative region",
# "CAD" is computer-aided design. Same judgement as UNMASKED_BY_DESIGN: the
# qualified name ("Peruvian Sol") still masks, and a corpus mangled by a
# three-letter false positive teaches the scorer more than the code hid.
AMBIGUOUS_CODES: Tuple[str, ...] = ("PEN", "COP", "PHP", "SAR", "CAD")


def _thin_entry(iso2: str) -> Dict[str, Tuple[str, ...]]:
    """One non-pilot country's surface forms, from THIN plus babel.

    Names come from the roster, from babel's territory list and from any alias
    in THIN, deduplicated. The currency contributes its qualified name and its
    ISO code — never a bare "dollar" or "pound", which half the roster shares.
    """
    entry = {k: tuple(v) for k, v in THIN.get(iso2, {}).items()}

    names = list(entry.get("names", ()))
    for name in (_ROSTER_NAMES.get(iso2), _BABEL_TERRITORIES.get(iso2)):
        if name and name not in names:
            names.append(name)
    entry["names"] = tuple(names)

    codes = numbers.get_territory_currencies(iso2) or []
    currency = []
    for code in codes[:1]:
        currency.append(numbers.get_currency_name(code, locale="en"))
        if code not in AMBIGUOUS_CODES:
            currency.append(code)
    if currency:
        entry["currency"] = tuple(currency)
    return entry


_BABEL_TERRITORIES: Dict[str, str] = dict(Locale("en").territories)
_ROSTER_NAMES: Dict[str, str] = {e["iso2"]: e["name"] for e in constants.COUNTRY_ROSTER}

# Curated first so a pilot country is never overwritten by its thin twin.
COUNTRIES.update({
    entry["iso2"]: _thin_entry(entry["iso2"])
    for entry in constants.COUNTRY_ROSTER
    if entry["iso2"] not in COUNTRIES
})

# The roster `scan` and `mask_foreign` assume when nobody says otherwise: the
# live one, not the pilot's five. Masking is production now, and a gate that
# defaults to checking five countries out of forty-eight is a gate that passes
# forty-three leaks.
DEFAULT_ROSTER: Tuple[str, ...] = tuple(e["iso2"] for e in constants.COUNTRY_ROSTER)

# Forms matched case-sensitively, because lowercased they are ordinary English.
# "TRY" is the lira's ISO code and also a verb; "US" is a pronoun; "BOK", "INE"
# and "real" all have innocent readings. Case-insensitive matching here would
# mangle text far from any country name and make the masked corpus unreadable.
#
# Every ISO currency code joins them for the same reason, as do the handful of
# thin-tier forms that are ordinary words in lower case: "poles", "wales",
# "turks", "chad".
_CASE_SENSITIVE = frozenset({
    "US", "USA", "USD", "U.S.", "U.S.A.", "BLS", "FOMC",
    "TRY", "CBRT", "TUIK", "TÜİK", "BRL", "BCB", "IBGE", "COPOM", "Copom",
    "EUR", "INE", "KRW", "BOK", "KOSTAT", "DPRK",
    "UK", "U.K.", "UAE", "U.A.E.", "PRC", "KSA", "Poles", "Wales",
} | {
    code
    for entry in constants.COUNTRY_ROSTER
    for code in (numbers.get_territory_currencies(entry["iso2"]) or ())
})


def _forms(iso2: str) -> List[Tuple[str, str]]:
    """Every surface form for one country with its replacement, longest first.

    Longest-first across *all* categories rather than within each, because the
    collisions that matter are cross-category: "Bank of Korea" (central bank)
    contains "Korea" (name), and "North Korea" (neighbour) contains it too.
    """
    entry = COUNTRIES[iso2]
    pairs = [(form, ROLES[category])
             for category, forms in entry.items()
             for form in forms]
    # Plural currency names, which a live run found surviving: a masked body
    # still saying "euros" narrows forty-eight countries to twelve. ISO codes
    # are excluded — "EURs" is not a word anyone writes.
    pairs += [(form + "s", ROLES["currency"])
              for form in entry.get("currency", ())
              if not form.endswith("s") and not form.isupper()]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def _compile(form: str) -> re.Pattern:
    """One surface form as a bounded pattern.

    Lookarounds rather than ``\\b`` because several forms end in a period
    ("U.S.") where a word boundary sits in the wrong place, and several contain
    one ("Washington, D.C.").
    """
    flags = 0 if form in _CASE_SENSITIVE else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(form)}(?!\w)", flags)


# Built once per country: this runs over every article body in a 2,600-snapshot
# backfill, and recompiling forty patterns per call would dominate the masking.
_PATTERNS: Dict[str, List[Tuple[re.Pattern, str, str]]] = {
    iso2: [(_compile(form), form, role) for form, role in _forms(iso2)]
    for iso2 in COUNTRIES
}


_FOREIGN_CACHE: Dict[Tuple[str, Tuple[str, ...]], Tuple[re.Pattern, ...]] = {}


def _foreign_patterns(iso2: str, roster: Optional[Iterable[str]] = None
                      ) -> Tuple[re.Pattern, ...]:
    """Two alternations — case-sensitive and not — over everyone but ``iso2``.

    Built lazily and cached: the default roster is the only one the daily run
    ever asks for, so the forty-eight patterns cost one compile each over the
    life of the process, and a test passing its own roster does not poison them.
    """
    key = (iso2, tuple(roster) if roster is not None else DEFAULT_ROSTER)
    if key not in _FOREIGN_CACHE:
        forms = sorted(
            (form for other in key[1] if other != iso2 and other in COUNTRIES
             for form, _ in _forms(other)),
            key=len, reverse=True)
        # The optional article is swallowed rather than left behind: without it
        # "the Bank of Korea" becomes "the another country", and text that
        # reads as damaged tells the scorer something was removed — which is
        # the thing masking is trying not to say.
        _FOREIGN_CACHE[key] = tuple(
            re.compile("(?<!\\w)(?:[Tt]he )?(?:"
                       + "|".join(re.escape(f) for f in group) + ")(?!\\w)", flags)
            for group, flags in (
                ([f for f in forms if f in _CASE_SENSITIVE], 0),
                ([f for f in forms if f not in _CASE_SENSITIVE], re.IGNORECASE),
            ) if group
        )
    return _FOREIGN_CACHE[key]


def terms(iso2: str) -> Tuple[str, ...]:
    """Every surface form this gazetteer knows for a country, longest first."""
    return tuple(form for form, _ in _forms(iso2))


def mentions(text: str, iso2: str) -> bool:
    """Does this text name the country in any form the gazetteer knows?

    Used both ways: to *find* articles about a country in a bulk archive that
    cannot be queried per country, and to prove a masked payload no longer
    names one.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern, _, _ in _PATTERNS[iso2])


def mask(text: str, iso2: str) -> str:
    """Replace every known surface form with the role it plays.

    Deterministic and offline — no model, no network. What it cannot know it
    leaves alone, which is why :func:`scan` runs afterwards rather than
    instead.
    """
    if not text:
        return text
    for pattern, _, role in _PATTERNS[iso2]:
        text = pattern.sub(role, text)
    return text


def mask_foreign(text: str, iso2: str, roster: Optional[Iterable[str]] = None) -> str:
    """Collapse every *other* roster country to one flat role.

    Runs after :func:`mask`, never instead of it: the scored country needs its
    real roles ("the central bank"), and everyone else needs to disappear.

    One compiled alternation per scored country rather than a loop over
    forty-seven gazetteers, because this runs over every article body in the
    payload and forty-seven times ten substitutions per field is the difference
    between a run and a wait. Alternatives are sorted longest-first — Python's
    ``|`` is leftmost-*first*, not leftmost-longest, so "Korea" listed before
    "North Korea" would leave "North another country" behind.
    """
    if not text:
        return text
    for pattern in _foreign_patterns(iso2, roster):
        text = pattern.sub(ROLES["foreign"], text)
    return text


def scan(text: str, roster: Iterable[str]) -> List[str]:
    """Every roster country name still present in a payload about to be sent.

    The integrity check, and the reason masking is trustworthy at all: the
    gazetteer is a list somebody wrote, so the run does not get to assume it
    was complete. Scans the whole roster, not just the country being scored —
    an article that names a *different* pilot country is still a leak, because
    the probe could rule it in or out by elimination.

    Returns:
        The forms found, in the order they were looked for. Empty means clean.
    """
    if not text:
        return []
    found: List[str] = []
    for iso2 in roster:
        for pattern, form, _ in _PATTERNS[iso2]:
            if pattern.search(text):
                found.append(form)
    return found
