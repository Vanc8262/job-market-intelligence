# Job Market Intelligence Platform

Job Market Intelligence Platform (JMIP) is a local, configuration-driven system for
collecting job postings from public applicant-tracking-system (ATS) feeds, preserving
source evidence, producing canonical market data, ranking current opportunities against
a candidate profile, and tracking applications in a local dashboard.

JMIP is primarily a **Bayesian machine-learning project**: its analytics move from fixed
rules toward Bayesian models that report uncertainty and keep a human in the loop
(Phase 8). Deep-learning components may be added in later phases.

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
| Candidate | Resume files, extracted text, reviewed facts (private, never committed) | `data/private/` |

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
| 6. Candidate workflow | Resume ingestion and extraction (6.1), review with revisions and edited versions (6.2), application comparison (6.3) | Complete |
| 7. Bayesian evaluation | Labeled benchmark sets, accuracy with credible intervals, calibration checks, and model comparison | Planned (redesigned) |
| 8. Bayesian analytics | Bayesian quality gate, salary, relevance, survival, and trend models (stages B0–B5) | B0–B1 complete; B2–B5 as data accumulates |
| 9. Deep learning | Title and skill embeddings (for example JobBERT or ModernBERT) as inputs to the Bayesian models | Future |

Application comparison (6.3) scores each posting against your approved resume on
**requirement coverage with evidence** and **taxonomy-weighted ATS keyword overlap**,
plus seniority fit and domain alignment (see [Application Comparison](#application-comparison-phase-63));
Phase 8 stage B3 later turns those signals into a calibrated fit probability with an
uncertainty range.

Phase 7 evaluates every model the Bayesian way: accuracy and agreement are reported as
posterior distributions (Beta-Binomial) with credible intervals, predicted probabilities
are checked for calibration (Brier score, reliability curves), and competing Phase 8
models are compared with PSIS-LOO in ArviZ.

### Phase 8: Bayesian Analytics

Each stage starts when its data exists. Every model is versioned in config, must pass a
diagnostics gate (R-hat ≤ 1.01, bulk/tail ESS ≥ 400, no divergences), and stays advisory:
changing a weight or threshold remains a human edit to YAML.

| Stage | Model | Replaces or extends | Starts when |
| --- | --- | --- | --- |
| B0 Foundation | `bayesian/` package, `config/bayesian.yaml`, diagnostics gate, tests | — | Now |
| B1 Quality and skills | Beta-Binomial quality gate; empirical-Bayes skill-share shrinkage | Fixed ratio thresholds; raw skill shares | Now (SciPy only) |
| B2 Salary | Hierarchical log-salary model (family, seniority, region, employer) | Disclosed-pay statistics only | ~100 postings with pay |
| B3 Relevance weights | Bayesian logistic regression, priors at the configured weights | Hand-set weights; simulator heuristic | 30–50 tracking labels |
| B4 Posting survival | Weibull survival with employer/source pooling | Fixed `missing_runs_before_removed` | ~100 observed removals |
| B5 Market trends | Negative-binomial trends and changepoint detection | Per-run snapshots | 8–12 weeks of daily runs |

Stack: NumPy and SciPy for the conjugate B1 models (always installed); PyMC 6 with
nutpie, ArviZ 1.0, and Bambi for sampled models from B2 on, as the optional `bayes`
extra (`pip install -e ".[bayes]"`). The full research and design notes live in the
*JMIP Bayesian ML Integration Plan* doc.

**B0 foundation (`bayesian/`, `config/bayesian.yaml`).**
- `diagnostics.py`: rank-normalized split R-hat (bulk and folded), bulk ESS, and tail
  ESS following Vehtari et al. (2021), implemented with NumPy/SciPy and checked against
  ArviZ in the tests. `check_inference_data()` gates any PyMC/ArviZ fit on R-hat ≤ 1.01,
  bulk and tail ESS ≥ 400, and zero divergences.
- `beta.py`: conjugate Beta-Binomial posteriors (mean, credible interval, probability of
  meeting a threshold) and empirical-Bayes priors fitted by maximum marginal likelihood.
- `records.py`: every Bayesian output carries a model card (name, stage, version,
  settings hash, advisory flag).

**B1 quality and skills.**
- *Quality gate* (`bayesian/quality.py`): each coverage or failure rate in
  `data_quality.yaml` gets a posterior and a verdict from the probability that the true
  rate meets its threshold: pass (≥ 90%), fail (≤ 10%), or uncertain. Each coverage rate
  is broken down by employer with an empirical-Bayes prior, naming the employers most
  likely below threshold. Shown under *Pipeline operations → Data quality*. With
  `mode: advisory` (default) the rules still decide; `mode: enforce` lets the Bayesian
  verdicts decide coverage checks. All-or-nothing thresholds (100% or 0%) stay rules.
- *Skill shares* (`bayesian/skills.py`): every top skill's share comes with a credible
  interval, and its share within each role family is shrunk toward the skill's typical
  share across families, so a family with six postings is not reported as "100% ask for
  Python". Shown under *Market intelligence → Skills*.

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

These steps assume the project lives in `%USERPROFILE%\Projects\job-market-intelligence`
and is edited in Visual Studio Code.

```powershell
git clone https://github.com/Vanc8262/job-market-intelligence.git "$env:USERPROFILE\Projects\job-market-intelligence"
cd "$env:USERPROFILE\Projects\job-market-intelligence"
py -m venv .venv                      # or: python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env           # optional overrides
.\.venv\Scripts\python.exe scripts\verify_project.py
.\.venv\Scripts\python.exe scripts\run_pipeline.py
```

**VS Code.** Open the folder (or a multi-project `.code-workspace` that includes it) and
choose `.venv\Scripts\python.exe` with *Python: Select Interpreter*. Recommended
extensions: Python, Pylance, Python Debugger, Ruff, and Jupyter. Pytest discovery works
from the project root; new terminals activate `.venv` automatically.

If several projects share the `Projects` folder, a `setup-venvs.ps1` helper in that
folder can rebuild this one environment with
`powershell -ExecutionPolicy Bypass -File .\setup-venvs.ps1 -Only JMIP` (add `-Recreate`
to start from a clean `.venv`).

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
| `config/resume_profile.yaml` | Job-search preferences (roles, tiers, home bases, employment types), weights, bands, fallback skills |
| `config/titles.yaml` | Title families, aliases, and seniority normalization |
| `config/skills.yaml` | Skill taxonomy and aliases |
| `config/companies.yaml` | Employer aliases and legal suffix handling |
| `config/locations.yaml` | Geography and workplace normalization |
| `config/salary.yaml` | Currency, pay-period, and annualization rules |
| `config/lifecycle.yaml` | Missing-run and removal behavior |
| `config/data_quality.yaml` | Blocking and advisory quality thresholds |
| `config/labels.yaml` | Labels tab: orphaned-label alert threshold and review-queue size per group |
| `config/settings.yaml` | Paths, retries, and processing limits |
| `config/logging.yaml` | Console and rotating file sinks |

### Job-search preferences

`config/resume_profile.yaml` is a template with example values. Copy it to
`config/resume_profile.local.yaml`, which Git ignores, and put your own name, home bases,
and preferences there; JMIP reads the local file whenever it exists. The approved,
active resume supplies what a resume says (skills and years of experience).

```powershell
Copy-Item config\resume_profile.yaml config\resume_profile.local.yaml
```

- **Role tiers.** `title_tiers` scale the title signal: Data Science / ML / AI roles
  1.0, software roles 0.85, data engineering, analytics, and research roles 0.7.
- **Avoided roles.** Titles with `avoid_title_keywords` (data entry, administrative,
  help desk, IT support) get no title credit and are capped at `avoided_title_score_cap`.
- **Home bases.** On-site and hybrid roles earn location credit only within `max_miles`
  of a `home_bases` entry.
  The drawer names the base and approximate distance. Remote roles always qualify.
- **Employment types.** `preferred_employment_types` lists part-time, internship,
  contract, and temporary work. Other known types are flagged; set
  `scoring.non_preferred_employment_score_cap` to keep them out of the Strong band.
- **Resume source.** `resume_source: approved_resume` (default) uses the approved active
  resume and falls back to `skills` in this file; `config` always uses the file. The
  Gold relevance summary records which one was used under `profile_source`.

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

## Scheduled Collection (GitHub Actions)

`.github/workflows/collect.yml` runs the full pipeline every day at 10:17 UTC and commits
Bronze, Silver, Gold, and checkpoints to the private `Vanc8262/jmip-data` repository
(through a deploy key stored as the `JMIP_DATA_DEPLOY_KEY` secret). It can also be run
by hand from the Actions tab. Nothing personal is used in the workflow: Gold relevance
there uses the template profile.

On your computer, clone the data repository next to the project and point JMIP at it:

```powershell
git clone https://github.com/Vanc8262/jmip-data.git ..\jmip-data
Add-Content .env "JMIP_PIPELINE_DATA_DIR=../jmip-data"
.\.venv\Scripts\python.exe scripts\sync_data.py   # pull, then re-rank with your resume
```

`sync_data.py` resets the clone to the collector's latest commit (Gold files rebuilt
locally are derived and safe to discard), then rebuilds Gold with your approved resume and
`config/resume_profile.local.yaml`, and compares every current posting with that resume
(Phase 6.3). Application tracking, resumes, and comparison results stay in `data/`.

**Choosing a cadence.** Each scheduled run is logged to `gold/cadence/observations.jsonl`.
`scripts/cadence_report.py` replays the daily history as weekly and biweekly schedules and
reports, per schedule, the share of postings it would have missed entirely, how many days
later it would have discovered postings, how long removed postings would still look open,
and the Actions minutes it would use. After 28 days of daily runs it recommends the
slowest schedule that misses at most 5% of postings. The latest report is in the data
repository's `CADENCE.md` and in each run's summary. To change the schedule, edit the
`cron` line in the workflow (weekly: `17 10 * * 1`; every other week is not expressible
in cron, so use weekly plus a week-number check, or the 1st and 15th: `17 10 1,15 * *`).

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
- **Shortlist and tracking:** shortlist, status, priority, dates, notes, and the resume
  version used, per posting.
- **Market intelligence:** skills, roles, employers, locations, and compensation summaries.
- **Pipeline operations:** read-only freshness, collection, quality, lifecycle, and Gold views.
- **Weight simulator:** test relevance weights against current Gold data without saving.
- **Resume review:** check extracted resume facts against the source text and approve them.
- **Labels:** every requirement label you saved, checked against the latest comparison, with
  an orphaned-label alert on the nav button (see [Labels tab](#labels-tab)).
- **Application fit:** a Fit column (sortable as "Application fit") and an Application
  comparison section in the detail drawer.

The server binds to `127.0.0.1` by default. It rejects requests whose `Host` is not a
local name (preventing DNS-rebinding reads), rejects writes from other origins, and
only accepts JSON request bodies.

## Resume Workflow (Phase 6)

Resumes stay on your computer. Originals, extracted text, and the candidate database live
in `data/private/`, which Git ignores; command output never includes resume text.

```powershell
# Store, extract, and parse a PDF or DOCX; --activate makes it the active resume
.\.venv\Scripts\python.exe scripts\manage_resumes.py ingest "C:\path\to\resume.pdf" `
  --label "Data science v1" --activate

.\.venv\Scripts\python.exe scripts\manage_resumes.py list
.\.venv\Scripts\python.exe scripts\manage_resumes.py activate <resume-version-id>
.\.venv\Scripts\python.exe scripts\manage_resumes.py reextract   # after parser changes
.\.venv\Scripts\python.exe scripts\manage_resumes.py supersede <extraction-id>   # --restore undoes
```

How it works:

1. **Safe reading.** The file type is detected from its content, not its extension. Size,
   PDF page count, password protection, and DOCX archive size, entry count, compression
   ratio, and DTDs are checked before text is used.
2. **Quality score.** Character and word counts, letter/digit share, and unreadable
   characters decide whether the text passed, needs attention, or failed (for example a
   scanned, image-only PDF). A failed extraction is stored but can never be activated.
3. **Parsing.** A deterministic parser proposes skills (mapped to `config/skills.yaml`
   plus your profile skills), experience, education, and projects. Every fact keeps the
   exact span of resume text it came from. Identical files are detected by SHA-256.
4. **Review.** Open **Resume review** in the dashboard. Click a fact's evidence chip to
   highlight it in the source text, correct or remove facts, add missing ones, and approve
   each section. Editing a section clears its approval. Save drafts as you go; the review
   is final (read-only) once all four sections are approved. A stale browser tab cannot
   overwrite a newer save.
5. **Fixing mistakes.** The review toolbar offers:
   - **Superseded:** hide an extraction from the review queue and from scoring (untick
     to restore). A new parse supersedes older pending reviews, and approving a review
     supersedes earlier approvals of the same resume version.
   - **Revise facts:** copy an approved extraction into a new pending review to correct
     parser or reviewer mistakes; the original approval counts until the revision is
     approved.
   - **Edit source text:** correct the resume text itself and save it as a new resume
     version derived from the original (the uploaded file is never changed). The new
     version is parsed and reviewed like any other.
   - **Make active resume:** choose which approved version drives scoring.
6. **Scoring.** Gold relevance scoring uses the active resume's approved facts for skills
   and experience, plus the preferences in `config/resume_profile.yaml`.
7. **Tracking.** The opportunity drawer records which resume version you used for an
   application.

## Application Comparison (Phase 6.3)

Every current posting is compared with your approved active resume on four dimensions,
each shown with the evidence behind it:

| Dimension | Weight | How it is measured |
| --- | --- | --- |
| Requirements coverage | 45% | The posting's requirement lines (Lever qualification lists, or the lines under headings such as "What you'll need" and "Nice to have") are tagged required or preferred and classified (see below). Each is marked ✓ met, ◐ partial, or ✗ missing, with the resume line, role, or degree that supports it. Preferred lines count half. |
| ATS keyword overlap | 30% | Taxonomy skills in the posting, weighted by rarity across current postings (IDF), so a rare skill counts more than "SQL". Lists the keywords to add (★ = named in the requirements). |
| Seniority fit | 15% | Posting level and minimum years against your resume's years of experience. |
| Domain alignment | 10% | Industry vocabulary (healthcare, finance, energy, …) in the posting, outside benefits sections, that also appears on your resume. |

Requirement lines are classified as:

- **skill:** names taxonomy skills ("Python or Go"); met when the resume lists them.
- **concept:** names broad abilities in plain words ("APIs, data pipelines, and cloud
  infrastructure"). Each concept is met by any of its resume terms (REST for APIs, AWS for
  cloud) or, for academic concepts like data structures, by a degree in a related field.
- **degree / field of study:** level and field; fields are compared by group, so a data
  science degree meets "fields such as Computer Science or Mathematics".
- **years:** minimum years of experience (also applied to "5+ years of Python").
- **trait (○ self-assess):** disposition or logistics a resume cannot show ("you approach
  problems with curiosity", "available to start in June"); listed for you to judge and
  left out of the score.
- **text:** anything else, judged by shared words after light stemming ("building",
  "builds" → "build") and synonym folding (ETL counts as pipeline, troubleshooting as
  debugging). The drawer shows which resume word matched, e.g. "debugging (troubleshot)".

When a line names specific products before an alternatives clause, having one of the
named products meets it. Otherwise the wording decides:

- "or another / any other / other / equivalent / alternative" ("TypeScript, or another
  general-purpose language"): an alternative your resume shows counts fully.
- "or comparable / similar / related / the like" ("Foundry, Contour, or comparable
  analytics platforms"): comparable experience earns `comparable_credit` (60%) and shows
  as partial, so a general skill does not count as experience with the named tool.

Named products you lack join the drawer's **Keywords to add** list (★, after taxonomy
skills named in the requirements), so every keyword for a resume edit is in one place.

Coverage and keyword scores are pulled toward 50% when a posting has only one or two
assessable lines, so thin postings cannot score 0 or 100 by accident. A posting without a
requirements section is scored on the other dimensions and capped at a "moderate" band;
one with neither requirements nor skills is "unknown". Bands: strong ≥ 75, moderate ≥ 55.
Weights, thresholds, and the domain, concept, field-of-study, trait, and synonym vocabularies live
in `config/comparison.yaml`; add a resume term to a concept there when a line you meet is
marked missing.

```powershell
.\.venv\Scripts\python.exe scripts\compare_applications.py   # also run by sync_data.py
```

Results contain resume evidence, so they are written to
`data/private/comparisons/latest.json` and never to the data repository. The opportunity
list only receives each posting's scores; the evidence is loaded when you open a posting.
Requirement parsing is rule-based and works on plain text, so read "missing" as "not found
on the resume by matching", and check the line yourself before rewriting your resume.

Each requirement line in the drawer has an **Agree** button and the other verdicts
(✓ met, ◐ partial, ✗ missing; "Your call" for self-assess lines). A click appends one JSON
line to `data/private/labels/requirement_labels.jsonl` with the posting id, requirement
text, the predicted status (taken from the stored comparison, not the browser), your
corrected status, the comparison model version, and a timestamp. The log is append-only;
the newest label for a line is the one shown. These labels are the hand-labeled set Phase 7
will use to measure comparison accuracy, and they never contain resume text.

### Labels tab

The **Labels** view lists the newest label for every requirement line across all postings
(`GET /api/labels`, read model in `comparison/label_review.py`) and compares each with the
latest comparison:

| State | Meaning |
| --- | --- |
| Current | The model still gives the verdict it gave when you labeled the line |
| Stale | The model's verdict changed since ("Then" vs "Now" columns) |
| Orphaned | The posting is still compared but the line is not, usually after a parser change rewords it |
| Posting closed | The posting is no longer active; the label is kept as evidence but cannot be changed |

The tiles at the top count each state and filter the table. Current and stale lines can be
re-labeled in place, which appends a new label exactly as the drawer does; earlier verdicts
stay under "earlier". Any label, including an orphaned or closed one, can be **withdrawn**:
a `withdraw` entry is appended (nothing is deleted), the line moves to the Withdrawn filter,
and it is left out of every count until you label it again. An optional note is saved with
your next change on a line, for example why you corrected it.

When the parser picked up a line that is not a requirement at all (benefits, a company
blurb, an application instruction), mark it **⊘ Not a requirement** in the drawer, the review
queue, or the table. That is a verdict on extraction rather than on matching: the line gets
its own tile and filter, is never counted as an agreement or a disagreement, and stays in the
log as evidence for a boilerplate or heading fix in `config/comparison.yaml`. It does not
change the posting's fit score until that config fix lands. In the review queue it still
counts as a sampled label, so Phase 7 can estimate how often each group holds such lines.
Withdraw the mark to make the line unlabeled again.

Label lines use schema v2: `action` (`label` or `withdraw`), `source` (`drawer`,
`labels_tab`, or `queue`), `sampled`, `sample_seed`, and `note`. Schema v1 lines still read
as drawer labels. The server sets `source` from a short allowed list and never accepts
`sampled` or a `queue` source from the browser: only lines the review queue draws at random
may be marked sampled, because only those give an unbiased Phase 7 accuracy estimate.

When orphaned labels pass `orphans.alert_count` or `orphans.alert_share` in
`config/labels.yaml`, the tab shows a warning and the nav button carries a ⚠ count.
Closed postings never raise the alert. Labels made in the drawer, in the Labels tab, and in
the review queue are always counted separately, because only randomly sampled lines give
an unbiased accuracy estimate.

### Review queue

**Labels → Review queue** shows one requirement line at a time, drawn at random from the
latest comparison (`GET /api/labels/queue`, sampler in `comparison/review_queue.py`). Lines
are grouped by kind and the model's verdict, for example "degree lines marked missing", and
the queue fills the emptiest group first until each holds `queue.target_per_group` sampled
labels (10 by default; a group with fewer lines asks for all of them). Self-assess lines are
left out (`queue.exclude_kinds`).

Each line's place in its group is a hash of `queue.seed`, the posting id, and the line, so
the order is reproducible and new postings land in random positions as the comparison is
re-run. The server accepts a queue label (`POST /api/labels/queue`) only for a line that is
currently first in its group, then saves it with `source: queue`, `sampled: true`, and the
seed. There is no skip button, because skipping hard or dull lines would bias the sample.
Groups are sampled to equal size, so Phase 7 weights each group by its `population` when it
combines group accuracies into one number. Withdrawing a queue label puts its line back.

### Changes and snapshots

**Labels → Changes** (`GET /api/labels/changes`, `comparison/label_changes.py`) reads the
append-only log as a change log: each entry is an event (labeled, changed, confirmed,
withdrawn, restored), and the first label made under a new comparison model version is
marked. It shows activity by day, the latest `recent_events` events, and progress of the
application-tracking decisions that train the weight simulator (10, with at least 3 of
each kind) and Phase 8 B3 (30 to 50). Which statuses count, and the thresholds, live under
`tracking` in `config/labels.yaml`.

A **snapshot** freezes the labeled set so a Phase 7 number can cite it. Because the log only
grows, a snapshot is its first N complete lines: the manifest in
`data/private/labels/snapshots/` stores N, their SHA-256, and counts (never requirement or
resume text). A snapshot whose prefix no longer matches is shown as not verified. Saving
without new labels returns the existing snapshot.

```powershell
.\.venv\Scripts\python.exe scripts\label_snapshots.py create   # also: list, verify (exit 1 if any mismatch)
```

## Engineering Commands

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy analytics bayesian comparison dashboard enrichment extractors models pipeline processors profiles storage utils
.\.venv\Scripts\python.exe -m pytest
node --check dashboard/static/app.js
node --check dashboard/static/resume-review.js
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
| Candidate database and resumes | `data/private/candidates.sqlite3`, `data/private/resumes/` |
| Application comparison results | `data/private/comparisons/latest.json` |
| Requirement verdict labels | `data/private/labels/requirement_labels.jsonl` |

CLI commands emit JSON summaries. Exit code `0` means success; pipeline and quality
commands return nonzero when a required stage fails.

## Project Structure

- `analytics/`: Gold summaries and the current-opportunities mart.
- `bayesian/`: Phase 8 Bayesian quality gate, skill shrinkage, and diagnostics.
- `comparison/`: Phase 6.3 requirement extraction and application comparison.
- `config/`: typed YAML configuration.
- `dashboard/`: local HTTP API and static dashboard.
- `extractors/`: shared interface and source adapters.
- `models/`: canonical Pydantic data models.
- `pipeline/`: collection, processing, lifecycle, and quality orchestration.
- `processors/`: canonical validation and normalization.
- `profiles/`: resume reading, quality checks, parsing, private storage, and review.
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
