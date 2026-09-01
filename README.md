# Job Market Intelligence Platform

Job Market Intelligence Platform (JMIP) is a production-oriented Python foundation for collecting, standardizing, enriching, and analyzing technical job postings across job boards and applicant tracking systems.

The current implementation provides configuration-driven ATS extraction, immutable Bronze
preservation, canonical normalization and validation, idempotent Silver processing, and
posting lifecycle history.

## Architecture
https://lucid.app/lucidchart/5f040c79-bd32-4af7-a116-f103ffa52082/edit?viewport_loc=-1029%2C-659%2C1849%2C1092%2CvUCeZlokt0CU

## Directory Guide

- `config/`: YAML configuration read only through `utils.config.Config`.
- `data/`: bronze, silver, gold, cache, and checkpoint directories.
- `extractors/`: shared contract and Greenhouse, Lever, Ashby, Workday, NextEra, and Phenom plugins.
- `processors/`: canonical validation and configuration-driven normalization.
- `models/`: Pydantic canonical schema for job postings.
- `storage/`: consistent storage interfaces for bronze, silver, gold, Parquet, and DuckDB.
- `utils/`: configuration, logging, checkpoint, retry, hashing, and helper utilities.
- `dashboard/`: future interactive dashboard entry point.
- `scripts/`: command-line orchestration entry points.
- `tests/`: unit tests for the foundation.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Install Playwright browser binaries only when a future extractor needs browser automation:

```bash
playwright install
```

## Configuration

Copy `.env.example` to `.env` for local overrides. YAML files in `config/` define stable defaults. Application code should use:

```python
from utils.config import Config

config = Config()
max_retries = config.settings.scraping.max_retries
enabled_sources = config.sources.enabled_sources
skill_categories = config.skills.categories
```

## Running

Run a bounded extraction across enabled targets and preserve raw responses in Bronze:

```powershell
python scripts/extract_jobs.py --limit 5
```

Normalize every configured Bronze snapshot into Silver:

```powershell
python scripts/process_bronze.py
```

Both commands support `--target NAME` multiple times. Bronze-to-Silver processing is idempotent:
unchanged snapshots update observation metadata without creating duplicate Silver files.

## Development Roadmap

1. Phase 1: project foundation and configuration.
2. Phase 2: source-specific extractors for ATS and job board sources.
3. Phase 3: validation, normalization, and canonical silver records.
4. Phase 4: enrichment for skills, seniority, compensation, and geography.
5. Phase 5: Gold summaries, trend analytics, DuckDB marts, and reporting.
6. Phase 6: dashboard and scheduled production workflows.

Build the current analytics-ready Gold summaries after Silver processing:

```powershell
.\.venv\Scripts\python.exe scripts\build_gold.py
```

This writes skills, titles, employers, locations, salaries, resume relevance,
the current-opportunities mart, and a build manifest beneath `data/gold/`.
`data/gold/current_opportunities/mart.json` contains one ranked row per active,
nonduplicate opportunity with canonical job attributes, compensation, skills,
resume-match explanations, direct application URL, and lifecycle freshness.
Resume relevance behavior is configured in `config/resume_profile.yaml`, including
signal weights, core skills, explicit/inferred evidence strength, title matching,
seniority penalties, relocation policy, eligibility caps, and match-band thresholds.

Run the resume-aligned multi-query collection profile and rebuild downstream layers:

```powershell
.\.venv\Scripts\python.exe scripts\collect_candidates.py
```

Use `--query-name`, `--target`, and `--limit-per-target` for staged runs. The
runner unions query matches per target, selects them round-robin across role
families, preserves new versions in Bronze, rebuilds Silver and Gold, and writes
the blocking quality report to `data/gold/data_quality/latest.json`. It also records
discovery, content change, disappearance, removal, and reactivation events beneath
`data/gold/lifecycle/`. A posting becomes removed only after the number of complete,
same-profile misses configured in `config/lifecycle.yaml`; failed or partial searches do
not advance that counter.

Reapply the latest persisted collection evidence without performing network extraction:

```powershell
.\.venv\Scripts\python.exe scripts\update_lifecycle.py
```

Launch the local opportunities dashboard:

```powershell
.\.venv\Scripts\python.exe -m dashboard.app
```

Open `http://127.0.0.1:8765`. The dashboard reads the current-opportunities Gold
mart at request time and provides ranked filtering, sorting, pagination, posting
details, direct application links, resume-score explanations, a shortlist, and
application tracking. Candidate workflow state is validated and atomically persisted
separately from derived analytics at `data/application_tracking/state.json`.

Lifecycle application is idempotent by collection run ID. `state.json` is the current
ledger, `latest.json` is the latest transition report, and `summary.json` provides current
status counts and recent employer activity. Silver `last_seen_at` advances on successful
search observations even when the immutable Bronze payload has not changed.

Recheck the latest Silver and collection results without extracting:

```powershell
.\.venv\Scripts\python.exe scripts\check_data_quality.py
```

## Engineering Standards

JMIP favors modular architecture, source-agnostic interfaces, configuration over hard-coded values, plugin-ready processing, strong typing, structured validation, centralized logging, checkpointing, and easy unit testing.
