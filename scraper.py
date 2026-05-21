"""
GOV.UK Employment Tribunal Decisions Scraper — Phase 3
======================================================
Searches the GOV.UK ET decisions service for cases tagged under Equality Act 2010
relevant jurisdiction codes. Extracts all available metadata from HTML pages,
then (optionally) downloads each decision PDF, extracts text with pdfplumber,
and sends it to the Claude API (claude-haiku-4-5-20251001) for structured analysis.

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

Additional fields populated by PDF parsing (Phase 3):
  - outcome           : "Claimant Won", "Respondent Won", "Settled",
                        "Withdrawn", or "Unknown"
  - total_award       : Total award in pounds as a number, or null
  - award_breakdown   : { injury_to_feelings, loss_of_earnings, other } (all numbers or null)
  - protected_characteristics : list from the nine EA2010 characteristics
  - claim_types       : list from Direct Discrimination, Indirect Discrimination,
                        Harassment, Victimisation, Failure to Make Reasonable Adjustments
  - judge             : Judge name as a string, or null
  - tribunal_office   : Tribunal office / hearing centre name, or null
  - key_principle     : One-sentence summary of the key legal principle, or null
  - pdf_parsed        : true if PDF was successfully parsed, false otherwise
  - pdf_parsed_at     : ISO 8601 timestamp of when PDF was parsed

Usage:
  pip install requests beautifulsoup4 lxml pdfplumber anthropic
  python scraper.py                  # normal scrape (no PDF parsing)
  python scraper.py --parse-pdfs     # scrape + parse PDFs for new cases
  python scraper.py --retroactive    # parse PDFs for all cases with null outcome

Requires ANTHROPIC_API_KEY environment variable for PDF parsing.
The script respects GOV.UK rate limits: 1.2-second delay between page requests.
"""

import argparse
import io
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
PDF_TIMEOUT     = 30           # seconds for PDF downloads
PDF_MAX_CHARS   = 12000        # characters to send to Claude (cost control)

# Jurisdiction codes relevant to Equality Act 2010 discrimination claims.
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

KEYWORD_QUERIES = ["equality act 2010"]

# Claude model for PDF analysis
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

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
            "BalanceWorks-ET-Tracker/2.0 "
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
    return raw


def split_case_name(case_name: str) -> tuple[str, str, str]:
    """Split 'Claimant v Respondent: 1234567/2023' into (claimant, respondent, ref)."""
    ref = ""
    ref_match = re.search(r"[:\s](\d{7}/\d{4})\s*$", case_name)
    if ref_match:
        ref = ref_match.group(1)
        name_part = case_name[: ref_match.start()].strip().rstrip(":")
    else:
        name_part = case_name.strip()

    v_split = re.split(r"\s+v\s+", name_part, maxsplit=1, flags=re.IGNORECASE)
    if len(v_split) == 2:
        claimant, respondent = v_split[0].strip(), v_split[1].strip()
    else:
        claimant, respondent = name_part, ""

    return claimant, respondent, ref


# ── PDF text extraction ────────────────────────────────────────────────────────

def extract_pdf_text(session: requests.Session, pdf_url: str) -> str | None:
    """
    Download a PDF from pdf_url and extract its text using pdfplumber.
    Returns extracted text (up to PDF_MAX_CHARS), or None on any failure.
    """
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — skipping PDF parsing")
        return None

    if not pdf_url:
        return None

    try:
        log.debug("Downloading PDF: %s", pdf_url)
        r = session.get(pdf_url, timeout=PDF_TIMEOUT, stream=True)
        r.raise_for_status()
        pdf_bytes = r.content
    except requests.RequestException as exc:
        log.warning("PDF download failed (%s): %s", pdf_url, exc)
        return None

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            full_text = "\n".join(pages_text)
        if not full_text.strip():
            log.warning("PDF extracted but no text found: %s", pdf_url)
            return None
        return full_text[:PDF_MAX_CHARS]
    except Exception as exc:
        log.warning("PDF text extraction failed (%s): %s", pdf_url, exc)
        return None


# ── Claude API structured extraction ─────────────────────────────────────────

NULL_PARSE_RESULT = {
    "outcome":                   "Unknown",
    "total_award":               None,
    "award_breakdown":           {
        "injury_to_feelings": None,
        "loss_of_earnings":   None,
        "other":              None,
    },
    "protected_characteristics": [],
    "claim_types":               [],
    "judge":                     None,
    "tribunal_office":           None,
    "key_principle":             None,
}

CLAUDE_PROMPT_TEMPLATE = """\
You are analysing an Employment Tribunal decision document from the UK.

Extract the following information from the text below and return ONLY a JSON object — no preamble, no explanation, no markdown fences.

JSON fields required:
- "outcome": one of "Claimant Won", "Respondent Won", "Settled", "Withdrawn", "Unknown"
- "total_award": total monetary award in pounds as a number (integer or float), or null if none
- "award_breakdown": object with three keys, each a number in pounds or null:
    - "injury_to_feelings"
    - "loss_of_earnings"
    - "other"
- "protected_characteristics": array of strings, each one of:
    Age, Disability, Gender Reassignment, Marriage and Civil Partnership,
    Pregnancy and Maternity, Race, Religion or Belief, Sex, Sexual Orientation
  (use only characteristics explicitly mentioned or clearly at issue)
- "claim_types": array of strings, each one of:
    Direct Discrimination, Indirect Discrimination, Harassment,
    Victimisation, Failure to Make Reasonable Adjustments
  (use only claim types that were actively considered)
- "judge": full name of the Employment Judge as a string, or null
- "tribunal_office": name of the tribunal office or hearing centre as a string, or null
- "key_principle": one concise sentence (max 25 words) summarising the key legal point decided, or null

DECISION TEXT:
{text}
"""


def parse_pdf_with_claude(pdf_text: str, api_key: str) -> dict:
    """
    Send extracted PDF text to Claude Haiku for structured extraction.
    Returns a dict with the extracted fields, or NULL_PARSE_RESULT on failure.
    """
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — skipping Claude parsing")
        return dict(NULL_PARSE_RESULT)

    if not pdf_text or not api_key:
        return dict(NULL_PARSE_RESULT)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = CLAUDE_PROMPT_TEMPLATE.format(text=pdf_text)

    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip any accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)

        # Validate and normalise — fall back to null for any missing/invalid field
        result = dict(NULL_PARSE_RESULT)

        outcome = parsed.get("outcome", "Unknown")
        if outcome in ("Claimant Won", "Respondent Won", "Settled", "Withdrawn", "Unknown"):
            result["outcome"] = outcome

        award = parsed.get("total_award")
        result["total_award"] = float(award) if isinstance(award, (int, float)) else None

        breakdown = parsed.get("award_breakdown") or {}
        result["award_breakdown"] = {
            "injury_to_feelings": (
                float(breakdown.get("injury_to_feelings"))
                if isinstance(breakdown.get("injury_to_feelings"), (int, float)) else None
            ),
            "loss_of_earnings": (
                float(breakdown.get("loss_of_earnings"))
                if isinstance(breakdown.get("loss_of_earnings"), (int, float)) else None
            ),
            "other": (
                float(breakdown.get("other"))
                if isinstance(breakdown.get("other"), (int, float)) else None
            ),
        }

        valid_chars = {
            "Age", "Disability", "Gender Reassignment",
            "Marriage and Civil Partnership", "Pregnancy and Maternity",
            "Race", "Religion or Belief", "Sex", "Sexual Orientation",
        }
        chars = parsed.get("protected_characteristics") or []
        result["protected_characteristics"] = [c for c in chars if c in valid_chars]

        valid_claims = {
            "Direct Discrimination", "Indirect Discrimination", "Harassment",
            "Victimisation", "Failure to Make Reasonable Adjustments",
        }
        claims = parsed.get("claim_types") or []
        result["claim_types"] = [c for c in claims if c in valid_claims]

        judge = parsed.get("judge")
        result["judge"] = str(judge).strip() if judge else None

        office = parsed.get("tribunal_office")
        result["tribunal_office"] = str(office).strip() if office else None

        principle = parsed.get("key_principle")
        result["key_principle"] = str(principle).strip() if principle else None

        return result

    except json.JSONDecodeError as exc:
        log.warning("Claude response was not valid JSON: %s", exc)
        return dict(NULL_PARSE_RESULT)
    except Exception as exc:
        log.warning("Claude API call failed: %s", exc)
        return dict(NULL_PARSE_RESULT)


def parse_case_pdf(session: requests.Session, case: dict, api_key: str) -> dict:
    """
    Full pipeline: download PDF → extract text → Claude analysis.
    Returns a dict with pdf_parsed, pdf_parsed_at, and all extracted fields.
    Always returns something — failures produce null fields with pdf_parsed=False.
    """
    pdf_url = case.get("pdf_url", "")
    base = {
        "pdf_parsed":    False,
        "pdf_parsed_at": datetime.now(timezone.utc).isoformat(),
        **NULL_PARSE_RESULT,
    }

    pdf_text = extract_pdf_text(session, pdf_url)
    if not pdf_text:
        log.info("  PDF text extraction failed or empty for %s", case.get("case_ref", "?"))
        return base

    log.info("  PDF text extracted (%d chars) — calling Claude …", len(pdf_text))
    time.sleep(0.5)  # brief pause before API call

    parsed = parse_pdf_with_claude(pdf_text, api_key)
    base.update(parsed)
    base["pdf_parsed"] = True
    base["pdf_parsed_at"] = datetime.now(timezone.utc).isoformat()

    log.info(
        "  → outcome=%s  award=%s  chars=%s  claims=%s  judge=%s",
        parsed.get("outcome"),
        parsed.get("total_award"),
        parsed.get("protected_characteristics"),
        parsed.get("claim_types"),
        parsed.get("judge"),
    )
    return base


# ── Search result scraper ─────────────────────────────────────────────────────

def get_search_page(session: requests.Session, params: dict, page: int) -> list[dict]:
    """Fetch one page of search results. Returns list of {slug, case_name, date_raw}."""
    p = dict(params)
    p["page"] = page
    url = BASE_URL + SEARCH_PATH + "?" + urlencode(p)

    try:
        r = session.get(url, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Search page failed (%s): %s", url, exc)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    results = []

    for item in soup.select(".gem-c-document-list__item, li.gem-c-document-list__item"):
        link_el = item.select_one("a")
        if not link_el:
            continue
        href  = link_el.get("href", "")
        title = link_el.get_text(strip=True)

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

    nav = soup.select_one(".gem-c-pagination, .pagination, nav[aria-label*='pagination']")
    if nav:
        text = nav.get_text(" ", strip=True)
        m = re.search(r"page\s+\d+\s+of\s+(\d+)", text, re.I)
        if m:
            return min(int(m.group(1)), MAX_PAGES)

    items = soup.select(".gem-c-document-list__item")
    return 1 if items else 0


# ── Individual decision page scraper ─────────────────────────────────────────

def scrape_decision_page(session: requests.Session, page_url: str) -> dict:
    """Fetch an individual decision page and extract all available metadata."""
    time.sleep(REQUEST_DELAY)

    try:
        r = session.get(page_url, timeout=SESSION_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Decision page failed (%s): %s", page_url, exc)
        return {}

    soup = BeautifulSoup(r.text, "lxml")

    title_el = soup.select_one("h1.gem-c-title__text, h1")
    case_name = title_el.get_text(strip=True) if title_el else ""
    claimant, respondent, case_ref = split_case_name(case_name)

    decision_date    = ""
    publication_date = ""
    jurisdiction_codes: list[str] = []
    country          = ""
    tribunal_level   = "Employment Tribunal"
    pdf_url          = ""

    meta_dl = soup.select_one("dl.gem-c-metadata, .publication-header__metadata")
    if meta_dl:
        for dt in meta_dl.find_all("dt"):
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
                jcodes = [li.get_text(strip=True) for li in dd.select("li")]
                if not jcodes:
                    jcodes = [v.strip() for v in value.split(",") if v.strip()]
                jurisdiction_codes = jcodes
            elif "country" in label or "nation" in label:
                country = value
            elif "tribunal" in label or "court" in label:
                tribunal_level = value

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

    if not decision_date:
        date_pattern = re.compile(
            r"(?:Decided|Decision date|Date):\s*(\d{1,2}\s+\w+\s+\d{4})", re.I
        )
        m = date_pattern.search(soup.get_text(" ", strip=True))
        if m:
            decision_date = parse_date(m.group(1))

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


# ── Persistence ───────────────────────────────────────────────────────────────

def load_existing(path: Path) -> dict[str, dict]:
    """Load existing JSON keyed by page_url."""
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


def save_results(path: Path, cases: dict[str, dict], pdf_parsing_enabled: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_cases = sorted(
        cases.values(),
        key=lambda c: c.get("decision_date", ""),
        reverse=True,
    )
    pdf_parsed_count = sum(1 for c in sorted_cases if c.get("pdf_parsed"))
    output = {
        "meta": {
            "description": (
                "Employment Tribunal decisions tagged with Equality Act 2010 "
                "jurisdiction codes, with PDF-parsed outcome and award data "
                "where available."
            ),
            "source":            "GOV.UK Employment Tribunal Decisions service",
            "source_url":        "https://www.gov.uk/employment-tribunal-decisions",
            "last_updated":      datetime.now(timezone.utc).isoformat(),
            "total_cases":       len(sorted_cases),
            "pdf_parsed_cases":  pdf_parsed_count,
            "pdf_parsing_model": CLAUDE_MODEL if pdf_parsing_enabled else None,
        },
        "cases": sorted_cases,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info(
        "Saved %d cases (%d PDF-parsed) to %s",
        len(sorted_cases), pdf_parsed_count, path,
    )


# ── Search and collect ────────────────────────────────────────────────────────

def collect_slugs(session: requests.Session) -> dict[str, dict]:
    """Run all searches and return a dict of {page_url: partial_record}."""
    slugs: dict[str, dict] = {}

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


# ── Retroactive PDF parsing mode ─────────────────────────────────────────────

def run_retroactive(api_key: str) -> None:
    """
    Process all existing cases in et-decisions.json that have no pdf_parsed=True,
    download and parse their PDFs, and update the file.
    """
    log.info("=== Retroactive PDF Parsing Mode ===")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot run retroactive parsing.")
        return

    existing = load_existing(OUTPUT_FILE)
    if not existing:
        log.error("No existing cases found in %s", OUTPUT_FILE)
        return

    # Find cases that haven't been PDF-parsed yet
    to_parse = [
        c for c in existing.values()
        if not c.get("pdf_parsed") and c.get("pdf_url")
    ]
    log.info(
        "Found %d cases total, %d need PDF parsing",
        len(existing), len(to_parse),
    )

    session = make_session()
    stats = {"attempted": 0, "parsed_ok": 0, "failed": 0}

    for i, case in enumerate(to_parse, 1):
        ref = case.get("case_ref") or case.get("page_url", "?")
        log.info("[%d/%d] Parsing PDF for: %s", i, len(to_parse), ref)
        stats["attempted"] += 1

        parse_result = parse_case_pdf(session, case, api_key)
        existing[case["page_url"]].update(parse_result)

        if parse_result.get("pdf_parsed"):
            stats["parsed_ok"] += 1
        else:
            stats["failed"] += 1

        # Save every 10 cases in case of interruption
        if i % 10 == 0:
            save_results(OUTPUT_FILE, existing, pdf_parsing_enabled=True)
            log.info("Intermediate save at %d/%d cases.", i, len(to_parse))

        time.sleep(REQUEST_DELAY)  # polite pacing between PDF downloads

    save_results(OUTPUT_FILE, existing, pdf_parsing_enabled=True)
    log.info(
        "Retroactive parsing complete — attempted: %d, parsed OK: %d, failed: %d",
        stats["attempted"], stats["parsed_ok"], stats["failed"],
    )

    # Print summary of what was extracted
    all_cases = list(existing.values())
    parsed = [c for c in all_cases if c.get("pdf_parsed")]
    outcomes = {}
    for c in parsed:
        o = c.get("outcome", "Unknown")
        outcomes[o] = outcomes.get(o, 0) + 1
    with_award = sum(1 for c in parsed if c.get("total_award"))
    with_chars = sum(1 for c in parsed if c.get("protected_characteristics"))
    with_judge = sum(1 for c in parsed if c.get("judge"))
    with_principle = sum(1 for c in parsed if c.get("key_principle"))
    total_award = sum(c.get("total_award") or 0 for c in parsed)

    log.info("--- Extraction summary ---")
    log.info("  PDF-parsed cases:          %d / %d", len(parsed), len(all_cases))
    log.info("  With outcome:              %d", sum(1 for c in parsed if c.get("outcome") != "Unknown"))
    log.info("  Outcome breakdown:         %s", outcomes)
    log.info("  With award amount:         %d", with_award)
    log.info("  Total awards extracted:    £%s", f"{total_award:,.0f}")
    log.info("  With characteristics:      %d", with_chars)
    log.info("  With judge name:           %d", with_judge)
    log.info("  With key principle:        %d", with_principle)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Balance Works ET Decisions Scraper"
    )
    parser.add_argument(
        "--parse-pdfs",
        action="store_true",
        help="Download and parse PDFs for newly scraped cases (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--retroactive",
        action="store_true",
        help="Parse PDFs for all existing cases with null outcome (requires ANTHROPIC_API_KEY)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if args.retroactive:
        run_retroactive(api_key)
        return

    log.info("=== Balance Works ET Decisions Scraper ===")
    session  = make_session()
    existing = load_existing(OUTPUT_FILE)
    log.info("Loaded %d existing cases from %s", len(existing), OUTPUT_FILE)

    candidates = collect_slugs(session)
    new_urls   = [url for url in candidates if url not in existing]
    log.info(
        "Found %d candidates (%d already scraped, %d new)",
        len(candidates), len(existing), len(new_urls),
    )

    merged = dict(existing)
    for i, url in enumerate(new_urls, 1):
        log.info("[%d/%d] Scraping: %s", i, len(new_urls), url)
        detail = scrape_decision_page(session, url)
        if detail:
            # Optionally parse PDF for new cases if --parse-pdfs was passed
            if args.parse_pdfs and api_key and detail.get("pdf_url"):
                log.info("  Parsing PDF for new case %s …", detail.get("case_ref", "?"))
                parse_result = parse_case_pdf(session, detail, api_key)
                detail.update(parse_result)
            merged[url] = detail
        if i % 50 == 0:
            save_results(OUTPUT_FILE, merged, pdf_parsing_enabled=args.parse_pdfs)
            log.info("Intermediate save at %d cases.", len(merged))

    save_results(OUTPUT_FILE, merged, pdf_parsing_enabled=args.parse_pdfs)
    log.info("Done. Total cases: %d", len(merged))


if __name__ == "__main__":
    main()
