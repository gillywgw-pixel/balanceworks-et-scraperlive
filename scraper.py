"""
GOV.UK Employment Tribunal Decisions Scraper
============================================
Searches the GOV.UK ET decisions service for cases tagged under Equality Act 2010
relevant jurisdiction codes. Extracts all available metadata from HTML pages
(no PDF parsing). Results are written to data/et-decisions.json and merged with
any previously scraped cases so the file is cumulative.

Metadata available from individual decision pages (without PDF):
  - case_ref          : Case reference number (e.g. "2304666/2020")
  - case_name         : Full "Claimant v Respondent: ref" string
  - claimant          : Claimant name extracted from case name
  - respondent        : Respondent name extracted from case name
  - decision_date     : Date of decision (ISO 8601)
  - publication_date  : Date published on GOV.UK (ISO 8601)
  - jurisdiction_codes: List of jurisdiction tags shown on the page
  - country           : "England and Wales" or "Scotland"
  - tribunal_level    : "Employment Tribunal" or "Employment Appeal Tribunal"
  - pdf_url           : Direct URL to the decision PDF
  - page_url          : URL of the GOV.UK HTML decision page
  - scraped_at        : ISO 8601 timestamp of when this record was scraped

NOT available from metadata (requires PDF parsing):
  - Outcome (claimant win / respondent win / settled)
  - Award amount or breakdown
  - Judge name
  - Tribunal office / hearing centre
  - Hearing date(s)

Usage:
  pip install requests beautifulsoup4 lxml
  python scraper.py

The script respects GOV.UK rate limits: 1-second delay between page requests.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL        = "https://www.gov.uk"
SEARCH_PATH     = "/employment-tribunal-decisions"
OUTPUT_FILE     = Path("data/et-decisions.json")
REQUEST_DELAY   = 1.2          # seconds between requests (polite crawling)
MAX_PAGES       = 50           # safety cap per jurisdiction search (~500 cases)
SESSION_TIMEOUT = 15           # seconds

# Jurisdiction codes relevant to Equality Act 2010 discrimination claims.
# These map to the jurisdiction_id URL parameter on the search page.
EA2010_JURISDICTION_IDS = [
    "AdaDis",   # Age Discrimination
    "DaDis",    # Disability Discrimination
    "SexD",     # Sex Discrimination
    "RacD",     # Race Discrimination
    "ReBDis",   # Religion or Belief Discrimination
    "SODis",    # Sexual Orientation Discrimination/Transexualism
    "MP",       # Maternity and Pregnancy Rights
    "VicDis",   # Victimisation Discrimination
    "Har",      # Harassment
]

# Supplementary keyword search for cases mentioning the Act directly
KEYWORD_QUERIES = [
    "equality act 2010",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── HTTP session ───────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "BalanceWorks-ET-Tracker/1.0 "
            "(research tool; equality data; "
            "contact: hello@balanceworks.org.uk)"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return s


# ── Parsing utilities ─────────────────────────────────────────────────────────

def parse_date(raw: str) -> str:
    """Convert 'DD Month YYYY' or 'Month YYYY' to ISO 8601 date string."""
    raw = raw.strip()
    for fmt in ("%d %B %Y", "%B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw  # return as-is if unparseable


def split_case_name(case_name: str) -> tuple[str, str, str]:
    """
    Split 'Claimant v Respondent: 1234567/2023' into
    (claimant, respondent, ref). Best-effort only.
    """
    ref = ""
    ref_match = re.search(r"[:\s](\d{7}/\d{4})\s*$", case_name)
    if ref_match:
        ref = ref_match.group(1)
        name_part = case_name[: ref_match.start()].strip().rstrip(":")
    else:
        name_part = case_name.strip()

    # Split on ' v ' (case-insensitive, surrounded by spaces)
    v_split = re.split(r"\s+v\s+", name_part, maxsplit=1, flags=re.IGNORECASE)
    if len(v_split) == 2:
        claimant, respondent = v_split[0].strip(), v_split[1].strip()
    else:
        claimant, respondent = name_part, ""

    return claimant, respondent, ref


# ── Search result scraper ─────────────────────────────────────────────────────

def get_search_page(
    session: requests.Session,
    params: dict,
    page: int,
) -> list[dict]:
    """Fetch one page of search results. Returns list of {slug, case_name, date_raw}."""
    p = dict(params)
    p["page"] = page
    url = BASE_URL + SEARCH_PATH + "?" + urlencode(p)
    log.debug("GET %s", url)

    try:
        r = session.get(url, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Search page failed (%s): %s", url, exc)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    results = []

    # GOV.UK ET decisions: each result is in a <div class="gem-c-document-list__item">
    for item in soup.select(".gem-c-document-list__item, li.gem-c-document-list__item"):
        link_el = item.select_one("a")
        if not link_el:
            continue
        href  = link_el.get("href", "")
        title = link_el.get_text(strip=True)

        # Extract date from the metadata line
        date_raw = ""
        for meta in item.select(".gem-c-document-list__item-metadata, .gem-c-document-list__attribute"):
            text = meta.get_text(strip=True)
            if re.search(r"\d{4}", text):
                date_raw = re.sub(r"^(Decided|Published|Date):\s*", "", text, flags=re.I)
                break

        slug = href.rstrip("/").split("/")[-1] if href else ""
        if slug and SEARCH_PATH in href:
            results.append({
                "slug":      slug,
                "case_name": title,
                "date_raw":  date_raw,
                "page_url":  urljoin(BASE_URL, href),
            })

    return results


def get_total_pages(session: requests.Session, params: dict) -> int:
    """Fetch page 1 to determine total page count."""
    p = dict(params)
    p["page"] = 1
    url = BASE_URL + SEARCH_PATH + "?" + urlencode(p)
    try:
        r = session.get(url, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return 0

    soup = BeautifulSoup(r.text, "lxml")

    # Look for "page N of M" in pagination
    nav = soup.select_one(".gem-c-pagination, .pagination, nav[aria-label*='pagination']")
    if nav:
        text = nav.get_text(" ", strip=True)
        m = re.search(r"page\s+\d+\s+of\s+(\d+)", text, re.I)
        if m:
            return min(int(m.group(1)), MAX_PAGES)

    # Fallback: count result items; if < 10, assume 1 page
    items = soup.select(".gem-c-document-list__item")
    return 1 if items else 0


# ── Individual decision page scraper ─────────────────────────────────────────

def scrape_decision_page(session: requests.Session, page_url: str) -> dict:
    """
    Fetch an individual decision page and extract all available metadata.
    Returns a dict with keys matching the schema documented at the top of this file.
    """
    time.sleep(REQUEST_DELAY)
    log.debug("GET %s", page_url)

    try:
        r = session.get(page_url, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Decision page failed (%s): %s", page_url, exc)
        return {}

    soup = BeautifulSoup(r.text, "lxml")

    # ── Case name / title ──────────────────────────────────────────────────────
    title_el = soup.select_one("h1.gem-c-title__text, h1")
    case_name = title_el.get_text(strip=True) if title_el else ""
    claimant, respondent, case_ref = split_case_name(case_name)

    # ── Metadata section ───────────────────────────────────────────────────────
    decision_date    = ""
    publication_date = ""
    jurisdiction_codes: list[str] = []
    country          = ""
    tribunal_level   = "Employment Tribunal"
    pdf_url          = ""

    # GOV.UK document detail pages use dl.gem-c-metadata or similar
    meta_dl = soup.select_one("dl.gem-c-metadata, .publication-header__metadata")
    if meta_dl:
        items = meta_dl.find_all("dt")
        for dt in items:
            label = dt.get_text(strip=True).lower()
            dd    = dt.find_next_sibling("dd")
            if not dd:
                continue
            value = dd.get_text(" ", strip=True)

            if "decision date" in label or "decided" in label:
                decision_date = parse_date(value)
            elif "published" in label or "publication" in label:
                publication_date = parse_date(value)
            elif "jurisdiction" in label:
                # Multiple jurisdictions may be listed as separate <li> or comma-separated
                jcodes = [li.get_text(strip=True) for li in dd.select("li")]
                if not jcodes:
                    jcodes = [v.strip() for v in value.split(",") if v.strip()]
                jurisdiction_codes = jcodes
            elif "country" in label or "nation" in label:
                country = value
            elif "tribunal" in label or "court" in label:
                tribunal_level = value

    # ── Fallback: look for structured data / JSON-LD ──────────────────────────
    for script in soup.select("script[type='application/ld+json']"):
        try:
            ld = json.loads(script.string or "")
            if isinstance(ld, dict):
                if not decision_date and ld.get("datePublished"):
                    decision_date = parse_date(ld["datePublished"])
                if not case_name and ld.get("name"):
                    case_name = ld["name"]
        except (json.JSONDecodeError, AttributeError):
            pass

    # ── Try to find date from page body if metadata parsing failed ─────────────
    if not decision_date:
        date_pattern = re.compile(
            r"(?:Decided|Decision date|Date):\s*(\d{1,2}\s+\w+\s+\d{4})",
            re.I,
        )
        body_text = soup.get_text(" ", strip=True)
        m = date_pattern.search(body_text)
        if m:
            decision_date = parse_date(m.group(1))

    # ── PDF link ───────────────────────────────────────────────────────────────
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if href.endswith(".pdf") or "/uploads/" in href or "assets.publishing" in href:
            pdf_url = urljoin(BASE_URL, href)
            break

    return {
        "case_ref":           case_ref,
        "case_name":          case_name,
        "claimant":           claimant,
        "respondent":         respondent,
        "decision_date":      decision_date,
        "publication_date":   publication_date,
        "jurisdiction_codes": jurisdiction_codes,
        "country":            country or "England and Wales",
        "tribunal_level":     tribunal_level,
        "pdf_url":            pdf_url,
        "page_url":           page_url,
        "scraped_at":         datetime.now(timezone.utc).isoformat(),
    }


# ── Main scrape loop ──────────────────────────────────────────────────────────

def load_existing(path: Path) -> dict[str, dict]:
    """Load existing JSON keyed by page_url to avoid re-scraping."""
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {c["page_url"]: c for c in data if c.get("page_url")}
            if isinstance(data, dict) and "cases" in data:
                return {c["page_url"]: c for c in data["cases"] if c.get("page_url")}
        except (json.JSONDecodeError, KeyError):
            log.warning("Could not load existing data — starting fresh.")
    return {}


def save_results(path: Path, cases: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_cases = sorted(
        cases.values(),
        key=lambda c: c.get("decision_date", ""),
        reverse=True,
    )
    output = {
        "meta": {
            "description": (
                "Employment Tribunal decisions tagged with Equality Act 2010 "
                "jurisdiction codes. Metadata only — outcomes and awards not "
                "available without PDF parsing."
            ),
            "source":        "GOV.UK Employment Tribunal Decisions service",
            "source_url":    "https://www.gov.uk/employment-tribunal-decisions",
            "last_updated":  datetime.now(timezone.utc).isoformat(),
            "total_cases":   len(sorted_cases),
            "fields_note": (
                "Fields available from metadata: case_ref, case_name, claimant, "
                "respondent, decision_date, publication_date, jurisdiction_codes, "
                "country, tribunal_level, pdf_url, page_url, scraped_at. "
                "Fields NOT available without PDF parsing: outcome, award, "
                "judge, tribunal_office, hearing_date."
            ),
        },
        "cases": sorted_cases,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("Saved %d cases to %s", len(sorted_cases), path)


def collect_slugs(session: requests.Session) -> dict[str, dict]:
    """
    Run all searches and return a dict of {page_url: partial_record}.
    Does not yet fetch individual pages.
    """
    slugs: dict[str, dict] = {}

    # Search by jurisdiction ID
    for jid in EA2010_JURISDICTION_IDS:
        params = {"jurisdiction_id": jid}
        log.info("Searching jurisdiction_id=%s …", jid)
        total = get_total_pages(session, params)
        log.info("  %d pages found for %s", total, jid)
        for page in range(1, total + 1):
            time.sleep(REQUEST_DELAY)
            results = get_search_page(session, params, page)
            for r in results:
                if r["page_url"] not in slugs:
                    slugs[r["page_url"]] = r
            log.info("  page %d/%d — %d unique cases so far", page, total, len(slugs))

    # Search by keyword
    for q in KEYWORD_QUERIES:
        params = {"query": q}
        log.info("Searching query='%s' …", q)
        total = get_total_pages(session, params)
        log.info("  %d pages found for '%s'", total, q)
        for page in range(1, total + 1):
            time.sleep(REQUEST_DELAY)
            results = get_search_page(session, params, page)
            for r in results:
                if r["page_url"] not in slugs:
                    slugs[r["page_url"]] = r
            log.info("  page %d/%d — %d unique cases so far", page, total, len(slugs))

    return slugs


def main() -> None:
    log.info("=== Balance Works ET Decisions Scraper ===")
    session  = make_session()
    existing = load_existing(OUTPUT_FILE)
    log.info("Loaded %d existing cases from %s", len(existing), OUTPUT_FILE)

    # Collect candidate URLs from search results
    candidates = collect_slugs(session)
    new_urls   = [url for url in candidates if url not in existing]
    log.info(
        "Found %d candidates (%d already scraped, %d new)",
        len(candidates), len(existing), len(new_urls),
    )

    # Fetch individual pages only for new cases
    merged = dict(existing)
    for i, url in enumerate(new_urls, 1):
        log.info("[%d/%d] Scraping: %s", i, len(new_urls), url)
        detail = scrape_decision_page(session, url)
        if detail:
            merged[url] = detail
        if i % 50 == 0:
            # Intermediate save every 50 cases in case of interruption
            save_results(OUTPUT_FILE, merged)
            log.info("Intermediate save at %d cases.", len(merged))

    save_results(OUTPUT_FILE, merged)
    log.info("Done. Total cases: %d", len(merged))


if __name__ == "__main__":
    main()
