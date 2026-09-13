"use strict";

const PAGE_SIZE = 25;
const ACTIVE_STATUSES = new Set(["applied", "interviewing", "offer"]);
const STATUS_OPTIONS = ["new", "reviewing", "saved", "applied", "interviewing", "offer", "rejected", "archived"];
const SIGNAL_KEYS = ["skills", "title", "experience", "location"];
const POSITIVE_FEEDBACK_STATUSES = new Set(["reviewing", "saved", "applied", "interviewing", "offer"]);
const state = { mart: null, market: null, operations: null, dataManagement: null, reporting: null, resumeReviews: null, reviewDraft: null, reviewExpanded: { skills: true, employment: false, education: false, projects: false }, selectedExtractionId: null, candidateProfile: null, resumeVersions: [], applicationComparisons: {}, opportunities: [], tracking: {}, filtered: [], page: 1, band: "all", view: "opportunities", marketView: "overview", operationsView: "overview", selectedJobId: null, simulatorWeights: null };
const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  ["freshnessLabel", "showingMetric", "strongMetric", "shortlistMetric", "applicationMetric", "showingLabel", "strongLabel", "shortlistLabel", "applicationLabel", "searchInput", "employerFilter", "familyFilter", "seniorityFilter", "workplaceFilter", "lifecycleFilter", "applicationFilter", "salaryFilter", "sortSelect", "opportunitiesBody", "resultCount", "activeFilters", "loadingState", "emptyState", "errorState", "errorMessage", "retryButton", "resetFilters", "emptyReset", "previousPage", "nextPage", "pageLabel", "bandFilters", "detailDrawer", "drawerBackdrop", "drawerContent", "drawerRank", "closeDrawer", "filtersPanel", "filterToggle", "viewSwitch", "viewTitle", "resultsPanel", "intelligencePanel", "intelligenceTabs", "intelligenceContent", "intelligenceFreshness", "qualityBadge", "operationsPanel", "operationsTabs", "operationsContent", "operationsFreshness", "operationsBadge", "refreshOperations", "pipelineAction", "runPipeline", "operationRunFeedback", "simulatorPanel", "simulatorContent", "simulatorStatus", "resumeReviewPanel", "resumeReviewContent", "resumeReviewSummary", "resumeReviewSelect", "reportingPanel", "reportingContent", "reportingFreshness", "reportingStatus", "generateReport", "pageName"].forEach((id) => { elements[id] = document.getElementById(id); });
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
  elements.operationsContent.addEventListener("click", handleDataManagementClick);
  elements.refreshOperations.addEventListener("click", () => refreshPipelineOperations());
  elements.runPipeline.addEventListener("click", runPipelineOperation);
  elements.simulatorContent.addEventListener("change", handleSimulatorChange);
  elements.simulatorContent.addEventListener("click", handleSimulatorClick);
  elements.generateReport.addEventListener("click", generateReport);
  elements.resumeReviewSelect.addEventListener("change", selectResumeReview);
  elements.resumeReviewContent.addEventListener("click", handleResumeReviewClick);
  elements.resumeReviewContent.addEventListener("change", handleResumeReviewChange);
  elements.resumeReviewContent.addEventListener("input", handleResumeReviewInput);
  elements.resumeReviewContent.addEventListener("submit", approveResumeReview);
  elements.opportunitiesBody.addEventListener("click", (event) => { const row = event.target.closest("tr[data-job-id]"); if (row) openDrawer(row.dataset.jobId); });
  elements.opportunitiesBody.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { const row = event.target.closest("tr[data-job-id]"); if (row) { event.preventDefault(); openDrawer(row.dataset.jobId); } }
  });
  elements.drawerContent.addEventListener("submit", saveTracking);
  elements.drawerContent.addEventListener("change", handleTrackingFormChange);
  elements.drawerContent.addEventListener("click", handleComparisonClick);
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
  try {
    const [martResponse, trackingResponse, profileResponse, marketResponse, operationsResponse, dataResponse, reportingResponse, reviewResponse] = await Promise.all([fetch("/api/opportunities", { cache: "no-store" }), fetch("/api/application-tracking", { cache: "no-store" }), fetch("/api/candidate-profile", { cache: "no-store" }), fetch("/api/market-intelligence", { cache: "no-store" }), fetch("/api/pipeline-operations", { cache: "no-store" }), fetch("/api/data-management", { cache: "no-store" }), fetch("/api/reporting", { cache: "no-store" }), fetch("/api/resume-reviews", { cache: "no-store" })]);
    if (!martResponse.ok || !trackingResponse.ok || !profileResponse.ok || !marketResponse.ok || !operationsResponse.ok || !dataResponse.ok || !reportingResponse.ok || !reviewResponse.ok) throw new Error("Dashboard data could not be loaded");
    const [mart, tracking, candidateProfile, market, operations, dataManagement, reporting, resumeReviews] = await Promise.all([martResponse.json(), trackingResponse.json(), profileResponse.json(), marketResponse.json(), operationsResponse.json(), dataResponse.json(), reportingResponse.json(), reviewResponse.json()]);
    if (!Array.isArray(mart.opportunities)) throw new Error("The Gold mart has no opportunities array");
    state.mart = mart;
    state.opportunities = mart.opportunities;
    state.tracking = tracking.records || {};
    state.candidateProfile = candidateProfile.profile;
    state.resumeVersions = candidateProfile.resume_versions || [];
    state.market = market;
    state.operations = operations;
    state.dataManagement = dataManagement;
    state.reporting = reporting;
    state.resumeReviews = resumeReviews;
    const reviewItems = resumeReviews.items || [];
    const firstPending = reviewItems.find((item) => item.extraction.can_review) || reviewItems[0];
    state.selectedExtractionId = firstPending?.extraction.extraction_id || null;
    state.reviewDraft = firstPending ? structuredClone(firstPending.extraction) : null;
    state.simulatorWeights = currentScoringWeights();
    populateFilters();
    elements.freshnessLabel.textContent = `Gold refreshed ${formatTimestamp(mart.generated_at)}`;
    elements.intelligenceFreshness.textContent = `Snapshot refreshed ${formatTimestamp(market.generated_at)}`;
    elements.operationsFreshness.textContent = `Latest evidence ${formatTimestamp(operations.generated_at)}`;
    elements.reportingFreshness.textContent = reporting.generated_at ? `Latest report ${formatTimestamp(reporting.generated_at)}` : "No report generated yet";
    applyFilters(true);
    schedulePipelinePoll();
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
  const reporting = state.view === "reporting";
  elements.filtersPanel.hidden = intelligence || operations || simulator || resumeReview || reporting;
  elements.resultsPanel.hidden = intelligence || operations || simulator || resumeReview || reporting;
  elements.intelligencePanel.hidden = !intelligence;
  elements.operationsPanel.hidden = !operations;
  elements.simulatorPanel.hidden = !simulator;
  elements.resumeReviewPanel.hidden = !resumeReview;
  elements.reportingPanel.hidden = !reporting;
  document.querySelector(".content-grid").classList.toggle("market-mode", intelligence);
  document.querySelector(".content-grid").classList.toggle("operations-mode", operations);
  document.querySelector(".content-grid").classList.toggle("simulator-mode", simulator);
  document.querySelector(".content-grid").classList.toggle("resume-review-mode", resumeReview);
  document.querySelector(".content-grid").classList.toggle("reporting-mode", reporting);
  elements.pageName.textContent = intelligence ? "Market intelligence" : operations ? "Pipeline operations" : simulator ? "Weight simulator" : resumeReview ? "Resume review" : reporting ? "Alerts & reports" : "Opportunities";
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
  if (state.view === "reporting") { renderReporting(); return; }
  renderActiveFilters(); renderTable(); renderPagination();
}
function renderMetrics() {
  if (state.view === "resume-review" && state.resumeReviews) {
    const items = state.resumeReviews.items || [];
    const selected = selectedReviewItem();
    elements.showingLabel.textContent = "Extractions";
    elements.strongLabel.textContent = "Pending review";
    elements.shortlistLabel.textContent = "Approved";
    elements.applicationLabel.textContent = "Text quality";
    elements.showingMetric.textContent = number(items.length);
    elements.strongMetric.textContent = number(state.resumeReviews.pending_count);
    elements.shortlistMetric.textContent = number(state.resumeReviews.approved_count);
    elements.applicationMetric.textContent = selected ? formatPercent(selected.extraction.quality_score) : "--";
    return;
  }
  if (state.view === "reporting" && state.reporting) {
    const latest = state.reporting.latest_report;
    const summary = latest?.summary || {};
    elements.showingLabel.textContent = "Active alerts";
    elements.strongLabel.textContent = "New alerts";
    elements.shortlistLabel.textContent = "Strong matches";
    elements.applicationLabel.textContent = "Report status";
    elements.showingMetric.textContent = number(state.reporting.active_alerts);
    elements.strongMetric.textContent = number(summary.new_alerts || 0);
    elements.shortlistMetric.textContent = number(summary.strong_matches || 0);
    elements.applicationMetric.textContent = latest ? formatLabel(latest.status) : "Pending";
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
    ${marketKpi("Employers", employers.distinct_employers, `${lifecycle.current_counts.active || 0} lifecycle-active postings`)}
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
  const rows = (summary.skills || []).slice(0, 40).map((item) => `<tr><td><strong>${escapeHtml(item.skill)}</strong></td><td>${escapeHtml((item.categories || []).map(formatLabel).join(", ") || "Uncategorized")}</td><td class="number-cell">${number(item.posting_count)}</td><td class="number-cell">${formatPercent(item.posting_share)}</td><td class="number-cell">${number(item.explicit_count)}</td><td class="number-cell">${number(item.inferred_count)}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Skill categories", "Share of analyzed postings")}${rankingList(summary.categories, "category", "posting_count", 10)}</section><section class="intel-block">${intelHeading("Coverage", "Canonical extraction signal")}<div class="coverage-pair"><div><strong>${number(summary.coverage.postings_with_skills)}</strong><span>Postings with skills / ${formatPercent(summary.coverage.posting_share)}</span></div><div><strong>${number(summary.coverage.distinct_skills)}</strong><span>Distinct normalized skills</span></div></div></section><section class="intel-block wide">${intelHeading("Top skills", "Explicit and inferred evidence")}${marketTable(["Skill", "Category", "Postings", "Share", "Explicit", "Inferred"], rows)}</section></div>`;
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
function intelHeading(title, detail) { return `<div class="intel-heading"><h2>${escapeHtml(title)}</h2><span>${escapeHtml(detail)}</span></div>`; }
function rankingList(items, labelKey, valueKey, limit = 10, formatter = number) {
  const selected = (items || []).slice(0, limit); const maximum = Math.max(...selected.map((item) => Number(item[valueKey] || 0)), 1);
  return `<div class="ranking-list">${selected.map((item) => `<div class="ranking-row"><span class="ranking-label" title="${escapeHtml(formatLabel(item[labelKey]))}">${escapeHtml(formatLabel(item[labelKey]))}</span><span class="ranking-track"><span class="ranking-fill" style="--bar-value:${Math.max(2, Number(item[valueKey] || 0) / maximum * 100)}%"></span></span><span class="ranking-value">${escapeHtml(formatter(item[valueKey]))}</span></div>`).join("")}</div>`;
}
function marketTable(headers, rows) { return `<div class="intel-table-shell"><table class="intel-table"><thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`; }
function qualityList(metrics) { return `<div class="quality-list">${(metrics || []).map((item) => `<div class="quality-row"><span>${escapeHtml(formatLabel(item.name))}</span><span class="${item.passed ? "quality-pass" : "quality-fail"}">${item.passed ? "Passed" : "Failed"} / ${escapeHtml(formatMetricValue(item.value))}</span></div>`).join("")}</div>`; }
function formatMetricValue(value) { return Number(value) <= 1 ? formatPercent(value) : number(value); }

async function generateReport() {
  const button = elements.generateReport;
  button.disabled = true;
  button.textContent = "Generating";
  try {
    const response = await fetch("/api/reporting/generate", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Report generation failed");
    const reportingResponse = await fetch("/api/reporting", { cache: "no-store" });
    if (!reportingResponse.ok) throw new Error("Generated report could not be reloaded");
    state.reporting = await reportingResponse.json();
    elements.reportingFreshness.textContent = `Latest report ${formatTimestamp(state.reporting.generated_at)}`;
    renderMetrics();
    renderReporting();
  } catch (error) {
    elements.reportingFreshness.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Generate report";
  }
}

function alertRows(alerts) {
  if (!alerts.length) return `<div class="report-empty">No alerts in this group</div>`;
  return `<div class="alert-list">${alerts.map((alert) => `<article class="alert-row"><span class="alert-severity ${escapeHtml(alert.severity)}">${escapeHtml(formatLabel(alert.severity))}</span><div><strong>${escapeHtml(alert.title)}</strong><p>${escapeHtml(alert.detail)}</p><span>${escapeHtml(formatLabel(alert.rule))} / ${alert.job_ids?.length || 0} linked opportunities</span></div><time>${escapeHtml(formatTimestamp(alert.last_seen_at))}</time></article>`).join("")}</div>`;
}

function renderReporting() {
  if (!state.reporting) { elements.reportingContent.innerHTML = `<div class="intel-empty">Reporting status is unavailable</div>`; return; }
  const schedule = state.reporting.configured_schedule || {};
  elements.reportingStatus.textContent = schedule.enabled ? `${formatLabel(schedule.cadence)} schedule configured` : "Schedule not registered";
  elements.reportingStatus.classList.toggle("pending", !schedule.enabled);
  const latest = state.reporting.latest_report;
  if (!latest) {
    elements.reportingContent.innerHTML = `<section class="report-first-run"><strong>No reporting baseline exists</strong><span>Generate the first report from current Gold evidence to initialize alert history.</span></section>`;
    return;
  }
  const active = (state.reporting.alerts || []).filter((alert) => alert.status === "active");
  const resolved = (state.reporting.alerts || []).filter((alert) => alert.status === "resolved");
  const summary = latest.summary || {};
  const opportunityRows = (latest.top_opportunities || []).map((item) => `<tr><td class="number-cell"><strong>${number(item.rank)}</strong></td><td><span class="job-title">${escapeHtml(item.title)}</span><span class="job-meta">${escapeHtml(item.location || "Unknown")}</span></td><td>${escapeHtml(item.employer)}</td><td><span class="score-badge score-${escapeHtml(item.match_band)}">${formatScore(item.resume_relevance_score)}</span></td><td><span>${escapeHtml(formatSource(item.source))}</span>${item.source_url ? `<span class="job-meta"><a class="table-link" href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">Open</a></span>` : ""}</td></tr>`).join("");
  const scheduleDays = schedule.cadence === "weekly" ? (schedule.weekdays || []).join(", ") : "Every day";
  elements.reportingContent.innerHTML = `<div class="report-grid">
    <section class="report-block alert-inbox"><div class="report-heading"><div><h2>Active alerts</h2><span>Current conditions requiring attention</span></div><span class="report-count">${active.length}</span></div>${alertRows(active)}</section>
    <section class="report-block schedule-block"><div class="report-heading"><div><h2>Schedule</h2><span>External task registration</span></div><span class="schedule-indicator ${schedule.enabled ? "" : "pending"}">${schedule.enabled ? "Enabled" : "Disabled"}</span></div><dl class="report-facts"><div><dt>Cadence</dt><dd>${escapeHtml(formatLabel(schedule.cadence))}</dd></div><div><dt>Time</dt><dd>${escapeHtml(schedule.time || "--")}</dd></div><div><dt>Timezone</dt><dd>${escapeHtml(schedule.timezone || "--")}</dd></div><div><dt>Days</dt><dd>${escapeHtml(scheduleDays)}</dd></div><div><dt>Pipeline refresh</dt><dd>${schedule.run_pipeline ? "Enabled" : "Report only"}</dd></div><div><dt>Last generated</dt><dd>${escapeHtml(formatTimestamp(latest.generated_at))}</dd></div></dl></section>
    <section class="report-block report-summary"><div class="report-heading"><div><h2>Latest digest</h2><span>${escapeHtml(latest.report_id)}</span></div><span class="report-health ${latest.status === "healthy" ? "" : "attention"}">${escapeHtml(formatLabel(latest.status))}</span></div><div class="digest-metrics"><div><span>Active opportunities</span><strong>${number(summary.active_opportunities)}</strong></div><div><span>Strong matches</span><strong>${number(summary.strong_matches)}</strong></div><div><span>Moderate matches</span><strong>${number(summary.moderate_matches)}</strong></div><div><span>Shortlisted</span><strong>${number(summary.shortlisted)}</strong></div><div><span>Applications</span><strong>${number(summary.active_applications)}</strong></div><div><span>Resolved alerts</span><strong>${number(summary.resolved_alerts)}</strong></div></div></section>
    <section class="report-block wide"><div class="report-heading"><div><h2>Top opportunities</h2><span>Snapshot captured in the latest report</span></div></div>${marketTable(["Rank", "Opportunity", "Employer", "Score", "Source"], opportunityRows)}</section>
    <section class="report-block wide"><div class="report-heading"><div><h2>Resolved history</h2><span>Conditions cleared by later report runs</span></div><span class="report-count quiet">${resolved.length}</span></div>${alertRows(resolved.slice(0, 20))}</section>
  </div>`;
}

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
    const score = item.score_cap_reason ? Math.min(preCapScore, onsiteCap) : preCapScore;
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
    schedulePipelinePoll();
  } catch (error) {
    elements.operationsFreshness.textContent = error.message;
    elements.operationsContent.innerHTML = `<div class="ops-status-line failed"><div><strong>Refresh failed</strong><span>${escapeHtml(error.message)}</span></div></div>`;
  } finally {
    if (!silent) { button.disabled = false; button.classList.remove("refresh-spin"); }
  }
}

function schedulePipelinePoll() { clearTimeout(state.pipelinePollTimer); state.pipelinePollTimer = null; if (state.operations?.control?.active_run_id) state.pipelinePollTimer = setTimeout(() => refreshPipelineOperations(true), 2000); }

async function runPipelineOperation() {
  const action = elements.pipelineAction.value;
  if (action === "full_pipeline" && !window.confirm("Start live collection against all enabled targets and rebuild downstream layers?")) return;
  elements.runPipeline.disabled = true;
  elements.operationRunFeedback.className = "operations-feedback pending";
  elements.operationRunFeedback.textContent = "Starting pipeline operation...";
  try {
    const response = await fetch("/api/pipeline-runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Pipeline operation could not be started");
    state.operationsView = "runs";
    elements.operationsTabs.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.operationsView === "runs"));
    elements.operationRunFeedback.textContent = `${formatLabel(result.action)} queued`;
    await refreshPipelineOperations(true);
  } catch (error) {
    elements.operationRunFeedback.className = "operations-feedback error";
    elements.operationRunFeedback.textContent = error.message;
    elements.runPipeline.disabled = false;
  }
}

function renderPipelineOperations() {
  if (!state.operations) { elements.operationsContent.innerHTML = `<div class="intel-empty">Operational summaries are unavailable</div>`; return; }
  const operations = state.operations;
  elements.operationsBadge.textContent = operations.status === "healthy" ? "Pipeline healthy" : operations.status === "stale" ? "Evidence stale" : "Attention required";
  elements.operationsBadge.classList.toggle("failed", operations.status === "failed");
  elements.operationsBadge.classList.toggle("stale", operations.status === "stale");
  const control = operations.control || { can_start: false, runs: [] };
  elements.runPipeline.disabled = !control.can_start;
  elements.pipelineAction.disabled = !control.can_start;
  if (control.active_run_id) { elements.operationRunFeedback.className = "operations-feedback pending"; elements.operationRunFeedback.textContent = "Pipeline operation running"; }
  else if (elements.operationRunFeedback.textContent === "Pipeline operation running") { elements.operationRunFeedback.className = "operations-feedback success"; elements.operationRunFeedback.textContent = "Pipeline operation finished"; }
  const renderers = { overview: renderOperationsOverview, collection: renderCollectionOperations, quality: renderQualityOperations, lifecycle: renderLifecycleOperations, outputs: renderGoldOperations, data: renderDataManagement, runs: renderPipelineRuns };
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

function renderPipelineRuns() {
  const runs = state.operations.control?.runs || [];
  if (!runs.length) return `<div class="intel-empty">No dashboard-initiated pipeline runs yet</div>`;
  return `<div class="ops-run-list">${runs.map((run) => `<section class="ops-run"><div class="ops-run-heading"><div><strong>${escapeHtml(formatLabel(run.action))}</strong><span>${escapeHtml(run.run_id)}</span></div>${opsBadge(formatLabel(run.status), run.status === "failed" ? "failed" : run.status === "running" || run.status === "queued" ? "warning" : "")}</div><div class="ops-run-meta"><span>Requested ${escapeHtml(formatTimestamp(run.requested_at))}</span><span>${run.started_at && run.finished_at ? escapeHtml(formatDuration(run.started_at, run.finished_at)) : run.status === "running" ? "In progress" : "Waiting"}</span></div><div class="ops-run-stages">${(run.stages || []).map((stage) => `<div><span class="stage-indicator ${stage.status === "failed" ? "failed" : stage.status === "running" || stage.status === "queued" ? "stale" : ""}"></span><strong>${escapeHtml(stage.name)}</strong><span>${escapeHtml(formatLabel(stage.status))}</span><time>${escapeHtml(formatTimestamp(stage.finished_at || stage.started_at))}</time>${stage.output_tail ? `<details><summary>Output</summary><pre>${escapeHtml(stage.output_tail)}</pre></details>` : ""}</div>`).join("")}</div></section>`).join("")}</div>`;
}

function renderDataManagement() {
  const data = state.dataManagement;
  if (!data) return `<div class="intel-empty">Data inventory is unavailable</div>`;
  const lineage = data.lineage || {};
  const integrity = data.integrity || {};
  const unmanaged = data.unmanaged || {};
  const assetRows = (data.assets || []).map((asset) => `<tr><td><strong>${escapeHtml(asset.label)}</strong><span class="job-meta">${escapeHtml(asset.path)}</span></td><td>${opsBadge(formatLabel(asset.status), asset.status === "missing" ? "failed" : asset.status === "empty" ? "warning" : "")}</td><td>${escapeHtml(formatLabel(asset.classification))}</td><td class="number-cell">${number(asset.file_count)}</td><td class="number-cell">${formatBytes(asset.size_bytes)}</td><td>${escapeHtml(formatTimestamp(asset.latest_modified_at))}</td><td>${escapeHtml(Object.entries(asset.formats || {}).map(([format, count]) => `${format} ${count}`).join(", ") || "None")}</td></tr>`).join("");
  const snapshots = (data.snapshots || []).map((snapshot) => `<div class="data-snapshot-row"><div><strong>${escapeHtml(snapshot.filename)}</strong><span>${number(snapshot.file_count)} files / ${formatBytes(snapshot.archive_bytes)} / ${escapeHtml(formatTimestamp(snapshot.created_at))}</span></div><a class="secondary-button" href="/api/data-snapshots/${encodeURIComponent(snapshot.filename)}" download>Download</a></div>`).join("") || `<div class="intel-empty compact">No recovery snapshots created</div>`;
  const unmanagedNames = (unmanaged.directories || []).map((name) => `<span class="skill-chip">${escapeHtml(name)}</span>`).join("");
  return `<div class="intel-grid three">
    ${marketKpi("Managed storage", formatBytes(data.total_bytes), `${number(data.total_files)} files`)}
    ${marketKpi("Unmanaged storage", formatBytes(unmanaged.size_bytes), `${number(unmanaged.directory_count)} directories`)}
    ${marketKpi("Integrity", integrity.passed ? "Passed" : "Review", integrity.lineage_consistent ? "Lineage consistent" : "Lineage mismatch")}
    <section class="ops-status-line ${integrity.passed ? "" : "stale"} wide"><div><strong>${integrity.passed ? "Managed data boundaries are consistent" : "Data management review recommended"}</strong><span>${number(integrity.temporary_files)} temporary files, ${number((integrity.missing_gold_outputs || []).length)} missing Gold outputs, storage format ${escapeHtml(data.storage_format)}.</span></div></section>
    <section class="intel-block">${intelHeading("Pipeline lineage", lineage.consistent ? "Linked run identifiers agree" : "Run identifiers need review")}${opsMeta([["Collection run", lineage.collection_run_id], ["Quality source", lineage.quality_collection_run_id], ["Lifecycle source", lineage.lifecycle_collection_run_id], ["Gold run", lineage.gold_run_id], ["Gold datasets", lineage.gold_dataset_count], ["Missing outputs", (lineage.missing_gold_outputs || []).join(", ") || "None"]])}</section>
    <section class="intel-block data-snapshot-block">${intelHeading("Recovery snapshots", "Private candidate data is excluded")}<button class="save-button" type="button" id="createDataSnapshot" ${state.operations?.control?.active_run_id ? "disabled" : ""}>Create snapshot</button><span class="save-feedback" id="dataSnapshotFeedback" role="status" aria-live="polite"></span><div class="data-snapshot-list">${snapshots}</div></section>
    <section class="intel-block wide">${intelHeading("Managed assets", `${number((data.assets || []).length)} explicit storage boundaries`)}${marketTable(["Asset", "Status", "Classification", "Files", "Size", "Latest", "Formats"], assetRows)}</section>
    <section class="intel-block wide">${intelHeading("Unmanaged directories", `${number(unmanaged.file_count)} files / ${formatBytes(unmanaged.size_bytes)}`)}<div class="chip-list">${unmanagedNames || `<span class="job-meta">No unmanaged data directories</span>`}</div></section>
  </div>`;
}

async function handleDataManagementClick(event) {
  const button = event.target.closest("#createDataSnapshot");
  if (!button) return;
  const feedback = document.getElementById("dataSnapshotFeedback");
  button.disabled = true;
  feedback.className = "save-feedback";
  feedback.textContent = "Creating snapshot...";
  try {
    const response = await fetch("/api/data-snapshots", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Data snapshot could not be created");
    const refreshed = await fetch("/api/data-management", { cache: "no-store" });
    if (!refreshed.ok) throw new Error("Snapshot inventory could not be refreshed");
    state.dataManagement = await refreshed.json();
    renderPipelineOperations();
  } catch (error) { button.disabled = false; feedback.className = "save-feedback error"; feedback.textContent = error.message; }
}

function formatBytes(value) { const bytes = Number(value || 0); if (bytes < 1024) return `${bytes} B`; const units = ["KB", "MB", "GB", "TB"]; let amount = bytes / 1024; let index = 0; while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; } return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[index]}`; }

function renderCollectionOperations() {
  const collection = state.operations.collection;
  const rows = (collection.targets || []).map((target) => `<tr><td><strong>${escapeHtml(target.target || "Unknown")}</strong><span class="job-meta">${escapeHtml(formatLabel(target.source))}</span></td><td>${opsBadge(target.succeeded ? "Passed" : "Failed", target.succeeded ? "" : "failed")}</td><td class="number-cell">${number(target.references_discovered)}</td><td class="number-cell">${number(target.references_selected)}</td><td class="number-cell">${number(target.postings_extracted)}</td><td class="number-cell">${number(target.raw_records_saved)}</td><td class="number-cell">${number(target.posting_failures)}</td><td>${escapeHtml(target.error || "None")}</td></tr>`).join("");
  return `<div class="intel-grid"><section class="intel-block">${intelHeading("Run details", formatTimestamp(collection.finished_at))}${opsMeta([["Run ID", collection.run_id], ["Profile", collection.profile_name], ["Duration", formatDuration(collection.started_at, collection.finished_at)], ["Target limit", collection.per_target_limit], ["Search attempts", collection.search_attempts], ["Search failures", collection.search_failures]])}</section><section class="intel-block">${intelHeading("Query profile", `${number((collection.query_names || []).length)} configured searches`)}<div class="chip-list">${(collection.query_names || []).map((query) => `<span class="skill-chip">${escapeHtml(formatLabel(query))}</span>`).join("")}</div></section><section class="intel-block wide">${intelHeading("Extractor targets", `${number(collection.targets_succeeded)} succeeded / ${number(collection.targets_failed)} failed`)}${marketTable(["Target", "Status", "Discovered", "Selected", "Extracted", "Raw saved", "Failures", "Error"], rows)}</section></div>`;
}

function renderQualityOperations() {
  const quality = state.operations.quality;
  const rows = (quality.metrics || []).map((metric) => `<tr><td><strong>${escapeHtml(formatLabel(metric.name))}</strong><span class="job-meta">${metric.blocking ? "Blocking" : "Advisory"}</span></td><td>${opsBadge(metric.passed ? "Passed" : "Failed", metric.passed ? "" : "failed")}</td><td class="number-cell">${escapeHtml(formatQualityValue(metric.value, metric))}</td><td class="number-cell">${escapeHtml(formatLabel(metric.comparison))} ${escapeHtml(formatQualityValue(metric.threshold, metric))}</td><td class="number-cell">${metric.available ? "Yes" : "No"}</td></tr>`).join("");
  const failed = quality.failed_metrics || [];
  return `<div class="intel-grid"><section class="ops-status-line ${quality.passed ? "" : "failed"} wide"><div><strong>${quality.passed ? "Quality gateway passed" : `${number(failed.length)} blocking checks failed`}</strong><span>${number(quality.valid_records)} valid records, ${number(quality.invalid_records)} invalid records, generated ${escapeHtml(formatTimestamp(quality.generated_at))}.</span></div></section><section class="intel-block wide">${intelHeading("Quality metrics", `${number((quality.metrics || []).length)} evaluated checks`)}${marketTable(["Metric", "Status", "Current", "Threshold", "Available"], rows)}</section></div>`;
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

function selectedReviewItem() { return (state.resumeReviews?.items || []).find((item) => item.extraction.extraction_id === state.selectedExtractionId) || null; }
function selectResumeReview() { state.selectedExtractionId = elements.resumeReviewSelect.value || null; const selected = selectedReviewItem(); state.reviewDraft = selected ? structuredClone(selected.extraction) : null; state.reviewExpanded = { skills: true, employment: false, education: false, projects: false }; renderMetrics(); renderResumeReview(); }

function renderResumeReview() {
  const items = state.resumeReviews?.items || [];
  elements.resumeReviewSelect.innerHTML = items.map((item) => { const extraction = item.extraction; const suffix = extraction.review_status === "pending" ? "Pending" : formatLabel(extraction.review_status); return `<option value="${escapeHtml(extraction.extraction_id)}" ${extraction.extraction_id === state.selectedExtractionId ? "selected" : ""}>${escapeHtml(item.resume_version.version_label)} / ${suffix}</option>`; }).join("");
  elements.resumeReviewSelect.disabled = items.length === 0;
  elements.resumeReviewSummary.textContent = items.length ? `${number(state.resumeReviews.pending_count)} pending / ${number(state.resumeReviews.approved_count)} approved` : "No resume extractions are available for review";
  const selected = selectedReviewItem();
  if (!selected || !state.reviewDraft) { const missing = number(state.resumeReviews?.resumes_without_extraction || 0); elements.resumeReviewContent.innerHTML = `<div class="review-empty"><strong>No structured extraction to review</strong><span>${missing} resume version(s) do not yet have an extraction.</span></div>`; return; }
  const extraction = state.reviewDraft;
  const readonly = !selected.extraction.can_review;
  const reviewed = new Set(extraction.reviewed_sections || []);
  const disabled = readonly ? "disabled" : "";
  const completeness = structuredCompleteness(extraction);
  elements.resumeReviewContent.innerHTML = `
    <div class="review-context"><div><span>Resume</span><strong>${escapeHtml(selected.resume_version.version_label)}</strong></div><div><span>Extraction</span><strong>${escapeHtml(formatLabel(extraction.status))}</strong></div><div><span>Text quality</span><strong>${formatPercent(extraction.quality_score)}</strong></div><div><span>Structured</span><strong>${formatPercent(completeness.score)} <small>${number(completeness.fact_count)} facts</small></strong></div><div><span>Review progress</span><strong class="review-state ${escapeHtml(extraction.review_status)}">${number(reviewed.size)} / 4 sections</strong></div></div>
    ${(extraction.warnings || []).length ? `<div class="review-warning"><strong>Extraction warnings</strong><span>${escapeHtml(extraction.warnings.join(" / "))}</span></div>` : ""}
    <form id="resumeReviewForm" class="resume-review-form">
      ${renderReviewSection("Skills", "skills", "Confirm names, normalization, category, and confidence.", renderSkillEditors(extraction.skills || [], readonly || reviewed.has("skills")), reviewed.has("skills"), readonly, completeness.section_counts.skills)}
      ${renderReviewSection("Employment", "employment", "Verify employers, titles, dates, locations, and accomplishments.", renderEmploymentEditors(extraction.work_experience || [], readonly || reviewed.has("employment")), reviewed.has("employment"), readonly, completeness.section_counts.employment)}
      ${renderReviewSection("Education", "education", "Verify institutions, credentials, fields of study, and dates.", renderEducationEditors(extraction.education || [], readonly || reviewed.has("education")), reviewed.has("education"), readonly, completeness.section_counts.education)}
      ${renderReviewSection("Projects", "projects", "Verify project names, descriptions, technologies, and outcomes.", renderProjectEditors(extraction.projects || [], readonly || reviewed.has("projects")), reviewed.has("projects"), readonly, completeness.section_counts.projects)}
      <section class="review-submit-block"><label class="review-comment"><span>Review note</span><textarea name="review_comment" maxlength="2000" rows="3" ${disabled} placeholder="Optional note about corrections">${escapeHtml(extraction.review_comment || "")}</textarea></label><div class="review-submit-actions">${readonly ? "" : `<button class="secondary-button" id="saveResumeDraft" type="button">Save draft</button>`}<button class="save-button" id="approveResumeReview" type="submit" ${disabled}>${readonly ? "Review completed" : "Complete review"}</button><span class="save-feedback" id="resumeReviewFeedback" role="status" aria-live="polite">${readonly && extraction.reviewed_at ? `Approved ${escapeHtml(formatTimestamp(extraction.reviewed_at))}` : extraction.review_updated_at ? `Draft saved ${escapeHtml(formatTimestamp(extraction.review_updated_at))}` : ""}</span></div></section>
    </form>`;
  updateReviewApprovalButton();
}

function renderReviewSection(title, key, description, content, approved, readonly, count) { const expanded = state.reviewExpanded[key]; const locked = readonly || approved; return `<section class="review-section ${approved ? "approved" : ""}" data-review-section="${key}"><div class="review-section-heading"><button class="review-section-toggle" type="button" data-toggle-review="${key}" aria-expanded="${expanded}"><span class="review-chevron ${expanded ? "expanded" : ""}" aria-hidden="true"></span><span><strong>${title}</strong><small>${number(count)} ${count === 1 ? "entry" : "entries"} / ${approved ? "Approved" : "Needs review"}</small></span></button><div class="review-section-actions"><button class="secondary-button review-add" type="button" data-add-review="${key}" ${locked ? "disabled" : ""}>Add</button>${readonly ? "" : `<button class="${approved ? "secondary-button" : "save-button"}" type="button" data-section-review="${key}" data-approved="${approved}">${approved ? "Reopen" : "Approve section"}</button>`}</div></div><div class="review-section-body" ${expanded ? "" : "hidden"}><p class="review-section-description">${description}</p><div class="review-editors">${content}</div></div></section>`; }
function renderSkillEditors(skills, readonly) { if (!skills.length) return `<div class="review-empty-row">No skills extracted. Add any missing skills before approving.</div>`; return `<div class="review-skill-header"><span>Skill</span><span>Normalized</span><span>Category</span><span>Confidence</span></div>${skills.map((skill, index) => `<div class="review-editor review-skill" data-index="${index}">${reviewField("Skill", "name", skill.name, readonly, true)}${reviewField("Normalized", "normalized_name", skill.normalized_name, readonly)}${reviewField("Category", "category", skill.category, readonly)}${reviewField("Confidence", "confidence", skill.confidence, readonly, true, "number", "0", "1", "0.01")}${evidencePreview(skill.evidence)}${removeReviewButton("skills", index, readonly)}</div>`).join("")}`; }
function renderEmploymentEditors(items, readonly) { if (!items.length) return `<div class="review-empty-row">No employment extracted. Add work history before approving if needed.</div>`; return items.map((item, index) => `<div class="review-editor review-employment" data-index="${index}">${reviewField("Employer", "employer", item.employer, readonly, true)}${reviewField("Title", "title", item.title, readonly, true)}${reviewField("Location", "location", item.location, readonly)}${reviewField("Start", "start", dateInputValue(item.start), readonly, false, "text", null, null, null, "YYYY or YYYY-MM")}${reviewField("End", "end", dateInputValue(item.end), readonly, false, "text", null, null, null, "YYYY or YYYY-MM")}${reviewCheckbox("Current role", "is_current", item.is_current, readonly)}${reviewTextarea("Highlights, one per line", "highlights", item.highlights, readonly)}${reviewField("Skills, comma separated", "skills", (item.skills || []).join(", "), readonly)}${evidencePreview(item.evidence)}${removeReviewButton("employment", index, readonly)}</div>`).join(""); }
function renderEducationEditors(items, readonly) { if (!items.length) return `<div class="review-empty-row">No education extracted. Add credentials before approving if needed.</div>`; return items.map((item, index) => `<div class="review-editor review-education" data-index="${index}">${reviewField("Institution", "institution", item.institution, readonly, true)}${reviewField("Degree", "degree", item.degree, readonly)}${reviewField("Field of study", "field_of_study", item.field_of_study, readonly)}${reviewField("Graduation", "graduation", dateInputValue(item.graduation), readonly, false, "text", null, null, null, "YYYY or YYYY-MM")}${reviewTextarea("Highlights, one per line", "highlights", item.highlights, readonly)}${evidencePreview(item.evidence)}${removeReviewButton("education", index, readonly)}</div>`).join(""); }
function renderProjectEditors(items, readonly) { if (!items.length) return `<div class="review-empty-row">No projects extracted. Add relevant projects before approving if needed.</div>`; return items.map((item, index) => `<div class="review-editor review-project" data-index="${index}">${reviewField("Project", "name", item.name, readonly, true)}${reviewTextarea("Description", "description", item.description ? [item.description] : [], readonly)}${reviewField("Technologies, comma separated", "technologies", (item.technologies || []).join(", "), readonly)}${reviewTextarea("Highlights, one per line", "highlights", item.highlights, readonly)}${evidencePreview(item.evidence)}${removeReviewButton("projects", index, readonly)}</div>`).join(""); }
function reviewField(label, name, value, readonly, required = false, type = "text", min = null, max = null, step = null, placeholder = "") { return `<label class="review-field"><span>${escapeHtml(label)}</span><input name="${name}" type="${type}" value="${escapeHtml(value ?? "")}" ${required ? "required" : ""} ${readonly ? "disabled" : ""} ${min !== null ? `min="${min}"` : ""} ${max !== null ? `max="${max}"` : ""} ${step !== null ? `step="${step}"` : ""} placeholder="${placeholder}"></label>`; }
function reviewTextarea(label, name, values, readonly) { return `<label class="review-field review-field-wide"><span>${escapeHtml(label)}</span><textarea name="${name}" rows="3" ${readonly ? "disabled" : ""}>${escapeHtml((values || []).join("\n"))}</textarea></label>`; }
function reviewCheckbox(label, name, checked, readonly) { return `<label class="review-inline-check"><input name="${name}" type="checkbox" ${checked ? "checked" : ""} ${readonly ? "disabled" : ""}><span>${escapeHtml(label)}</span></label>`; }
function removeReviewButton(section, index, readonly) { return `<button class="icon-button review-remove" type="button" data-remove-review="${section}" data-index="${index}" aria-label="Remove entry" title="Remove entry" ${readonly ? "disabled" : ""}>&times;</button>`; }
function evidencePreview(evidence) { const excerpts = (evidence || []).slice(0, 2).map((item) => `${item.section}: ${item.text}`).join(" / "); return excerpts ? `<details class="review-evidence"><summary>Source evidence</summary><p>${escapeHtml(excerpts)}</p></details>` : ""; }
function dateInputValue(value) { if (!value) return ""; return value.month ? `${value.year}-${String(value.month).padStart(2, "0")}` : String(value.year); }

async function handleResumeReviewClick(event) {
  const toggle = event.target.closest("button[data-toggle-review]");
  const add = event.target.closest("button[data-add-review]");
  const remove = event.target.closest("button[data-remove-review]");
  const sectionReview = event.target.closest("button[data-section-review]");
  const saveDraft = event.target.closest("#saveResumeDraft");
  if (!toggle && !add && !remove && !sectionReview && !saveDraft) return;
  try { syncReviewDraft(); } catch (error) { showReviewFeedback(error.message, true); return; }
  if (toggle) { const key = toggle.dataset.toggleReview; state.reviewExpanded[key] = !state.reviewExpanded[key]; renderResumeReview(); return; }
  if (add) { addReviewEntry(add.dataset.addReview); state.reviewExpanded[add.dataset.addReview] = true; renderResumeReview(); return; }
  if (remove) { removeReviewEntry(remove.dataset.removeReview, Number(remove.dataset.index)); renderResumeReview(); return; }
  if (saveDraft) { await saveResumeReviewDraft(); return; }
  const key = sectionReview.dataset.sectionReview;
  const approved = sectionReview.dataset.approved === "true";
  if (!approved && !validateReviewSection(key)) return;
  const sections = new Set(state.reviewDraft.reviewed_sections || []);
  if (approved) sections.delete(key); else sections.add(key);
  state.reviewDraft.reviewed_sections = [...sections];
  state.reviewExpanded[key] = approved;
  await saveResumeReviewDraft();
}
function handleResumeReviewChange() { handleResumeReviewInput(); }
function handleResumeReviewInput() { const feedback = document.getElementById("resumeReviewFeedback"); if (feedback && selectedReviewItem()?.extraction.can_review) { feedback.textContent = "Unsaved changes"; feedback.className = "save-feedback pending"; } }
function addReviewEntry(section) { const defaults = { skills: { name: "", normalized_name: null, category: null, confidence: 1, evidence: [] }, employment: { employer: "", title: "", location: null, start: null, end: null, is_current: false, highlights: [], skills: [], evidence: [] }, education: { institution: "", degree: null, field_of_study: null, graduation: null, highlights: [], evidence: [] }, projects: { name: "", description: null, technologies: [], highlights: [], evidence: [] } }; const fields = { skills: "skills", employment: "work_experience", education: "education", projects: "projects" }; state.reviewDraft[fields[section]].push(defaults[section]); }
function removeReviewEntry(section, index) { const fields = { skills: "skills", employment: "work_experience", education: "education", projects: "projects" }; state.reviewDraft[fields[section]].splice(index, 1); }

function syncReviewDraft() {
  const form = document.getElementById("resumeReviewForm");
  if (!form || !state.reviewDraft) return;
  state.reviewDraft.skills = [...form.querySelectorAll(".review-skill")].map((row) => { const prior = state.reviewDraft.skills[Number(row.dataset.index)] || {}; return mergeReviewIdentity(prior, { name: reviewValue(row, "name"), normalized_name: nullable(reviewValue(row, "normalized_name")), category: nullable(reviewValue(row, "category")), confidence: Number(reviewValue(row, "confidence")), evidence: prior.evidence || [] }); });
  state.reviewDraft.work_experience = [...form.querySelectorAll(".review-employment")].map((row) => { const prior = state.reviewDraft.work_experience[Number(row.dataset.index)] || {}; const current = row.querySelector('[name="is_current"]').checked; return mergeReviewIdentity(prior, { employer: reviewValue(row, "employer"), title: reviewValue(row, "title"), location: nullable(reviewValue(row, "location")), start: parseResumeDate(reviewValue(row, "start")), end: current ? null : parseResumeDate(reviewValue(row, "end")), is_current: current, highlights: lineValues(reviewValue(row, "highlights")), skills: commaValues(reviewValue(row, "skills")), evidence: prior.evidence || [] }); });
  state.reviewDraft.education = [...form.querySelectorAll(".review-education")].map((row) => { const prior = state.reviewDraft.education[Number(row.dataset.index)] || {}; return mergeReviewIdentity(prior, { institution: reviewValue(row, "institution"), degree: nullable(reviewValue(row, "degree")), field_of_study: nullable(reviewValue(row, "field_of_study")), graduation: parseResumeDate(reviewValue(row, "graduation")), highlights: lineValues(reviewValue(row, "highlights")), evidence: prior.evidence || [] }); });
  state.reviewDraft.projects = [...form.querySelectorAll(".review-project")].map((row) => { const prior = state.reviewDraft.projects[Number(row.dataset.index)] || {}; const description = reviewValue(row, "description").trim(); return mergeReviewIdentity(prior, { name: reviewValue(row, "name"), description: description || null, technologies: commaValues(reviewValue(row, "technologies")), highlights: lineValues(reviewValue(row, "highlights")), evidence: prior.evidence || [] }); });
  state.reviewDraft.review_comment = form.elements.review_comment.value;
}
function mergeReviewIdentity(prior, value) { return { ...Object.fromEntries(Object.entries(prior || {}).filter(([key]) => key.endsWith("_id"))), ...value }; }
function reviewValue(row, name) { return row.querySelector(`[name="${name}"]`)?.value || ""; }
function nullable(value) { const cleaned = value.trim(); return cleaned || null; }
function lineValues(value) { return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean); }
function commaValues(value) { return value.split(",").map((item) => item.trim()).filter(Boolean); }
function parseResumeDate(value) { const cleaned = value.trim(); if (!cleaned) return null; const match = /^(19|20|21)\d{2}(?:-(0[1-9]|1[0-2]))?$/.exec(cleaned); if (!match) throw new Error(`Use YYYY or YYYY-MM for dates: ${cleaned}`); const [year, month] = cleaned.split("-").map(Number); return { year, month: month || null, precision: month ? "month" : "year", display_text: cleaned }; }
function structuredCompleteness(extraction) { const section_counts = { skills: (extraction.skills || []).length, employment: (extraction.work_experience || []).length, education: (extraction.education || []).length, projects: (extraction.projects || []).length }; const populated = Object.values(section_counts).filter((count) => count > 0).length; return { score: populated / 4, fact_count: Object.values(section_counts).reduce((total, count) => total + count, 0), section_counts }; }
function validateReviewSection(key) { const section = document.querySelector(`[data-review-section="${key}"]`); const invalid = [...(section?.querySelectorAll("[required]") || [])].find((input) => !input.checkValidity()); if (!invalid) return true; state.reviewExpanded[key] = true; invalid.reportValidity(); showReviewFeedback(`Complete required ${formatLabel(key)} fields before approval`, true); return false; }
function reviewPayload() { return { skills: state.reviewDraft.skills, work_experience: state.reviewDraft.work_experience, education: state.reviewDraft.education, projects: state.reviewDraft.projects, approved_sections: state.reviewDraft.reviewed_sections || [], review_comment: state.reviewDraft.review_comment || "" }; }
function updateReviewApprovalButton() { const button = document.getElementById("approveResumeReview"); if (!button || !selectedReviewItem()?.extraction.can_review) return; button.disabled = new Set(state.reviewDraft?.reviewed_sections || []).size !== 4; }
function showReviewFeedback(message, error = false) { const feedback = document.getElementById("resumeReviewFeedback"); if (!feedback) return; feedback.textContent = message; feedback.className = `save-feedback ${error ? "error" : "success"}`; }

async function refreshResumeReviews() { const refreshed = await fetch("/api/resume-reviews", { cache: "no-store" }); if (!refreshed.ok) throw new Error("Saved review could not be refreshed"); state.resumeReviews = await refreshed.json(); const updated = selectedReviewItem(); state.reviewDraft = updated ? structuredClone(updated.extraction) : null; renderMetrics(); renderResumeReview(); }

async function saveResumeReviewDraft() {
  const selected = selectedReviewItem();
  if (!selected?.extraction.can_review) return;
  try {
    syncReviewDraft();
    showReviewFeedback("Saving draft...");
    const response = await fetch(`/api/resume-reviews/${encodeURIComponent(state.selectedExtractionId)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reviewPayload()) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Resume review draft could not be saved");
    await refreshResumeReviews();
  } catch (error) { showReviewFeedback(error.message, true); }
}

async function approveResumeReview(event) {
  event.preventDefault();
  const selected = selectedReviewItem();
  if (!selected?.extraction.can_review) return;
  const button = document.getElementById("approveResumeReview");
  try {
    syncReviewDraft();
    if (state.reviewDraft.reviewed_sections.length !== 4) throw new Error("Review and approve all four sections first");
    button.disabled = true;
    showReviewFeedback("Saving reviewed facts...");
    const response = await fetch(`/api/resume-reviews/${encodeURIComponent(state.selectedExtractionId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reviewPayload()) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Resume review could not be approved");
    await refreshResumeReviews();
  } catch (error) { button.disabled = false; showReviewFeedback(error.message, true); }
}

function stageRow(name, status, detail, timestamp, freshness) { const age = freshness?.age_hours == null ? "age unavailable" : `${number(freshness.age_hours)}h old`; return `<div class="ops-stage"><span class="stage-indicator ${status}"></span><strong>${escapeHtml(name)}${status === "stale" ? `<small>${escapeHtml(age)}</small>` : ""}</strong><span>${escapeHtml(detail)}</span><time>${escapeHtml(formatTimestamp(timestamp))}</time></div>`; }
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
  loadApplicationComparisons(jobId);
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

async function loadApplicationComparisons(jobId) {
  const container = document.getElementById("applicationComparisonResult");
  if (!container) return;
  try {
    const response = await fetch(`/api/application-comparisons/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Comparison history could not be loaded");
    state.applicationComparisons[jobId] = result;
    if (state.selectedJobId === jobId) container.innerHTML = renderApplicationComparison(result.latest);
  } catch (error) { if (state.selectedJobId === jobId) container.innerHTML = `<div class="comparison-empty error">${escapeHtml(error.message)}</div>`; }
}

function handleComparisonClick(event) {
  const button = event.target.closest("#runApplicationComparison");
  if (button) runApplicationComparison(button);
}

async function runApplicationComparison(button) {
  const jobId = state.selectedJobId;
  const select = document.getElementById("comparisonResumeVersion");
  const container = document.getElementById("applicationComparisonResult");
  if (!jobId || !select?.value || !container) return;
  button.disabled = true;
  container.innerHTML = `<div class="comparison-empty">Running deterministic comparison...</div>`;
  try {
    const response = await fetch(`/api/application-comparisons/${encodeURIComponent(jobId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resume_version_id: select.value }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Application comparison failed");
    state.applicationComparisons[jobId] = { job_id: jobId, latest: result, comparisons: [result, ...(state.applicationComparisons[jobId]?.comparisons || [])] };
    container.innerHTML = renderApplicationComparison(result);
  } catch (error) { container.innerHTML = `<div class="comparison-empty error">${escapeHtml(error.message)}</div>`; }
  finally { button.disabled = false; }
}

function approvedResumeVersions() {
  const approvedIds = new Set((state.resumeReviews?.items || []).filter((item) => item.extraction.review_status === "approved").map((item) => item.resume_version.resume_version_id));
  return state.resumeVersions.filter((version) => approvedIds.has(version.resume_version_id));
}

function applicationComparisonMarkup(item, tracking) {
  const versions = approvedResumeVersions();
  const selectedId = versions.some((version) => version.resume_version_id === tracking.resume_version_id) ? tracking.resume_version_id : versions.find((version) => version.is_active)?.resume_version_id || versions[0]?.resume_version_id || "";
  const options = versions.map((version) => `<option value="${escapeHtml(version.resume_version_id)}" ${version.resume_version_id === selectedId ? "selected" : ""}>${escapeHtml(version.version_label)}</option>`).join("");
  return `<section class="comparison-section"><div class="section-heading"><h3>Application comparison</h3><span class="comparison-method">Rules v1.0.0</span></div><div class="comparison-controls"><label class="tracking-field"><span>Approved resume</span><select id="comparisonResumeVersion" ${versions.length ? "" : "disabled"}><option value="">${versions.length ? "Select resume" : "Approve a resume extraction first"}</option>${options}</select></label><button class="save-button" id="runApplicationComparison" type="button" ${versions.length ? "" : "disabled"}>Compare</button></div><div id="applicationComparisonResult">${renderApplicationComparison(state.applicationComparisons[item.job_id]?.latest, true)}</div></section>`;
}

function renderApplicationComparison(run, loading = false) {
  if (!run) return `<div class="comparison-empty">${loading ? "Loading prior comparisons..." : "No comparison has been run for this opportunity."}</div>`;
  const metrics = (run.metrics || []).map((metric) => `<div class="comparison-metric"><div><strong>${escapeHtml(formatLabel(metric.name))}</strong><span>${escapeHtml(metric.explanation)}</span></div><b>${formatScore(metric.value)}</b></div>`).join("");
  const matched = (run.findings || []).filter((finding) => finding.kind === "matched");
  const gaps = (run.findings || []).filter((finding) => finding.kind === "missing" || finding.kind === "partial");
  const findingRows = [...matched.slice(0, 8), ...gaps.slice(0, 12)].map((finding) => `<li class="comparison-finding ${escapeHtml(finding.kind)}"><span>${escapeHtml(formatLabel(finding.kind))}</span><div><strong>${escapeHtml(finding.subject)}</strong><p>${escapeHtml(finding.explanation)}</p></div></li>`).join("");
  const warnings = (run.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  return `<div class="comparison-score"><div>${formatScore(run.overall_score)}</div><p><strong>Application evidence score</strong><span>Compared ${escapeHtml(formatTimestamp(run.completed_at))} / ${escapeHtml(run.analyzer_name)} ${escapeHtml(run.analyzer_version)}</span></p></div><div class="comparison-metrics">${metrics}</div>${findingRows ? `<h4>Evidence and gaps</h4><ul class="comparison-findings">${findingRows}</ul>` : ""}${warnings ? `<details class="comparison-warnings"><summary>Unavailable signals (${(run.warnings || []).length})</summary><ul>${warnings}</ul></details>` : ""}`;
}

function drawerMarkup(item) {
  const tracking = trackingFor(item.job_id);
  const components = ["skills", "title", "experience", "location"].map((name) => { const component = item.score_components[name]; return `<div class="component"><div class="component-top"><strong>${formatLabel(name)}</strong><span>${formatScore(component.points)} / ${formatScore(component.maximum_points)}</span></div><div class="component-track"><div class="component-fill" style="--value:${Math.max(0, Math.min(100, component.signal * 100))}%"></div></div></div>`; }).join("");
  const skillChips = (item.skill_match_details || []).length ? item.skill_match_details.map((skill) => `<span class="skill-chip">${escapeHtml(skill.skill)}<small>${escapeHtml(skill.evidence)} / ${escapeHtml(skill.importance)}</small></span>`).join("") : `<span class="job-meta">No profile skills detected</span>`;
  const titleExplanation = item.title_match_type === "exact_title" ? `Exact normalized match: ${item.normalized_title}` : item.title_match_type === "family" ? `Target family match: ${formatLabel(item.family)}` : item.title_match_type === "phrase" ? `Contiguous title phrase: ${(item.matched_title_signals || []).join(", ")}` : item.title_match_type === "fuzzy" ? "Partial title-token overlap" : "No target title match";
  const locationExplanation = item.home_region_match ? "Home-region match" : item.workplace_type === "remote" ? "Remote-accessible role" : `${formatLabel(item.workplace_type)} outside the home region`;
  return `<h2 class="detail-title">${escapeHtml(item.title)}</h2><p class="detail-employer">${escapeHtml(item.employer)} / ${escapeHtml(item.location || "Unknown location")}</p>
    <div class="detail-actions">${item.url ? `<a class="primary-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open posting</a>` : ""}</div>
    <section class="tracking-section"><div class="section-heading"><h3>Application tracking</h3><span class="tracking-updated">${tracking.updated_at ? `Updated ${escapeHtml(formatTimestamp(tracking.updated_at))}` : "Not tracked yet"}</span></div>
      <form id="trackingForm" class="tracking-form"><label class="shortlist-control"><input type="checkbox" name="shortlisted" ${tracking.shortlisted ? "checked" : ""}><span>Shortlist this opportunity</span></label>
        <div class="tracking-grid">${selectField("Status", "status", "trackingStatus", tracking.status, STATUS_OPTIONS)}${selectField("Priority", "priority", "trackingPriority", tracking.priority, ["none", "low", "medium", "high"])}${inputField("Application date", "application_date", "applicationDate", "date", tracking.application_date)}${inputField("Follow-up date", "follow_up_date", "followUpDate", "date", tracking.follow_up_date)}</div>
        ${resumeVersionField(tracking.resume_version_id)}
        <label class="tracking-field"><span>Notes</span><textarea name="notes" id="trackingNotes" maxlength="5000" rows="4" placeholder="Contacts, preparation, next action">${escapeHtml(tracking.notes || "")}</textarea></label>
        <div class="tracking-actions"><button class="save-button" type="submit">Save tracking</button><span class="save-feedback" role="status" aria-live="polite"></span></div></form></section>
    ${applicationComparisonMarkup(item, tracking)}
    <div class="detail-facts">${fact("Workplace", formatLabel(item.workplace_type))}${fact("Seniority", formatLabel(item.seniority || "unknown"))}${fact("Compensation", formatSalary(item))}${fact("Lifecycle", formatLabel(item.lifecycle_status))}${fact("Last seen", formatDate(item.last_seen_at))}${fact("Source", formatLabel(item.source))}</div>
    <section class="detail-section"><div class="section-heading"><h3>Resume relevance</h3><span class="match-band">${escapeHtml(item.match_band)} match</span></div><div class="score-overview"><div class="score-number">${formatScore(item.resume_relevance_score)}</div><div class="score-copy"><strong>${item.rank === 1 ? "Highest-ranked opportunity" : `Opportunity rank ${item.rank}`}</strong><span>${item.score_cap_reason ? `Score capped: ${formatLabel(item.score_cap_reason)}` : "No eligibility cap applied"}</span></div></div><div class="component-list">${components}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Matched skills</h3><span class="match-band">${(item.matched_skills || []).length} matched</span></div><div class="chip-list">${skillChips}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Match explanation</h3></div><ul class="explanation-list"><li><strong>Title</strong><span>${escapeHtml(titleExplanation)}</span></li><li><strong>Experience</strong><span>${escapeHtml(formatLabel(item.seniority || "unknown"))} seniority / ${formatScore(item.score_components.experience.signal * 100)}% signal</span></li><li><strong>Location</strong><span>${escapeHtml(locationExplanation)}</span></li><li><strong>Freshness</strong><span>${item.days_since_last_seen === 0 ? "Seen in the latest observation" : `Last seen ${item.days_since_last_seen} days ago`}</span></li></ul></section>`;
}

function selectField(label, name, id, value, options) { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><select name="${name}" id="${id}">${options.map((option) => `<option value="${option}" ${option === value ? "selected" : ""}>${formatLabel(option)}</option>`).join("")}</select></label>`; }
function resumeVersionField(selectedId) {
  const options = state.resumeVersions.map((version) => `<option value="${escapeHtml(version.resume_version_id)}" ${version.resume_version_id === selectedId ? "selected" : ""}>${escapeHtml(version.version_label)}${version.is_active ? " (Active)" : ""}</option>`).join("");
  const emptyLabel = state.resumeVersions.length ? "No resume selected" : "No uploaded resumes";
  return `<label class="tracking-field"><span>Resume version</span><select name="resume_version_id" id="resumeVersionId" ${state.resumeVersions.length ? "" : "disabled"}><option value="">${emptyLabel}</option>${options}</select></label>`;
}
function inputField(label, name, id, type, value, placeholder = "") { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><input name="${name}" id="${id}" type="${type}" value="${escapeHtml(value || "")}" maxlength="200" placeholder="${escapeHtml(placeholder)}"></label>`; }
function fact(label, value) { return `<div class="fact"><span class="fact-label">${escapeHtml(label)}</span><span class="fact-value">${escapeHtml(value || "Not available")}</span></div>`; }
function formatLabel(value) { return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatSource(value) { return String(value || "unknown").toLowerCase() === "usajobs" ? "USAJOBS" : formatLabel(value); }
function formatScore(value) { return Number(value || 0).toFixed(Number(value || 0) % 1 === 0 ? 0 : 1); }
function number(value) { return Number(value || 0).toLocaleString("en-US", { maximumFractionDigits: 1 }); }
function formatPercent(value) { return `${(Number(value || 0) * 100).toFixed(1)}%`; }
function formatMoney(value, currency = "USD") { return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(Number(value || 0)); }
function formatSalary(item) { if (!item.has_salary) return "Salary not listed"; const formatter = new Intl.NumberFormat("en-US", { style: "currency", currency: item.salary_currency || "USD", maximumFractionDigits: 0 }); if (item.salary_minimum != null && item.salary_maximum != null) return `${formatter.format(item.salary_minimum)} - ${formatter.format(item.salary_maximum)}`; return formatter.format(item.salary_minimum ?? item.salary_maximum); }
function formatDate(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(new Date(value)) : "Unknown"; }
function formatTimestamp(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)) : "Unknown"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]); }
