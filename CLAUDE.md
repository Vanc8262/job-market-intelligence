# CLAUDE.md

Guidance for Claude Code in this repository. `README.md` is the user-facing manual; this
file is how to work on the code. Personal context for the owner lives in
`CLAUDE.local.md` (git-ignored); read it if it exists.

## What JMIP is

Job Market Intelligence Platform: collects postings from public ATS feeds (Greenhouse,
Lever, Ashby, Workday, NextEra, Phenom), normalizes them Bronze → Silver → Gold, ranks
current opportunities against the owner's approved resume, compares each posting with
that resume (Phase 6.3), and serves a local dashboard. It is the owner's real job-search
tool **and** a public portfolio project, so code quality, tests, and honest evidence
matter more than speed. Personal project, no deadline.

Direction: primarily a **Bayesian** ML project (Phase 8, advisory models with
uncertainty, human edits YAML to change behavior). Embedding models (JobBERT/ModernBERT)
are deferred to Phase 9.

## Environment

- Owner develops on **Windows** (PowerShell), project at
  `C:\Users\vanc8\Projects\job-market-intelligence`, venv in `.venv`.
  Python: `.\.venv\Scripts\python.exe` (in Git Bash: `.venv/Scripts/python.exe`).
- Python 3.12+, line length 100, Ruff + strict mypy.
- Collected data lives in a **separate private repo**, `Vanc8262/jmip-data`, cloned at
  `..\jmip-data`. `.env` sets `JMIP_PIPELINE_DATA_DIR=../jmip-data`, which rebases
  bronze/silver/gold/checkpoints there. `data/private` and `data/application_tracking`
  always stay in this project's `data/`.
- GitHub Actions `collect.yml` runs collection daily at **10:17 UTC** and pushes to
  jmip-data (deploy key secret `JMIP_DATA_DEPLOY_KEY`). Keep that schedule unless asked.
  `quality.yml` runs the verify gate on push.

## Commands

```powershell
.\.venv\Scripts\python.exe scripts\verify_project.py      # the gate: ruff, mypy, pytest, node --check
.\.venv\Scripts\python.exe -m pytest tests\test_comparison.py -q
.\.venv\Scripts\python.exe -m ruff format .
.\.venv\Scripts\python.exe scripts\sync_data.py           # pull jmip-data, rebuild Gold, run 6.3 comparison
.\.venv\Scripts\python.exe scripts\compare_applications.py  # 6.3 comparison only
.\.venv\Scripts\python.exe -m dashboard.app                # http://127.0.0.1:8765 (run as a module)
.\.venv\Scripts\python.exe scripts\manage_resumes.py list
.\.venv\Scripts\python.exe scripts\cadence_report.py
```

There is no `scripts/run_dashboard.py`; the dashboard is `-m dashboard.app`.

## Non-negotiable rules

1. **Privacy.** The owner's resume, extracted text, reviewed facts, comparison results,
   and tracking state live only in `data/private/` and `data/application_tracking/`
   (git-ignored). Never commit them, never write them to jmip-data, never print resume
   text in CLI output or logs. Personal preferences (name, home bases) live only in
   `config/resume_profile.local.yaml` (git-ignored); `config/resume_profile.yaml` is a
   template with example values.
2. **Tests use synthetic resumes only** (`tests/support/resumes.py`, "Jordan Rivera").
   Never use the real resume in tests, fixtures, or examples.
3. **Run `scripts/verify_project.py` before every commit**; it must print
   `All checks passed.` Run `ruff format .` first.
4. **Commits:** author `Vanc8262 <86838787+Vanc8262@users.noreply.github.com>`; push to
   `master`. Descriptive subject + body explaining why.
5. **Dashboard assets:** when `app.js`, `resume-review.js`, or `styles.css` change, bump
   the `?v=resume-vN` asset version in `dashboard/static/index.html` **and** in
   `tests/test_dashboard.py` (currently `resume-v12`).
6. **Config over code:** thresholds, weights, vocabularies, and taxonomies go in
   `config/*.yaml`, each validated by a Pydantic model in `utils/config.py`
   (`extra="forbid"`; add the field there when adding a YAML key). Bump the config's
   `version` when scoring behavior changes.
7. **Bayesian outputs are advisory.** They report posteriors/verdicts with model cards;
   changing a weight or threshold remains a human YAML edit. Sampled models (B2+) must
   pass the diagnostics gate (R-hat ≤ 1.01, bulk/tail ESS ≥ 400, no divergences).
8. **Source compliance:** only sources whose policy in `config/sources.yaml` allows
   automation may be collected; never add scrapers for LinkedIn, Indeed, Handshake, etc.
9. **Evidence-first:** every score shown to the owner must expose what produced it
   (matched skills, resume lines, components). Prefer "not found on the resume" wording
   over claims the owner lacks something.

## Code map

| Path | Role |
| --- | --- |
| `extractors/` | One adapter per ATS family → canonical `JobPosting` (`models/job_posting.py`). Lever joins `descriptionPlain` + `lists` + `additionalPlain` and fills qualifications. |
| `processors/normalizer.py` | Silver normalization: skills (`config/skills.yaml`, with `ambiguous` rules for R, C, Go, Excel…), titles and role families (`config/titles.yaml` `family_rules`), locations, salary. `skills_in_text()` is the shared skill matcher. |
| `pipeline/` | Collection, processing, lifecycle, data-quality gateway. |
| `analytics/` | Gold summaries, current-opportunities mart, relevance scoring, cadence monitoring. |
| `bayesian/` | Phase 8 B0–B1: diagnostics (ArviZ-equivalent R-hat/ESS), Beta-Binomial posteriors, quality-gate verdicts, skill-share shrinkage, model cards. |
| `profiles/` | Phase 6.1–6.2 resume workflow: safe PDF/DOCX reading (clip-aware PDF text), quality scoring, parser, SQLite repository, review/approve/revise/supersede, active version. `profile_source.py` resolves the approved resume for scoring. |
| `comparison/` | Phase 6.3 application comparison (below). |
| `dashboard/` | `ThreadingHTTPServer` JSON API + vanilla JS (`static/`). Reads `configured_gold_root()`; comparisons from `data/private/comparisons/latest.json`. |
| `scripts/` | CLI entry points (JSON output, non-zero exit on failure). |
| `utils/config.py` | Typed config loader for every YAML file. |

## Phase 6.3 application comparison (`comparison/`)

- `requirements.py`: finds requirement lines (Lever qualifications → heading sections →
  cue-sentence fallback), tags required/preferred, filters boilerplate (EEO,
  accommodation, perks, scam warnings), and classifies each line: `skill`, `concept`,
  `degree` (incl. field-of-study lines), `years`, `trait` (self-assess, not scored),
  `text`. `named_items()` extracts products before "or comparable / or another …".
- `vocabulary.py`: compiles `config/comparison.yaml` concepts, fields of study (grouped
  for "related field"), traits, synonym groups; light stemmer `stem()`.
- `compare.py`: `assess_requirement()` (met/partial/missing with resume evidence),
  IDF-weighted keyword overlap, seniority, domain; `compare_posting()` combines them
  (requirements 0.45, keywords 0.30, seniority 0.15, domain 0.10, rescaled over present
  dimensions, neutral evidence prior, keyword-only postings capped at "moderate").
  Named products: a named one met → full credit; "or another/equivalent" → an
  alternative counts fully; "or comparable/similar" → `comparable_credit` (0.6).
  Missing named products go to `named_to_add` ("Keywords to add" in the drawer).
- `runner.py`: compares all active Silver postings, writes results privately.
- `labels.py`: owner agree/correct verdicts from the drawer (`POST /api/requirement-labels`),
  appended to `data/private/labels/requirement_labels.jsonl`; the predicted status is read
  from the stored comparison. This is the Phase 7 labeled set.
- When the owner reports a wrong ✓/◐/✗, fix it in the vocabulary/config first, add a
  regression test in `tests/test_comparison.py` with the synthetic resume, and sanity
  check on real postings (counts by kind/status) before committing.

## Roadmap

| Phase | Status |
| --- | --- |
| 1–5 Foundation → Dashboard | Complete |
| 6 Candidate workflow (6.1 ingest, 6.2 review/revise, 6.3 comparison) | Complete; 6.3 being tuned from owner feedback |
| 7 Bayesian evaluation (labeled sets, credible intervals, calibration, PSIS-LOO) | Planned |
| 8 Bayesian analytics B0–B1 | Complete |
| 8 B2 salary, B3 relevance/fit probability, B4 survival, B5 trends | When data allows (see README thresholds) |
| 9 Deep learning (JobBERT/ModernBERT embeddings feeding Bayesian models) | Future |

Candidate next work: copy/export ★ keywords across the shortlist; resume-edit
suggestions saved as a new resume version; Phase 7 hand-labeled requirement lines to
measure 6.3 accuracy with credible intervals; review the cadence report after 28 days of
daily runs (recommends daily vs weekly vs biweekly).

## Working style

- The owner reviews results in the dashboard and reports specific mismatches; respond
  with the cause, the fix, and how it was verified.
- End every response with a short list of next potential steps/directions.
- Ask before destructive git operations (force-push, history rewrite) or changing the
  collection schedule.
