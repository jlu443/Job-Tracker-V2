"""Is a free-text job location in the United States?

Locations arrive in every shape: "Austin, TX", "San Francisco, CA, US",
"Pune, MH, IN", "Remote - Canada", "London, England, United Kingdom",
"3 Locations". Matching is on whole words, never substrings ("Milwaukee"
must not hit "uk", "Indianapolis" must not hit "india").
"""

from __future__ import annotations

import re

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
_US_WORDS = {"united states", "usa", "u.s.", "u.s.a.", "us", "america"}

_NON_US = {
    # countries
    "canada", "uk", "united kingdom", "england", "scotland", "wales", "ireland",
    "germany", "france", "india", "china", "japan", "australia", "singapore",
    "netherlands", "spain", "italy", "poland", "brazil", "mexico", "israel",
    "sweden", "switzerland", "portugal", "romania", "hungary", "czechia",
    "czech republic", "austria", "belgium", "denmark", "norway", "finland",
    "korea", "south korea", "taiwan", "hong kong", "philippines", "vietnam",
    "malaysia", "indonesia", "thailand", "argentina", "colombia", "chile",
    "costa rica", "uae", "united arab emirates", "saudi arabia", "egypt",
    "nigeria", "kenya", "south africa", "new zealand", "turkey", "ukraine",
    "serbia", "greece", "bulgaria", "lithuania", "estonia", "latvia", "emea",
    "apac", "latam", "latin america", "south america", "central america",
    # large tech-hiring cities abroad (often listed without a country)
    "london", "toronto", "vancouver", "montreal", "ottawa", "waterloo",
    "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "gurgaon",
    "gurugram", "noida", "mumbai", "delhi", "dublin", "berlin", "munich",
    "paris", "amsterdam", "tel aviv", "zurich", "stockholm", "warsaw", "krakow",
    "barcelona", "madrid", "lisbon", "sydney", "melbourne", "tokyo", "seoul",
    "shanghai", "beijing", "shenzhen", "taipei", "manila", "sao paulo",
    "mexico city", "guadalajara", "cambridge, uk", "edinburgh", "manchester",
    # The rest of the world's countries (2026-10-16: "Bratislava, Bratislavsky
    # kraj, Slovakia" passed). Left out: names that are also US places a
    # location might name without a state ("Lebanon, NJ" has one, but
    # "Georgia", "Jordan", "Peru", "Panama City" can stand alone).
    "slovakia", "slovenia", "croatia", "bosnia", "montenegro", "albania",
    "north macedonia", "kosovo", "moldova", "belarus", "russia",
    "iceland", "luxembourg", "cyprus", "liechtenstein", "monaco", "andorra",
    "armenia", "azerbaijan", "kazakhstan", "uzbekistan", "kyrgyzstan", "mongolia",
    "pakistan", "bangladesh", "sri lanka", "nepal", "bhutan", "maldives", "myanmar",
    "cambodia", "laos", "brunei", "macau", "macao", "iraq", "iran", "syria",
    "kuwait", "qatar", "bahrain", "oman", "yemen", "morocco", "algeria", "tunisia",
    "libya", "ghana", "ethiopia", "uganda", "tanzania", "rwanda", "senegal",
    "cameroon", "zambia", "zimbabwe", "botswana", "namibia", "mozambique",
    "mauritius", "ecuador", "bolivia", "paraguay", "uruguay", "venezuela",
    "guatemala", "honduras", "el salvador", "nicaragua", "dominican republic",
    "bahamas", "barbados", "fiji", "papua new guinea",
    "deutschland", "espana", "italia", "nederland", "schweiz", "osterreich",
    # More cities abroad that locations name alone ("Milan", "Prague"). Not
    # ones a US city shares (Birmingham, Bristol, Cambridge, Lima, Reading,
    # Rome GA, Valencia CA, Vienna VA, Athens GA, Geneva IL, ...).
    "milan", "milano", "turin", "bologna", "prague",
    "budapest", "bucharest", "brussels", "antwerp", "copenhagen", "oslo",
    "helsinki", "istanbul", "ankara", "dubai", "abu dhabi", "riyadh",
    "doha", "lagos", "nairobi", "johannesburg", "cape town", "buenos aires",
    "bogota", "medellin", "montevideo", "auckland", "kuala lumpur", "penang",
    "jakarta", "bangkok", "ho chi minh", "hanoi", "osaka", "kyoto", "yokohama",
    "nagoya", "chengdu", "hangzhou", "guangzhou", "nanjing", "wuhan", "suzhou",
    "xiamen", "tianjin", "dalian", "glasgow", "belfast",
    "nottingham", "crawley", "stevenage", "frankfurt",
    "cologne", "stuttgart", "dusseldorf", "leipzig", "nuremberg",
    "karlsruhe", "lyon", "toulouse", "marseille", "grenoble", "nantes", "lille",
    "eindhoven", "rotterdam", "utrecht", "the hague", "delft",
    "lausanne", "basel", "porto", "malaga", "bilbao",
    "gdansk", "wroclaw", "poznan", "cluj", "sofia", "zagreb",
    "ljubljana", "bratislava", "kyiv", "kiev", "tallinn", "riga", "vilnius",
    "haifa", "jerusalem", "herzliya", "calgary", "winnipeg",
    "mississauga", "kitchener", "burnaby", "ahmedabad",
    "kolkata", "coimbatore", "kochi", "chandigarh", "jaipur", "karachi", "lahore",
    "islamabad", "dhaka", "colombo", "cebu", "makati", "taguig", "busan", "pangyo",
}
# ISO country codes seen in "City, Region, CC" strings. Deliberately excludes
# codes that are also US state abbreviations (CA, DE, IN, ...) — those are
# only read as countries when they are the third component.
# US state codes that are also ISO country codes (IN India, CA Canada, IL
# Israel, ...): as the last part of "City, Region, XX" they're countries.
_STATE_COUNTRY_CODES = {"AL", "AR", "AZ", "CA", "CO", "DE", "GA", "ID", "IL", "IN", "LA",
                        "MA", "MD", "ME", "MN", "MT", "NE", "PA", "SC", "SD", "TN", "VA"}
# US states whose name holds a foreign place ("new mexico": "mexico").
_STATE_NAMES_WITH_FOREIGN_WORDS = {n for n in _US_STATE_NAMES
                                   if any(w in _NON_US for w in n.split())}
_COUNTRY_CODES = {"GB", "UK", "IE", "FR", "CN", "JP", "AU", "SG", "NL", "ES",
                  "IT", "PL", "BR", "MX", "IL", "SE", "CH", "PT", "RO", "KR",
                  "TW", "HK", "PH", "NZ", "AE", "CZ", "AT", "BE", "DK", "NO", "FI",
                  # ISO codes that aren't also US state codes ("Ho Chi Minh, VN")
                  "VN", "TH", "MY", "KH", "LK", "BD", "PK", "NP", "ZA", "NG", "KE", "EG",
                  # Canadian provinces ("Surrey, BC", "Toronto, ON")
                  "BC", "ON", "QC", "AB", "MB", "NS", "NB", "YT", "NT", "NU",
                  "TR", "GR", "HU", "SK", "SI", "HR", "RS", "BG", "UA", "LT", "LV", "EE",
                  "IS", "LU", "CY", "CL", "PE", "UY", "EC", "CR", "DO", "GT", "SA", "QA",
                  "KW", "BH", "OM", "JO", "LB", "RU", "BY", "KZ", "UZ"}


def _phrases(loc: str) -> set[str]:
    """Every 1-3 word phrase in the location, lowercased. Words inside a US
    state's name are dropped ("New Mexico" holds no "mexico")."""
    words = re.findall(r"[a-z][a-z.]*", loc.lower())
    out = set()
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            out.add(" ".join(words[i:i + n]).strip("."))
    for state in out & _STATE_NAMES_WITH_FOREIGN_WORDS:
        out -= {w for w in out if w != state and f" {w} " in f" {state} "}
    return out


def is_us(location: str) -> bool:
    """True if the location is (or plausibly is) in the US.

    Unknown / unparseable locations pass, so a sparse location field never
    silently hides a job.
    """
    return us_verdict(location) is not False


def us_verdict(location: str) -> bool | None:
    """True / False when the location says US / abroad; None when it can't
    tell ("Main Campus", "2 Locations", a bare "Biggin Hill")."""
    if not location or not location.strip():
        return None
    # Multi-location strings ("New York, NY; London, UK") — any US part wins.
    parts = [p for p in re.split(r"\s*(?:;|\||/| or )\s*", location) if p.strip()]
    if len(parts) > 1:
        verdicts = [us_verdict(p) for p in parts]
        return True if True in verdicts else (False if all(v is False for v in verdicts)
                                               else None)

    comps = [c.strip() for c in location.split(",") if c.strip()]
    last = comps[-1].upper() if comps else ""
    # Workday style "Alzenau,DEU" / "Austin,USA": ISO-3166 alpha-3 country.
    if len(comps) >= 2 and re.fullmatch(r"[A-Z]{3}", comps[-1]):
        return last == "USA"
    # "City, ST" with a US state code no country shares is the US, whatever
    # the city is called ("Paris, TX", "Dublin, OH", "Milan, MI").
    if (len(comps) == 2 and comps[-1] in _US_STATES
            and comps[-1] not in _STATE_COUNTRY_CODES):
        return True
    # "City, Region, CC": a trailing 2-letter code is a country code, unless
    # it's a US state that isn't also one ("Central New Mexico, Albuquerque,
    # NM"; but "Bangalore, KA, IN" is India).
    if len(comps) >= 3 and re.fullmatch(r"[A-Z]{2}", last):
        return last == "US" or (last in _US_STATES and last not in _STATE_COUNTRY_CODES)
    # Standalone codes in other layouts: "Framingham, MA 01701", PwC's
    # "MI-Detroit", "US - Mt. Vernon - IN", "Cruiserath - IE", "IT - Torino".
    # A state code doesn't outweigh a foreign place named alongside it
    # ("Montreal, CA", "IN - Chennai"); letters include accented ones
    # ("MONTRÉAL" holds no "AL").
    codes = set(re.findall(r"(?<![^\W\d_])([A-Z]{2,3})(?![^\W\d_])", location))
    if codes & {"US", "USA"}:
        return True
    foreign, states = codes & _COUNTRY_CODES, codes & (_US_STATES - {"DE"})
    if foreign and not states:
        return False
    if states and not foreign and not _phrases(location) & _NON_US:
        return True

    phrases = _phrases(location)
    if phrases & _US_WORDS or phrases & _US_STATE_NAMES:
        # "New South Wales" etc. would be caught below; US words are decisive
        # only when nothing foreign is named alongside them.
        if not phrases & _NON_US:
            return True
    if phrases & _NON_US:
        return False
    if len(comps) >= 2 and re.fullmatch(r"[A-Z]{2}", last):
        if last in _US_STATES:
            return True
        if last in _COUNTRY_CODES:
            return False
    return None


# European posting conventions no US posting uses: gender markers ("H/F",
# "(m/w/d)", "all genders") and local internship words ("Werkstudent",
# "Stage 2027", "Stagiaire", "VIE", "Duales Studium"). Workday's location is
# often a bare city ("Hamburg", "Tarn (81)", "2 Locations"), which is_us()
# lets through; Workday's intern filters (2026-10-07) surfaced ~170 such
# jobs in one morning.
_FOREIGN_TITLE = re.compile(
    r"\((?:[hfmwdxn]\s*/\s*){2,}[hfmwdxn]\)|\b(?:h/f|f/h|m/w/d|w/m/d|m/f/d|f/m/d|d/f/m|f/m/x|"
    r"m/f/x|h/f/x|h/f/n|x/w/m|w/m/x)\b|\bx\s*\|\s*[wf]\s*\|\s*m\b|\ball\s+genders?\b|"
    r"\bwerkstudent\w*|\bpraktik\w*|\bstagiaire\b|^\s*stage\b|\bstage\s+(?:20\d\d|de\s+\d|-)|"
    r"\bv\.?i\.?e\.?(?=\W|$)|\balternan\w*|\bpr[aá]cticas\b|\bbecari[oa]\b|\btirocini\w*|"
    r"\bausbildung\b|\bauszubildende\w*|\bduales?\s+studium|\bmasterarbeit|\bbachelorarbeit",
    re.I)


def clearly_us(location: str) -> bool:
    """The location names the US outright, not just "plausibly" (is_us lets
    unknowns through). "DE" alone is Germany as often as Delaware."""
    if re.search(r"\bUnited States\b|\bUSA\b|,\s*US\b", location or ""):
        return True
    phrases = _phrases(location or "")
    if phrases & _US_STATE_NAMES and not phrases & _NON_US:
        return True
    comps = [c.strip() for c in (location or "").split(",") if c.strip()]
    return any(c.upper() in _US_STATES and c.upper() != "DE" for c in comps[1:])


# Country codes a job page can give that are the US (Puerto Rico and the
# territories, like "PR" among _US_STATES).
US_COUNTRY_CODES = {"US", "PR", "GU", "VI", "AS", "MP"}


def known_country(country: str) -> bool:
    """A job page's ISO alpha-2 country, as opposed to '' (never looked) or
    '-' (looked; the page didn't say)."""
    return bool(re.fullmatch(r"[A-Z]{2}", country or ""))


def is_us_job(title: str, location: str, country: str = "") -> bool:
    """is_us(location), unless the title says the job is abroad: a foreign
    place ("Intern (London)") or a European posting convention without a
    location that clearly names the US. A country read from the job page
    (`country`, ISO alpha-2) decides outright: Workday's location is often a
    bare "Crawley" or "2 Locations" that is_us() can't place (24% of the
    week's announcements to 2026-10-10)."""
    if known_country(country):
        return country in US_COUNTRY_CODES
    if not is_us(location) or title_names_foreign_place(title):
        return False
    return not (_FOREIGN_TITLE.search(title or "") and not clearly_us(location))


def title_names_foreign_place(title: str) -> bool:
    """'GPU Architecture Engineer - China', 'Intern (London)': the title's
    trailing qualifier names a place abroad. Matters when the location field
    is just "3 Locations"."""
    tail = re.split(r"\s[-–—|]\s|\(", title or "")[-1]
    return bool(_phrases(tail) & _NON_US)
