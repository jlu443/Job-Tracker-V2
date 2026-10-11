"""Which field a job belongs to, for the Specializations sheet.

Six fields, one per job, checked in this order (first match wins):

    cyber       Cybersecurity      security engineering/analysis, SOC, IAM, GRC
    data_ai     Data & AI/ML       data science/engineering/analytics, ML/AI, BI
    tech        Tech               software, hardware, IT, product
    healthcare  Healthcare         clinical care, nursing, pharmacy, therapy, public health
    science     Science & Research lab and research science (biology, chemistry, physics, ...)
    business    Business           finance, consulting, marketing, sales, operations, HR, legal

Built on classify.categorize (the tech functions) plus title keywords for the
rest. Jobs in none of them (mechanical / civil engineering, trades,
teaching, ...) aren't listed.
"""

from __future__ import annotations

import re

SPECIALIZATIONS = [
    ("tech", "Tech"),
    ("business", "Business"),
    ("healthcare", "Healthcare"),
    ("science", "Science & Research"),
    ("data_ai", "Data & AI/ML"),
    ("cyber", "Cybersecurity"),
]

_I = re.IGNORECASE

_CYBER = re.compile(
    r"\b(cyber\w*|infosec|information\s+security|"
    r"security\s+(?:engineer\w*|analyst|operations|architect\w*|research\w*|consult\w*|"
    r"specialist|intern\w*|co-?op|associate|software|assurance|compliance|risk|testing|"
    r"tester|developer|automation|governance)|"
    r"(?:application|network|cloud|product|offensive|defensive|data|it|endpoint|platform|"
    r"enterprise|systems?|information|software|embedded)\s+security|"
    r"soc\s+analyst|security\s+operations\s+cent(?:er|re)|penetration|pen[\s-]?test\w*|"
    r"red\s+team\w*|blue\s+team\w*|threat\s+(?:intel\w*|hunt\w*|detection|analy\w*)|"
    r"vulnerabilit\w*|incident\s+response|identity\s+(?:(?:&|and)\s+)?access|iam|"
    r"devsecops|appsec|secops|grc|digital\s+forensics|malware|cryptograph\w*|zero\s+trust)\b", _I)
# Guards, patrol, campus police: physical security, not cyber.
_PHYSICAL_SECURITY = re.compile(
    r"\b(guard|officer|patrol|loss\s+prevention|armed|unarmed|campus\s+security|"
    r"site\s+security|physical\s+security|security\s+services|social\s+security|"
    r"event\s+security|security\s+(?:screener|dispatch\w*|supervisor))\b", _I)

# data_ml titles whose evidence is data / ML / analytics, not just "research"
# or robotics.
_DATA_AI = re.compile(
    r"\b(machine\s+learning|ml|ai|artificial\s+intelligence|deep\s+learning|gen\s*ai|llm\w*|"
    r"nlp|natural\s+language|computer\s+vision|vision|data|analytics|analyst|statistic\w*|"
    r"business\s+intelligence|bi|power\s+bi|informatics|bioinformatics|biostatistic\w*|"
    r"operations\s+research|algorithm\w*|computational|geospatial|gis|image\s+processing|"
    r"applied\s+scien\w*|decision\s+scien\w*|quantitative)\b", _I)
_ROBOTICS = re.compile(r"\b(robot\w*|autonom\w*|perception|motion\s+planning|mechatronic\w*)\b",
                       _I)
_COMPUTING = re.compile(r"\b(engineer\w*|software|developer|programm\w*|computer|computing|"
                        r"systems?|ux|user\s+experience)\b", _I)
_QUANT_DEV = re.compile(r"\b(developer|software|engineer\w*|programm\w*|technolog\w*)\b", _I)

_HEALTH = re.compile(
    r"\b(nurs\w*|rn|lpn|cna|clinical|clinic|patients?|hospital\w*|medical|medicine|"
    r"physician\w*|surg(?:ery|ical|eon)\w*|pharmacy|pharmacist\w*|pharmacies|therap(?:y|ist)\w*|"
    r"rehab\w*|dental|dentist\w*|hygienist|orthodont\w*|veterinar\w*|vet\s+tech\w*|emt|"
    r"paramedic\w*|radiolog\w*|radiograph\w*|sonograph\w*|ultrasound|x-ray|mri|"
    r"phlebotom\w*|respiratory|anesthe\w*|optometr\w*|ophthalm\w*|audiolog\w*|"
    r"speech[\s-]language|behavioral\s+health|mental\s+health|health|healthcare|caregiver\w*|"
    r"home\s+care|care\s+(?:coordinator|manager|navigator|partner|team)|dietitian\w*|"
    r"nutrition\w*|public\s+health|epidemiolog\w*|histolog\w*|cytolog\w*|patholog\w*|"
    r"oncolog\w*|cardi(?:ac|o\w*)|neurolog\w*|pediatric\w*|psychiatr\w*|counsel(?:or|ing)|"
    r"social\s+work\w*|scribe|telemetry|icu|emergency\s+department|perioperative|"
    r"medical\s+assistant|pharmacy\s+technician)\b", _I)

_SCIENCE = re.compile(
    r"\b(scien\w*|research\w*|lab|labs|laborator\w*|biolog\w*|biochem\w*|biotech\w*|"
    r"bioprocess\w*|bioassay\w*|biophysic\w*|chemi\w*|chemist\w*|physics|physicist\w*|astro\w*|"
    r"geolog\w*|geophysic\w*|geoscien\w*|ecolog\w*|environmental\s+(?:science|scientist|"
    r"technician|specialist|intern\w*|monitoring|sampling)|microbio\w*|molecular|"
    r"cell\s+(?:culture|biology|therapy|line)|genetic\w*|genomic\w*|immunolog\w*|"
    r"neuroscien\w*|pharmacolog\w*|toxicolog\w*|assay\w*|formulation|analytical|"
    r"r\s*&\s*d|postdoc\w*|post[\s-]doc\w*|materials\s+scien\w*|agronom\w*|botan\w*|"
    r"zoolog\w*|marine|oceanograph\w*|hydrolog\w*|meteorolog\w*|atmospheric|wildlife|"
    r"fisheries|forestry|conservation|entomolog\w*|plant\s+breed\w*)\b", _I)
# Engineers (medical devices, clinical engineering) and workplace safety
# ("Environmental, Health & Safety Intern") aren't patient care.
_NOT_HEALTH = re.compile(r"\b(engineer\w*|ehs|hse|health\s*(?:,\s*|&\s*|and\s+)safety)\b", _I)

# Research that's business work.
_BUSINESS_RESEARCH = re.compile(
    r"\b(market|equity|investment|credit|consumer|customer|sales|ux|user|fixed\s+income|"
    r"economic|policy)\s+research\b", _I)

_BUSINESS = re.compile(
    r"\b(financ\w*|accounting|accountant\w*|audit\w*|tax|banking|bank|banker|investment\w*|"
    r"investor\w*|wealth|asset\s+management|private\s+equity|equity\s+research|venture|"
    r"capital\s+markets?|insurance|actuar\w*|underwrit\w*|credit|lending|loans?|mortgage|"
    r"treasury|trading|traders?|quant\w*|economics?|economist|consult\w*|strategy|strategic|"
    r"marketing|brand|advertising|media\s+planning|public\s+relations|communications|sales|"
    r"business|commercial|merchandis\w*|retail|buyer|purchasing|procurement|sourcing|"
    r"supply\s+chain|logistics|operations|project\s+manag\w*|program\s+manag\w*|"
    r"management\s+(?:trainee|consult\w*|development|program|intern\w*|associate)|"
    r"leadership\s+(?:development\s+)?program|human\s+resources|hr|recruit\w*|talent|"
    r"people\s+(?:operations|partner|analytics|team)|payroll|compensation|benefits|"
    r"real\s+estate|property|legal|paralegal|law\s+clerk|compliance|risk|fraud|aml|kyc|"
    r"customer\s+success|account\s+(?:manag\w*|executive|coordinator)|client|analyst|"
    r"market|e-?commerce|events?\s+(?:coordinator|planning)|fundrais\w*|development\s+officer|"
    r"relationship\s+manag\w*|customer\s+(?:service|experience|support)|social\s+media|"
    r"digital\s+media|government\s+(?:relations|affairs)|public\s+(?:policy|affairs))\b",
    _I)
# Hands-on and engineering roles a business keyword can appear in ("Sales
# Engineer", "Operations Technician", "Commercial Electrician").
_NOT_BUSINESS = re.compile(
    r"\b(engineer\w*|technician\w*|mechanic\w*|operators?|drivers?|electrician\w*|welder\w*|"
    r"machinist\w*|assembler\w*|installer\w*|teacher\w*|tutor\w*|cook\w*|chef\w*|"
    r"warehouse|forklift|janitor\w*|custodian\w*|cashier\w*|barista\w*|server\w*|"
    r"lifeguard\w*|nanny|housekeep\w*)\b", _I)


def specialization(title: str, category: str) -> str:
    """The field a job belongs to (see the module docstring), or '' for none."""
    t = title or ""
    if _CYBER.search(t) and not _PHYSICAL_SECURITY.search(t):
        return "cyber"
    if category == "data_ml":
        if _DATA_AI.search(t):
            return "data_ai"
        if _ROBOTICS.search(t) or _COMPUTING.search(t):
            return "tech"               # "Robotics Engineer", "Research Engineer"
        if _BUSINESS_RESEARCH.search(t):
            return "business"           # "Market Research Intern"
        return "science"                # "Research Intern", "Research Scientist"
    if category in ("software", "hardware", "product"):
        return "tech"
    if category == "quant":
        return "tech" if _QUANT_DEV.search(t) else "business"
    if _HEALTH.search(t) and not _NOT_HEALTH.search(t):
        return "healthcare"
    if _SCIENCE.search(t) and not _BUSINESS_RESEARCH.search(t):
        return "science"
    if _BUSINESS.search(t) and not _NOT_BUSINESS.search(t):
        return "business"
    return ""
