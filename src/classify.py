"""Classify a job title into one of: intern | new_grad | mid | senior.

Two layers:
  1. Keyword/regex pass — deterministic, free, handles the clear majority.
  2. Zero-shot NLI fallback — facebook/bart-large-mnli, runs locally, no API key.
     Model is downloaded once (~1.6 GB) and cached by HuggingFace.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

ROLE_TYPES = ("intern", "new_grad", "mid", "senior")

# Bump whenever the rules change what counts as intern/new_grad. The first run
# on a new version stores jobs it newly recognizes on already-known boards
# silently (unless freshly posted) instead of announcing weeks-old postings.
VERSION = 7

# "APM" the entry role (Associate Product Manager), not the product category
# (Application / Asset Performance Management): "Sales Manager – APM
# Software Sales", "APM / Bentley AssetWise Engineer" (2026-10-11).
_APM = (r"apm(?!\s*/(?!\s*product)|\s+(?:software|solutions?|tools?|platforms?|products?|"
        r"engineer\w*|developer|specialist|consultant|sales|implementation)\b)")

# Stated level a title can carry that the senior words miss: "Software
# Engineer, Mid" (not "Mid-Atlantic", "Mid-Market", "Midwest"), and academic
# ranks ("Assistant or Associate Professor"; not the "Faculty of Medicine").
# A description that welcomes new graduates doesn't make these entry level
# (2026-10-16: both came in through plain-title description reads).
_MID = (r"(?<!to\s)(?<!to-)mid(?:[\s-]*level)?(?![\s-]*(?:atlantic|west\w*|south\w*|america\w*|"
        r"market|size\w*|stream|year|day|shift|town|land|continent|range|career|corporate\w*))")
_ACADEMIC = r"professor\w*|(?<!of\s)faculty(?!\s+of)|lecturer\w*"

# Ordered most-specific first. First matching pattern wins.
_RULES: list[tuple[str, str]] = [
    # Staff who run fellowships, ahead of the intern rule's "fellowship":
    # "Fellowship-Trained Physician", "Fellowship Program Director",
    # "Residency/Fellowship Coordinator", "Program Lead, AI Fellowship".
    (r"\b(fellowship[\s-]+trained|fellowships?(?:\s+program)?\s+director|"
     r"senior\s+(?:research\s+)?fellow)\b", "senior"),
    (r"\b(fellowships?(?:\s+(?:and|&)\s+internships?)?(?:\s+[\w-]+){0,3}?\s+"
     r"(?:coordinator|manager|administrator|advisor|specialist)|"
     r"(?:manager|director|lead)\b[\s,:–-]+(?:[\w&–-]+\s+){0,4}fellowships?)\b", "mid"),
    (r"\b(intern|interns|internships?|co-?op|summer\s+(?:analyst|associate)|"
     # "student" alone, but not a department serving students ("Student
     # Success Coach", "Student Services Coordinator", "Student Living").
     r"working\s+student|work[\s-]*study|"
     # UK/EU internships: "Industrial Placement", "Software Engineering
     # Placement", "6 month placement", "Off Cycle"; not "Job Placement
     # Specialist", "Media Placement", a graduate scheme's "Graduate
     # Placement" or a location ("Placement: Yogyakarta").
     r"(?<!job )(?<!media )(?<!\bad )(?<!product )(?<!patient )(?<!client )(?<!school )"
     r"(?<!\bgraduate )placements?(?!\s*:)(?!\s+(?:specialist|coordinator|manager|officer|advisor|consultant|"
     r"agent|director|lead|counsel\w*|services?|support)\b)|off[\s-]?cycle|"
     r"undergrad(?:uate)?\s+(?:research\w*|student|assistant)|fellows\s+program|"
     r"student(?!\s+(?:success|engagement|affairs|services?|living|"
     r"health|financial|support|teacher|life|housing|involvement|activities|records|"
     r"accounts|aid|union|center|conduct|enrollment|loans?|accessibility|wellness|information|"
     r"organizations?|government|clubs?|"
     r"retention|advis\w*|recruit\w*|admissions?|development|care|programs?\s+"
     r"(?:coordinator|manager|director))\b)|apprentice(?:ship)?|fellowship|externship|"
     r"graduate\s+(?:research\s+)?assistant)\b",
     "intern"),
    # Recruiters *of* students ("Campus Recruiter", "University & Early
    # Talent Recruiter") are staff, unless the title itself is junior.
    (r"^(?!.*\b(?:junior|jr\.?|entry[\s-]*level|(?:new|recent)\s+(?:college\s+)?grad\w*|"
     r"20\d\d\s+start)(?!\w)).*\b(recruiters?|recruiting\s+coordinator|"
     r"talent\s+acquisition\s+(?:partner|specialist|coordinator|consultant|manager|"
     r"lead|advisor|associate)|university\s+relations)\b", "mid"),
    (r"\b(new\s*grad|new\s*graduate|new\s+college\s+grad(?:uate)?|ncg|"
     r"university\s*grad(?:uate)?|recent\s+grad(?:uate)?|"
     # "campus" as hiring, not a place ("Main Campus", "Prescott Campus")
     r"campus\s*[-:/]?\s*(?:hire|hiring|graduate|grad|undergrad\w*|program|"
     r"full[\s-]?time|recruit(?:ing|ment)|20\d\d)|20\d\d\w?\s+campus|"
     r"^campus\s*:?\s+(?=\w)(?!(?:security|police|safety|services?|ministry|minister|director|"
     r"manager|operations|experience|relations|planning|life|store|visit\w*|tours?|"
     r"it|dining|facilit\w*|recreation|engagement|coordinator|admin\w*)\b)|"
     r"fresh\s+grad(?:uate)?s?|"
     r"early\s*(?:career|talent|in\s+career)|entry[\s-]*level|emerging\s+talent|"
     r"level\s*0|"
     # "Graduate Performance Engineer", "Quant Developer, Graduate", "(Grad)"
     # Not "Senior Analyst, Graduate & Undergraduate Brand Management".
     r"graduate(?!\s+(?:degree|school|studies)|\s*(?:&|and)\s+undergrad)|grads?|"
     r"(?:university|college)\s+hire|"
     r"class\s+of\s+20\d\d|rotation(?:al)?\s+(?:program|engineer)|"
     r"leadership\s+development\s+program|residency|junior|jr\.?|"
     # "Associate Data Analyst", "Associate Machine Learning Engineer":
     # associate + up to two words + a junior role noun.
     # ...but not "Senior Associate Scientist": the senior rule decides those.
     r"(?<!senior )(?<!sr\. )(?<!sr )(?<!lead )(?<!principal )(?<!staff )"
     r"associate(?:\s+[\w/&-]+){0,2}?\s+(?:engineer|developer|analyst|scientist|"
     r"consultant|product\s+manager|programmer)|" + _APM + r"|"
     # named entry programs: "Career Accelerator Program", "AI Resident"
     r"(?:career\s+)?accelerator\s+program|launch\s+program|analyst\s+program|"
     r"development\s+program|graduate\s+scheme|(?:ai|ml|aiml|research)\s+resident)\b",
     "new_grad"),
    # "Software Engineer I", "Analyst 1" — level one of a laddered title.
    # Not level one of a senior ladder ("Senior Software Engineer I", "Staff
    # Engineer 1", "Associate Principal Scientist I"): the senior rule decides.
    (r"^(?!.*\b(?:senior|sr\.?|staff|principal|lead|mid[\s-]*level)\b)"
     r".*\b(?:engineer|developer|analyst|scientist|programmer|technician|"
     r"designer|specialist)\s*(?:i|1)\b", "new_grad"),
    # "Data Scientist - PhD (2026)": degree plus a graduation year.
    (r"\b(?:phd|ph\.d|ms|m\.s|bs|b\.s|masters?|bachelors?)\b.{0,20}\b20[2-3]\d\b",
     "new_grad"),
    # "Member of Technical Staff" is a flat title at AI labs, not a level.
    (r"\b(senior|sr\.?|(?<!technical )(?<!trading )(?<!research )staff|principal|lead|"
     r"architect|distinguished|director|head\s+of|vp|vice\s+president|manager|mgr|fellow|"
     + _ACADEMIC + r")\b", "senior"),
    (r"\b(iii|iv|v)\b", "senior"),
    (r"\b(ii|2|" + _MID + r")\b", "mid"),
    (r"\b\d{2,}\+?\s*years?\b", "senior"),  # "5+ years", "10 years"
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _RULES]

# Job function. Ordered: first match wins, so specific families come before
# the generic "engineer" catch-all, and non-software engineering disciplines
# are claimed before it can mislabel them as software.
# Weak words ("analyst", "scientist", "product", "markets", "engineer") need
# company: on their own they put 31% of a 240-title sample of announcements
# (2026-09-27..10-10) in a tech function that wasn't ("Fraud Analyst I",
# "Associate Scientist, Molecular Function", "Product Development
# Technician", "Wealth Management Client Associate - Boston Market",
# "Facilities Engineer I"). tests/golden_categories.tsv holds that sample.
#
# "Mobile" apps, not a mobile glass / home / crane service ("Automotive Mobile
# Glass Technician Apprentice", 2026-10-16).
_MOBILE = (r"mobile(?!\s+(?:glass|home|crane|equipment|unit|technician|mechanic|medical|"
           r"clinic|service|repair|detailing))")
# Words that make a title tech whatever else it says.
_STRONG_TECH = (r"software|swe|sde|developers?|programm(?:er|ing)|devops|sre|site\s+reliability|"
                r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|web|" + _MOBILE + r"|ios|android|cloud|"
                r"cyber\w*|security|computer|embedded|firmware|sdet|it|information\s+"
                r"(?:technology|systems)|data|database|java|python|c\+\+|network(?!ing\s+event)\w*|"
                r"ai|ml|machine\s+learning|gis|systems?\s+(?:engineer\w*|analyst|architect\w*)|"
                r"(?:systems?|solutions?|computer)\s+architecture|digital\s+technolog\w*|"
                r"modell?ing\s+(?:&|and)\s+simulation|identity\s+(?:(?:&|and)\s+)?access|"
                r"service\s+desk|help\s*desk")
# Engineering, science, care and business disciplines that aren't computing.
_NON_COMPUTING = (
    r"mechanical|civil|chemical|industrial|"
    r"manufacturing|process|structur\w*|environmental|propulsion|thermal|materials|biomedical|"
    r"petroleum|mining|nuclear|construction|field\s+(?:service|engineer\w*|operations|"
    r"technician|applications)|sales|marketing|legal|finance|financial|accounting|"
    r"recruit\w*|hr|human\s+resources|traffic|bridge|highway|roadway|transportation|transit|"
    r"rail\w*|track\s+design|geotechnical|water|wastewater|hvac|production|"
    r"operations\s+engineer\w*|power\s+(?:delivery|generation|plant|systems?)|"
    r"transmission\s+line|substation|utility|utilities|land\s+development|site\s+development|"
    r"urban|facilit\w*|factory|quality|supplier|reliability|safety|maintenance|equipment|"
    r"planning|planner|project\s+engineer\w*|superintendent|estimat\w*|asset\s+management|"
    r"obsolescence|supportability|logistics|aerospace|aeronautic\w*|airframe|stress|"
    r"design\s+assurance|regulatory|technicians?|operators?|architectur\w*|landscape|"
    r"surveying|drafting|cad|welding|tooling|fire\s+protection|food|agricultur\w*|"
    r"chemist\w*|chemistry|biolog\w*|physics|lng|oil|gas|energy\s+storage|battery|"
    r"bearings?|seals?|packaging|plumbing|lighting|acoustic\w*|geolog\w*|audit\w*|"
    r"assurance|tax|banking|wealth|insurance|actuar\w*|underwrit\w*|compliance|"
    # Care and lab work: "Ultrasound Technologist", "MRI Technologist" fell
    # through to the "technolog..." catch-all (2026-10-11).
    r"technologists?|ultrasound|sonograph\w*|mri|radiolog\w*|x-ray|surgical|clinical|"
    r"medical|nurs\w*|pharmac\w*|dental|veterinar\w*|antibiotic\w*|microbio\w*|"
    r"\w*immunolog\w*|molecular|cell|drug\s+discovery|"
    # 2026-10-16: "Power Engineering Intern", "Metrology Engineering Intern",
    # "Postdoctoral Associate - Cardioimmunology Research"
    r"power\s+engineer\w*|metrolog\w*|cardio\w*|endocrin\w*|oncolog\w*|physiolog\w*|"
    r"neuroscien\w*|genetic\w*|genomic\w*")
_CATEGORY_RULES: list[tuple[str, str]] = [
    # Quant: not "Investment Banking", "Capital Markets", "Wealth Management
    # ... Market", "Credit Analyst Program".
    (r"\b(quant|quants|quantitative|trading|traders?|algorithmic|market[\s-]+mak\w*|strats?|"
     r"model\s+(?:risk|validation)|credit\s+risk)\b", "quant"),
    (r"\b(asic|fpga|rtl|silicon|circuit|analog|mixed[\s-]signal|hardware|"
     r"physical\s+design|design\s+verification|dft|photonics|semiconductor|"
     r"pcb|signal\s+(?:and|&)\s+power\s+integrity|signal\s+integrity|electromechanical|"
     r"serdes|high[\s-]speed\s+(?:digital|serial|interface\w*|io|i/o)|"
     # "Electrical Designer", "Electrical Validation Intern", "Electrical
     # Component Engineer"; not "Electrical Apprentice" or "Electrical
     # Construction ... Intern".
     r"electrical(?:\s*(?:and|&|/)\s*\w+)?\s+(?:(?:component|circuit|power|controls?|product|"
     r"instrumentation)\s+)?(?:engineer\w*|design\w*|validation|test\w*|"
     r"integration|systems?|analysis|intern|co-?op)|"
     # "AMS Validation", "Functional Validation", "Formal Verification"; not
     # "Model Validation" (quant, above) or "Data Validation".
     r"(?:ams|electrical|functional|system|silicon|chip|hardware|post[\s-]?si)\s+validation|"
     r"formal\s+verification|rfic|radio\s+frequency|ic\s+(?:design\w*|test|layout)|"
     r"optical|optics|photonic\w*|dram|field[\s-]programmable|harness\w*|sensors?|"
     r"telematics|"
     r"cpu|gpu\s+architecture|soc|rf|transistor|avionics|electronics?|microelectronic\w*|"
     r"mechatronics|power\s+electronics|gpu|design\s+for\s+test|"
     # Chip packaging and reliability; not a consumer-goods "Packaging
     # Engineer" or a plant's "Reliability Engineer".
     r"(?:advanced|semiconductor|chip|ic|substrate)\s+packaging|"
     r"(?:hardware|silicon|chip|board|component|semiconductor)\s+reliability)\b", "hardware"),
    # Lab and other non-computing scientists: "Associate Scientist I",
    # "Scientist I - Microbiology", "Forensic Scientist"; not "Research
    # Scientist" or "Scientist, Machine Learning".
    (r"^(?!.*\b(?:" + _STRONG_TECH + r"|deep\s+learning|computational|nlp|llm|vision|"
     r"algorithm\w*|statistic\w*|bioinformatics|informatics|quantitative|perception|"
     r"robotics)\b)(?!.*\b(?:research|applied|decision)\s+scien).*\bscientists?\b", "other"),
    # Research in a non-computing field: "Research Technician - Antibiotic
    # Discovery", "Postdoctoral Researcher (Geotechnical Engineering)".
    (r"^(?!.*\b(?:" + _STRONG_TECH + r"|deep\s+learning|computational|nlp|llm|vision|"
     r"algorithm\w*|statistic\w*|bioinformatics|informatics|quantitative|perception|"
     r"robotics|analytics|computing|recommendation\w*|autonom\w*|av|research\s+scientists?)\b)"
     r"(?=.*\b(?:" + _NON_COMPUTING + r")\b).*\b(research\w*|postdoc\w*|"
     r"post[\s-]?doctoral|r\s*&\s*d)\b", "other"),
    (r"\b(machine\s+learning|ml|ai|artificial\s+intelligence|deep\s+learning|gen\s*ai|"
     r"data\s+scien\w*|data\s+engineer\w*|data\s+analy\w*|analytics|"
     # Not "Data Entry Coordinator", "Data Center ...", "Data Annotation
     # Analyst", "Data Processing Support".
     r"data(?!\s+(?:entry|cent(?:er|re)s?|collect\w*|annotat\w*|label\w*|processing|capture|"
     r"technician|operator|input))|"
     r"computer\s+vision|vision|image\s+processing|nlp|natural\s+language|llm|research\w*|"
     r"applied\s+scien\w*|(?:computer|computational|decision|perception|robotics)\s+scientists?|"
     r"business\s+intelligence|bi(?!-)|algorithm\w*|autonom\w*|robotic\w*|perception|"
     r"statistic\w*|computational|bioinformatics|geospatial|informatics|biostatistic\w*|"
     r"power\s+bi|motion\s+planning|biometrics|operations\s+research|"
     # "Clinical Applications Analyst (EPIC ...)"
     r"applications?\s+analyst)\b", "data_ml"),
    # Not "Product Development Technician", "Product Chemistry", "New
    # Product Development", "Product Engineer".
    (r"\b(product\s+(?:manag\w*|owner|design\w*|analyst|strateg\w*|growth|marketing\s+manag\w*|"
     r"intern\w*|co-?op)|apm|technical\s+program\s+manag\w*|tpm|offering\s+manag\w*|ux|"
     r"user\s+experience)\b", "product"),
    (r"\b(security\s+(?:guard|officer))\b", "other"),
    # Engineering and business disciplines that aren't computing, unless
    # the title also names computing work ("Manufacturing Software Engineer").
    (r"^(?!.*\b(?:" + _STRONG_TECH + r")\b).*\b(" + _NON_COMPUTING + r")\b", "other"),
    (r"\b(software|swe|sde|developer|programmer|devops|sre|site\s+reliability|"
     r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|web|" + _MOBILE + r"|ios|android|"
     r"cloud|platform|infrastructure|security|cyber\w*|computer\s+science|"
     r"embedded|firmware|qa|sdet|test\s+automation|forward\s+deployed|"
     # not the hiring process: "MBA Strategy Intern - Rolling Application"
     r"it|(?<!rolling\s)applications?(?!\s+(?:deadline|process|window|period))|automation|"
     r"technical\s+(?:staff|support|consultant|solutions)|coding|"
     r"digital\s+(?:&\s+|and\s+)?(?:technolog\w*|tech|solutions|engineer\w*|transformation|"
     r"innovation|platforms?|services)|network(?:ing|\s+(?:engineer\w*|admin\w*|security|"
     r"operations?|infrastructure|technician|analyst))|"
     r"modell?ing\s+(?:&|and)\s+simulation|identity\s+(?:(?:&|and)\s+)?access|"
     r"service\s+desk|help\s*desk|"
     r"information\s+(?:technology|systems)|java|python|c\+\+|golang|"
     r"computing|gis|(?:software|solutions?|cloud|data|enterprise|systems?|security|network|"
     r"it|technical|application)\s+architect\w*|(?:systems?|solutions?|computer)\s+architecture|"
     r"systems?\s+analyst|sap|mainframe|iot|website\s+develop\w*|database|"
     r"game\s+design\w*|"
     r"unity|unreal|quantum)\b", "software"),
    (r"\b(systems?\s+engineer\w*|solutions?\s+engineer|engineer\w*|"
     r"technolog\w*)\b", "software"),
]
_CATEGORY_COMPILED = [(re.compile(p, re.IGNORECASE), c) for p, c in _CATEGORY_RULES]

CATEGORIES = ("software", "data_ml", "hardware", "quant", "product", "other")


# "Analyst", "scientist" and "research" put a title in data_ml, which also
# caught lab, finance and admin roles: "Clinical Laboratory Scientist I",
# "Credit Analyst Program", "Payroll Analyst I", "Research Archives Intern"
# were 165 of 1,479 data_ml announcements (2026-10-01..07). Such a title is
# "other" unless it also names tech work.
_NOT_TECH_WORK = (
    r".*\b(?:clinical|laborator\w*|lab|qc|quality\s+control|chromatograph\w*|microbio\w*|"
    r"pharmacolog\w*|in\s+vivo|accounting|accountant|accounts?\s+(?:payable|receivable)|"
    r"inventory|claims|commissions?|complaints|nurs\w*|pharmac\w*|archiv\w*|teaching|tutor|"
    r"administrative|retail|payroll|tax|audit\w*|financial\s+analyst|fp&a|credit\s+analyst|"
    r"procurement|purchasing|supply\s+chain|logistics|hr|human\s+resources|recruit\w*|legal|"
    r"paralegal|sales|marketing|communications|customer\s+service|phlebotom\w*|histolog\w*|"
    r"patholog\w*|veterinar\w*|dental|medical\s+assistant|technical\s+writ\w*|"
    # research outside computing ("Research Analyst 1 - Ophthalmology",
    # "Research Farm Summer Intern", "R&D Immunology")
    r"ophthalm\w*|oncolog\w*|cancer|immunolog\w*|biolog\w*|chemi\w*|molecular|cell|farm|"
    r"agricultur\w*|environmental|justice|policy|social|psycholog\w*|education|econom\w*)\b")
_NOT_TECH_DATA = re.compile(
    r"^(?!.*\b(?:data|machine\s+learning|ml|ai|artificial\s+intelligence|software|analytics|"
    r"engineer\w*|computational|bioinformatics|informatics|biostatistic\w*|statistic\w*|"
    r"quantitative|developer|programmer|cyber\w*|algorithm\w*|modell?ing|it|applications?|"
    r"systems?|gis|mapping)\b)" + _NOT_TECH_WORK, re.IGNORECASE)
# Software's weaker words ("application", "digital", "technical") likewise
# hit "Registered Nurse ... Fast-Track Application", "Digital Marketing
# Intern", "Technical Sales Development Program" (2026-10-07).
_NOT_TECH_SOFTWARE = re.compile(
    r"^(?!.*\b(?:software|swe|sde|developer|programmer|devops|sre|engineer\w*|technolog\w*|"
    r"information\s+systems|web|mobile|cloud|infrastructure|security|cyber\w*|computer|embedded|firmware|qa|sdet|"
    r"it|data|network\w*|database|coding|java|python)\b)" + _NOT_TECH_WORK, re.IGNORECASE)


def categorize(title: str) -> str:
    for pattern, category in _CATEGORY_COMPILED:
        if pattern.search(title):
            if (category == "data_ml" and _NOT_TECH_DATA.search(title)
                    or category == "software" and _NOT_TECH_SOFTWARE.search(title)):
                return "other"
            return category
    return "other"


# New-grad roles often have plain titles ("Software Engineer"); the
# description is what says so. Only consulted when the title is inconclusive.
_NG_STRONG = re.compile(
    r"\bnew[\s-]grad(?:uate)?s?\b|\brecent (?:college |university )?grad(?:uate)?s?\b|"
    r"\b(?:university|college) grad(?:uate)?s?\b|\bclass of 20\d\d\b|"
    r"\bgraduat(?:e|ing|ion)\w*\s+(?:in|by|between|from)\s+(?:\w+\s+)?20(?:2[5-9])\b|"
    r"\b(?:0|zero)\s*(?:-|–|to)\s*(?:1|2|one|two)\+?\s*years?\b|"
    r"\bno (?:prior |previous )?(?:professional |work |industry )?experience (?:is )?"
    r"(?:required|necessary)\b|\bentry[\s-]level (?:role|position|opportunity|engineer)\b")
_SENIOR_REQ = re.compile(
    r"\b(?:[3-9]|1\d)\s*\+?\s*(?:or more\s+)?years?\s+(?:of\s+)?(?:professional\s+|relevant\s+|"
    r"industry\s+|work\s+|hands-on\s+)?experience")


def new_grad_from_description(text: str) -> bool:
    """A plain-titled posting that the description marks as new-grad."""
    t = (text or "").lower()
    return bool(_NG_STRONG.search(t)) and not _SENIOR_REQ.search(t)


def role_hint_from_description(title: str, description: str) -> str:
    """'new_grad' for a title the keyword rules can't place but whose
    description marks it as new-grad; '' otherwise."""
    if classify_by_keyword(title) is None and new_grad_from_description(description):
        return "new_grad"
    return ""


# Seniority a title states outright. A curated list's intern/new_grad label
# gives way only to these: "Senior Full Stack Engineer" slipped into a list
# is senior, but a listed new-grad "Product Manager" or "AI Fellow" is not.
# "Manager" counts too (decided 2026-10-09): Simplify's new-grad list
# carries post-MBA roles like Capital One's "Manager, Product Management",
# too senior for this feed. Entry titles the rules already place first
# ("Associate Product Manager", "Product Manager Intern"); a "Manager in
# Training" isn't one.
_MANAGER = r"(?:manager|mgr)(?![\s-]*in[\s-]+training)"
_EXPLICIT_SENIOR = re.compile(
    r"\b(senior|sr\.?|(?<!technical )(?<!trading )(?<!research )staff|principal|lead|"
    r"distinguished|director|head\s+of|vp|vice\s+president|iii|iv|" + _ACADEMIC + r"|"
    + _MID + r"|" + _MANAGER + r")\b", re.IGNORECASE)


# An ATS's own level / employment-type value: "Summer Internship",
# "Intern/Co-op", "Full-Time: New Grad", "Entry level", "Early Career Talent".
_INTERN_LEVEL = re.compile(r"\b(?:intern\w*|co-?op|student|trainee)\b", re.I)
_GRAD_LEVEL = re.compile(r"\b(?:new\s*grad\w*|graduate|entry[\s_-]*level|early\s+career\w*|"
                         r"university|campus|emerging\s+talent)\b", re.I)


def hint_from_level(value: str) -> str:
    """'intern' / 'new_grad' for an ATS level value that says so, else ''."""
    if _INTERN_LEVEL.search(value or ""):
        return "intern"
    return "new_grad" if _GRAD_LEVEL.search(value or "") else ""


# Campus student jobs a board files under "Student": not internships
# ("School of Computer Science Teaching Assistant Positions", McGill).
_CAMPUS_STUDENT_JOB = re.compile(r"\b(?:teaching\s+assistant\w*|tas?\s+positions?|grader|"
                                 r"tutor\w*|peer\s+mentor|residen(?:t|ce)\s+(?:advisor|assistant)|"
                                 r"course\s+assistant|demonstrator|invigilator)\b", re.IGNORECASE)


_ENTRY_MANAGER = re.compile(r"\b(?:associate\s+(?:\w+\s+)?(?:product|program|project)\s+manager|"
                            + _APM + r")\b", re.IGNORECASE)


def role_with_hint(title: str, hint: str) -> str:
    """Role for a posting whose source asserts `hint` (intern/new_grad)."""
    by_title = classify_by_keyword(title)
    if by_title in ("intern", "new_grad"):
        return by_title
    if _CAMPUS_STUDENT_JOB.search(title):
        return by_title or "mid"
    if _EXPLICIT_SENIOR.search(title) and not _ENTRY_MANAGER.search(title):
        return "senior"
    return hint


def is_entry_level(title: str) -> bool:
    return classify_by_keyword(title) in ("intern", "new_grad")

# Descriptive labels for zero-shot NLI — more context beats bare words.
_ZS_LABELS = {
    "intern": "internship, co-op, or summer position for students",
    "new_grad": "entry-level position for new or recent graduates",
    "mid": "mid-level position requiring a few years of experience",
    "senior": "senior, staff, lead, principal, or management role",
}

_pipeline = None  # lazily loaded


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from transformers import pipeline
        _pipeline = pipeline(
            "zero-shot-classification",
            model="facebook/bart-large-mnli",
            device=-1,  # CPU; set to 0 for GPU
        )
    return _pipeline


# A new-grad match on a word that isn't about level ("... Requires WA
# Residency") doesn't outweigh stated seniority, unless the title also says
# it's junior ("Junior to Senior Power BI Developer").
_JUNIOR_STATED = re.compile(r"\b(?:junior|jr\.?|entry[\s-]*level|new\s*grad\w*|"
                            r"graduates?(?!\s*(?:&|and)\s+undergrad)|"
                            r"grads?|ncg|early\s+(?:in\s+)?careers?|campus|university|college|"
                            r"associate\s+(?:\w+\s+)?(?:product|program|project)\s+manager|"
                            + _APM + r"|apprentice\w*|rotation\w*|20\d\d|"
                            # one posting for several levels from associate up:
                            # "Associate Engineer/Engineer/Senior Engineer"
                            r"associate(?:\s+[\w&-]+){0,2}\s*(?:/|or\b|to\b)|/\s*associate\b|"
                            r"associate\s+(\w+)\s*,\s*\1)",
                            re.IGNORECASE)
# Stated seniority for that check; not "III"/"IV", which in "Technician
# I-IV" is a range starting at entry level.
_SENIOR_WORD = re.compile(r"\b(?:senior|sr\.?|(?<!technical )(?<!trading )(?<!research )staff|"
                          r"principal|lead|distinguished|director|head\s+of|vp|vice\s+president|"
                          + _MANAGER
                          + r")\b", re.IGNORECASE)


_RANK = re.compile(r"\b(?:senior|sr\.?|principal|distinguished|director|head\s+of|vp|"
                   r"vice\s+president)\b", re.IGNORECASE)


def classify_by_keyword(title: str) -> str | None:
    """Return a role type, or None if no rule matches (inconclusive)."""
    for pattern, label in _COMPILED:
        m = pattern.search(title)
        if m:
            if (label == "new_grad" and _SENIOR_WORD.search(title)
                    and not _JUNIOR_STATED.search(title)):
                return "senior"
            # "Placement" is a UK internship but also a line of business
            # ("Sr Analyst, Placement - Life"): stated rank wins. Not
            # "manager": "Industrial Placement - Project Manager" is a UK
            # internship.
            if (label == "intern" and m.group(0).lower().startswith("placement")
                    and _RANK.search(title)
                    and not re.search(r"\b(?:intern\w*|co-?op|student|apprentice\w*)\b", title,
                                      re.I)):
                return "senior"
            return label
    return None


def classify_by_zeroshot(title: str) -> str | None:
    """Zero-shot NLI classification. Returns a valid role type, or None on error."""
    try:
        clf = _get_pipeline()
        result = clf(title, list(_ZS_LABELS.values()), multi_label=False)
        best_desc = result["labels"][0]
        # Map description back to role type key
        desc_to_key = {v: k for k, v in _ZS_LABELS.items()}
        return desc_to_key.get(best_desc)
    except Exception as exc:
        log.warning(f"  ! zero-shot classify failed for {title!r}: {exc}")
        return None


def classify(title: str, settings: dict) -> str:
    """Keyword pass first; zero-shot NLI only on inconclusive titles. Defaults to 'mid'."""
    label = classify_by_keyword(title)
    if label:
        return label

    if settings.get("use_llm_fallback"):
        label = classify_by_zeroshot(title)
        if label:
            return label

    return "mid"  # safe default when nothing else decides


def _zeroshot_many(titles: list[str]) -> dict[str, str]:
    """One batched zero-shot call for many titles. Returns title -> role type."""
    try:
        clf = _get_pipeline()
        results = clf(titles, list(_ZS_LABELS.values()), multi_label=False,
                      batch_size=8)
        if isinstance(results, dict):    # pipeline unwraps single-item lists
            results = [results]
        desc_to_key = {v: k for k, v in _ZS_LABELS.items()}
        out = {}
        for title, res in zip(titles, results):
            key = desc_to_key.get(res["labels"][0])
            if key:
                out[title] = key
        return out
    except Exception as exc:
        log.warning(f"  ! zero-shot batch classify failed: {exc}")
        return {}


def classify_batch(titles: list[str], settings: dict) -> list[str]:
    """Classify many titles at once.

    Keyword pass first; the inconclusive remainder is deduped and sent through
    the zero-shot model in one batched call — orders of magnitude faster than
    one pipeline invocation per title on CPU.
    """
    labels = [classify_by_keyword(t) for t in titles]
    if settings.get("use_llm_fallback"):
        pending = sorted({t for t, l in zip(titles, labels) if l is None})
        if pending:
            log.info(f"  zero-shot classifying {len(pending)} unique ambiguous titles ...")
            resolved = _zeroshot_many(pending)
            labels = [l or resolved.get(t) for t, l in zip(titles, labels)]
    return [l or "mid" for l in labels]
