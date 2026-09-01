"use strict";

const PAGE_SIZE = 25;
const ACTIVE_STATUSES = new Set(["applied", "interviewing", "offer"]);
const STATUS_OPTIONS = ["new", "reviewing", "saved", "applied", "interviewing", "offer", "rejected", "archived"];
const state = { mart: null, opportunities: [], tracking: {}, filtered: [], page: 1, band: "all", view: "opportunities", selectedJobId: null };
const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  ["freshnessLabel", "showingMetric", "strongMetric", "shortlistMetric", "applicationMetric", "searchInput", "employerFilter", "familyFilter", "seniorityFilter", "workplaceFilter", "lifecycleFilter", "applicationFilter", "salaryFilter", "sortSelect", "opportunitiesBody", "resultCount", "activeFilters", "loadingState", "emptyState", "errorState", "errorMessage", "retryButton", "resetFilters", "emptyReset", "previousPage", "nextPage", "pageLabel", "bandFilters", "detailDrawer", "drawerBackdrop", "drawerContent", "drawerRank", "closeDrawer", "filtersPanel", "filterToggle", "viewSwitch", "viewTitle"].forEach((id) => { elements[id] = document.getElementById(id); });
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
    elements.viewTitle.textContent = state.view === "shortlist" ? "Shortlist" : "Current opportunities";
    applyFilters(true);
  });
  elements.opportunitiesBody.addEventListener("click", (event) => { const row = event.target.closest("tr[data-job-id]"); if (row) openDrawer(row.dataset.jobId); });
  elements.opportunitiesBody.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { const row = event.target.closest("tr[data-job-id]"); if (row) { event.preventDefault(); openDrawer(row.dataset.jobId); } }
  });
  elements.drawerContent.addEventListener("submit", saveTracking);
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
  try {
    const [martResponse, trackingResponse] = await Promise.all([fetch("/api/opportunities", { cache: "no-store" }), fetch("/api/application-tracking", { cache: "no-store" })]);
    if (!martResponse.ok || !trackingResponse.ok) throw new Error("Dashboard data could not be loaded");
    const [mart, tracking] = await Promise.all([martResponse.json(), trackingResponse.json()]);
    if (!Array.isArray(mart.opportunities)) throw new Error("The Gold mart has no opportunities array");
    state.mart = mart;
    state.opportunities = mart.opportunities;
    state.tracking = tracking.records || {};
    populateFilters();
    elements.freshnessLabel.textContent = `Gold refreshed ${formatTimestamp(mart.generated_at)}`;
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
function render() { renderMetrics(); renderActiveFilters(); renderTable(); renderPagination(); }
function renderMetrics() {
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
  const payload = { shortlisted: data.get("shortlisted") === "on", status: data.get("status"), priority: data.get("priority"), application_date: data.get("application_date") || null, follow_up_date: data.get("follow_up_date") || null, resume_version: data.get("resume_version") || null, notes: data.get("notes") || "" };
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
  const locationExplanation = item.home_region_match ? "Home-region match" : item.workplace_type === "remote" ? "Remote-accessible role" : `${formatLabel(item.workplace_type)} outside the home region`;
  return `<h2 class="detail-title">${escapeHtml(item.title)}</h2><p class="detail-employer">${escapeHtml(item.employer)} / ${escapeHtml(item.location || "Unknown location")}</p>
    <div class="detail-actions">${item.url ? `<a class="primary-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open posting</a>` : ""}</div>
    <section class="tracking-section"><div class="section-heading"><h3>Application tracking</h3><span class="tracking-updated">${tracking.updated_at ? `Updated ${escapeHtml(formatTimestamp(tracking.updated_at))}` : "Not tracked yet"}</span></div>
      <form id="trackingForm" class="tracking-form"><label class="shortlist-control"><input type="checkbox" name="shortlisted" ${tracking.shortlisted ? "checked" : ""}><span>Shortlist this opportunity</span></label>
        <div class="tracking-grid">${selectField("Status", "status", "trackingStatus", tracking.status, STATUS_OPTIONS)}${selectField("Priority", "priority", "trackingPriority", tracking.priority, ["none", "low", "medium", "high"])}${inputField("Application date", "application_date", "applicationDate", "date", tracking.application_date)}${inputField("Follow-up date", "follow_up_date", "followUpDate", "date", tracking.follow_up_date)}</div>
        ${inputField("Resume version", "resume_version", "resumeVersion", "text", tracking.resume_version, "Example: analytics-v3")}
        <label class="tracking-field"><span>Notes</span><textarea name="notes" id="trackingNotes" maxlength="5000" rows="4" placeholder="Contacts, preparation, next action">${escapeHtml(tracking.notes || "")}</textarea></label>
        <div class="tracking-actions"><button class="save-button" type="submit">Save tracking</button><span class="save-feedback" role="status" aria-live="polite"></span></div></form></section>
    <div class="detail-facts">${fact("Workplace", formatLabel(item.workplace_type))}${fact("Seniority", formatLabel(item.seniority || "unknown"))}${fact("Compensation", formatSalary(item))}${fact("Lifecycle", formatLabel(item.lifecycle_status))}${fact("Last seen", formatDate(item.last_seen_at))}${fact("Source", formatLabel(item.source))}</div>
    <section class="detail-section"><div class="section-heading"><h3>Resume relevance</h3><span class="match-band">${escapeHtml(item.match_band)} match</span></div><div class="score-overview"><div class="score-number">${formatScore(item.resume_relevance_score)}</div><div class="score-copy"><strong>${item.rank === 1 ? "Highest-ranked opportunity" : `Opportunity rank ${item.rank}`}</strong><span>${item.score_cap_reason ? `Score capped: ${formatLabel(item.score_cap_reason)}` : "No eligibility cap applied"}</span></div></div><div class="component-list">${components}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Matched skills</h3><span class="match-band">${(item.matched_skills || []).length} matched</span></div><div class="chip-list">${skillChips}</div></section>
    <section class="detail-section"><div class="section-heading"><h3>Match explanation</h3></div><ul class="explanation-list"><li><strong>Title</strong><span>${escapeHtml(titleExplanation)}</span></li><li><strong>Experience</strong><span>${escapeHtml(formatLabel(item.seniority || "unknown"))} seniority / ${formatScore(item.score_components.experience.signal * 100)}% signal</span></li><li><strong>Location</strong><span>${escapeHtml(locationExplanation)}</span></li><li><strong>Freshness</strong><span>${item.days_since_last_seen === 0 ? "Seen in the latest observation" : `Last seen ${item.days_since_last_seen} days ago`}</span></li></ul></section>`;
}

function selectField(label, name, id, value, options) { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><select name="${name}" id="${id}">${options.map((option) => `<option value="${option}" ${option === value ? "selected" : ""}>${formatLabel(option)}</option>`).join("")}</select></label>`; }
function inputField(label, name, id, type, value, placeholder = "") { return `<label class="tracking-field"><span>${escapeHtml(label)}</span><input name="${name}" id="${id}" type="${type}" value="${escapeHtml(value || "")}" maxlength="200" placeholder="${escapeHtml(placeholder)}"></label>`; }
function fact(label, value) { return `<div class="fact"><span class="fact-label">${escapeHtml(label)}</span><span class="fact-value">${escapeHtml(value || "Not available")}</span></div>`; }
function formatLabel(value) { return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatScore(value) { return Number(value || 0).toFixed(Number(value || 0) % 1 === 0 ? 0 : 1); }
function formatSalary(item) { if (!item.has_salary) return "Salary not listed"; const formatter = new Intl.NumberFormat("en-US", { style: "currency", currency: item.salary_currency || "USD", maximumFractionDigits: 0 }); if (item.salary_minimum != null && item.salary_maximum != null) return `${formatter.format(item.salary_minimum)} - ${formatter.format(item.salary_maximum)}`; return formatter.format(item.salary_minimum ?? item.salary_maximum); }
function formatDate(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(new Date(value)) : "Unknown"; }
function formatTimestamp(value) { return value ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)) : "Unknown"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]); }
