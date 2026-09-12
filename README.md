# Job Market Intelligence Platform

Job Market Intelligence Platform (JMIP) is a local, configuration-driven system for
collecting job postings, preserving source evidence, producing canonical market data,
ranking current opportunities, and managing candidate resume evidence.

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
 Dashboard, applications, reports, and alerts
```

| Layer | Contract | Primary location |
| --- | --- | --- |
| Bronze | Immutable source responses and audit evidence | `data/bronze/` |
| Silver | Validated, normalized, deduplicated job postings | `data/silver/` |
| Gold | Current opportunities, summaries, lifecycle, and quality | `data/gold/` |
| Candidate | Profiles, resume versions, reviewed facts, and analyses | `data/private/` |
| Operations | Dashboard-initiated pipeline run history | `data/operations/` |

The active storage format is JSON. SQLite is used for transactional candidate and
resume metadata.

## Project Phases

| Phase | Outcome | Status |
| --- | --- | --- |
| 1. Foundation | Typed configuration, canonical models, storage, logging, shared extractor contract | Complete |
| 2. Acquisition | ATS extractors, candidate queries, immutable Bronze collection | Complete |
| 3. Transformation | Bronze-to-Silver normalization, deduplication, and validation | Complete |
| 4. Intelligence | Lifecycle, Gold summaries, relevance scoring, and quality gateway | Complete |
| 5. Reporting | Dashboard, applications, market views, alerts, and reports | Complete |
| 6. Candidate workflow | Profile storage, secure resume ingestion, review, and comparison | Complete |
| 7. Model evaluation | Neutral harness and private reviewed benchmarks; results stay advisory | In progress |
| 8. Product reliability | UX, pipeline controls, data inventory, snapshots, CI, and cleanup | In progress |

## Design Principles

- **Source-agnostic core:** adapters emit the same canonical `JobPosting` model.
- **Configuration over hard coding:** targets, taxonomy, thresholds, and weights live in YAML.
- **Immutable evidence:** Bronze responses are preserved and never rewritten in place.
- **Typed boundaries:** Pydantic validates canonical, lifecycle, profile, and evaluation data.
- **Idempotent processing:** unchanged inputs do not create duplicate canonical state.
- **Evidence-first scoring:** recommendations expose matched signals and score components.
- **Explicit quality gates:** blocking thresholds fail instead of silently lowering quality.
- **Private by default:** resumes, extracted text, benchmarks, and model reports stay private.
- **Human approval:** resume facts cannot drive analysis until every section is approved.
- **Bounded automation:** dashboard operations use fixed commands and reject concurrent runs.
- **Reproducibility:** manifests, run IDs, fingerprints, model revisions, and logs retain provenance.
- **No generative LLM decisions:** model work is evaluation-only and cannot replace ranking.

## Setup

Requirements: Windows PowerShell, Python 3.12 or newer, and internet access for live
collection or initial model downloads.

```powershell
cd C:\path\to\job-market-intelligence
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts\run_pipeline.py
```

`run_pipeline.py` validates configuration and logging only. It does not collect jobs.
The current HTTP extractors do not require Playwright browser binaries.

## Configuration

| File | Purpose |
| --- | --- |
| `config/extractors.yaml` | Company targets, identifiers, enablement, and order |
| `config/sources.yaml` | Source endpoints, rate limits, and enablement |
| `config/candidate_queries.yaml` | Named searches and default per-target limit |
| `config/resume_profile.yaml` | Candidate goals, relevance signals, weights, and bands |
| `config/titles.yaml` | Title families, aliases, and seniority normalization |
| `config/companies.yaml` | Employer aliases and legal suffix handling |
| `config/locations.yaml` | Geography and workplace normalization |
| `config/salary.yaml` | Currency, pay-period, and annualization rules |
| `config/lifecycle.yaml` | Missing-run and removal behavior |
| `config/data_quality.yaml` | Blocking and advisory quality thresholds |
| `config/reporting.yaml` | Reports, alerts, and schedule defaults |
| `config/model_evaluation.yaml` | Model revisions, datasets, metrics, and policy |
| `config/settings.yaml` | Paths, retries, ingestion limits, and comparison weights |
| `config/logging.yaml` | Console and rotating file sinks |

Use names from `config/extractors.yaml` with `--target`. Repeat `--target` or
`--query-name` to select multiple values.

### Source Access Policies

Every source in `config/sources.yaml` has a typed compliance policy:

| Policy | Automated collection |
| --- | --- |
| `public_api` | Allowed through a configured public API |
| `partner_api` | Allowed only after partner access is authorized and enabled |
| `public_feed` | Allowed through the configured public career feed |
| `manual_import` | File or user-mediated import only; extractor automation is blocked |
| `unsupported_automation` | Automation is explicitly blocked |

### Manual Opportunity Imports

`ManualOpportunityImporter` accepts user-supplied links, CSV exports, and RFC 5322
job-alert emails. It never opens or fetches a submitted URL. Each import stores the
original artifact and record-level provenance in immutable Bronze storage before
normalizing, validating, and deduplicating the corresponding Silver records.

Import one or more pasted links:

```powershell
.\.venv\Scripts\python.exe scripts\import_opportunities.py `
  --link "https://www.indeed.com/viewjob?jk=..." `
  --link "https://www.linkedin.com/jobs/view/..."
```

Import a text file, CSV export, or saved job-alert email:

```powershell
.\.venv\Scripts\python.exe scripts\import_opportunities.py --links-file .\imports\links.txt
.\.venv\Scripts\python.exe scripts\import_opportunities.py --csv .\imports\jobs.csv
.\.venv\Scripts\python.exe scripts\import_opportunities.py --email .\imports\alert.eml
```

Link files may contain a URL alone or
`title | company | location | URL`. CSV headers accept common aliases such as
`job_title`, `employer`, `job_location`, `job_url`, `description`, `salary`,
`job_type`, `date_posted`, `job_id`, and `source`. Use `--source indeed` when an
artifact needs one explicit source override.

### SmartRecruiters Targets

SmartRecruiters uses its documented public Posting API. Add an employer only after
confirming the company identifier shown in its public SmartRecruiters career URL:

```yaml
- name: example-smartrecruiters
  source: smartrecruiters
  enabled: true
  company_name: Example Company
  company_identifier: ExampleCompany
  page_size: 100
```

The shared adapter supports `country`, `region`, `city`, `department`, and
`language` filters. The normal extraction and Bronze-to-Silver commands work once
the target is present in `config/extractors.yaml`. See
`docs/source-expansion-research.md` for the inclusion and exclusion rationale for
SmartRecruiters, iCIMS, Oracle Recruiting, and SAP SuccessFactors.

### Workable Targets

Workable's documented public account endpoint returns only published jobs and does
not require an API key. Confirm the account subdomain from an employer's
`apply.workable.com/<subdomain>` careers URL, then add a target:

```yaml
- name: example-workable
  source: workable
  enabled: true
  company_name: Example Company
  account_subdomain: example
```

The adapter downloads one `details=true` account snapshot per pipeline run and
filters it locally across all configured candidate queries. It supports `city`,
`state`, `country`, `department`, `employment_type`, and `workplace_type` filters.
It never uses Workable SPI tokens or accesses draft, closed, archived, confidential,
candidate, recruiter, or employee data. The configured 30 requests per minute is
below Workable's documented limit of 10 requests per 10 seconds.

### USAJOBS API

USAJOBS searches current open federal announcements with an API key and the email
address used to register that key. Put both values in the local `.env` file:

```dotenv
USAJOBS_API_KEY=your-key
USAJOBS_USER_AGENT_EMAIL=the-email-used-to-request-the-key@example.com
```

Do not add either value to YAML or commit `.env`. The configured
`federal-usajobs` target is enabled and requests at most 100 results across three
pages per query. The source is
paced at 20 requests per minute, honors `429` retry guidance, and caps every
configured search below USAJOBS' 10,000-row maximum.

The adapter always sends `Fields=Full` and `WhoMayApply=Public`. Supported optional
filters include `position_title`, `minimum_salary`, `job_category_code`,
`organization`, `date_posted`, `radius`, `hiring_path`, and `remote`. Credentials
are sent only in the required request headers and are never included in Bronze.

### Jobicy Discovery

Jobicy is an enabled, keyless discovery source for public remote jobs. The default
`jobicy-remote-us` target downloads one broad, 200-record US snapshot and applies
the candidate profile's title and location queries locally. Jobicy remains the
attributed source, and each record retains its canonical Jobicy URL and raw Bronze
provenance.

The adapter uses a process-spanning cache at `data/cache/jobicy/` and will not poll
a target more than once per hour, including after a failed request. When a refresh
fails and an older snapshot exists, extraction continues from that stale snapshot.
The cache interval is enforced at a minimum of 60 minutes and cannot be lowered in
configuration. Optional target-level `geo` and `industry` parameters narrow the
upstream snapshot; `industry`, `job_type`, and `level` search filters run locally.
No API key or Jobicy account is required.

### Authorized Handshake EDU API

A normal Handshake student, alumni, employer, or individual account does not grant
EDU API access. Handshake Support must provide Developer Portal credentials,
approve the institution-scoped subscription, and enable its generated API key.
The project does not accept Handshake passwords, browser cookies, or session tokens.

After an authorized institution provisions access:

1. Set `HANDSHAKE_EDU_API_KEY` in `.env`; never put the key in YAML or source control.
2. Set the `handshake` source to `enabled: true` and add it to `enabled_sources`.
3. Add an explicitly enabled target such as:

```yaml
- name: authorized-handshake
  source: handshake
  enabled: true
  institution_scope: your-institution-stable-name
  institution_name: Your Institution
  api_key_env: HANDSHAKE_EDU_API_KEY
  authorized_use_confirmed: true
  page_size: 50
  updated_since: "2026-01-01T00:00:00Z"
```

The adapter uses only documented read-only EDU API entities: `jobs`, `employers`,
and `job_locations`. It sends the provisioned key through `x-api-key`, stores no
credential in Bronze, and fails closed on missing, rejected, or revoked access.
No Handshake target is included by default.

Set `authorized_use_confirmed: true` only after the institution confirms that its
Handshake agreement permits this project's storage and job-market analytics use.
API-key possession alone does not satisfy this gate.

`enabled_sources` is the automation allowlist. Configuration validation and the
extractor registry both reject enabled targets whose policies prohibit automation;
`allow_disabled` cannot bypass this boundary.

## Recommended Workflow

### 1. Verify The Project

```powershell
.\.venv\Scripts\python.exe scripts\verify_project.py
```

This runs Ruff, strict mypy, all tests, and dashboard JavaScript validation without
network access.

### 2. Run A Small Live Pipeline

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py `
  --query-name data-engineering `
  --target chewy-phenom `
  --limit-per-target 5
```

This performs collection, Bronze preservation, Silver processing, lifecycle updates,
Gold generation, and the quality gateway.

### 3. Run The Configured Pipeline

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py
```

The default profile runs all enabled targets and queries with the configured limit.
Override it for a larger run:

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py --limit-per-target 50
```

### 4. Confirm Quality

```powershell
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

Inspect `data/gold/data_quality/latest.json` before relying on refreshed Gold outputs.

### 5. Launch The Dashboard

```powershell
.\.venv\Scripts\python.exe -m dashboard.app
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). If that port is occupied:

```powershell
.\.venv\Scripts\python.exe -m dashboard.app --port 8766
```

## Pipeline Commands

### Collection

Collect configured searches and run every downstream stage:

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py
```

Collect Bronze only:

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py --skip-downstream
```

Run one source query against selected extractors:

```powershell
.\.venv\Scripts\python.exe scripts\extract_jobs.py `
  --query "Data Engineer" `
  --location "Florida" `
  --target chewy-phenom `
  --target nextera-energy `
  --limit 10
```

An empty `--query` matches all source postings. Identical payloads remain idempotent in
Bronze.

### Rebuild Without Network Requests

```powershell
.\.venv\Scripts\python.exe scripts\process_bronze.py
.\.venv\Scripts\python.exe scripts\update_lifecycle.py
.\.venv\Scripts\python.exe scripts\build_gold.py
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

Limit Bronze processing to selected targets or snapshots:

```powershell
.\.venv\Scripts\python.exe scripts\process_bronze.py `
  --target chewy-phenom `
  --target nextera-energy `
  --limit 100
```

Include inactive, removed, expired, and duplicate jobs in research summaries:

```powershell
.\.venv\Scripts\python.exe scripts\build_gold.py --include-inactive
```

## Dashboard Workflows

The local dashboard provides:

- Ranked opportunities with filters, explanations, and detail drawers.
- Shortlist and application tracking with resume-version selection.
- Market summaries for skills, titles, employers, locations, salaries, and lifecycle.
- Pipeline freshness, failures, provenance, run controls, and bounded log tails.
- Data inventory, lineage checks, unmanaged-data detection, and recovery snapshots.
- Resume review for skills, employment, education, and projects.
- Deterministic application-specific resume comparisons.
- Weight simulation, calibration feedback, alerts, and report snapshots.

Dashboard pipeline actions are limited to downstream rebuild, quality check, and full
pipeline. Recovery snapshots include configuration, Bronze, Silver, Gold, and
checkpoints. Candidate-private data and workflow state are excluded.

## Candidate And Resume Workflow

Initialize or resynchronize the default profile:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_default_profile.py
```

Securely ingest and optionally activate a PDF or DOCX resume:

```powershell
.\.venv\Scripts\python.exe scripts\ingest_resume.py `
  "C:\path\to\resume.pdf" `
  --label "Data engineering v1" `
  --activate
```

Activation requires passing extraction quality checks. It does not approve facts.
Open **Resume review**, correct the proposals, and approve all four sections before
application comparison is allowed.

Re-extract pending resumes after parser improvements:

```powershell
.\.venv\Scripts\python.exe scripts\reextract_resumes.py
.\.venv\Scripts\python.exe scripts\reextract_resumes.py --extraction-id <extraction-id>
```

Original files and extracted text remain private. SQLite stores typed metadata,
quality results, review state, hashes, and provenance.

## Reports And Alerts

Generate a report from current Gold data without collection:

```powershell
.\.venv\Scripts\python.exe scripts\generate_report.py
```

Refresh the pipeline first, or fail scheduled work on critical alerts:

```powershell
.\.venv\Scripts\python.exe scripts\generate_report.py --run-pipeline
.\.venv\Scripts\python.exe scripts\generate_report.py --fail-on-critical
```

Register or remove the optional Windows scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_report.ps1 `
  -Time "07:30"

powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_report.ps1 `
  -Time "07:30" -RunPipeline

powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_report.ps1 `
  -Uninstall
```

Scheduling is opt-in. Review `config/reporting.yaml` before registering the task.

## Model Evaluation

The provider-neutral evaluation harness is advisory:

| Provider | Current use |
| --- | --- |
| `resume-ner` | Candidate-fact extraction evaluation |
| `jobbert-v3` | Title aliases, taxonomy mapping, and related-role retrieval |
| `job-eval-modernbert` | Shadow-only application fit evaluation |

Install optional dependencies and cache pinned model snapshots:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ml-evaluation]"
.\.venv\Scripts\python.exe scripts\cache_job_eval_models.py
```

Run configured evaluations:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_models.py resume-ner
.\.venv\Scripts\python.exe scripts\evaluate_models.py jobbert-v3
.\.venv\Scripts\python.exe scripts\evaluate_models.py job-eval-modernbert `
  --backend model_evaluation.job_eval_backend:score_pair
```

JobEval runs through a reviewed adapter in a network-disabled subprocess. It cannot
alter deterministic application scores or opportunity rankings.

### Private Benchmarks

Create, draft, review, inspect, and freeze a human-reviewed Resume NER benchmark:

```powershell
.\.venv\Scripts\python.exe scripts\manage_benchmarks.py init private-resume-ner-v1 `
  --task resume_ner --version 1.0.0 `
  --source "Candidate-approved resumes reviewed against source files"

.\.venv\Scripts\python.exe scripts\manage_benchmarks.py draft-resume `
  private-resume-ner-v1 <extraction-id>

.\.venv\Scripts\python.exe scripts\manage_benchmarks.py export-case `
  private-resume-ner-v1 resume-ner-<extraction-id>

.\.venv\Scripts\python.exe scripts\manage_benchmarks.py review-case `
  private-resume-ner-v1 resume-ner-<extraction-id> `
  --reviewer candidate-owner --decision approve `
  --case-file <private-review-packet.json> --attest

.\.venv\Scripts\python.exe scripts\manage_benchmarks.py status private-resume-ner-v1
.\.venv\Scripts\python.exe scripts\manage_benchmarks.py freeze private-resume-ner-v1
```

Evaluate frozen private data by passing its JSONL and YAML manifest with `--dataset`
and `--manifest`. Private benchmark text and model reports must never be committed.

## Engineering Commands

Run the complete local and CI quality gate:

```powershell
.\.venv\Scripts\python.exe scripts\verify_project.py
```

Run individual checks:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy analytics dashboard enrichment extractors `
  model_evaluation models pipeline processors profiles reporting storage utils
.\.venv\Scripts\python.exe -m pytest
node --check dashboard/static/app.js
```

Preview and apply allowlisted development-artifact cleanup:

```powershell
.\.venv\Scripts\python.exe scripts\clean_workspace.py
.\.venv\Scripts\python.exe scripts\clean_workspace.py --apply
```

Cleanup is dry-run by default. It can select known tool caches and `data/test-*` or
`data/pytest-*`; it cannot select `tmp/` or managed production/private data.

The same verification gate runs in `.github/workflows/quality.yml` on pushes and pull
requests.

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
| Reports and alert state | `data/reports/`, `data/alerts/` |
| Candidate profile database | `data/private/profile_metadata.sqlite3` |
| Private model data | `data/private/model_benchmarks/`, `data/private/model_evaluations/` |
| Recovery snapshots | `data/backups/` |

CLI commands emit JSON summaries where practical. Exit code `0` indicates success;
pipeline and quality commands return nonzero when a required stage fails.

## Project Structure

- `analytics/`: Gold summaries and current-opportunities mart.
- `config/`: typed YAML configuration.
- `dashboard/`: local HTTP API and static dashboard.
- `extractors/`: shared interface and source adapters.
- `model_evaluation/`: model contracts, metrics, benchmarks, and isolated adapters.
- `models/`: canonical Pydantic data models.
- `pipeline/`: collection, processing, lifecycle, and quality orchestration.
- `processors/`: canonical validation and normalization.
- `profiles/`: candidate repository, resume extraction, and comparison.
- `reporting/`: alerts and report generation.
- `scripts/`: supported command-line entry points.
- `storage/`: Bronze, Silver, Gold, Parquet, and DuckDB adapters.
- `tests/`: unit and integration tests.
- `utils/`: configuration, logging, checkpoints, retries, and hashing.

Architecture diagram: [Lucidchart](https://lucid.app/lucidchart/5f040c79-bd32-4af7-a116-f103ffa52082/edit?viewport_loc=-1029%2C-659%2C1849%2C1092%2CvUCeZlokt0CU)

## Troubleshooting

### Python Is Not Found

```powershell
.\.venv\Scripts\python.exe --version
```

If it is missing, install Python 3.12 and repeat **Setup**.

### Dashboard API Requests Return 404

An older process is usually serving stale code. Stop it or launch the current module
on a different port:

```powershell
.\.venv\Scripts\python.exe -m dashboard.app --port 8766
```

Run `python -m dashboard.app`, not `python dashboard\app.py`.

### Gold Data Is Missing Or Stale

```powershell
.\.venv\Scripts\python.exe scripts\process_bronze.py
.\.venv\Scripts\python.exe scripts\update_lifecycle.py
.\.venv\Scripts\python.exe scripts\build_gold.py
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

### Live Extraction Fails

1. Inspect the command JSON and `logs/errors.log`.
2. Confirm identifiers and enablement in `config/extractors.yaml`.
3. Retry one target and query with `--limit-per-target 5`.
4. Review retry, timeout, and rate-limit settings before changing code.

### The Quality Gateway Fails

Inspect `failed_metrics` in `data/gold/data_quality/latest.json`. Fix collection or
normalization problems before lowering thresholds in `config/data_quality.yaml`.
