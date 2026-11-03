# Phase 8 B3: Bayesian relevance weights (design)

Status: **approved 2026-11-03** (all recommendations Q1–Q7 accepted); implemented in
`bayesian/relevance.py`, `bayesian/evaluation.py`, `analytics/relevance_model.py`, and
`scripts/fit_relevance_weights.py`.
Date: 2026-11-02. Data as of 2026-10-31: 35 active tracking labels (23 positive, 12 negative);
B3 minimum 30, target 50 (`config/labels.yaml` → `tracking`).

## 1. Goal

Learn, from my own tracking decisions, how much each relevance signal actually predicts
that I act on a posting, and turn the relevance signals into a **probability with an
uncertainty range**. B3 is advisory: it reports a posterior and a suggested weight set;
changing `weights:` in `config/resume_profile.local.yaml` stays a human edit.

B3 answers three questions, each with a credible interval:

1. **Is the current score informative?** Does a higher relevance score make me more likely
   to keep a posting?
2. **Are the weights right?** Where do the 35/30/20/15 skills/title/experience/location
   weights sit inside the posterior, and which ones does the data push on?
3. **What is the chance I keep this posting?** A calibrated P(positive) per current posting,
   which Phase 7 can check for calibration and compare across models with PSIS-LOO.

Non-goals for this stage: changing scores in Gold, predicting interviews or offers (far too
few), embeddings (Phase 9), and changing which statuses count as positive (decided
2026-11-02: positives stay reviewing, saved, applied, interviewing, offer; negative is
archived; rejected is neither).

## 2. What exists today

- **Score** (`analytics/gold_summaries.py::_relevance_row`): four signals in [0, 1]
  (skills, title, experience, location), combined as
  `score = 100 * Σ_k w_k s_k / Σ_k w_k`, then caps (non-home onsite 79, avoided title 39,
  optional employment cap). Bands: strong ≥ 80, moderate ≥ 60.
- **Training data** (`analytics/tracking_labels.py`): every status change is logged with
  the signals shown at that moment. `training_rows()` returns the newest active positive or
  negative decision per posting with `skills_signal`…`location_signal`, `pre_cap_score`,
  `score_cap_reason`, `fit_score`, `requirements_score`, `keywords_score`, the weights in
  force, `source` (tracking / sync / labels_tab) and `captured`. Snapshots freeze the log
  by prefix + SHA-256.
- **Simulator heuristic** (`dashboard/static/app.js`): shifts each weight by up to 20 points
  per unit of mean-signal difference between positives and archived. No uncertainty.
- **B0 foundation** (`bayesian/`): `check_inference_data()` gate (R-hat ≤ 1.01, bulk/tail
  ESS ≥ 400, 0 divergences), `model_card()`, `config/bayesian.yaml`, optional `bayes`
  extra (PyMC 6 + nutpie, ArviZ 1.0, Bambi).

A useful property: the signals do **not** depend on the weights. Weights only combine
them, so decisions logged under earlier weight settings are still valid training rows.
Signals do depend on the resume version and scoring policy; the fit records that mix.

## 3. The model

### 3.1 Main model (M2): weights on the simplex

The score is a weighted average, so B3 keeps that shape and learns the weights directly:

```
w     ~ Dirichlet(κ · w0)                 # w0 = current weights / 100 = (.35,.30,.20,.15)
τ     ~ LogNormal(log 4, 0.75)            # how sharply score separates kept from archived
α     ~ Normal(0, 1.5)                    # log-odds of keeping a posting at the average score
S_i   = Σ_k w_k s_ik                      # the score in [0, 1] under weights w
logit P(y_i = 1) = α + τ · (S_i − S̄)       # S̄ = mean current score in the training rows
y_i   ~ Bernoulli(P)
```

Why this parameterization:

- **Priors centered on the current weights** are literal: E[w] = w0. κ (prior strength,
  default 40) acts like "κ pseudo-observations of the current weights", so with 35 labels
  the data can move the weights but not swing them wildly.
- The **posterior of w is directly a suggested weight set** in the same units as YAML
  (rounded to integers totaling 100), with an interval per weight. No translation from
  free logistic coefficients back to weights.
- Weights stay non-negative, matching `ResumeRelevanceWeights` (`ge=0`). A signal that does
  not help is shrunk toward small weight rather than turned negative.
- τ is the "is the score informative" parameter: P(τ > 0) is 1 by construction, so the
  question is answered by comparing against M0/M1 (3.2) and by τ's size. Under the prior,
  a 25-point score difference multiplies the odds of keeping a posting by a median 2.7×
  (90% prior interval about 1.3× to 31×): weak, but it rules out absurd separations. The
  prior predictive check (5.1) confirms this on the real signals.

Caps are not modeled separately: an avoided title already has title signal 0 and a
non-home onsite job already has location signal 0, so the signals carry the cap. The
pre-cap score is what M2 rebuilds. M3 below tests whether caps or 6.3 fit add anything.

### 3.2 Comparison models (for PSIS-LOO)

| Model | Linear predictor | Question it answers |
| --- | --- | --- |
| M0 baseline | α | Base rate only: is any signal better than "I keep about 2 in 3"? |
| M1 current score | α + τ·(S(w0) − S̄) | Is today's hand-set score informative and calibrated? |
| M2 learned weights | α + τ·(S(w) − S̄), w ~ Dirichlet(κ w0) | Do learned weights predict better than the current ones? |
| M3 weights + fit | M2 + γ·(fit_score/100 − mean), γ ~ Normal(0, 1.5) | Does 6.3 application fit add information beyond relevance? |

M3 is fit only on rows that have a fit score. Rows without one are reported, and M3's LOO
is compared with M2 refit on the same rows, so the comparison is like for like.

Expected honest result at 35 labels: M1 and M2 within about 1 SE of each other in ELPD, and
wide weight intervals. That is a valid outcome. B3 then reports "no evidence to change the
weights yet" and how many more labels would likely be needed (section 6).

### 3.3 Things deliberately left out at this size

- Employer or family random effects (35 rows across many employers; revisit at ~150).
- Ordinal outcome (archived < reviewing < applied < interviewing): too few above "applied".
- Interactions and non-linear signal transforms.
- Selection model for unlabeled "new" postings (see 4.2).

## 4. Data

### 4.1 Training rows

Input is a **verified tracking snapshot** (`scripts/tracking_labels.py snapshot`), so every
fit names the exact log prefix it used. From `training_rows()`:

- Keep rows with `captured = true` and all four signals present.
- Drop `captured = false` rows (no signals) and report how many.
- `source = sync` rows carry signals from the day they were added, not the day decided.
  **Default: include them**, and run a sensitivity fit without them; the report shows both
  weight posteriors so I can see whether they matter.
- Record the mix of `resume_version_id` and `comparison_model_version` in the output.

### 4.2 Known biases (stated on the model card)

- **Selection / range restriction.** I only label postings I look at, which are mostly the
  top of the ranking. The model learns what separates "kept" from "archived" among postings
  already ranked high; it is not an estimate over all postings. The report shows the score
  range covered by training rows, and per-posting probabilities outside that range are
  flagged as extrapolation.
- **Class balance** 23/12 is fine for logistic regression; no reweighting.
- **Weak positives.** "Reviewing" is a soft yes. Kept as decided; a sensitivity fit with
  positives = saved and above is listed as optional (open question Q5).
- **Drift.** Signals are recomputed when scoring policy or the resume changes. The fit
  records versions so a later fit can be compared fairly.

## 5. Inference and checks

### 5.1 Before fitting: prior predictive check

Draw from the prior on the actual training signals and report the implied P(positive)
spread and the implied weight distribution. Pass condition (shown, not enforced): the
prior does not put most mass on probabilities near 0 or 1.

### 5.2 Sampling and the diagnostics gate

- PyMC model, nutpie sampler, 4 chains × 1000 tuning + 1000 draws, fixed seed from config,
  `target_accept` 0.95 (Dirichlet + small data can cause divergences).
- `check_inference_data()` must pass (R-hat ≤ 1.01, bulk and tail ESS ≥ 400, 0
  divergences) for every reported model. **A model that fails the gate is not reported**:
  the output says which check failed and shows no posterior numbers for it.

### 5.3 After fitting: Phase 7 hooks

B3 produces probabilities, so it is the first model the Phase 7 calibration and model
comparison checks run on. All of these use **leave-one-out** predictions, never in-sample:

- **PSIS-LOO** (ArviZ `loo`/`compare`) for M0–M3: ELPD, ELPD difference and its SE, and
  Pareto k per row. Rows with k > 0.7 are listed; if any exist for a model, its LOO is
  marked unreliable (with 35 rows, exact refits for those rows are cheap and are done
  instead, up to `max_exact_refits`).
- **Brier score** from LOO predictive probabilities, with its standard error, versus
  the M0 base-rate Brier.
- **Reliability curve**: rows binned by LOO probability (default 4 bins at this size),
  each bin's observed keep-rate as a Beta-Binomial posterior (`bayesian/beta.py`) with a
  90% interval, plotted against the bin's mean predicted probability.
- **LOO-PIT / posterior predictive check**: predicted vs observed number of positives.

## 6. What B3 outputs

All outputs are written privately to `data/private/models/b3/` (they encode my
preferences), never to Gold or jmip-data:

- `latest.json`: model card, snapshot id + SHA, row counts and exclusions, diagnostics per
  model, LOO table, calibration numbers, and for M2:
  - each weight's posterior mean and 90% interval, the current weight, and
    P(w_k > current w_k);
  - a **suggested weight set** (posterior mean, integers totaling 100);
  - a **recommendation verdict** per weight: "raise" / "lower" only when
    P(w_k above or below current) ≥ 0.9 **and** M2 beats M1 by ELPD difference > 2 SE;
    otherwise "keep". Thresholds in config.
  - "labels likely needed": a simple projection from the current interval widths
    (width ∝ 1/√n) of when intervals would be narrow enough to separate ±5 points.
- `posterior.npz`: posterior draws of every model that passed, for reproducing plots
  (NumPy archive, so no netCDF library is needed).
- Per-posting P(positive) with 90% interval for current mart postings, computed by the
  CLI from the stored posterior (the dashboard never samples).

Where it shows in the dashboard (proposed, see Q3):

- **Labels → Training data → B3 model** panel: verdict, weight intervals vs current,
  LOO table, reliability curve, model card, exclusions and biases.
- **Weight simulator**: a "B3 posterior" row beside the existing heuristic recommendation,
  with "Apply" filling the simulator only (still no save to YAML). The heuristic stays,
  labeled as a heuristic.
- Not yet in the opportunities table or drawer. A per-posting probability column waits
  until the calibration check looks acceptable.

## 7. Configuration

New section in `config/bayesian.yaml`, validated by a new Pydantic model in
`utils/config.py` (`extra="forbid"`):

```yaml
relevance_weights:          # B3
  enabled: true
  version: 1.0.0
  prior_strength: 40        # κ: Dirichlet pseudo-counts around the current weights
  tau_prior: {median: 4.0, sigma: 0.75}         # LogNormal
  intercept_sigma: 1.5
  fit_coefficient_sigma: 1.5                    # M3 only
  models: [baseline, current_score, learned_weights, weights_plus_fit]
  include_sync_rows: true
  sensitivity: {without_sync: true, prior_strengths: [20, 80]}
  sampler: {name: nutpie, chains: 4, tune: 1000, draws: 1000, target_accept: 0.95, seed: 20261102}
  recommend: {direction_probability: 0.9, elpd_se_multiple: 2.0, target_interval_points: 10}
  calibration_bins: 4
  max_exact_refits: 10
  pareto_k_threshold: 0.7
```

The minimum label count stays in `config/labels.yaml` (`b3_minimum: 30`); the CLI refuses to
fit below it. A prior-strength sensitivity (κ = 20 and 80) is reported beside the main fit
so I can see how much the prior drives the answer.

## 8. Code plan (after sign-off)

| Piece | Role |
| --- | --- |
| `bayesian/relevance.py` | Row preparation (pure NumPy, testable without PyMC), model builders M0–M3 (PyMC imported lazily), summaries, recommendation verdicts. |
| `bayesian/evaluation.py` | Phase 7 pieces reused by later stages: LOO table, LOO Brier, reliability bins with Beta intervals. |
| `analytics/relevance_model.py` | Snapshot → training rows → fit → private outputs; reads the latest report for the dashboard. |
| `scripts/fit_relevance_weights.py` | CLI: `--snapshot ID` (or newest verified), fits, gates, writes `data/private/models/b3/`, JSON summary on stdout, non-zero exit on gate failure. Never prints posting text. |
| `dashboard/app.py` | `GET /api/models/b3` reads `latest.json`; no sampling in the server. |
| `dashboard/static/` | Training data panel + simulator row (asset version bump). |
| `utils/config.py` | `RelevanceWeightsConfig`. |
| `tests/test_bayesian_relevance.py` | Synthetic rows only. Unit tests for prep/verdicts/config without PyMC; a parameter-recovery test (simulate 300 rows from known weights, check intervals cover them) and a gate-failure test, skipped when the `bayes` extra is absent. |

CI note: `quality.yml` installs `requirements-dev.txt` only, so PyMC tests skip in CI unless
I add the extra there (open question Q6). Sampling time at this size is seconds.

Suggested slices: (1) config + row prep + M1/M2 + gate + CLI; (2) M0/M3, LOO, calibration;
(3) dashboard panel and simulator row; (4) README + CLAUDE.md updates.

## 9. Open questions for sign-off

| # | Question | Recommendation |
| --- | --- | --- |
| Q1 | Weights on the simplex (Dirichlet, centered on current weights) vs free logistic coefficients per signal | Simplex: directly gives YAML-ready weights and keeps them non-negative. |
| Q2 | Prior strength κ = 40 (≈ the label count) | 40, with 20 and 80 shown as sensitivity. |
| Q3 | Where B3 shows: Training data panel + simulator row; no drawer/table probability yet | Yes, add the per-posting probability column only after calibration looks fine. |
| Q4 | Include `sync` rows by default, with a sensitivity fit without them | Include. |
| Q5 | Also run a sensitivity fit with positives = saved and above (does not change labels.yaml) | Optional; I'd skip it until ~50 labels. |
| Q6 | Install the `bayes` extra in CI so the recovery test runs there | Yes, if install time is acceptable (PyMC + nutpie wheels). |
| Q7 | Keep the simulator heuristic beside B3 | Keep, labeled as a heuristic, until B3's verdict is stable. |
