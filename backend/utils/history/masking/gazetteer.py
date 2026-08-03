"""Which words give a country away, and what each becomes instead.

The masked run asks the scorer to judge a country it cannot name. That only
means anything if the country really is unnameable, so this module is the
deterministic first pass: every surface form that identifies one of the five
pilot countries, mapped to the functional role it plays.

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
from typing import Dict, Iterable, List, Tuple

# Bump on any change to COUNTRIES or ROLES. Stamped into every masked run's
# manifest so a score can always be traced to the mask map that produced it.
MASK_MAP_VERSION = "g1"

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

# Forms matched case-sensitively, because lowercased they are ordinary English.
# "TRY" is the lira's ISO code and also a verb; "US" is a pronoun; "BOK", "INE"
# and "real" all have innocent readings. Case-insensitive matching here would
# mangle text far from any country name and make the masked corpus unreadable.
_CASE_SENSITIVE = frozenset({
    "US", "USA", "USD", "U.S.", "U.S.A.", "BLS", "FOMC",
    "TRY", "CBRT", "TUIK", "TÜİK", "BRL", "BCB", "IBGE", "COPOM", "Copom",
    "EUR", "INE", "KRW", "BOK", "KOSTAT", "DPRK",
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
