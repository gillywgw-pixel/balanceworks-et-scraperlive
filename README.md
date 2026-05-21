# Balance Works — ET Decisions Scraper

Monthly GitHub Actions scraper for the [GOV.UK Employment Tribunal Decisions service](https://www.gov.uk/employment-tribunal-decisions), targeting cases tagged with Equality Act 2010 jurisdiction codes. Decision PDFs are downloaded and analysed using the Claude API (Haiku model) to extract structured outcome and award data.

## Setup

1. **Fork / create this repository** on GitHub.
2. **Enable GitHub Actions** (Settings → Actions → Allow all actions).
3. **Add `ANTHROPIC_API_KEY` as a repository secret** (Settings → Secrets and variables → Actions → New repository secret). Required for PDF parsing.
4. The workflow runs on the first Monday of each month at 06:00 UTC and commits `data/et-decisions.json`.
5. The ET Tracker tool fetches data via the GitHub Contents API — no additional configuration required once the repository is set up.

## What is scraped

Searches the GOV.UK ET decisions service using nine Equality Act 2010 jurisdiction codes plus a keyword query. For each new case found, the individual decision page is fetched to extract metadata, then the decision PDF is downloaded and parsed with Claude.

## Available metadata (from HTML — no PDF required)

| Field | Example |
|-------|---------|
| `case_ref` | `2304666/2020` |
| `case_name` | `H Thomson v St Eanswythe's...` |
| `claimant` | `H Thomson` |
| `respondent` | `St Eanswythe's Church...` |
| `decision_date` | `2026-04-20` |
| `publication_date` | `2026-05-18` |
| `jurisdiction_codes` | `["Disability Discrimination", "Unfair Dismissal"]` |
| `country` | `England and Wales` |
| `tribunal_level` | `Employment Tribunal` |
| `pdf_url` | `https://assets.publishing.service.gov.uk/...` |
| `page_url` | `https://www.gov.uk/employment-tribunal-decisions/...` |
| `scraped_at` | `2026-05-18T06:12:34+00:00` |

## Additional fields from PDF parsing (Phase 3 — requires ANTHROPIC_API_KEY)

| Field | Example |
|-------|---------|
| `outcome` | `"Claimant Won"` |
| `total_award` | `18500.0` |
| `award_breakdown.injury_to_feelings` | `8500.0` |
| `award_breakdown.loss_of_earnings` | `10000.0` |
| `award_breakdown.other` | `null` |
| `protected_characteristics` | `["Disability", "Race"]` |
| `claim_types` | `["Direct Discrimination", "Harassment"]` |
| `judge` | `"Employment Judge Smith"` |
| `tribunal_office` | `"London Central"` |
| `key_principle` | `"Reasonable adjustments duty was not discharged."` |
| `pdf_parsed` | `true` |
| `pdf_parsed_at` | `2026-05-21T07:00:00+00:00` |

## Local usage

```bash
pip install -r requirements.txt

# Scrape only (no PDF parsing)
python scraper.py

# Scrape and parse PDFs for new cases
ANTHROPIC_API_KEY=sk-ant-... python scraper.py --parse-pdfs

# Retroactive: parse PDFs for all existing cases with no outcome data
ANTHROPIC_API_KEY=sk-ant-... python scraper.py --retroactive
```

Results are written to `data/et-decisions.json`.

## GitHub Actions schedule

The workflow runs on the **first Monday of each month** using the cron expression `0 6 1-7 * 1` (Mondays that fall on days 1–7 of the month). It can also be triggered manually from the Actions tab.

The `ANTHROPIC_API_KEY` secret must be set in repository settings for PDF parsing to work. If the secret is absent, the scraper runs in metadata-only mode without error.

## PDF parsing approach

1. Each decision PDF is downloaded to memory (no disk writes).
2. Text is extracted using **pdfplumber** (free, no API cost).
3. The first 12,000 characters of extracted text are sent to **Claude Haiku** with a structured prompt requesting a JSON response.
4. The response is validated and normalised before saving.
5. If any step fails (download, extraction, API call), the case is saved with all metadata fields intact and PDF fields set to null — failures are never fatal.

Approximate cost: ~$0.003 per case (input + output tokens at Haiku rates).
