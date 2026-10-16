# Job Market Intelligence Platform

Job Market Intelligence Platform (JMIP) is a local, configuration-driven system for
collecting job postings from public applicant-tracking-system (ATS) feeds, preserving
source evidence, producing canonical market data, ranking current opportunities against
a candidate profile, and tracking applications in a local dashboard.

Supported source families: **Greenhouse, Lever, Ashby, Workday, NextEra Energy, and
Phenom-backed career sites**.

## System Flow

```text
Configured targets + candidate queries
                  |
                  v
        Extract and preserve Bronze
                  |
                  v
       Normalize and validate Silver
                  |
          +-------+-------+
          |               |
          v               v
   Lifecycle tracking   Data-quality gateway
          |               |
          +-------+-------+
                  v
       Gold marts and relevance scores
                  |
                  v
   Dashboard: opportunities, shortlist, market views
```

| Layer | Contract | Location |
| --- | --- | --- |
| Bronze | Immutable source responses and audit evidence | `data/bronze/` |
| Silver | Validated, normalized, deduplicated job postings | `data/silver/` |
| Gold | Current opportunities, summaries, lifecycle, and quality | `data/gold/` |
| Tracking | Shortlist and application status you enter in the dashboard | `data/application_tracking/` |

All layers are stored as JSON. Everything under `data/` and `logs/` is local runtime
state and is excluded from version control.

## Project Status

| Phase | Outcome | Status |
| --- | --- | --- |
| 1. Foundation | Typed configuration, canonical models, storage, logging, extractor contract | Complete |
| 2. Acquisition | Six ATS extractors, candidate queries, immutable Bronze collection | Complete |
| 3. Transformation | Bronze-to-Silver normalization, deduplication, and validation | Complete |
| 4. Intelligence | Lifecycle, Gold summaries, relevance scoring, and quality gateway | Complete |
| 5. Reporting | Dashboard: opportunities, shortlist, tracking, market views, pipeline status, weight simulator | Complete |
| 6. Candidate workflow | Resume ingestion, review, and application comparison | Not started in this codebase |
| 7. Model evaluation | Title-normalization and application-fit benchmarks | Not started in this codebase |

## Design Principles

- **Source-agnostic core:** every adapter emits the same canonical `JobPosting` model.
- **Configuration over hard coding:** targets, taxonomy, thresholds, and weights live in YAML.
- **Immutable evidence:** Bronze responses are preserved and never rewritten in place.
- **Typed boundaries:** Pydantic validates canonical, lifecycle, and tracking data.
- **Idempotent processing:** unchanged inputs do not create duplicate canonical state.
- **Evidence-first scoring:** recommendations expose matched signals and score components.
- **Explicit quality gates:** blocking thresholds fail instead of silently lowering quality.
- **Compliance-aware collection:** sources declare an access policy; automation is allowlisted.
- **Local by default:** the dashboard binds to `127.0.0.1` and rejects foreign hosts and origins.

## Setup

Requirements: Windows PowerShell (or any shell), Python 3.12 or newer, and internet access
for live collection. Node.js is optional and only used to syntax-check the dashboard script.

```powershell
cd C:\path\to\job-market-intelligence
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env   # optional overrides
.\.venv\Scripts\python.exe scripts\run_pipeline.py
```

`run_pipeline.py` validates configuration and logging only; it does not collect jobs.
Use `requirements.txt` instead of `requirements-dev.txt` for a runtime-only install.

Scripts resolve `data/` and `logs/` against the project folder, so they can be run from
any working directory.

## Configuration

| File | Purpose |
| --- | --- |
| `config/extractors.yaml` | Company targets, identifiers, enablement, and order |
| `config/sources.yaml` | Source endpoints, access policies, rate limits, and enablement |
| `config/candidate_queries.yaml` | Named searches and default per-target limit |
| `config/resume_profile.yaml` | Candidate goals, relevance signals, weights, and bands |
| `config/titles.yaml` | Title families, aliases, and seniority normalization |
| `config/skills.yaml` | Skill taxonomy and aliases |
| `config/companies.yaml` | Employer aliases and legal suffix handling |
| `config/locations.yaml` | Geography and workplace normalization |
| `config/salary.yaml` | Currency, pay-period, and annualization rules |
| `config/lifecycle.yaml` | Missing-run and removal behavior |
| `config/data_quality.yaml` | Blocking and advisory quality thresholds |
| `config/settings.yaml` | Paths, retries, and processing limits |
| `config/logging.yaml` | Console and rotating file sinks |

`.env` may override `JMIP_ENVIRONMENT`, `JMIP_LOGGING_LEVEL`, `JMIP_STORAGE_DEFAULT`,
`JMIP_SCRAPING_MAX_RETRIES`, and `JMIP_SCRAPING_CHECKPOINT_INTERVAL`.

Use target names from `config/extractors.yaml` with `--target`. Repeat `--target` or
`--query-name` to select several values.

### Source Access Policies

Every source in `config/sources.yaml` declares a typed compliance policy:

| Policy | Automated collection |
| --- | --- |
| `public_api` | Allowed through a configured public API |
| `partner_api` | Allowed only after partner access is authorized and enabled |
| `public_feed` | Allowed through the configured public career feed |
| `manual_import` | Automation is blocked |
| `unsupported_automation` | Automation is blocked |

`enabled_sources` is the automation allowlist. Configuration validation and the extractor
registry both reject enabled targets whose policy prohibits automation, and
`allow_disabled` cannot bypass that boundary. Indeed, LinkedIn, Handshake, iCIMS, Oracle
Recruiting, and SAP SuccessFactors are recorded with blocking or partner policies and
have no extractor.

## Recommended Workflow

### 1. Verify the project

```powershell
.\.venv\Scripts\python.exe scripts\verify_project.py
```

Runs Ruff, strict mypy, the test suite, and a dashboard JavaScript syntax check without
network access. The same gate runs in GitHub Actions (`.github/workflows/quality.yml`).

### 2. Run a small live pipeline

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py `
  --query-name data-engineering `
  --target chewy-phenom `
  --limit-per-target 5
```

This performs collection, Bronze preservation, Silver processing, lifecycle updates,
Gold generation, and the quality gateway.

### 3. Run the configured pipeline

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py
.\.venv\Scripts\python.exe scripts\collect_candidates.py --limit-per-target 50
```

The first form runs all enabled targets and queries with the configured limit.

### 4. Confirm quality

```powershell
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

Inspect `data/gold/data_quality/latest.json` before relying on refreshed Gold outputs.

### 5. Launch the dashboard

```powershell
.\.venv\Scripts\python.exe -m dashboard.app
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). If that port is busy, add
`--port 8766`. Run it as a module (`-m dashboard.app`), not as `dashboard\app.py`.

## Pipeline Commands

Collect Bronze only:

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py --skip-downstream
```

Run one query against selected extractors:

```powershell
.\.venv\Scripts\python.exe scripts\extract_jobs.py `
  --query "Data Engineer" `
  --location "Florida" `
  --target chewy-phenom `
  --target nextera-energy `
  --limit 10
```

An empty `--query` matches all source postings. Identical payloads stay idempotent in Bronze.

Rebuild downstream layers without network requests:

```powershell
.\.venv\Scripts\python.exe scripts\process_bronze.py
.\.venv\Scripts\python.exe scripts\update_lifecycle.py
.\.venv\Scripts\python.exe scripts\build_gold.py
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

Limit Bronze processing with `--target` and `--limit`. Include inactive, removed,
expired, and duplicate jobs in research summaries with
`scripts\build_gold.py --include-inactive`.

## Dashboard

- **Opportunities:** ranked postings with filters, score explanations, and a detail drawer.
- **Shortlist and tracking:** shortlist, status, priority, dates, and notes per posting.
- **Market intelligence:** skills, roles, employers, locations, and compensation summaries.
- **Pipeline operations:** read-only freshness, collection, quality, lifecycle, and Gold views.
- **Weight simulator:** test relevance weights against current Gold data without saving.

The server binds to `127.0.0.1` by default. It rejects requests whose `Host` is not a
local name (preventing DNS-rebinding reads), rejects writes from other origins, and
only accepts JSON request bodies.

## Engineering Commands

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy analytics dashboard enrichment extractors models pipeline processors storage utils
.\.venv\Scripts\python.exe -m pytest
node --check dashboard/static/app.js
```

`scripts/build_phase1_research_notes_doc.py` and
`scripts/update_research_notes_with_phase1.py` regenerate the research-notes document
in `docs/`; install the optional `docs` extra (`pip install -e ".[docs]"`) first.

## Important Outputs

| Output | Location |
| --- | --- |
| Application and error logs | `logs/jmip.log`, `logs/errors.log` |
| Extractor checkpoints | `data/checkpoints/` |
| Latest collection summary | `data/gold/collection/latest.json` |
| Lifecycle state and events | `data/gold/lifecycle/` |
| Current-opportunities mart | `data/gold/current_opportunities/mart.json` |
| Gold build manifest | `data/gold/manifest/latest.json` |
| Latest quality report | `data/gold/data_quality/latest.json` |
| Application tracking | `data/application_tracking/state.json` |

CLI commands emit JSON summaries. Exit code `0` means success; pipeline and quality
commands return nonzero when a required stage fails.

## Project Structure

- `analytics/`: Gold summaries and the current-opportunities mart.
- `config/`: typed YAML configuration.
- `dashboard/`: local HTTP API and static dashboard.
- `extractors/`: shared interface and source adapters.
- `models/`: canonical Pydantic data models.
- `pipeline/`: collection, processing, lifecycle, and quality orchestration.
- `processors/`: canonical validation and normalization.
- `scripts/`: supported command-line entry points.
- `storage/`: Bronze, Silver, and Gold storage (Parquet and DuckDB adapters are placeholders).
- `tests/`: unit and integration tests.
- `utils/`: configuration, logging, checkpoints, retries, and hashing.

Architecture diagram: [Lucidchart](https://lucid.app/lucidchart/5f040c79-bd32-4af7-a116-f103ffa52082/edit?viewport_loc=-1029%2C-659%2C1849%2C1092%2CvUCeZlokt0CU)

## Troubleshooting

**Python is not found.** Run `.\.venv\Scripts\python.exe --version`; if it is missing,
install Python 3.12 and repeat Setup.

**Dashboard shows "Opportunity data is unavailable".** No Gold mart exists yet. Run a
pipeline (step 2) or the offline rebuild commands.

**Dashboard API returns 404.** An older process is serving stale code. Stop it or start
the current module on another port with `--port 8766`.

**Live extraction fails.** Inspect the command JSON and `logs/errors.log`, confirm the
identifiers in `config/extractors.yaml`, retry one target with `--limit-per-target 5`,
and review retry, timeout, and rate-limit settings before changing code.

**The quality gateway fails.** Inspect `failed_metrics` in
`data/gold/data_quality/latest.json`. Fix collection or normalization problems before
lowering thresholds in `config/data_quality.yaml`.
