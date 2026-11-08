"""
The names that would tell the scorer which country it is reading about.

The scorer reads an anonymous payload (step 5e, Fix 8): the digests and the
full texts are rewritten in role language by gpt-4o-mini, and this module is
the check in code that the rewrite held. For every roster country it holds the
names and aliases, the codes, the demonyms, the capital and largest cities,
the currency's names, symbols and ISO code, and a few proper nouns that name
the country as surely as its name does (its parliament, its central bank's
nickname, its national oil company, the neighbour its news is about). The
data is here, in the file, so a run can be reproduced from the commit alone.

For a subject country the terms are all of its own, plus the names and
aliases of the other roster countries, which the masked text writes by their
relation ("a neighbouring country", "a major power"). Other countries'
demonyms, cities and currencies are not terms: "American troops" in a Kuwaiti
digest names a major power, not Kuwait, and the digest prompt handles it.

Matching is on whole words, case-insensitive, except for codes (`US`, `KWD`,
`Fed`), which are case-sensitive so that "us" and "fed up" pass. Currency
symbols and short codes that sit flush against digits (`€250m`, `KD602m`,
`R$1.2bn`) have no trailing boundary. "US dollars" and "USD" are what the
masked text is told to write for money, so they are exempt unless the subject
is the United States.

Its known limit, accepted in the brief: this removes names, not events. A coup
in July 2016 is Turkey whether or not Turkey is written.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.util.hashing import content_hash

__all__ = ["GAZETTEER", "GAZETTEER_VERSION", "MaskLeak", "terms_for", "scan",
           "payload_leaks", "redact", "assert_clean", "SKIP_KEYS", "EXEMPT_PHRASES"]


class MaskLeak(RuntimeError):
    """A gazetteer term survived masking in a payload about to be sent."""


#: Keys whose string values are never scanned. Publishers are outlets, not
#: the country, and the brief keeps them. The structural facts keep the
#: income group, region and currency regime as the brief says, and "North
#: America" or "euro area" would otherwise read as a leak.
SKIP_KEYS: Tuple[str, ...] = ("publisher", "structural_facts")

#: What the masked text is told to write for money in dollars. Removed from
#: the text before scanning unless the subject is the United States.
EXEMPT_PHRASES: Tuple[str, ...] = ("US dollars", "US dollar", "U.S. dollars", "U.S. dollar",
                                   "US$", "USD")

_EURO = {"currency": ["euro", "euros"], "symbols": ["€", "EUR"]}

#: Per roster country. `names` and `aliases` are the forms the country is
#: written as; `codes` are case-sensitive abbreviations; `extra` are proper
#: nouns that identify the country without naming it (its parliament, its
#: national companies, the neighbour its news is about). Cities are the five
#: largest (with spelling variants), the capital listed apart.
GAZETTEER: Dict[str, Dict[str, Any]] = {
    "AU": {"names": ["Australia", "Commonwealth of Australia"], "aliases": [],
           "codes": ["AUS"], "demonyms": ["Australian", "Australians", "Aussie", "Aussies"],
           "capital": "Canberra",
           "cities": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide"],
           "currency": ["Australian dollar", "Australian dollars", "dollar", "dollars"],
           "symbols": ["A$", "AU$", "AUD"], "extra": ["RBA", "ASX", "Canberra"]},
    "AT": {"names": ["Austria", "Republic of Austria", "Österreich"], "aliases": [],
           "codes": ["AUT"], "demonyms": ["Austrian", "Austrians"], "capital": "Vienna",
           "cities": ["Vienna", "Wien", "Graz", "Linz", "Salzburg", "Innsbruck"],
           **_EURO, "extra": ["Nationalrat", "ÖVP", "FPÖ", "SPÖ"]},
    "BE": {"names": ["Belgium", "Kingdom of Belgium"], "aliases": [],
           "codes": ["BEL"], "demonyms": ["Belgian", "Belgians"], "capital": "Brussels",
           "cities": ["Brussels", "Bruxelles", "Antwerp", "Ghent", "Charleroi", "Liège", "Liege", "Bruges"],
           **_EURO, "extra": ["Flanders", "Flemish", "Wallonia", "Walloon"]},
    "CA": {"names": ["Canada"], "aliases": [],
           "codes": ["CAN"], "demonyms": ["Canadian", "Canadians"], "capital": "Ottawa",
           "cities": ["Toronto", "Montreal", "Montréal", "Vancouver", "Calgary", "Edmonton", "Ottawa"],
           "currency": ["Canadian dollar", "Canadian dollars", "dollar", "dollars", "loonie"],
           "symbols": ["C$", "CA$", "CAD"], "extra": ["Ontario", "Quebec", "Québec", "Alberta", "TSX"]},
    "DK": {"names": ["Denmark", "Kingdom of Denmark", "Danmark"], "aliases": [],
           "codes": ["DNK"], "demonyms": ["Danish", "Dane", "Danes"], "capital": "Copenhagen",
           "cities": ["Copenhagen", "København", "Aarhus", "Odense", "Aalborg", "Esbjerg"],
           "currency": ["krone", "kroner", "Danish krone"], "symbols": ["DKK"],
           "extra": ["Folketing", "Greenland", "Faroe"]},
    "FI": {"names": ["Finland", "Republic of Finland", "Suomi"], "aliases": [],
           "codes": ["FIN"], "demonyms": ["Finnish", "Finn", "Finns"], "capital": "Helsinki",
           "cities": ["Helsinki", "Espoo", "Tampere", "Vantaa", "Oulu", "Turku"],
           **_EURO, "extra": ["Eduskunta"]},
    "FR": {"names": ["France", "French Republic"], "aliases": [],
           "codes": ["FRA"], "demonyms": ["French"], "capital": "Paris",
           "cities": ["Paris", "Marseille", "Lyon", "Toulouse", "Nice", "Bordeaux"],
           **_EURO, "extra": ["Élysée", "Elysée", "Elysee", "Matignon", "Assemblée nationale",
                              "Rassemblement National", "CAC 40"]},
    "DE": {"names": ["Germany", "Federal Republic of Germany", "Deutschland"], "aliases": [],
           "codes": ["DEU", "GER"], "demonyms": ["German", "Germans"], "capital": "Berlin",
           "cities": ["Berlin", "Hamburg", "Munich", "München", "Cologne", "Köln", "Frankfurt"],
           **_EURO, "extra": ["Bundestag", "Bundesrat", "Bundesbank", "Bavaria", "Bavarian",
                              "CDU", "CSU", "SPD", "AfD", "DAX"]},
    "HK": {"names": ["Hong Kong", "Hong Kong SAR", "Hong Kong SAR, China", "Hongkong"], "aliases": [],
           "codes": ["HK", "HKG", "HKSAR"],
           "demonyms": ["Hong Konger", "Hong Kongers", "Hongkonger", "Hongkongers"],
           "capital": "Hong Kong",
           "cities": ["Kowloon", "Sha Tin", "Tuen Mun", "Mong Kok", "Causeway Bay", "Lantau",
                      "Victoria Harbour", "Tsim Sha Tsui"],
           "currency": ["Hong Kong dollar", "Hong Kong dollars", "dollar", "dollars"],
           "symbols": ["HK$", "HKD"],
           "extra": ["Beijing", "Chinese", "HKMA", "Hang Seng", "LegCo", "Legislative Council",
                     "Chief Executive", "mainland", "Mainland"]},
    "IE": {"names": ["Ireland", "Republic of Ireland", "Éire", "Eire"], "aliases": [],
           "codes": ["IRL"], "demonyms": ["Irish"], "capital": "Dublin",
           "cities": ["Dublin", "Cork", "Limerick", "Galway", "Waterford"],
           **_EURO, "extra": ["Dáil", "Dail", "Oireachtas", "Taoiseach", "Sinn Féin", "Sinn Fein",
                              "Fianna Fáil", "Fianna Fail", "Fine Gael"]},
    "IL": {"names": ["Israel", "State of Israel"], "aliases": [],
           "codes": ["ISR"], "demonyms": ["Israeli", "Israelis"], "capital": "Jerusalem",
           "cities": ["Jerusalem", "Tel Aviv", "Haifa", "Rishon LeZion", "Petah Tikva", "Beersheba"],
           "currency": ["shekel", "shekels", "new shekel"], "symbols": ["₪", "NIS", "ILS"],
           "extra": ["Knesset", "Gaza", "West Bank", "Hamas", "Hezbollah", "IDF", "Likud", "Mossad"]},
    "IT": {"names": ["Italy", "Italian Republic", "Italia"], "aliases": [],
           "codes": ["ITA"], "demonyms": ["Italian", "Italians"], "capital": "Rome",
           "cities": ["Rome", "Milan", "Milano", "Naples", "Napoli", "Turin", "Torino", "Palermo", "Genoa"],
           **_EURO, "extra": ["Quirinale", "Palazzo Chigi", "Fratelli d'Italia", "Lega", "BTP", "BTPs"]},
    "JP": {"names": ["Japan", "Nippon", "Nihon"], "aliases": [],
           "codes": ["JPN"], "demonyms": ["Japanese"], "capital": "Tokyo",
           "cities": ["Tokyo", "Yokohama", "Osaka", "Nagoya", "Sapporo", "Fukuoka"],
           "currency": ["yen"], "symbols": ["¥", "JPY", "JP¥"],
           "extra": ["Nikkei", "Kasumigaseki", "Nagatacho", "LDP", "Komeito", "Okinawa"]},
    "NL": {"names": ["Netherlands", "the Netherlands", "Holland", "Kingdom of the Netherlands"],
           "aliases": [], "codes": ["NLD"], "demonyms": ["Dutch"], "capital": "Amsterdam",
           "cities": ["Amsterdam", "Rotterdam", "The Hague", "Den Haag", "Utrecht", "Eindhoven"],
           **_EURO, "extra": ["Tweede Kamer", "PVV", "VVD"]},
    "NZ": {"names": ["New Zealand", "Aotearoa"], "aliases": [],
           "codes": ["NZ", "NZL"], "demonyms": ["New Zealander", "New Zealanders", "Kiwi", "Kiwis"],
           "capital": "Wellington",
           "cities": ["Auckland", "Wellington", "Christchurch", "Hamilton", "Tauranga", "Dunedin"],
           "currency": ["New Zealand dollar", "New Zealand dollars", "dollar", "dollars"],
           "symbols": ["NZ$", "NZD"], "extra": ["RBNZ", "Māori", "Maori", "Beehive"]},
    "NO": {"names": ["Norway", "Kingdom of Norway", "Norge"], "aliases": [],
           "codes": ["NOR"], "demonyms": ["Norwegian", "Norwegians"], "capital": "Oslo",
           "cities": ["Oslo", "Bergen", "Trondheim", "Stavanger", "Drammen"],
           "currency": ["krone", "kroner", "Norwegian krone"], "symbols": ["NOK"],
           "extra": ["Storting", "Norges Bank", "Equinor", "North Sea"]},
    "PT": {"names": ["Portugal", "Portuguese Republic"], "aliases": [],
           "codes": ["PRT"], "demonyms": ["Portuguese"], "capital": "Lisbon",
           "cities": ["Lisbon", "Lisboa", "Porto", "Oporto", "Braga", "Coimbra", "Funchal", "Amadora"],
           **_EURO, "extra": ["Assembleia da República", "Banco de Portugal", "Madeira", "Azores",
                              "Açores", "Chega", "PSD", "Algarve", "Tagus", "Tejo"]},
    "SG": {"names": ["Singapore", "Republic of Singapore"], "aliases": [],
           "codes": ["SG", "SGP"], "demonyms": ["Singaporean", "Singaporeans"], "capital": "Singapore",
           "cities": ["Changi", "Jurong", "Marina Bay", "Sentosa", "Orchard Road"],
           "currency": ["Singapore dollar", "Singapore dollars", "dollar", "dollars"],
           "symbols": ["S$", "SG$", "SGD"], "extra": ["Temasek", "GIC", "MAS", "PAP", "HDB", "CPF"]},
    "ES": {"names": ["Spain", "Kingdom of Spain", "España"], "aliases": [],
           "codes": ["ESP"], "demonyms": ["Spanish", "Spaniard", "Spaniards"], "capital": "Madrid",
           "cities": ["Madrid", "Barcelona", "Valencia", "Seville", "Sevilla", "Zaragoza", "Málaga", "Malaga"],
           **_EURO, "extra": ["Catalonia", "Catalan", "Basque", "Cortes", "Moncloa", "PSOE", "Vox", "IBEX"]},
    "SE": {"names": ["Sweden", "Kingdom of Sweden", "Sverige"], "aliases": [],
           "codes": ["SWE"], "demonyms": ["Swedish", "Swede", "Swedes"], "capital": "Stockholm",
           "cities": ["Stockholm", "Gothenburg", "Göteborg", "Malmö", "Malmo", "Uppsala", "Västerås"],
           "currency": ["krona", "kronor", "Swedish krona"], "symbols": ["SEK"],
           "extra": ["Riksbank", "Riksdag"]},
    "CH": {"names": ["Switzerland", "Swiss Confederation", "Schweiz", "Suisse", "Svizzera"], "aliases": [],
           "codes": ["CHE"], "demonyms": ["Swiss"], "capital": "Bern",
           "cities": ["Bern", "Berne", "Zurich", "Zürich", "Geneva", "Genève", "Basel", "Lausanne"],
           "currency": ["Swiss franc", "Swiss francs", "franc", "francs"], "symbols": ["CHF"],
           "extra": ["SNB", "Bundesrat", "canton", "cantons", "cantonal"]},
    "GB": {"names": ["United Kingdom", "Great Britain", "Britain", "England", "Scotland", "Wales",
                     "Northern Ireland"], "aliases": [],
           "codes": ["UK", "U.K.", "GB", "GBR"],
           "demonyms": ["British", "Briton", "Britons", "English", "Scottish", "Scots", "Welsh"],
           "capital": "London",
           "cities": ["London", "Birmingham", "Manchester", "Glasgow", "Leeds", "Liverpool", "Edinburgh"],
           "currency": ["pound", "pounds", "pound sterling", "sterling"], "symbols": ["£", "GBP"],
           "extra": ["Westminster", "Downing Street", "Whitehall", "Bank of England", "gilt", "gilts",
                     "FTSE", "Tories", "Tory", "Labour Party", "Holyrood", "Stormont"]},
    "US": {"names": ["United States", "United States of America", "America"], "aliases": [],
           "codes": ["US", "U.S.", "USA", "U.S.A."], "demonyms": ["American", "Americans"],
           "capital": "Washington",
           "cities": ["Washington", "Washington, D.C.", "New York", "Los Angeles", "Chicago", "Houston",
                      "Phoenix"],
           "currency": ["dollar", "dollars", "US dollar", "US dollars", "U.S. dollar", "U.S. dollars"],
           "symbols": ["$", "US$", "USD"],
           "extra": ["Federal Reserve", "Fed", "White House", "Wall Street", "Pentagon", "Capitol Hill",
                     "Treasuries", "Republican", "Republicans", "Democrat", "Democrats", "GOP", "Medicare",
                     "Medicaid", "Supreme Court of the United States", "S&P 500", "Dow Jones", "Nasdaq"]},
    "BR": {"names": ["Brazil", "Brasil", "Federative Republic of Brazil"], "aliases": [],
           "codes": ["BRA"], "demonyms": ["Brazilian", "Brazilians"], "capital": "Brasília",
           "cities": ["Brasília", "Brasilia", "São Paulo", "Sao Paulo", "Rio de Janeiro", "Rio", "Salvador",
                      "Fortaleza", "Belo Horizonte"],
           "currency": ["reais", "Brazilian real", "Brazilian reais"], "symbols": ["R$", "BRL"],
           "extra": ["Selic", "Planalto", "Petrobras", "Bovespa", "Ibovespa", "Copom", "Amazon rainforest",
                     "Amazonia", "Amazônia"]},
    "CL": {"names": ["Chile", "Republic of Chile"], "aliases": [],
           "codes": ["CHL"], "demonyms": ["Chilean", "Chileans"], "capital": "Santiago",
           "cities": ["Santiago", "Valparaíso", "Valparaiso", "Concepción", "Concepcion", "Antofagasta",
                      "Viña del Mar"],
           "currency": ["Chilean peso", "Chilean pesos", "peso", "pesos"], "symbols": ["CLP", "CLP$"],
           "extra": ["Codelco", "Atacama", "La Moneda", "SQM"]},
    "CN": {"names": ["China", "People's Republic of China", "People’s Republic of China", "Mainland China"],
           "aliases": [], "codes": ["PRC", "CHN"], "demonyms": ["Chinese"], "capital": "Beijing",
           "cities": ["Beijing", "Shanghai", "Shenzhen", "Guangzhou", "Chongqing", "Tianjin", "Wuhan"],
           "currency": ["yuan", "renminbi"], "symbols": ["¥", "CNY", "CN¥", "RMB"],
           "extra": ["Politburo", "PBOC", "PBoC", "CCP", "Communist Party", "National People's Congress",
                     "Zhongnanhai", "Xinjiang", "Tibet", "Taiwan Strait"]},
    "CO": {"names": ["Colombia", "Republic of Colombia"], "aliases": [],
           "codes": ["COL"], "demonyms": ["Colombian", "Colombians"], "capital": "Bogotá",
           "cities": ["Bogotá", "Bogota", "Medellín", "Medellin", "Cali", "Barranquilla", "Cartagena"],
           "currency": ["Colombian peso", "Colombian pesos", "peso", "pesos"], "symbols": ["COP"],
           "extra": ["Ecopetrol", "FARC", "ELN", "Casa de Nariño"]},
    "CZ": {"names": ["Czechia", "Czech Republic", "Česko"], "aliases": [],
           "codes": ["CZE"], "demonyms": ["Czech", "Czechs"], "capital": "Prague",
           "cities": ["Prague", "Praha", "Brno", "Ostrava", "Plzeň", "Plzen", "Liberec"],
           "currency": ["koruna", "korunas", "Czech crown", "Czech crowns"], "symbols": ["Kč", "CZK"],
           "extra": ["ČNB", "CNB", "ANO", "ODS", "Hrad"]},
    "EG": {"names": ["Egypt", "Arab Republic of Egypt"], "aliases": [],
           "codes": ["EGY"], "demonyms": ["Egyptian", "Egyptians"], "capital": "Cairo",
           "cities": ["Cairo", "Alexandria", "Giza", "Shubra El Kheima", "Port Said", "Suez"],
           "currency": ["Egyptian pound", "Egyptian pounds", "pound", "pounds"], "symbols": ["EGP", "E£", "LE"],
           "extra": ["Suez Canal", "Nile", "Sinai", "Muslim Brotherhood", "CBE", "Tahrir"]},
    "GR": {"names": ["Greece", "Hellenic Republic", "Hellas"], "aliases": [],
           "codes": ["GRC"], "demonyms": ["Greek", "Greeks", "Hellenic"], "capital": "Athens",
           "cities": ["Athens", "Thessaloniki", "Patras", "Piraeus", "Heraklion", "Larissa"],
           **_EURO, "extra": ["Syriza", "Aegean", "Crete", "Peloponnese"]},
    "HU": {"names": ["Hungary", "Magyarország"], "aliases": [],
           "codes": ["HUN"], "demonyms": ["Hungarian", "Hungarians", "Magyar", "Magyars"], "capital": "Budapest",
           "cities": ["Budapest", "Debrecen", "Szeged", "Miskolc", "Pécs", "Pecs", "Győr"],
           "currency": ["forint", "forints"], "symbols": ["Ft", "HUF"],
           "extra": ["Fidesz", "Tisza", "Jobbik", "MNB", "Danube"]},
    "IN": {"names": ["India", "Republic of India", "Bharat"], "aliases": [],
           "codes": ["IND"], "demonyms": ["Indian", "Indians"], "capital": "New Delhi",
           "cities": ["New Delhi", "Delhi", "Mumbai", "Bengaluru", "Bangalore", "Hyderabad", "Chennai", "Kolkata"],
           "currency": ["rupee", "rupees", "Indian rupee"], "symbols": ["₹", "INR", "Rs", "Rs."],
           "extra": ["Lok Sabha", "Rajya Sabha", "RBI", "BJP", "Sensex", "Nifty", "lakh", "lakhs", "crore",
                     "crores", "Kashmir", "Adani", "Reliance Industries"]},
    "ID": {"names": ["Indonesia", "Republic of Indonesia"], "aliases": [],
           "codes": ["IDN"], "demonyms": ["Indonesian", "Indonesians"], "capital": "Jakarta",
           "cities": ["Jakarta", "Surabaya", "Bandung", "Medan", "Bekasi", "Semarang", "Nusantara"],
           "currency": ["rupiah"], "symbols": ["Rp", "IDR"],
           "extra": ["Bali", "Java", "Sumatra", "Sulawesi", "Papua", "Bank Indonesia", "Pertamina"]},
    "KW": {"names": ["Kuwait", "State of Kuwait"], "aliases": [],
           "codes": ["KWT"], "demonyms": ["Kuwaiti", "Kuwaitis"], "capital": "Kuwait City",
           "cities": ["Kuwait City", "Hawalli", "Salmiya", "Al Ahmadi", "Ahmadi", "Jahra", "Al Jahra",
                      "Farwaniya", "Sabah Al Salem", "Mubarak Al-Kabeer"],
           "currency": ["dinar", "dinars", "Kuwaiti dinar", "Kuwaiti dinars"], "symbols": ["KD", "KWD"],
           "extra": ["KIA", "KPC", "KFH", "Burgan", "Boubyan", "Shuwaikh", "Mina Al Ahmadi", "Al Zour",
                     "Failaka", "Bubiyan", "Boursa Kuwait", "Al Sabah", "Al-Sabah"]},
    "MY": {"names": ["Malaysia"], "aliases": [],
           "codes": ["MYS"], "demonyms": ["Malaysian", "Malaysians", "Malay", "Malays"], "capital": "Kuala Lumpur",
           "cities": ["Kuala Lumpur", "Putrajaya", "George Town", "Johor Bahru", "Ipoh", "Shah Alam", "Penang",
                      "Sabah", "Sarawak", "Johor"],
           "currency": ["ringgit"], "symbols": ["RM", "MYR"],
           "extra": ["Petronas", "1MDB", "Bank Negara", "UMNO", "Barisan Nasional", "Pakatan Harapan",
                     "Bursa Malaysia", "Dewan Rakyat"]},
    "MX": {"names": ["Mexico", "México", "United Mexican States"], "aliases": [],
           "codes": ["MEX"], "demonyms": ["Mexican", "Mexicans"], "capital": "Mexico City",
           "cities": ["Mexico City", "Ciudad de México", "Guadalajara", "Monterrey", "Puebla", "Tijuana",
                      "Ciudad Juárez", "Juárez", "Juarez"],
           "currency": ["Mexican peso", "Mexican pesos", "peso", "pesos"], "symbols": ["MX$", "MXN"],
           "extra": ["Pemex", "Banxico", "Morena", "PRI", "PAN", "Sinaloa", "Jalisco", "CFE", "USMCA"]},
    "PE": {"names": ["Peru", "Perú", "Republic of Peru"], "aliases": [],
           "codes": ["PER"], "demonyms": ["Peruvian", "Peruvians"], "capital": "Lima",
           "cities": ["Lima", "Arequipa", "Trujillo", "Chiclayo", "Piura", "Cusco", "Cuzco", "Callao"],
           "currency": ["sol", "soles", "nuevo sol", "nuevos soles"], "symbols": ["S/", "S/.", "PEN"],
           "extra": ["BCRP", "Andes", "Andean", "Fujimori", "Fujimorista"]},
    "PH": {"names": ["Philippines", "Republic of the Philippines", "the Philippines"], "aliases": [],
           "codes": ["PHL", "PH"], "demonyms": ["Filipino", "Filipinos", "Filipina", "Filipinas", "Philippine", "Pinoy"],
           "capital": "Manila",
           "cities": ["Manila", "Quezon City", "Davao", "Cebu", "Makati", "Taguig", "Caloocan", "Mindanao",
                      "Luzon", "Visayas"],
           "currency": ["Philippine peso", "Philippine pesos", "peso", "pesos"], "symbols": ["₱", "PHP"],
           "extra": ["Bangko Sentral", "BSP", "Malacañang", "Malacanang", "Duterte", "Marcos", "PSEi"]},
    "PL": {"names": ["Poland", "Republic of Poland", "Polska"], "aliases": [],
           "codes": ["POL"], "demonyms": ["Polish", "Pole", "Poles"], "capital": "Warsaw",
           "cities": ["Warsaw", "Warszawa", "Kraków", "Krakow", "Łódź", "Lodz", "Wrocław", "Wroclaw", "Poznań",
                      "Poznan", "Gdańsk", "Gdansk"],
           "currency": ["złoty", "zloty", "zlotys", "złote", "zlote"], "symbols": ["zł", "PLN"],
           "extra": ["Sejm", "Senat", "PiS", "Law and Justice", "Civic Platform", "Civic Coalition", "NBP",
                     "Tusk", "Kaczyński", "Kaczynski", "WIG20"]},
    "QA": {"names": ["Qatar", "State of Qatar"], "aliases": [],
           "codes": ["QAT"], "demonyms": ["Qatari", "Qataris"], "capital": "Doha",
           "cities": ["Doha", "Al Rayyan", "Al Wakrah", "Lusail", "Al Khor", "Umm Salal"],
           "currency": ["riyal", "riyals", "Qatari riyal", "Qatari riyals"], "symbols": ["QR", "QAR"],
           "extra": ["QIA", "Al Jazeera", "Ras Laffan", "QatarEnergy", "Al Thani", "Al-Thani", "Shura Council"]},
    "SA": {"names": ["Saudi Arabia", "Kingdom of Saudi Arabia", "Saudi"], "aliases": [],
           "codes": ["KSA", "SAU"], "demonyms": ["Saudi", "Saudis"], "capital": "Riyadh",
           "cities": ["Riyadh", "Jeddah", "Jiddah", "Mecca", "Makkah", "Medina", "Madinah", "Dammam", "Khobar",
                      "Neom", "NEOM"],
           "currency": ["riyal", "riyals", "Saudi riyal", "Saudi riyals"], "symbols": ["SR", "SAR"],
           "extra": ["Aramco", "PIF", "Vision 2030", "Tadawul", "SAMA", "Al Saud", "Al-Saud", "Hajj",
                     "Houthi", "Houthis"]},
    "ZA": {"names": ["South Africa", "Republic of South Africa"], "aliases": [],
           "codes": ["ZAF", "RSA"], "demonyms": ["South African", "South Africans"], "capital": "Pretoria",
           "cities": ["Pretoria", "Tshwane", "Cape Town", "Johannesburg", "Durban", "Port Elizabeth", "Gqeberha",
                      "Bloemfontein", "Soweto"],
           "currency": ["rand"], "symbols": ["R", "ZAR"],
           "extra": ["ANC", "Eskom", "Transnet", "JSE", "SARB", "Gauteng", "KwaZulu-Natal", "Western Cape",
                     "EFF", "Democratic Alliance", "MK Party", "load-shedding", "load shedding"]},
    "KR": {"names": ["South Korea", "Republic of Korea", "Korea"], "aliases": [],
           "codes": ["ROK", "KOR"], "demonyms": ["Korean", "Koreans", "South Korean", "South Koreans"],
           "capital": "Seoul",
           "cities": ["Seoul", "Busan", "Incheon", "Daegu", "Daejeon", "Gwangju", "Ulsan", "Sejong"],
           "currency": ["Korean won", "South Korean won"], "symbols": ["₩", "KRW"],
           "extra": ["Samsung", "Hyundai", "SK Hynix", "Kospi", "KOSPI", "Pyongyang", "North Korea",
                     "North Korean", "DPRK", "Yoon", "Chaebol", "chaebol", "chaebols"]},
    "TW": {"names": ["Taiwan", "Republic of China", "Chinese Taipei", "Formosa"], "aliases": [],
           "codes": ["TWN", "ROC"], "demonyms": ["Taiwanese"], "capital": "Taipei",
           "cities": ["Taipei", "New Taipei", "Kaohsiung", "Taichung", "Tainan", "Taoyuan", "Hsinchu", "Keelung"],
           "currency": ["New Taiwan dollar", "New Taiwan dollars", "Taiwan dollar", "Taiwan dollars", "dollar",
                        "dollars"],
           "symbols": ["NT$", "NTD", "TWD"],
           "extra": ["Legislative Yuan", "Executive Yuan", "Kuomintang", "KMT", "DPP", "Democratic Progressive Party",
                     "TSMC", "Beijing", "Chinese", "cross-strait", "PLA", "People's Liberation Army", "Taiex",
                     "TAIEX", "Lai Ching-te", "Penghu", "Kinmen", "Matsu"]},
    "TH": {"names": ["Thailand", "Kingdom of Thailand", "Siam"], "aliases": [],
           "codes": ["THA"], "demonyms": ["Thai", "Thais"], "capital": "Bangkok",
           "cities": ["Bangkok", "Krung Thep", "Nonthaburi", "Chiang Mai", "Pattaya", "Phuket", "Hat Yai"],
           "currency": ["baht"], "symbols": ["฿", "THB"],
           "extra": ["Pheu Thai", "Move Forward", "People's Party", "Shinawatra", "Bhumjaithai", "SET index"]},
    "TR": {"names": ["Turkey", "Türkiye", "Turkiye", "Republic of Türkiye", "Republic of Turkey"], "aliases": [],
           "codes": ["TUR"], "demonyms": ["Turkish", "Turk", "Turks"], "capital": "Ankara",
           "cities": ["Ankara", "Istanbul", "İstanbul", "Izmir", "İzmir", "Bursa", "Antalya", "Adana"],
           "currency": ["lira", "liras", "Turkish lira"], "symbols": ["₺", "TRY", "TL"],
           "extra": ["AKP", "CHP", "Bosphorus", "Anatolia", "Anatolian", "PKK", "Erdoğan", "Erdogan", "Kurdish",
                     "Kurds", "Borsa Istanbul", "BIST"]},
    "AE": {"names": ["United Arab Emirates", "Emirates"], "aliases": [],
           "codes": ["UAE", "U.A.E.", "ARE"], "demonyms": ["Emirati", "Emiratis"], "capital": "Abu Dhabi",
           "cities": ["Abu Dhabi", "Dubai", "Sharjah", "Ajman", "Ras Al Khaimah", "Fujairah", "Al Ain"],
           "currency": ["dirham", "dirhams"], "symbols": ["AED", "Dh", "Dhs", "DH"],
           "extra": ["ADNOC", "Mubadala", "ADIA", "Emaar", "DP World", "ADQ", "DIFC", "ADGM", "Etihad",
                     "Al Nahyan", "Al Maktoum"]},
    "RU": {"names": ["Russia", "Russian Federation", "Rossiya"], "aliases": [],
           "codes": ["RUS"], "demonyms": ["Russian", "Russians"], "capital": "Moscow",
           "cities": ["Moscow", "Saint Petersburg", "St Petersburg", "St. Petersburg", "Novosibirsk",
                      "Yekaterinburg", "Kazan", "Nizhny Novgorod", "Vladivostok"],
           "currency": ["ruble", "rubles", "rouble", "roubles"], "symbols": ["₽", "RUB"],
           "extra": ["Kremlin", "Duma", "State Duma", "Gazprom", "Rosneft", "Sberbank", "Lukoil", "Siberia",
                     "Siberian", "Ukraine", "Ukrainian", "Kyiv", "Kiev", "Donbas", "Crimea", "FSB", "Putin",
                     "MOEX", "Urals crude", "Urals"]},
}

GAZETTEER_VERSION = content_hash(json.dumps(GAZETTEER, sort_keys=True, ensure_ascii=False))

#: Replacement by code when a term survives the rewrite, by the kind of term.
REPLACEMENT = {
    "name": "[the country]",
    "alias": "[the country]",
    "code": "[the country]",
    "demonym": "[the country]",
    "extra": "[the country]",
    "capital": "[a city]",
    "city": "[a city]",
    "currency": "[local currency]",
    "symbol": "[local currency]",
    "other_country": "[another country]",
}

#: Terms matched without a trailing boundary: they sit flush against digits.
_SYMBOL_KINDS = ("symbol",)
#: Single characters or letters that need a digit after them to mean money.
_NEEDS_DIGIT = {"$", "R", "Ft", "Rp", "Rs", "Rs.", "Dh", "Dhs", "DH", "LE", "TL", "SR", "QR", "RM", "KD",
                "S/", "S/."}


def _is_code_like(term: str) -> bool:
    """Case-sensitive: codes and all-caps abbreviations."""
    letters = [c for c in term if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(term) <= 6


def _pattern(term: str, kind: str) -> re.Pattern:
    esc = re.escape(term)
    if kind in _SYMBOL_KINDS:
        tail = r"(?=\s?\d)" if term in _NEEDS_DIGIT else ""
        return re.compile(r"(?<!\w)" + esc + tail, re.UNICODE)
    flags = re.UNICODE if _is_code_like(term) else (re.IGNORECASE | re.UNICODE)
    return re.compile(r"(?<!\w)" + esc + r"(?!\w)", flags)


def terms_for(iso2: str) -> List[Tuple[str, str]]:
    """``[(term, kind), ...]`` for the subject country, longest terms first."""
    iso2 = iso2.upper()
    g = GAZETTEER.get(iso2)
    if g is None:
        raise KeyError(f"no gazetteer entry for {iso2}")
    out: List[Tuple[str, str]] = []
    out += [(t, "name") for t in g.get("names", [])]
    out += [(t, "alias") for t in g.get("aliases", [])]
    out += [(t, "code") for t in g.get("codes", [])]
    out += [(t, "demonym") for t in g.get("demonyms", [])]
    if g.get("capital"):
        out.append((g["capital"], "capital"))
    out += [(t, "city") for t in g.get("cities", [])]
    out += [(t, "currency") for t in g.get("currency", [])]
    out += [(t, "symbol") for t in g.get("symbols", [])]
    out += [(t, "extra") for t in g.get("extra", [])]
    for other, og in GAZETTEER.items():
        if other == iso2:
            continue
        out += [(t, "other_country") for t in og.get("names", []) + og.get("aliases", [])]
    seen = set()
    uniq = []
    for t, k in out:
        if (t, k) not in seen and t:
            seen.add((t, k))
            uniq.append((t, k))
    uniq.sort(key=lambda tk: -len(tk[0]))
    return uniq


_COMPILED: Dict[str, List[Tuple[str, str, re.Pattern]]] = {}


def _compiled(iso2: str) -> List[Tuple[str, str, re.Pattern]]:
    iso2 = iso2.upper()
    if iso2 not in _COMPILED:
        _COMPILED[iso2] = [(t, k, _pattern(t, k)) for t, k in terms_for(iso2)]
    return _COMPILED[iso2]


def _strip_exempt(text: str, iso2: str) -> str:
    if iso2.upper() == "US":
        return text
    for phrase in EXEMPT_PHRASES:
        text = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " " * len(phrase), text)
    return text


def scan(iso2: str, text: Any) -> List[Tuple[str, str]]:
    """The gazetteer terms found in `text` for the subject `iso2`, as
    ``[(term, kind), ...]``, each term once."""
    if not isinstance(text, str) or not text:
        return []
    body = _strip_exempt(text, iso2)
    hits: List[Tuple[str, str]] = []
    for term, kind, pat in _compiled(iso2):
        if pat.search(body):
            hits.append((term, kind))
    return hits


def redact(iso2: str, text: str) -> Tuple[str, List[Dict[str, str]]]:
    """Replace every term in `text` by its placeholder. Returns the new text
    and ``[{"term", "kind", "replacement"}, ...]`` for what was replaced."""
    if not isinstance(text, str) or not text:
        return text, []
    replaced: List[Dict[str, str]] = []
    exempt_spans: List[Tuple[int, int]] = []
    if iso2.upper() != "US":
        for phrase in EXEMPT_PHRASES:
            for m in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
                exempt_spans.append(m.span())

    def protected(start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e in exempt_spans)

    out = text
    for term, kind, pat in _compiled(iso2):
        pieces = []
        last = 0
        changed = False
        for m in pat.finditer(out):
            if protected(m.start(), m.end()):
                continue
            pieces.append(out[last:m.start()])
            pieces.append(REPLACEMENT[kind])
            last = m.end()
            changed = True
        if changed:
            pieces.append(out[last:])
            out = "".join(pieces)
            replaced.append({"term": term, "kind": kind, "replacement": REPLACEMENT[kind]})
            exempt_spans = []
            if iso2.upper() != "US":
                for phrase in EXEMPT_PHRASES:
                    for m in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", out):
                        exempt_spans.append(m.span())
    return out, replaced


def _walk(value: Any, path: str, skip_keys: Sequence[str]) -> Iterable[Tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in skip_keys:
                continue
            yield from _walk(v, f"{path}.{k}" if path else str(k), skip_keys)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]", skip_keys)


def payload_leaks(iso2: str, payload: Any, *, skip_keys: Sequence[str] = SKIP_KEYS) -> List[Dict[str, str]]:
    """Every gazetteer hit in every string value of `payload`, except under
    `skip_keys`, as ``[{"path", "term", "kind"}, ...]``."""
    leaks: List[Dict[str, str]] = []
    for path, text in _walk(payload, "", skip_keys):
        for term, kind in scan(iso2, text):
            leaks.append({"path": path, "term": term, "kind": kind})
    return leaks


def publisher_mentions(iso2: str, payload: Any) -> int:
    """How many publisher strings carry a term. Reported, never failed."""
    n = 0
    for path, text in _walk(payload, "", ()):
        if path.endswith(".publisher") and scan(iso2, text):
            n += 1
    return n


def assert_clean(iso2: str, payload: Any) -> None:
    """Raise :class:`MaskLeak` if any term survives in `payload`."""
    leaks = payload_leaks(iso2, payload)
    if leaks:
        shown = ", ".join(f"{l['term']} ({l['kind']}) at {l['path']}" for l in leaks[:5])
        raise MaskLeak(f"{iso2}: {len(leaks)} gazetteer term(s) in the payload about to be sent: {shown}")
