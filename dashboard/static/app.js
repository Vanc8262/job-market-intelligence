"use strict";

const PAGE_SIZE = 25;
const ACTIVE_STATUSES = new Set(["applied", "interviewing", "offer"]);
const STATUS_OPTIONS = ["new", "reviewing", "saved", "applied", "interviewing", "offer", "rejected", "archived"];
const SIGNAL_KEYS = ["skills", "title", "experience", "location"];
const POSITIVE_FEEDBACK_STATUSES = new Set(["reviewing", "saved", "applied", "interviewing", "offer"]);
const state = { mart: null, market: null, operations: null, candidate: null, opportunities: [], tracking: {}, filtered: [], page: 1, band: "all", view: "opportunities", marketView: "overview", operationsView: "overview", selectedJobId: null, simulatorWeights: null };
const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  ["freshnessLabel", "showingMetric", "strongMetric", "shortlistMetric", "applicationMetric", "showingLabel", "strongLabel", "shortlistLabel", "applicationLabel", "searchInput", "employerFilter", "familyFilter", "seniorityFilter", "workplaceFilter", "lifecycleFilter", "applicationFilter", "salaryFilter", "sortSelect", "opportunitiesBody", "resultCount", "activeFilters", "loadingState", "emptyState", "errorState", "errorMessage", "retryButton", "resetFilters", "emptyReset", "previousPage", "nextPage", "pageLabel", "bandFilters", "detailDrawer", "drawerBackdrop", "drawerContent", "drawerRank", "closeDrawer", "filtersPanel", "filterToggle", "viewSwitch", "viewTitle", "resultsPanel", "intelligencePanel", "intelligenceTabs", "intelligenceContent", "intelligenceFreshness", "qualityBadge", "operationsPanel", "operationsTabs", "operationsContent", "operationsFreshness", "operationsBadge", "refreshOperations", "simulatorPanel", "simulatorContent", "simulatorStatus", "resumeReviewPanel", "resumeReviewContent", "resumeReviewSummary", "resumeReviewSelect", "resumeReviewShowSuperseded", "pageName"].forEach((id) => { elements[id] = document.getElementById(id); });
  bindEvents();
  loadData();
});

function bindEvents() {
  let searchTimer;
  elements.searchInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => applyFilters(true), 120); });
  ["employerFilter", "familyFilter", "seniorityFilter", "workplaceFilter", "lifecycleFilter", "applicationFilter", "salaryFilter", "sortSelect"].forEach((id) => elements[id].addEventListener("change", () => applyFilters(true)));
  elements.bandFilters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-value]");
    if (!button) return;
    state.band = button.dataset.value;
    elements.bandFilters.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    applyFilters(true);
  });
  elements.viewSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-view]");
    if (!button) return;
    state.view = button.dataset.view;
    elements.viewSwitch.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    setDashboardView();
    applyFilters(true);
  });
  elements.intelligenceTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-intelligence-view]");
    if (!button) return;
    state.marketView = button.dataset.intelligenceView;
    elements.intelligenceTabs.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    renderMarketIntelligence();
  });
  elements.operationsTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-operations-view]");
    if (!button) return;
    state.operationsView = button.dataset.operationsView;
    elements.operationsTabs.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    renderPipelineOperations();
  });
  elements.refreshOperations.addEventListener("click", () => refreshPipelineOperations());
  elements.simulatorContent.addEventListener("change", handleSimulatorChange);
  elements.simulatorContent.addEventListener("click", handleSimulatorClick);
  elements.opportunitiesBody.addEventListener("click", (event) => { const row = event.target.closest("tr[data-job-id]"); if (row) openDrawer(row.dataset.jobId); });
  elements.opportunitiesBody.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { const row = event.target.closest("tr[data-job-id]"); if (row) { event.preventDefault(); openDrawer(row.dataset.jobId); } }
  });
  elements.drawerContent.addEventListener("submit", saveTracking);
  bindResumeReview();
  elements.drawerContent.addEventListener("change", handleTrackingFormChange);
  elements.resetFilters.addEventListener("click", resetFilters);
  elements.emptyReset.addEventListener("click", resetFilters);
  elements.retryButton.addEventListener("click", loadData);
  elements.previousPage.addEventListener("click", () => changePage(-1));
  elements.nextPage.addEventListener("click", () => changePage(1));
  elements.closeDrawer.addEventListener("click", closeDrawer);
  elements.drawerBackdrop.addEventListener("click", closeDrawer);
  elements.filterToggle.addEventListener("click", () => { const open = elements.filtersPanel.classList.toggle("open"); elements.filterToggle.setAttribute("aria-expanded", String(open)); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
}

async function loadData() {
  elements.loadingState.hidden = false;
  elements.errorState.hidden = true;
  elements.emptyState.hidden = true;
  refreshCandidateProfile();
  try {
    const [martResponse, trackingResponse, marketResponse, operationsResponse] = await Promise.all([fetch("/api/opportunities", { cache: "no-store" }), fetch("/api/application-tracking", { cache: "no-store" }), fetch("/api/market-intelligence", { cache: "no-store" }), fetch("/api/pipeline-operations", { cache: "no-store" })]);
    if (!martResponse.ok) throw new Error((await martResponse.json().catch(() => ({}))).error || "Opportunity data could not be loaded");
    if (!trackingResponse.ok) throw new Error("Application tracking could not be loaded");
    const [mart, tracking] = await Promise.all([martResponse.json(), trackingResponse.json()]);
    if (!Array.isArray(mart.opportunities)) throw new Error("The Gold mart has no opportunities array");
    state.mart = mart;
    state.opportunities = mart.opportunities;
    state.tracking = tracking.records || {};
    state.market = marketResponse.ok ? await marketResponse.json() : null;
    state.operations = operationsResponse.ok ? await operationsResponse.json() : null;
    state.simulatorWeights = currentScoringWeights();
    populateFilters();
    elements.freshnessLabel.textContent = `Gold refreshed ${formatTimestamp(mart.generated_at)}`;
    elements.intelligenceFreshness.textContent = state.market ? `Snapshot refreshed ${formatTimestamp(state.market.generated_at)}` : "Market summaries unavailable. Run scripts/build_gold.py.";
    elements.operationsFreshness.textContent = state.operations ? `Latest evidence ${formatTimestamp(state.operations.generated_at)}` : "Pipeline evidence unavailable. Run the pipeline first.";
    applyFilters(true);
    elements.loadingState.hidden = true;
  } catch (error) {
    elements.loadingState.hidden = true;
    elements.errorState.hidden = false;
    elements.errorMessage.textContent = error.message;
  }
}

function populateFilters() {
  populateSelect(elements.employerFilter, uniqueValues("employer"));
  populateSelect(elements.familyFilter, uniqueValues("family"), formatLabel);
  populateSelect(elements.seniorityFilter, uniqueValues("seniority"), formatLabel);
  populateSelect(elements.workplaceFilter, uniqueValues("workplace_type"), formatLabel);
  populateSelect(elements.lifecycleFilter, uniqueValues("lifecycle_status"), formatLabel);
}
function uniqueValues(field) { return [...new Set(state.opportunities.map((item) => item[field]).filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b))); }
function populateSelect(select, values, labeler = (value) => value) {
  const first = select.options[0];
  select.replaceChildren(first, ...values.map((value) => { const option = document.createElement("option"); option.value = value; option.textContent = labeler(value); return option; }));
}
function trackingFor(jobId) { return state.tracking[jobId] || { shortlisted: false, status: "new", priority: "none", notes: "" }; }
function setDashboardView() {
  const intelligence = state.view === "intelligence";
  const operations = state.view === "operations";
  const simulator = state.view === "simulator";
  const resumeReview = state.view === "resume-review";
  const panelMode = intelligence || operations || simulator || resumeReview;
  elements.filtersPanel.hidden = panelMode;
  elements.resultsPanel.hidden = panelMode;
  elements.intelligencePanel.hidden = !intelligence;
  elements.operationsPanel.hidden = !operations;
  elements.simulatorPanel.hidden = !simulator;
  elements.resumeReviewPanel.hidden = !resumeReview;
  const grid = document.querySelector(".content-grid");
  grid.classList.toggle("market-mode", intelligence);
  grid.classList.toggle("operations-mode", operations);
  grid.classList.toggle("simulator-mode", simulator);
  grid.classList.toggle("resume-review-mode", resumeReview);
  elements.pageName.textContent = intelligence ? "Market intelligence" : operations ? "Pipeline operations" : simulator ? "Weight simulator" : resumeReview ? "Resume review" : "Opportunities";
  if (resumeReview && !state.review.list && !state.review.loading) loadResumeReviews();
  elements.viewTitle.textContent = state.view === "shortlist" ? "Shortlist" : "Current opportunities";
}

function applyFilters(resetPage = false) {
  const query = elements.searchInput.value.trim().toLowerCase();
  const filters = { employer: elements.employerFilter.value, family: elements.familyFilter.value, seniority: elements.seniorityFilter.value, workplace_type: elements.workplaceFilter.value, lifecycle_status: elements.lifecycleFilter.value };
  state.filtered = state.opportunities.filter((item) => {
    const tracking = trackingFor(item.job_id);
    if (state.view === "shortlist" && !tracking.shortlisted) return false;
    if (elements.applicationFilter.value && tracking.status !== elements.applicationFilter.value) return false;
    if (state.band !== "all" && item.match_band !== state.band) return false;
    if (elements.salaryFilter.checked && !item.has_salary) return false;
    if (Object.entries(filters).some(([field, value]) => value && item[field] !== value)) return false;
    if (!query) return true;
    return [item.title, item.normalized_title, item.employer, item.location, ...(item.skills || [])].filter(Boolean).join(" ").toLowerCase().includes(query);
  });
  sortRows(state.filtered, elements.sortSelect.value);
  if (resetPage) state.page = 1;
  render();
}
function sortRows(rows, sort) {
  const comparators = {
    rank: (a, b) => a.rank - b.rank,
    freshness: (a, b) => new Date(b.last_seen_at) - new Date(a.last_seen_at) || a.rank - b.rank,
    salary: (a, b) => (b.salary_midpoint || -1) - (a.salary_midpoint || -1) || a.rank - b.rank,
    employer: (a, b) => a.employer.localeCompare(b.employer) || a.rank - b.rank,
  };
  rows.sort(comparators[sort] || comparators.rank);
}
function render() {
  renderMetrics();
  if (state.view === "intelligence") { renderMarketIntelligence(); return; }
  if (state.view === "operations") { renderPipelineOperations(); return; }
  if (state.view === "simulator") { renderWeightSimulator(); return; }
  if (state.view === "resume-review") { renderResumeReview(); return; }
  renderActiveFilters(); renderTable(); renderPagination();
}
function renderMetrics() {
  if (state.view === "resume-review") {
    const list = state.review.list;
    const extraction = state.review.detail?.extraction;
    elements.showingLabel.textContent = "Extractions";
    elements.strongLabel.textContent = "Pending review";
    elements.shortlistLabel.textContent = "Approved";
    elements.applicationLabel.textContent = "Text quality";
    elements.showingMetric.textContent = list ? number(list.items.length) : "--";
    elements.strongMetric.textContent = list ? number(list.pending_count) : "--";
    elements.shortlistMetric.textContent = list ? number(list.approved_count) : "--";
    elements.applicationMetric.textContent = extraction ? formatPercent(extraction.quality_score) : "--";
    return;
  }
  if (state.view === "simulator" && state.market) {
    const simulation = calculateSimulation();
    const feedback = calculateCalibration();
    elements.showingLabel.textContent = "Weight total";
    elements.strongLabel.textContent = "Strong matches";
    elements.shortlistLabel.textContent = "Rank changes";
    elements.applicationLabel.textContent = "Feedback labels";
    elements.showingMetric.textContent = `${SIGNAL_KEYS.reduce((total, key) => total + state.simulatorWeights[key], 0)}%`;
    elements.strongMetric.textContent = number(simulation.filter((item) => item.match_band === "strong").length);
    elements.shortlistMetric.textContent = number(simulation.filter((item) => item.rank_delta !== 0).length);
    elements.applicationMetric.textContent = `${feedback.labelCount} / ${feedback.minimumLabels}`;
    return;
  }
  if (state.view === "operations" && state.operations) {
    const operations = state.operations;
    const collection = operations.collection;
    const qualityMetrics = operations.quality.metrics || [];
    const events = operations.lifecycle.event_counts || {};
    elements.showingLabel.textContent = "Pipeline status";
    elements.strongLabel.textContent = "Targets succeeded";
    elements.shortlistLabel.textContent = "Quality checks";
    elements.applicationLabel.textContent = "Lifecycle events";
    elements.showingMetric.textContent = formatLabel(operations.status);
    elements.strongMetric.textContent = `${number(collection.targets_succeeded)} / ${number(collection.targets_attempted)}`;
    elements.shortlistMetric.textContent = `${number(qualityMetrics.filter((item) => item.passed).length)} / ${number(qualityMetrics.length)}`;
    elements.applicationMetric.textContent = number(Object.values(events).reduce((total, value) => total + Number(value || 0), 0));
    return;
  }
  if (state.view === "intelligence" && state.market) {
    const summaries = state.market.summaries;
    const workplace = summaries.locations.workplace_types || [];
    const accessible = workplace.filter((item) => item.workplace_type === "remote" || item.workplace_type === "hybrid").reduce((total, item) => total + item.posting_share, 0);
    elements.showingLabel.textContent = "Active records";
    elements.strongLabel.textContent = "Average relevance";
    elements.shortlistLabel.textContent = "Salary coverage";
    elements.applicationLabel.textContent = "Remote or hybrid";
    elements.showingMetric.textContent = Number(summaries.skills.records_analyzed || 0).toLocaleString();
    elements.strongMetric.textContent = formatScore(summaries.relevance.score_summary.average);
    elements.shortlistMetric.textContent = formatPercent(summaries.salaries.coverage.posting_share);
    elements.applicationMetric.textContent = formatPercent(accessible);
    return;
  }
  elements.showingLabel.textContent = "Showing";
  elements.strongLabel.textContent = "Strong matches";
  elements.shortlistLabel.textContent = "Shortlisted";
  elements.applicationLabel.textContent = "Active applications";
  elements.showingMetric.textContent = state.filtered.length.toLocaleString();
  elements.strongMetric.textContent = state.filtered.filter((item) => item.match_band === "strong").length.toLocaleString();
  elements.shortlistMetric.textContent = Object.values(state.tracking).filter((item) => item.shortlisted).length.toLocaleString();
  elements.applicationMetric.textContent = Object.values(state.tracking).filter((item) => ACTIVE_STATUSES.has(item.status)).length.toLocaleString();
  elements.resultCount.textContent = `${state.filtered.length.toLocaleString()} ${state.view === "shortlist" ? "shortlisted postings" : "active postings"}`;
}
function renderTable() {
  const start = (state.page - 1) * PAGE_SIZE;
  elements.opportunitiesBody.innerHTML = state.filtered.slice(start, start + PAGE_SIZE).map((item) => {
    const tracking = trackingFor(item.job_id);
    const marker = tracking.shortlisted ? `<span class="shortlist-marker">Shortlisted</span>` : "";
    return `<tr tabindex="0" data-job-id="${escapeHtml(item.job_id)}" aria-label="Open ${escapeHtml(item.title)} at ${escapeHtml(item.employer)}">
      <td class="rank-cell">${item.rank}</td><td><span class="score-badge score-${escapeHtml(item.match_band)}">${formatScore(item.resume_relevance_score)}</span></td>
      <td><span class="job-title">${escapeHtml(item.title)}</span><span class="job-meta">${escapeHtml(formatLabel(item.seniority || "unknown"))} / ${escapeHtml(formatSalary(item))} ${marker}</span></td>
      <td class="cell-truncate">${escapeHtml(item.employer)}</td><td class="cell-truncate" title="${escapeHtml(item.location || "Unknown")}">${escapeHtml(item.location || "Unknown")}</td>
      <td><span class="status-label ${escapeHtml(item.workplace_type)}">${escapeHtml(formatLabel(item.workplace_type))}</span></td><td><span class="status-label application-${escapeHtml(tracking.status)}">${escapeHtml(formatLabel(tracking.status))}</span></td></tr>`;
  }).join("");
  elements.emptyState.querySelector("strong").textContent = state.view === "shortlist" && state.filtered.length === 0 ? "Your shortlist is empty" : "No opportunities match these filters";
  elements.emptyState.hidden = state.filtered.length !== 0;
  elements.opportunitiesBody.closest("table").hidden = state.filtered.length === 0;
}
function renderPagination() {
  const pages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
  if (state.page > pages) state.page = pages;
  elements.pageLabel.textContent = `Page ${state.page} of ${pages}`;
  elements.previousPage.disabled = state.page <= 1;
  elements.nextPage.disabled = state.page >= pages;
}
function renderActiveFilters() {
  const chips = [];
  if (state.band !== "all") chips.push(`Match: ${formatLabel(state.band)}`);
  if (elements.searchInput.value.trim()) chips.push(`Search: ${elements.searchInput.value.trim()}`);
  [[elements.employerFilter, "Employer"], [elements.familyFilter, "Family"], [elements.seniorityFilter, "Seniority"], [elements.workplaceFilter, "Workplace"], [elements.lifecycleFilter, "Lifecycle"], [elements.applicationFilter, "Application"]].forEach(([select, label]) => { if (select.value) chips.push(`${label}: ${formatLabel(select.value)}`); });
  if (elements.salaryFilter.checked) chips.push("Salary listed");
  elements.activeFilters.innerHTML = chips.map((chip) => `<span class="filter-chip">${escapeHtml(chip)}</span>`).join("");
}

function renderMarketIntelligence() {
  if (!state.market) { elements.intelligenceContent.innerHTML = `<div class="intel-empty">Market summaries are unavailable</div>`; return; }
  const quality = state.market.summaries.data_quality;
  elements.qualityBadge.textContent = quality.passed ? `Quality passed / ${quality.valid_records} valid records` : `${quality.failed_metrics} quality checks failed`;
  elements.qualityBadge.classList.toggle("failed", !quality.passed);
  const renderers = { overview: renderMarketOverview, skills: renderSkillsView, roles: renderRolesView, employers: renderEmployersView, locations: renderLocationsView, compensation: renderCompensationView };
  elements.intelligenceContent.innerHTML = (renderers[state.marketView] || renderers.overview)();
}

function renderMarketOverview() {
  const { skills, titles, employers, salaries, relevance, lifecycle, data_quality: quality } = state.market.summaries;
  const score = relevance.score_summary;
  return `<div class="intel-grid three">
    ${marketKpi("Postings analyzed", skills.records_analyzed, `${skills.excluded_inactive} inactive excluded`)}
    ${marketKpi("Distinct skills", skills.coverage.distinct_skills, `${formatPercent(skills.coverage.posting_share)} skill coverage`)}
    ${marketKpi("Strong and moderate", Number(score.strong_matches || 0) + Number(score.moderate_matches || 0), `${formatScore(score.maximum)} highest relevance`)}
    ${marketKpi("Employers", employers.distinct_employers, `${(lifecycle.current_counts || {}).active || 0} lifecycle-active postings`)}
    ${marketKpi("Distinct titles", titles.distinct_titles, `${titles.families.length} role families`)}
    ${marketKpi("Salary disclosed", salaries.coverage.postings_with_salary, `${formatPercent(salaries.coverage.posting_share)} of postings`)}
    <section class="intel-block">${intelHeading("Skills in demand", "Top 10 by posting count")}${rankingList(skills.skills, "skill", "posting_count", 10)}</section>
    <section class="intel-block">${intelHeading("Employer concentration", "Current active snapshot")}${rankingList(employers.employers, "employer", "posting_count", 10)}</section>
    <section class="intel-block">${intelHeading("Role demand", "Normalized titles")}${rankingList(titles.titles, "title", "posting_count", 10)}</section>
    <section class="intel-block">${intelHeading("Lifecycle activity", "Latest collection")}${rankingList(lifecycle.employer_activity, "employer", "events", 10)}</section>
    <section class="intel-block wide">${intelHeading("Data-quality gateway", quality.passed ? "All blocking checks passed" : "Review failed checks")}${qualityList(quality.metrics)}</section>
  </div>`;
}

function renderSkillsView() {
  const summary = state.market.summaries.skills;
  const bayes = summary.bayesian;
  const intervals = new Map((bayes?.skills || []).map((row) => [row.skill, row.interval]));
  const rows = (summary.skills || []).slice(0, 40).map((item) => `<tr><td><strong>${escapeHtml(item.skill)}</strong></td><td>${escapeHtml((item.categories || []).map(formatLabel).join(", ") || "Uncategorized")}</td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatPercent(item.posting_share)}</td><td class="number-cell">${intervalText(intervals.get(item.skill))}</td><td class="number-cell">${number(item.explicit_count)}</td><td class="number-cell">${number(item.inferred_count)}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Skill categories", "Share of analyzed postings")}${rankingList(summary.categories, "category", "posting_count", 10)}</section><section class="intel-block">${intelHeading("Coverage", "Canonical extraction signal")}<div class="coverage-pair"><div><strong>${number(summary.coverage.postings_with_skills)}</strong><span>Postings with skills / ${formatPercent(summary.coverage.posting_share)}</span></div><div><strong>${number(summary.coverage.distinct_skills)}</strong><span>Distinct normalized skills</span></div></div></section><section class="intel-block wide">${intelHeading("Top skills", "Explicit and inferred evidence")}${marketTable(["Skill", "Category", "Postings", "Share", `${Math.round((bayes?.credible_level || 0.9) * 100)}% interval`, "Explicit", "Inferred"], rows)}</section>${skillFamilyBlock(bayes)}</div>`;
}

function renderRolesView() {
  const summary = state.market.summaries.titles;
  const rows = (summary.titles || []).slice(0, 40).map((item) => `<tr><td><strong>${escapeHtml(item.title)}</strong></td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatPercent(item.posting_share)}</td><td class="number-cell">${formatScore(item.average_resume_relevance)}</td><td class="number-cell">${number((item.raw_variants || []).length)}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Role families", "Normalized demand")}${rankingList(summary.families, "family", "posting_count", 12)}</section><section class="intel-block">${intelHeading("Seniority mix", "Current posting share")}${rankingList(summary.seniority, "level", "posting_count", 10)}</section><section class="intel-block wide">${intelHeading("Normalized titles", `${summary.distinct_titles} distinct titles`)}${marketTable(["Title", "Postings", "Share", "Average fit", "Variants"], rows)}</section></div>`;
}

function renderEmployersView() {
  const summary = state.market.summaries.employers;
  const rows = (summary.employers || []).map((item) => `<tr><td><strong>${escapeHtml(item.employer)}</strong></td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatPercent(item.posting_share)}</td><td class="number-cell">${formatScore(item.average_resume_relevance)}</td><td class="number-cell">${number(item.remote_count)}</td><td class="number-cell">${number(item.salary_posting_count)}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Hiring volume", "Active posting count")}${rankingList(summary.employers, "employer", "posting_count", 12)}</section><section class="intel-block">${intelHeading("Resume alignment", "Average relevance score")}${rankingList([...summary.employers].sort((a, b) => b.average_resume_relevance - a.average_resume_relevance), "employer", "average_resume_relevance", 12, (value) => formatScore(value))}</section><section class="intel-block wide">${intelHeading("Employer comparison", `${summary.distinct_employers} employers`)}${marketTable(["Employer", "Postings", "Share", "Average fit", "Remote", "Salary listed"], rows)}</section></div>`;
}

function renderLocationsView() {
  const summary = state.market.summaries.locations;
  const rows = (summary.locations || []).slice(0, 40).map((item) => `<tr><td><strong>${escapeHtml(item.location)}</strong></td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatPercent(item.posting_share)}</td></tr>`).join("");
  return `<div class="intel-grid three"><section class="intel-block">${intelHeading("Workplace model", "Remote, hybrid, and onsite")}${rankingList(summary.workplace_types, "workplace_type", "posting_count", 10)}</section><section class="intel-block">${intelHeading("Regions", "Top normalized regions")}${rankingList(summary.regions, "region", "posting_count", 12)}</section><section class="intel-block">${intelHeading("Cities", "Top normalized cities")}${rankingList(summary.cities, "city", "posting_count", 12)}</section><section class="intel-block wide">${intelHeading("Raw location demand", `${summary.distinct_locations} distinct locations`)}${marketTable(["Location", "Postings", "Share"], rows)}</section></div>`;
}

function renderCompensationView() {
  const summary = state.market.summaries.salaries;
  const coverage = summary.coverage;
  const currencyRows = (summary.currencies || []).map((item) => `<tr><td><strong>${escapeHtml(item.currency)}</strong></td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatMoney(item.median_annual_midpoint, item.currency)}</td><td class="number-cell">${formatMoney(item.average_annual_midpoint, item.currency)}</td><td class="number-cell">${formatMoney(item.annual_minimum_observed, item.currency)}</td><td class="number-cell">${formatMoney(item.annual_maximum_observed, item.currency)}</td></tr>`).join("");
  const employerRows = (summary.employers || []).map((item) => `<tr><td><strong>${escapeHtml(item.employer)}</strong></td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatMoney(item.median_annual_midpoint, "USD")}</td><td class="number-cell">${formatMoney(item.average_annual_midpoint, "USD")}</td><td class="number-cell">${formatMoney(item.annual_minimum_observed, "USD")}</td><td class="number-cell">${formatMoney(item.annual_maximum_observed, "USD")}</td></tr>`).join("");
  return `<div class="intel-grid three">${marketKpi("Salary disclosed", coverage.postings_with_salary, formatPercent(coverage.posting_share))}${marketKpi("Not disclosed", coverage.postings_without_salary, "current postings")}${marketKpi("Currencies", (summary.currencies || []).length, "annualized values")}<section class="intel-block wide">${intelHeading("Compensation by currency", "Annualized midpoint and observed range")}${marketTable(["Currency", "Postings", "Median", "Average", "Minimum", "Maximum"], currencyRows)}</section><section class="intel-block wide">${intelHeading("Employer compensation", "Available disclosures only")}${employerRows ? marketTable(["Employer", "Postings", "Median", "Average", "Minimum", "Maximum"], employerRows) : `<div class="intel-empty">No employer compensation summaries available</div>`}</section></div>`;
}

function marketKpi(label, value, detail) { const displayValue = typeof value === "number" ? number(value) : String(value ?? "0"); return `<div class="intel-kpi"><span>${escapeHtml(label)}</span><strong>${escapeHtml(displayValue)}</strong><small>${escapeHtml(detail)}</small></div>`; }
// Phase 8 B1: shrunk skill shares by role family, with credible intervals.
function skillFamilyBlock(bayes) {
  if (!bayes || !(bayes.families || []).length) return "";
  const families = bayes.families.slice(0, 5);
  const level = Math.round((bayes.credible_level || 0.9) * 100);
  const rows = (bayes.skills || []).slice(0, 12).map((row) => `<tr><td><strong>${escapeHtml(row.skill)}</strong></td>${families.map((family) => { const cell = row.by_family?.[family.family]; return cell ? `<td class="number-cell" title="Raw ${formatPercent(cell.raw_share)} (${number(cell.count)} of ${number(family.postings)}); ${level}% interval ${intervalText(cell)}">${formatPercent(cell.mean)}<small class="job-meta"> ${intervalText(cell)}</small></td>` : `<td class="number-cell">–</td>`; }).join("")}</tr>`).join("");
  return `<section class="intel-block wide">${intelHeading("Skill demand by role family", `Bayesian estimate with ${level}% interval; small families borrow strength from the rest`)}${marketTable(["Skill", ...families.map((family) => `${formatLabel(family.family)} (${number(family.postings)})`)], rows)}</section>`;
}

function intervalText(interval) { return interval ? `${formatPercent(interval.low)}–${formatPercent(interval.high)}` : "–"; }

function intelHeading(title, detail) { return `<div class="intel-heading"><h2>${escapeHtml(title)}</h2><span>${escapeHtml(detail)}</span></div>`; }
function rankingList(items, labelKey, valueKey, limit = 10, formatter = number) {
  const selected = (items || []).slice(0, limit); const maximum = Math.max(...selected.map((item) => Number(item[valueKey] || 0)), 1);
  return `<div class="ranking-list">${selected.map((item) => `<div class="ranking-row"><span class="ranking-label" title="${escapeHtml(formatLabel(item[labelKey]))}">${escapeHtml(formatLabel(item[labelKey]))}</span><span class="ranking-track"><span class="ranking-fill" style="--bar-value:${Math.max(2, Number(item[valueKey] || 0) / maximum * 100)}%"></span></span><span class="ranking-value">${escapeHtml(formatter(item[valueKey]))}</span></div>`).join("")}</div>`;
}
// Headers take the alignment of their column: numeric cells (and their headers) align right.
function marketTable(headers, rows) {
  const firstRow = (rows.match(/<tr[^>]*>([\s\S]*?)<\/tr>/) || [])[1] || "";
  const numeric = (firstRow.match(/<td[^>]*>/g) || []).map((cell) => /number-cell/.test(cell));
  return `<div class="intel-table-shell"><table class="intel-table"><thead><tr>${headers.map((header, index) => `<th${numeric[index] ? ' class="number-cell"' : ""}>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
function qualityList(metrics) { return `<div class="quality-list">${(metrics || []).map((item) => `<div class="quality-row"><span>${escapeHtml(formatLabel(item.name))}</span><span class="${item.passed ? "quality-pass" : "quality-fail"}">${item.passed ? "Passed" : "Failed"} / ${escapeHtml(formatMetricValue(item.value))}</span></div>`).join("")}</div>`; }
function formatMetricValue(value) { return Number(value) <= 1 ? formatPercent(value) : number(value); }

function currentScoringProfile() {
  return state.market?.summaries?.relevance?.profile || {};
}

function currentScoringWeights() {
  const configured = currentScoringProfile().weights || {};
  const firstComponents = state.opportunities[0]?.score_components || {};
  return Object.fromEntries(SIGNAL_KEYS.map((key) => [key, Math.round(Number(configured[key] ?? firstComponents[key]?.maximum_points ?? 0))]));
}

function calculateSimulation() {
  const scoring = currentScoringProfile().scoring || {};
  const strongThreshold = Number(scoring.strong_match_threshold ?? 80);
  const moderateThreshold = Number(scoring.moderate_match_threshold ?? 60);
  const onsiteCap = Number(scoring.non_home_onsite_score_cap ?? 79);
  const rows = state.opportunities.map((item) => {
    const contributions = Object.fromEntries(SIGNAL_KEYS.map((key) => {
      const signal = Number(item.score_components?.[key]?.signal || 0);
      return [key, { signal, points: signal * Number(state.simulatorWeights[key] || 0) }];
    }));
    const preCapScore = SIGNAL_KEYS.reduce((total, key) => total + contributions[key].points, 0);
    const cap = item.score_cap_reason ? Number(item.score_cap ?? onsiteCap) : null;
    const score = cap === null ? preCapScore : Math.min(preCapScore, cap);
    const matchBand = score >= strongThreshold ? "strong" : score >= moderateThreshold ? "moderate" : "limited";
    return { ...item, simulated_score: Math.round(score * 100) / 100, simulated_pre_cap_score: Math.round(preCapScore * 100) / 100, simulated_components: contributions, match_band: matchBand };
  });
  rows.sort((a, b) => b.simulated_score - a.simulated_score || a.rank - b.rank || String(a.job_id).localeCompare(String(b.job_id)));
  return rows.map((item, index) => ({ ...item, simulated_rank: index + 1, rank_delta: item.rank - (index + 1) }));
}

function feedbackLabel(jobId) {
  const tracking = state.tracking[jobId];
  if (!tracking) return null;
  if (tracking.status === "archived") return "negative";
  if (tracking.shortlisted || POSITIVE_FEEDBACK_STATUSES.has(tracking.status)) return "positive";
  return null;
}

function averageSignals(rows) {
  return Object.fromEntries(SIGNAL_KEYS.map((key) => [key, rows.length ? rows.reduce((total, item) => total + Number(item.score_components?.[key]?.signal || 0), 0) / rows.length : 0]));
}

function normalizeRecommendedWeights(rawWeights) {
  const bounded = Object.fromEntries(SIGNAL_KEYS.map((key) => [key, Math.max(5, Math.min(50, rawWeights[key]))]));
  const total = SIGNAL_KEYS.reduce((sum, key) => sum + bounded[key], 0) || 1;
  const exact = SIGNAL_KEYS.map((key) => ({ key, value: bounded[key] / total * 100 }));
  const rounded = Object.fromEntries(exact.map(({ key, value }) => [key, Math.floor(value)]));
  let remainder = 100 - SIGNAL_KEYS.reduce((sum, key) => sum + rounded[key], 0);
  exact.sort((a, b) => (b.value - Math.floor(b.value)) - (a.value - Math.floor(a.value))).slice(0, remainder).forEach(({ key }) => { rounded[key] += 1; });
  return rounded;
}

function calculateCalibration() {
  const positive = state.opportunities.filter((item) => feedbackLabel(item.job_id) === "positive");
  const negative = state.opportunities.filter((item) => feedbackLabel(item.job_id) === "negative");
  const positiveSignals = averageSignals(positive);
  const negativeSignals = averageSignals(negative);
  const minimumLabels = 10;
  const ready = positive.length >= 3 && negative.length >= 3 && positive.length + negative.length >= minimumLabels;
  const differences = Object.fromEntries(SIGNAL_KEYS.map((key) => [key, positiveSignals[key] - negativeSignals[key]]));
  const recommended = ready ? normalizeRecommendedWeights(Object.fromEntries(SIGNAL_KEYS.map((key) => [key, state.simulatorWeights[key] + differences[key] * 20]))) : null;
  return { positive, negative, positiveSignals, negativeSignals, differences, recommended, ready, labelCount: positive.length + negative.length, minimumLabels };
}

function allocateWeight(changedKey, nextValue) {
  const selected = Math.max(0, Math.min(100, Math.round(Number(nextValue))));
  const otherKeys = SIGNAL_KEYS.filter((key) => key !== changedKey);
  const remaining = 100 - selected;
  const previousTotal = otherKeys.reduce((total, key) => total + state.simulatorWeights[key], 0);
  const exact = otherKeys.map((key) => ({ key, value: previousTotal ? state.simulatorWeights[key] / previousTotal * remaining : remaining / otherKeys.length }));
  const weights = { [changedKey]: selected, ...Object.fromEntries(exact.map(({ key, value }) => [key, Math.floor(value)])) };
  let remainder = 100 - SIGNAL_KEYS.reduce((total, key) => total + weights[key], 0);
  exact.sort((a, b) => (b.value - Math.floor(b.value)) - (a.value - Math.floor(a.value))).slice(0, remainder).forEach(({ key }) => { weights[key] += 1; });
  state.simulatorWeights = weights;
}

function handleSimulatorChange(event) {
  const input = event.target.closest("[data-weight-key]");
  if (!input) return;
  allocateWeight(input.dataset.weightKey, input.value);
  renderMetrics();
  renderWeightSimulator();
}

function handleSimulatorClick(event) {
  const preset = event.target.closest("button[data-weight-preset]");
  if (preset) {
    const presets = {
      balanced: currentScoringWeights(),
      geography: { skills: 35, title: 30, experience: 25, location: 10 },
      location: { skills: 30, title: 30, experience: 20, location: 20 },
    };
    state.simulatorWeights = { ...presets[preset.dataset.weightPreset] };
    renderMetrics();
    renderWeightSimulator();
    return;
  }
  const recommendation = event.target.closest("button[data-apply-recommendation]");
  if (recommendation) {
    const calibration = calculateCalibration();
    if (calibration.recommended) state.simulatorWeights = { ...calibration.recommended };
    renderMetrics();
    renderWeightSimulator();
  }
}

function weightControl(key) {
  const value = state.simulatorWeights[key];
  return `<label class="weight-control"><span>${escapeHtml(formatLabel(key))}</span><input type="range" min="0" max="60" step="1" value="${value}" data-weight-key="${key}" aria-label="${escapeHtml(formatLabel(key))} weight"><strong>${value}</strong></label>`;
}

function weightsMatch(candidate) {
  return SIGNAL_KEYS.every((key) => Number(state.simulatorWeights[key]) === Number(candidate[key]));
}

function rankMovement(delta) {
  if (!delta) return `<span class="rank-delta stable">No change</span>`;
  return `<span class="rank-delta ${delta > 0 ? "up" : "down"}">${delta > 0 ? "+" : ""}${delta}</span>`;
}

function renderCalibration(calibration) {
  const evidenceRows = SIGNAL_KEYS.map((key) => `<tr><td><strong>${escapeHtml(formatLabel(key))}</strong></td><td class="number-cell">${formatPercent(calibration.positiveSignals[key])}</td><td class="number-cell">${formatPercent(calibration.negativeSignals[key])}</td><td class="number-cell"><span class="signal-difference ${calibration.differences[key] >= 0 ? "positive" : "negative"}">${calibration.differences[key] >= 0 ? "+" : ""}${(calibration.differences[key] * 100).toFixed(0)} pts</span></td></tr>`).join("");
  if (!calibration.ready) {
    return `<section class="sim-block calibration-block"><div class="sim-heading"><div><h2>Calibration feedback</h2><span>Preference evidence from tracking decisions</span></div><span class="calibration-badge pending">Collecting evidence</span></div><div class="calibration-progress"><strong>${calibration.labelCount} of ${calibration.minimumLabels} labels</strong><span>${calibration.positive.length} positive / ${calibration.negative.length} archived</span><div><span style="--progress:${Math.min(100, calibration.labelCount / calibration.minimumLabels * 100)}%"></span></div></div><p class="calibration-note">Recommendations unlock after at least 10 labels, including 3 positive and 3 archived decisions. Rejected applications remain outcome evidence and do not count as a preference signal.</p>${calibration.labelCount ? marketTable(["Signal", "Positive", "Archived", "Difference"], evidenceRows) : ""}</section>`;
  }
  const recommendation = SIGNAL_KEYS.map((key) => `<div><span>${escapeHtml(formatLabel(key))}</span><strong>${calibration.recommended[key]}</strong><small>${calibration.recommended[key] - state.simulatorWeights[key] >= 0 ? "+" : ""}${calibration.recommended[key] - state.simulatorWeights[key]}</small></div>`).join("");
  return `<section class="sim-block calibration-block"><div class="sim-heading"><div><h2>Calibration feedback</h2><span>${calibration.labelCount} preference labels analyzed</span></div><span class="calibration-badge">Recommendation ready</span></div><div class="recommendation-grid">${recommendation}</div><button class="secondary-button" type="button" data-apply-recommendation>Apply recommendation</button>${marketTable(["Signal", "Positive", "Archived", "Difference"], evidenceRows)}<p class="calibration-note">The recommendation shifts at most 20 points per unit of observed signal separation, keeps every component between 5 and 50, and always totals 100.</p></section>`;
}

function renderWeightSimulator() {
  if (!state.market || !state.simulatorWeights) { elements.simulatorContent.innerHTML = `<div class="intel-empty">Scoring data is unavailable</div>`; return; }
  const simulation = calculateSimulation();
  const calibration = calculateCalibration();
  const scoring = currentScoringProfile().scoring || {};
  const changed = SIGNAL_KEYS.some((key) => state.simulatorWeights[key] !== currentScoringWeights()[key]);
  const presetWeights = { geography: { skills: 35, title: 30, experience: 25, location: 10 }, location: { skills: 30, title: 30, experience: 20, location: 20 } };
  elements.simulatorStatus.textContent = changed ? "Unsaved scenario" : "Current production weights";
  elements.simulatorStatus.classList.toggle("changed", changed);
  const rows = simulation.slice(0, 20).map((item) => {
    const components = SIGNAL_KEYS.map((key) => `<span title="${escapeHtml(formatLabel(key))}: ${formatScore(item.simulated_components[key].points)}">${key.slice(0, 1).toUpperCase()} ${formatScore(item.simulated_components[key].points)}</span>`).join("");
    return `<tr><td class="number-cell"><strong>${item.simulated_rank}</strong></td><td class="number-cell">${rankMovement(item.rank_delta)}</td><td><span class="job-title">${escapeHtml(item.title)}</span><span class="job-meta">${escapeHtml(item.employer)} / original rank ${item.rank}</span></td><td><span class="score-badge score-${escapeHtml(item.match_band)}">${formatScore(item.simulated_score)}</span></td><td class="number-cell">${formatScore(item.resume_relevance_score)}</td><td><div class="contribution-list">${components}</div></td></tr>`;
  }).join("");
  const movers = [...simulation].filter((item) => item.rank_delta !== 0).sort((a, b) => Math.abs(b.rank_delta) - Math.abs(a.rank_delta)).slice(0, 6);
  const moverRows = movers.map((item) => `<div class="mover-row"><div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.employer)}</span></div>${rankMovement(item.rank_delta)}</div>`).join("") || `<div class="sim-empty">No rank movement under this scenario</div>`;
  elements.simulatorContent.innerHTML = `<div class="sim-grid">
    <section class="sim-block weight-panel"><div class="sim-heading"><div><h2>Signal weights</h2><span>Adjusting one signal redistributes the remaining points</span></div><strong>100 total</strong></div><div class="preset-control" aria-label="Weight presets"><button type="button" data-weight-preset="balanced" class="${!changed ? "active" : ""}">Balanced</button><button type="button" data-weight-preset="geography" class="${weightsMatch(presetWeights.geography) ? "active" : ""}">Geography flexible</button><button type="button" data-weight-preset="location" class="${weightsMatch(presetWeights.location) ? "active" : ""}">Location focus</button></div><div class="weight-controls">${SIGNAL_KEYS.map(weightControl).join("")}</div><div class="threshold-line"><span>Strong ${formatScore(scoring.strong_match_threshold ?? 80)}+</span><span>Moderate ${formatScore(scoring.moderate_match_threshold ?? 60)}+</span><span>Onsite cap ${formatScore(scoring.non_home_onsite_score_cap ?? 79)}</span></div></section>
    <section class="sim-block mover-panel"><div class="sim-heading"><div><h2>Biggest movers</h2><span>Absolute rank change in this scenario</span></div></div>${moverRows}</section>
    <section class="sim-block wide"><div class="sim-heading"><div><h2>Simulated ranking</h2><span>Top 20 current opportunities</span></div><span>${simulation.filter((item) => item.match_band === "strong").length} strong matches</span></div>${marketTable(["Rank", "Move", "Opportunity", "Simulated", "Current", "Contributions"], rows)}</section>
    ${renderCalibration(calibration)}
  </div>`;
}

async function refreshPipelineOperations(silent = false) {
  const button = elements.refreshOperations;
  if (!silent) { button.disabled = true; button.classList.add("refresh-spin"); elements.operationsFreshness.textContent = "Refreshing operational status"; }
  try {
    const response = await fetch("/api/pipeline-operations", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Operational status could not be refreshed");
    state.operations = result;
    elements.operationsFreshness.textContent = `Latest evidence ${formatTimestamp(result.generated_at)}`;
    renderMetrics();
    renderPipelineOperations();
  } catch (error) {
    elements.operationsFreshness.textContent = error.message;
    elements.operationsContent.innerHTML = `<div class="ops-status-line failed"><div><strong>Refresh failed</strong><span>${escapeHtml(error.message)}</span></div></div>`;
  } finally {
    if (!silent) { button.disabled = false; button.classList.remove("refresh-spin"); }
  }
}

function renderPipelineOperations() {
  if (!state.operations) { elements.operationsContent.innerHTML = `<div class="intel-empty">Operational summaries are unavailable</div>`; return; }
  const operations = state.operations;
  elements.operationsBadge.textContent = operations.status === "healthy" ? "Pipeline healthy" : operations.status === "stale" ? "Evidence stale" : "Attention required";
  elements.operationsBadge.classList.toggle("failed", operations.status === "failed");
  elements.operationsBadge.classList.toggle("stale", operations.status === "stale");
  const renderers = { overview: renderOperationsOverview, collection: renderCollectionOperations, quality: renderQualityOperations, lifecycle: renderLifecycleOperations, outputs: renderGoldOperations };
  elements.operationsContent.innerHTML = (renderers[state.operationsView] || renderers.overview)();
}

function renderOperationsOverview() {
  const { collection, quality, lifecycle, gold, status } = state.operations;
  const eventTotal = Object.values(lifecycle.event_counts || {}).reduce((total, value) => total + Number(value || 0), 0);
  const statusCopy = status === "healthy" ? "The latest collection, lifecycle, Gold build, and quality gateway completed without recorded failures." : status === "stale" ? "The latest persisted outputs passed, but one or more stages are older than the 36-hour freshness target." : "One or more pipeline stages require review. Open the focused monitoring views for details.";
  const freshness = state.operations.freshness || {};
  const stages = [
    ["Collection", operationStageStatus(!collection.targets_failed, freshness.collection), `${number(collection.postings_extracted)} postings extracted across ${number(collection.targets_attempted)} targets`, collection.finished_at, freshness.collection],
    ["Silver validation", operationStageStatus(!quality.invalid_records, freshness.quality), `${number(quality.valid_records)} valid / ${number(quality.invalid_records)} invalid records`, quality.generated_at, freshness.quality],
    ["Lifecycle", operationStageStatus(!(lifecycle.failures || []).length, freshness.lifecycle), `${number(lifecycle.tracked_postings)} tracked / ${number(eventTotal)} latest events`, lifecycle.observed_at, freshness.lifecycle],
    ["Gold build", operationStageStatus(!(gold.failures || []).length, freshness.gold), `${number(gold.records_analyzed)} analyzed / ${number(Object.keys(gold.datasets || {}).length)} datasets`, gold.generated_at, freshness.gold],
    ["Quality gateway", operationStageStatus(quality.passed, freshness.quality), `${number((quality.metrics || []).filter((item) => item.passed).length)} of ${number((quality.metrics || []).length)} checks passed`, quality.generated_at, freshness.quality],
  ];
  const alertCount = Number(collection.targets_failed || 0) + Number(collection.search_failures || 0) + Number(collection.posting_failures || 0) + Number(quality.failed_metrics || 0) + (lifecycle.failures || []).length + (gold.failures || []).length;
  return `<div class="intel-grid three">
    <section class="ops-status-line ${status === "failed" ? "failed" : status === "stale" ? "stale" : ""} wide"><div><strong>${status === "healthy" ? "Latest pipeline evidence is healthy" : status === "stale" ? "Pipeline evidence is stale" : "Pipeline attention required"}</strong><span>${escapeHtml(statusCopy)}</span></div></section>
    ${marketKpi("Run duration", formatDuration(collection.started_at, collection.finished_at), `${number(collection.search_attempts)} search attempts`)}
    ${marketKpi("References discovered", collection.references_discovered, `${number(collection.references_selected)} selected`)}
    ${marketKpi("Unique postings", collection.unique_postings, `${number(collection.raw_records_saved)} new raw snapshots`)}
    ${marketKpi("Valid Silver records", quality.valid_records, `${number(quality.invalid_records)} invalid`)}
    ${marketKpi("Lifecycle tracked", lifecycle.tracked_postings, `${number(eventTotal)} latest events`)}
    ${marketKpi("Operational alerts", alertCount, alertCount ? "Review failed stages" : "No recorded failures")}
    <section class="intel-block wide">${intelHeading("Stage status", "Latest persisted evidence")}<div class="ops-stage-list">${stages.map(([name, stageStatus, detail, timestamp, stageFreshness]) => stageRow(name, stageStatus, detail, timestamp, stageFreshness)).join("")}</div></section>
    <section class="intel-block">${intelHeading("Collection run", collection.profile_name || "Candidate profile")}${opsMeta([["Run ID", collection.run_id], ["Queries", (collection.query_names || []).join(", ")], ["Per-target limit", collection.per_target_limit], ["Failure rate", formatPercent(collection.extraction_failure_rate)]])}</section>
    <section class="intel-block">${intelHeading("Gold build", gold.profile_name || "Resume profile")}${opsMeta([["Run ID", gold.run_id], ["Input fingerprint", gold.input_fingerprint], ["Silver records", gold.silver_records_found], ["Inactive excluded", gold.excluded_inactive]])}</section>
  </div>`;
}

function operationStageStatus(passed, freshness) { if (!passed) return "failed"; return freshness?.status === "fresh" ? "passed" : "stale"; }

function renderCollectionOperations() {
  const collection = state.operations.collection;
  const rows = (collection.targets || []).map((target) => `<tr><td><strong>${escapeHtml(target.target || "Unknown")}</strong><span class="job-meta">${escapeHtml(formatLabel(target.source))}</span></td><td>${opsBadge(target.succeeded ? "Passed" : "Failed", target.succeeded ? "" : "failed")}</td><td class="number-cell">${number(target.references_discovered)}</td><td class="number-cell">${number(target.references_selected)}</td><td class="number-cell">${number(target.postings_extracted)}</td><td class="number-cell">${number(target.raw_records_saved)}</td><td class="number-cell">${number(target.posting_failures)}</td><td>${escapeHtml(target.error || "None")}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Run details", formatTimestamp(collection.finished_at))}${opsMeta([["Run ID", collection.run_id], ["Profile", collection.profile_name], ["Duration", formatDuration(collection.started_at, collection.finished_at)], ["Target limit", collection.per_target_limit], ["Search attempts", collection.search_attempts], ["Search failures", collection.search_failures]])}</section><section class="intel-block">${intelHeading("Query profile", `${number((collection.query_names || []).length)} configured searches`)}<div class="chip-list">${(collection.query_names || []).map((query) => `<span class="skill-chip">${escapeHtml(formatLabel(query))}</span>`).join("")}</div></section><section class="intel-block wide">${intelHeading("Extractor targets", `${number(collection.targets_succeeded)} succeeded / ${number(collection.targets_failed)} failed`)}${marketTable(["Target", "Status", "Discovered", "Selected", "Extracted", "Raw saved", "Failures", "Error"], rows)}</section></div>`;
}

function renderQualityOperations() {
  const quality = state.operations.quality;
  const bayes = quality.bayesian;
  const bayesChecks = new Map((bayes?.checks || []).map((check) => [check.name, check]));
  const rows = (quality.metrics || []).map((metric) => `<tr><td><strong>${escapeHtml(formatLabel(metric.name))}</strong><span class="job-meta">${metric.blocking ? "Blocking" : "Advisory"}</span></td><td>${opsBadge(metric.passed ? "Passed" : "Failed", metric.passed ? "" : "failed")}</td><td class="number-cell">${escapeHtml(formatQualityValue(metric.value, metric))}</td><td class="number-cell">${escapeHtml(formatLabel(metric.comparison))} ${escapeHtml(formatQualityValue(metric.threshold, metric))}</td><td class="number-cell">${metric.available ? "Yes" : "No"}</td><td>${bayesianCell(bayesChecks.get(metric.name), metric)}</td></tr>`).join("");
  const failed = quality.failed_metrics || [];
  return `<div class="intel-grid"><section class="ops-status-line ${quality.passed ? "" : "failed"} wide"><div><strong>${quality.passed ? "Quality gateway passed" : `${number(failed.length)} blocking checks failed`}</strong><span>${number(quality.valid_records)} valid records, ${number(quality.invalid_records)} invalid records, generated ${escapeHtml(formatTimestamp(quality.generated_at))}.</span></div></section><section class="intel-block wide">${intelHeading("Quality metrics", `${number((quality.metrics || []).length)} evaluated checks`)}${marketTable(["Metric", "Status", "Current", "Threshold", "Available", "Bayesian (B1)"], rows)}</section>${bayesianEmployerBlock(bayes)}</div>`;
}

function renderLifecycleOperations() {
  const lifecycle = state.operations.lifecycle;
  const counts = lifecycle.current_counts || {};
  const eventCounts = lifecycle.event_counts || {};
  const rows = (lifecycle.events || []).slice(0, 75).map((event) => `<tr><td>${opsBadge(formatLabel(event.event_type), event.event_type === "missing" || event.event_type === "removed" ? "warning" : "")}</td><td><strong>${escapeHtml(event.title || "Unknown")}</strong><span class="job-meta">${escapeHtml(event.employer || "Unknown employer")}</span></td><td>${escapeHtml(event.target_name || "Unknown")}</td><td>${escapeHtml(formatLabel(event.previous_status || "new"))} -> ${escapeHtml(formatLabel(event.current_status))}</td><td>${escapeHtml(formatTimestamp(event.detected_at))}</td></tr>`).join("");
  return `<div class="intel-grid three">${marketKpi("Active", counts.active, "currently observed")}${marketKpi("Missing", counts.missing, "awaiting removal threshold")}${marketKpi("Removed", counts.removed, "confirmed unavailable")}<section class="intel-block">${intelHeading("Latest transitions", "Event counts")}${rankingList(Object.entries(eventCounts).map(([event_type, count]) => ({ event_type, count })), "event_type", "count", 10)}</section><section class="intel-block">${intelHeading("Employer activity", "Latest lifecycle run")}${rankingList(lifecycle.employer_activity || [], "employer", "events", 12)}</section><section class="intel-block wide">${intelHeading("Recent lifecycle events", `${number((lifecycle.events || []).length)} transitions`)}${rows ? marketTable(["Event", "Posting", "Target", "Transition", "Detected"], rows) : `<div class="intel-empty">No lifecycle transitions in the latest run</div>`}</section></div>`;
}

function renderGoldOperations() {
  const gold = state.operations.gold;
  const rows = Object.entries(gold.datasets || {}).map(([name, dataset]) => `<tr><td><strong>${escapeHtml(formatLabel(name))}</strong></td><td>${escapeHtml(dataset.key || "Unknown")}</td><td>${escapeHtml(dataset.path || "Unknown")}</td><td>${opsBadge("Available")}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Build provenance", formatTimestamp(gold.generated_at))}${opsMeta([["Run ID", gold.run_id], ["Profile", gold.profile_name], ["Input fingerprint", gold.input_fingerprint], ["Records analyzed", gold.records_analyzed]])}</section><section class="intel-block">${intelHeading("Build exclusions", "Canonical validation")}${opsMeta([["Silver records found", gold.silver_records_found], ["Inactive excluded", gold.excluded_inactive], ["Invalid Silver", gold.invalid_silver_records], ["Build failures", (gold.failures || []).length]])}</section><section class="intel-block wide">${intelHeading("Published Gold datasets", `${number(Object.keys(gold.datasets || {}).length)} outputs`)}${marketTable(["Dataset", "Storage key", "Relative path", "Status"], rows)}</section></div>`;
}

function stageRow(name, status, detail, timestamp, freshness) { const age = freshness?.age_hours == null ? "age unavailable" : `${number(freshness.age_hours)}h old`; return `<div class="ops-stage"><span class="stage-indicator ${status}"></span><strong>${escapeHtml(name)}${status === "stale" ? `<small>${escapeHtml(age)}</small>` : ""}</strong><span>${escapeHtml(detail)}</span><time>${escapeHtml(formatTimestamp(timestamp))}</time></div>`; }
// Phase 8 B1: probability that each rate truly meets its threshold.
function bayesianCell(check, metric) {
  if (!check) return `<span class="job-meta">${metric?.name === "minimum_records" ? "Not modeled: a count, not a rate" : "–"}</span>`;
  if (check.verdict === "estimate") return `${opsBadge("Estimate", "neutral")}<span class="job-meta"> ${formatPercent(check.posterior.mean)} · ${intervalText(check.posterior)}</span>`;
  const style = check.verdict === "fail" ? "failed" : check.verdict === "uncertain" ? "warning" : "";
  const detail = check.probability_meets === null || check.probability_meets === undefined ? "all-or-nothing rule" : `P(meets) ${formatPercent(check.probability_meets)}`;
  return `${opsBadge(formatLabel(check.verdict), style)}<span class="job-meta"> ${escapeHtml(detail)} · ${intervalText(check.posterior)}</span>`;
}

function bayesianEmployerBlock(bayes) {
  if (!bayes) return "";
  const rows = (bayes.checks || []).filter((check) => check.comparison !== "informational").flatMap((check) => (check.by_employer?.rows || []).filter((row) => row.verdict !== "pass").slice(0, 5).map((row) => `<tr><td><strong>${escapeHtml(formatLabel(check.name))}</strong></td><td>${escapeHtml(row.employer)}</td><td class="number-cell">${number(row.successes)} / ${number(row.trials)}</td><td class="number-cell">${formatPercent(row.posterior.mean)} <small class="job-meta">${intervalText(row.posterior)}</small></td><td>${opsBadge(formatLabel(row.verdict), row.verdict === "fail" ? "failed" : "warning")}</td></tr>`)).join("");
  const mode = bayes.mode === "enforce" ? "Bayesian verdicts decide coverage checks" : "Advisory: rule thresholds still decide pass or fail";
  return `<section class="intel-block wide">${intelHeading("Employers below threshold (Bayesian)", mode)}${rows ? marketTable(["Check", "Employer", "Postings", "Estimated rate", "Verdict"], rows) : `<div class="intel-empty">No employer is likely or possibly below a threshold</div>`}</section>`;
}

function opsBadge(label, style = "") { return `<span class="ops-badge ${style}">${escapeHtml(label)}</span>`; }
function opsMeta(items) { return `<div class="ops-meta">${items.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "Not available")}</strong></div>`).join("")}</div>`; }
function formatDuration(start, end) { const seconds = Math.max(0, (new Date(end) - new Date(start)) / 1000); if (!Number.isFinite(seconds)) return "Unknown"; if (seconds < 60) return `${Math.round(seconds)} sec`; const minutes = Math.floor(seconds / 60); const remainder = Math.round(seconds % 60); return `${minutes}m ${remainder}s`; }
function formatQualityValue(value, metric) { return metric.denominator != null || Number(metric.threshold) <= 1 ? formatPercent(value) : number(value); }
function resetFilters() {
  elements.searchInput.value = "";
  [elements.employerFilter, elements.familyFilter, elements.seniorityFilter, elements.workplaceFilter, elements.lifecycleFilter, elements.applicationFilter].forEach((select) => { select.value = ""; });
  elements.salaryFilter.checked = false; elements.sortSelect.value = "rank"; state.band = "all";
  elements.bandFilters.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.value === "all"));
  applyFilters(true);
}
function changePage(delta) { state.page += delta; renderTable(); renderPagination(); document.querySelector(".results-toolbar").scrollIntoView({ behavior: "smooth", block: "start" }); }

function openDrawer(jobId) {
  const item = state.opportunities.find((opportunity) => opportunity.job_id === jobId);
  if (!item) return;
  state.selectedJobId = jobId;
  elements.drawerRank.textContent = `Rank ${item.rank} / ${formatLabel(item.match_band)} match`;
  elements.drawerContent.innerHTML = drawerMarkup(item);
  elements.drawerBackdrop.hidden = false; elements.detailDrawer.classList.add("open"); elements.detailDrawer.setAttribute("aria-hidden", "false"); document.body.style.overflow = "hidden"; elements.closeDrawer.focus();
}
function closeDrawer() {
  if (!elements.detailDrawer.classList.contains("open")) return;
  elements.detailDrawer.classList.remove("open"); elements.detailDrawer.setAttribute("aria-hidden", "true"); elements.drawerBackdrop.hidden = true; document.body.style.overflow = "";
  const selected = document.querySelector(`tr[data-job-id="${CSS.escape(state.selectedJobId || "")}"]`); if (selected) selected.focus(); state.selectedJobId = null;
}
function handleTrackingFormChange(event) {
  if (event.target.id === "trackingStatus" && event.target.value === "applied") { const field = document.getElementById("applicationDate"); if (!field.value) field.value = new Date().toISOString().slice(0, 10); }
}
async function saveTracking(event) {
  if (event.target.id !== "trackingForm") return;
  event.preventDefault();
  const jobId = state.selectedJobId; if (!jobId) return;
  const form = event.target; const button = form.querySelector("button[type='submit']"); const feedback = form.querySelector(".save-feedback"); const data = new FormData(form);
  const payload = { shortlisted: data.get("shortlisted") === "on", status: data.get("status"), priority: data.get("priority"), application_date: data.get("application_date") || null, follow_up_date: data.get("follow_up_date") || null, resume_version_id: data.get("resume_version_id") || null, notes: data.get("notes") || "" };
  button.disabled = true; feedback.className = "save-feedback"; feedback.textContent = "Saving";
  try {
    const response = await fetch(`/api/application-tracking/${encodeURIComponent(jobId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json(); if (!response.ok) throw new Error(result.error || "Tracking update failed");
    state.tracking[jobId] = result; feedback.classList.add("success"); feedback.textContent = `Saved ${formatTimestamp(result.updated_at)}`; applyFilters(false);
  } catch (error) { feedback.classList.add("error"); feedback.textContent = error.message; } finally { button.disabled = false; }
}

function drawerMarkup(item) {
  const tracking = trackingFor(item.job_id);
  const components = ["skills", "title", "experience", "location"].map((name) => { const component = item.score_components[name]; return `<div class="component"><div class="component-top"><strong>${formatLabel(name)}</strong><span>${formatScore(component.points)} / ${formatScore(component.maximum_points)}</span></div><div class="component-track"><div class="component-fill" style="--value:${Math.max(0, Math.min(100, component.signal * 100))}%"></div></div></div>`; }).join("");
  const skillChips = (item.skill_match_details || []).length ? item.skill_match_details.map((skill) => `<span class="skill-chip">${escapeHtml(skill.skill)}<small>${escapeHtml(skill.evidence)} / ${escapeHtml(skill.importance)}</small></span>`).join("") : `<span class="job-meta">No profile skills detected</span>`;
  const titleExplanation = item.title_match_type === "exact_title" ? `Exact normalized match: ${item.normalized_title}` : item.title_match_type === "family" ? `Target family match: ${formatLabel(item.family)}` : item.title_match_type === "phrase" ? `Contiguous title phrase: ${(item.matched_title_signals || []).join(", ")}` : item.title_match_type === "fuzzy" ? "Partial title-token overlap" : "No target title match";
  const tierNote = (item.avoided_title_keywords || []).length ? ` · avoided role type (${item.avoided_title_keywords.join(", ")})` : item.title_tier ? ` · ${item.title_tier} tier` : "";
  const employmentNote = item.employment_preferred === false ? ` · ${formatLabel(item.employment_type)} is outside your preferred employment types` : "";
  const locationExplanation = item.home_base ? `About ${formatScore(item.home_base_miles)} mi from ${item.home_base}` : item.home_region_match ? "Home-region match" : item.workplace_type === "remote" ? "Remote-accessible role" : `${formatLabel(item.workplace_type)} outside the home region`;
  return `<h2 class="detail-title">${escapeHtml(item.title)}</h2><p class="detail-employer">${escapeHtml(item.employer)} / ${escapeHtml(item.location || "Unknown location")}</p>
    <div class="detail-actions">${item.url ? `<a class="primary-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open posting</a>` : ""}</div>
    <section class="tracking-section"><div class="section-heading"><h3>Application tracking</h3><span class="tracking-updated">${tracking.updated_at ? `Updated ${escapeHtml(formatTimestamp(tracking.updated_at))}` : "Not tracked yet"}</span></div>
      <form id="trackingForm" class="tracking-form"><label class="shortlist-control"><input type="checkbox" name="shortlisted" ${tracking.shortlisted ? "checked" : ""}><span>Shortlist this opportunity</span></label>
        <div class="tracking-grid">${selectField("Status", "status", "trackingStatus", tracking.status, STATUS_OPTIONS)}${selectField("Priority", "priority", "trackingPriority", tracking.priority, ["none", "low", "medium", "high"])}${inputField("Application date", "application_date", "applicationDate", "date", tracking.application_date)}${inputField("Follow-up date", "follow_up_date", "followUpDate", "date", tracking.follow_up_date)}</div>
        ${resumeVersionOptions(tracking.resume_version_id)}
        <label class="tracking-field"><span>Notes</span><textarea name="notes" id="trackingNotes" maxlength="5000" rows="4" placeholder="Contacts, preparation, next action">${escapeHtml(tracking.notes || "")}</textarea></label>
        <div class="tracking-actions"><button class="save-button" type="submit">Save tracking</button><span class="save-feedback" role="status" aria-live="polite"></span></div></form></section>
    <div class="detail-facts">${fact("Workplace", formatLabel(item.workplace_type))}${fact("Seniority", formatLabel(item.seniority || "unknown"))}${fact("Compensation", formatSalary(item))}${fact("Lifecycle", formatLabel(item.lifecycle_status))}${fact("Last seen", formatDate(item.last_seen_at))}${fact("Source", formatLabel(item.source))}</div>
    <section class="detail-section"><div class="section-heading"><h3>Resume relevance</h3><span class="match-band">${escapeHtml(item.match_band)} match</span></div><div class="score-overview"><div class="score-number">${formatScore(item.resume_relevance_score)}</div><div class="score-copy"><strong>${item.rank === 1 ? "Highest-ranked opportunity" : `Opportunity rank ${item.rank}`}</strong><span>${item.score_cap_reason ? `Score capped: ${formatLabel(item.score_cap_reason)}` : "No eligibility cap applied"}</span></div></div><div class="component-list">${components}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Matched skills</h3><span class="match-band">${(item.matched_skills || []).length} matched</span></div><div class="chip-list">${skillChips}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Match explanation</h3></div><ul class="explanation-list"><li><strong>Title</strong><span>${escapeHtml(titleExplanation + tierNote + employmentNote)}</span></li><li><strong>Experience</strong><span>${escapeHtml(formatLabel(item.seniority || "unknown"))} seniority / ${formatScore(item.score_components.experience.signal * 100)}% signal</span></li><li><strong>Location</strong><span>${escapeHtml(locationExplanation)}</span></li><li><strong>Freshness</strong><span>${item.days_since_last_seen === 0 ? "Seen in the latest observation" : `Last seen ${item.days_since_last_seen} days ago`}</span></li></ul></section>`;
}

function selectField(label, name, id, value, options) { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><select name="${name}" id="${id}">${options.map((option) => `<option value="${option}" ${option === value ? "selected" : ""}>${formatLabel(option)}</option>`).join("")}</select></label>`; }
function inputField(label, name, id, type, value, placeholder = "") { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><input name="${name}" id="${id}" type="${type}" value="${escapeHtml(value || "")}" maxlength="200" placeholder="${escapeHtml(placeholder)}"></label>`; }
function fact(label, value) { return `<div class="fact"><span class="fact-label">${escapeHtml(label)}</span><span class="fact-value">${escapeHtml(value || "Not available")}</span></div>`; }
function formatLabel(value) { return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatSource(value) { return formatLabel(value); }
function formatScore(value) { return Number(value || 0).toFixed(Number(value || 0) % 1 === 0 ? 0 : 1); }
function number(value) { return Number(value || 0).toLocaleString("en-US", { maximumFractionDigits: 1 }); }
function formatPercent(value) { return `${(Number(value || 0) * 100).toFixed(1)}%`; }
function formatMoney(value, currency = "USD") { return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(Number(value || 0)); }
function formatSalary(item) { if (!item.has_salary) return "Salary not listed"; const formatter = new Intl.NumberFormat("en-US", { style: "currency", currency: item.salary_currency || "USD", maximumFractionDigits: 0 }); if (item.salary_minimum != null && item.salary_maximum != null) return `${formatter.format(item.salary_minimum)} - ${formatter.format(item.salary_maximum)}`; return formatter.format(item.salary_minimum ?? item.salary_maximum); }
function formatDate(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(new Date(value)) : "Unknown"; }
function formatTimestamp(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)) : "Unknown"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]); }
