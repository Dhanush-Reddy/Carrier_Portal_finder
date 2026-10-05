"""Word lists used to recognise careers links and classify portals."""

from __future__ import annotations

import re

# Careers wording in link text, across the languages large employers use most.
CAREER_TEXT_RE = re.compile(
    r"\b(careers?|jobs?|job search|search jobs|join us|join our team|work with us|"
    r"work for us|working (?:at|here|with us)|vacanc(?:y|ies)|job openings|"
    r"open positions|recruitment|recruiting|karriere|jobs & karriere|stellenangebote|"
    r"carri[eè]res?|emplois?|carreras?|empleos?|trabaja con nosotros|carreiras?|"
    r"trabalhe conosco|vagas|lavora con noi|carriere|opportunit[aà] di lavoro|"
    r"werken bij|vacatures|kariera|praca|kariyer|jobb|ledige stillinger|"
    r"lediga jobb|työpaikat|rekrytointi)\b",
    re.IGNORECASE,
)
# Scripts with no word boundaries: matched as substrings.
CAREER_TEXT_CJK = ("採用", "招聘", "채용", "求人", "人才招聘", "加入我们", "職缺", "リクルート")

# Careers wording inside URLs (hosts and path segments).
CAREER_URL_TOKENS = {
    "career", "careers", "jobs", "job", "joinus", "join-us", "work-with-us",
    "workwithus", "work-for-us", "vacancies", "recruitment", "recruiting",
    "karriere", "stellenangebote", "carrieres", "carriere", "carreras", "empleo",
    "empleos", "carreiras", "vagas", "werkenbij", "werken-bij", "vacatures",
    "kariera", "kariyer", "saiyo", "recruit", "talent", "jobsearch", "job-search",
}

# Third-party job boards and social sites are never an official portal.
EXCLUDED_HOSTS = (
    "linkedin.com", "indeed.", "glassdoor.", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "monster.", "ziprecruiter.com", "naukri.com",
    "seek.com", "stepstone.", "xing.com", "tiktok.com", "weibo.com", "wechat.com",
    "apple.com/app-store", "play.google.com", "apps.apple.com",
)

GRADUATE_RE = re.compile(
    r"\b(graduates?|grad(?:uate)? programs?|early[ -]careers?|students?|campus|"
    r"universit(?:y|ies)|interns?|internships?|apprentices?|apprenticeships?|"
    r"trainees?|traineeships?|entry[ -]level|emerging talent|young professionals|"
    r"nachwuchs|absolventen|praktik(?:um|a)|ausbildung|duales studium|studierende|"
    r"sch[uü]ler|werkstudent(?:en)?|stagiaires?|alternance|becarios?|"
    r"pr[aá]cticas|estagi[aá]rios?|新卒|校园招聘|校招)\b",
    re.IGNORECASE,
)

REGION_WORDS = (
    "Europe", "EMEA", "APAC", "Asia Pacific", "Asia", "Americas", "North America",
    "Latin America", "LATAM", "Middle East", "Africa", "Nordics", "Benelux", "DACH",
    "Oceania", "Greater China", "ANZ",
)

COUNTRIES = (
    "Argentina", "Australia", "Austria", "Bangladesh", "Belgium", "Brazil", "Bulgaria",
    "Canada", "Chile", "China", "Colombia", "Costa Rica", "Croatia", "Czech Republic",
    "Czechia", "Denmark", "Egypt", "Estonia", "Finland", "France", "Germany", "Ghana",
    "Greece", "Hong Kong", "Hungary", "India", "Indonesia", "Ireland", "Israel", "Italy",
    "Japan", "Kenya", "Korea", "South Korea", "Latvia", "Lithuania", "Luxembourg",
    "Malaysia", "Mexico", "Morocco", "Netherlands", "New Zealand", "Nigeria", "Norway",
    "Pakistan", "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
    "Saudi Arabia", "Serbia", "Singapore", "Slovakia", "Slovenia", "South Africa", "Spain",
    "Sri Lanka", "Sweden", "Switzerland", "Taiwan", "Thailand", "Turkey", "Türkiye",
    "Ukraine", "United Arab Emirates", "UAE", "United Kingdom", "UK", "Great Britain",
    "United States", "USA", "Uruguay", "Vietnam", "Deutschland", "España", "Italia",
    "Brasil", "México", "Nederland", "Österreich", "Schweiz", "Suisse", "Polska",
    "Sverige", "Danmark", "Norge", "Suomi", "日本", "中国", "한국",
)

_REGION_NAMES = sorted(REGION_WORDS + COUNTRIES, key=len, reverse=True)
_REGION_TEXT_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(n) for n in _REGION_NAMES) + r")(?![\w])"
)
# URL path segments: "united-kingdom", "north_america", "india".
_REGION_SLUGS = {
    re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"): n
    for n in _REGION_NAMES
    if n.isascii() and len(n) > 3
}

COMPANY_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "llc", "lp", "llp", "group", "holdings", "holding", "sa", "ag", "se", "nv",
    "bv", "gmbh", "spa", "ab", "asa", "oyj", "as", "kk", "the", "and", "of", "de",
    "international", "global",
}

JOB_CONTENT_RE = re.compile(
    r"(search (?:all )?jobs|job search|view (?:all )?jobs|see (?:all )?jobs|"
    r"explore (?:all )?jobs|find (?:a )?jobs?|browse jobs|open positions|open roles|"
    r"current openings|job openings|job opportunities|apply now|vacancies|"
    r"stellenangebote|offres d.emploi|ofertas de empleo|vagas abertas|"
    r"posizioni aperte|vacatures)",
    re.IGNORECASE,
)


def has_career_text(text: str) -> bool:
    return bool(CAREER_TEXT_RE.search(text)) or any(t in text for t in CAREER_TEXT_CJK)


def url_tokens(host: str, path: str) -> set[str]:
    tokens = set(re.split(r"[./]", host.lower()))
    for seg in path.lower().split("/"):
        if seg:
            tokens.add(seg)
            tokens.update(re.split(r"[-_]", seg))
    return tokens


def has_career_url(host: str, path: str) -> bool:
    return bool(url_tokens(host, path) & CAREER_URL_TOKENS)


def region_of(text: str, path: str) -> str | None:
    m = _REGION_TEXT_RE.search(text)
    if m:
        return m.group(1)
    for seg in path.lower().split("/"):
        seg = re.sub(r"[^a-z0-9]+", "-", seg).strip("-")
        if seg in _REGION_SLUGS:
            return _REGION_SLUGS[seg]
    return None


def name_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in tokens if t not in COMPANY_SUFFIXES and len(t) >= 2]


def name_matches(name: str, *targets: str) -> bool:
    """True if the whole company name (minus suffixes) appears in a URL or title.

    Deliberately strict: "General Motors" must not match "general-electric".
    """
    tokens = name_tokens(name)
    if not tokens:
        return False
    joined = "".join(tokens)
    for target in targets:
        compact = re.sub(r"[^a-z0-9]+", "", (target or "").lower())
        if len(joined) >= 3 and joined in compact:
            return True
    return False
