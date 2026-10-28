"use strict";

// Labels tab (Phase 7.1). Loaded after app.js and shares its `state`, `elements`, and
// formatting helpers. Lists the newest owner label per requirement line across every
// posting, flags labels that went stale or orphaned against the latest comparison, and
// re-labels a line through the same append-only endpoint the drawer uses.

const LABEL_STATE_NAMES = { current: "Current", stale: "Stale", orphaned: "Orphaned", closed: "Posting closed" };
const LABEL_STATE_HINTS = {
  current: "The model still gives the verdict you labeled",
  stale: "The model's verdict changed after you labeled this line",
  orphaned: "The posting is still compared, but this line is not (usually a parser change reworded it)",
  closed: "The posting is no longer active; the label still counts but cannot be changed",
};
const LABEL_FILTERS = [["all", "All labels"], ["disagree", "Disagreements"], ["stale", "Stale"], ["orphaned", "Orphaned"], ["closed", "Posting closed"], ["current", "Current"]];

state.labels = { data: null, loading: false, error: null, filter: "all", kind: "", search: "", busy: null, feedback: null };

function bindLabels() {
  elements.refreshLabels.addEventListener("click", () => loadLabels());
  elements.labelsContent.addEventListener("click", handleLabelsClick);
  elements.labelsContent.addEventListener("change", handleLabelsFilter);
  let searchTimer;
  elements.labelsContent.addEventListener("input", (event) => {
    if (event.target.id !== "labelsSearch") return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.labels.search = event.target.value; renderLabelRows(); }, 120);
  });
}

async function loadLabels() {
  const labels = state.labels;
  labels.loading = true;
  labels.error = null;
  if (state.view === "labels") renderLabels();
  try {
    const response = await fetch("/api/labels", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Labels could not be loaded");
    labels.data = payload;
  } catch (error) {
    labels.error = error.message;
  }
  labels.loading = false;
  updateLabelsAlert();
  if (state.view === "labels") renderLabels();
}

// The nav badge shows the orphan alert from any view, so a parser change that breaks
// labels is noticed without opening the tab.
function updateLabelsAlert() {
  const alert = state.labels.data?.summary?.orphan_alert;
  const badge = elements.labelsAlert;
  badge.hidden = !alert?.active;
  badge.textContent = alert?.active ? `⚠ ${alert.orphaned}` : "";
  badge.title = alert?.active ? alert.reasons.join("; ") : "";
}

function renderLabels() {
  const labels = state.labels;
  const summary = labels.data?.summary;
  if (labels.loading && !labels.data) { elements.labelsSummary.textContent = "Loading labels"; elements.labelsContent.innerHTML = `<div class="loading-state"><span>Loading labels</span></div>`; return; }
  if (labels.error) { elements.labelsSummary.textContent = "Labels unavailable"; elements.labelsContent.innerHTML = `<div class="error-state"><strong>Labels could not be loaded</strong><span>${escapeHtml(labels.error)}</span></div>`; return; }
  if (!summary) return;
  const model = labels.data.comparison_model;
  elements.labelsSummary.textContent = labels.data.comparison_generated_at ? `${number(summary.total)} labeled lines · checked against the comparison from ${formatTimestamp(labels.data.comparison_generated_at)}${model?.version ? ` (model ${model.version})` : ""}` : `${number(summary.total)} labeled lines · no comparison results yet; run scripts/sync_data.py`;
  if (!summary.total) { elements.labelsContent.innerHTML = `<div class="empty-state"><strong>No labels yet</strong><span>Open a posting and agree with or correct a ✓/◐/✗ verdict to save your first label.</span></div>`; return; }
  const kinds = Object.keys(summary.by_kind).sort();
  elements.labelsContent.innerHTML = `${labelsAlertBanner(summary.orphan_alert)}${labelsTracker(summary)}
    <div class="labels-toolbar">
      <label class="tracking-field"><span>Show</span><select id="labelsFilter">${LABEL_FILTERS.map(([value, text]) => `<option value="${value}"${labels.filter === value ? " selected" : ""}>${text}</option>`).join("")}</select></label>
      <label class="tracking-field"><span>Kind</span><select id="labelsKind"><option value="">All kinds</option>${kinds.map((kind) => `<option value="${escapeHtml(kind)}"${labels.kind === kind ? " selected" : ""}>${escapeHtml(formatLabel(kind))}</option>`).join("")}</select></label>
      <label class="tracking-field labels-search"><span>Search</span><input id="labelsSearch" type="search" placeholder="Requirement or posting" value="${escapeHtml(labels.search)}"></label>
    </div>
    <div id="labelsRows"></div>`;
  renderLabelRows();
}

function labelsAlertBanner(alert) {
  if (!alert?.active) return "";
  return `<div class="labels-alert" role="alert"><strong>${number(alert.orphaned)} labels no longer match a requirement line</strong><span>${escapeHtml(alert.reasons.join("; "))}. A parser change probably reworded these lines; the labels are kept and still listed.</span><button type="button" class="secondary-button" data-labels-filter="orphaned">Show orphaned</button></div>`;
}

// Tracker strip: each tile filters the table. Drawer and queue labels are always counted
// separately, because only randomly sampled queue labels give an unbiased accuracy.
function labelsTracker(summary) {
  const alert = summary.orphan_alert;
  const sources = summary.by_source || {};
  const tile = (filter, label, value, detail, extra = "") => `<button type="button" class="intel-kpi labels-tile${extra}${state.labels.filter === filter ? " active" : ""}" data-labels-filter="${filter}"><span>${escapeHtml(label)}</span><strong>${number(value)}</strong><small>${escapeHtml(detail)}</small></button>`;
  const share = alert.share ? ` · ${(alert.share * 100).toFixed(0)}%` : "";
  return `<div class="labels-tracker">
    ${tile("all", "Labeled lines", summary.total, `Drawer ${number(sources.drawer || 0)} · Queue ${number(sources.queue || 0)}`)}
    ${tile("disagree", "Disagreements", summary.disagree, `${number(summary.agree)} agreed with the model`)}
    ${tile("stale", "Stale", summary.states.stale, "Model verdict changed since")}
    ${tile("orphaned", "Orphaned", summary.states.orphaned, `Alert at ${number(alert.alert_count)}${alert.alert_share ? ` or ${(alert.alert_share * 100).toFixed(0)}%` : ""}${share}`, alert.active ? " alert" : "")}
    ${tile("closed", "Posting closed", summary.states.closed, "Kept as evidence")}
  </div>`;
}

function filteredLabelRows() {
  const { filter, kind, search } = state.labels;
  const query = search.trim().toLowerCase();
  return (state.labels.data?.labels || []).filter((row) => {
    if (filter === "disagree" && row.agrees) return false;
    if (!["all", "disagree"].includes(filter) && row.state !== filter) return false;
    if (kind && (row.kind || "unknown") !== kind) return false;
    if (query && ![row.requirement, row.title, row.employer, row.job_id].some((value) => String(value || "").toLowerCase().includes(query))) return false;
    return true;
  });
}

function renderLabelRows() {
  const container = document.getElementById("labelsRows");
  if (!container) return;
  const rows = filteredLabelRows();
  const feedback = state.labels.feedback ? `<p class="save-feedback ${state.labels.feedback.error ? "error" : "success"}">${escapeHtml(state.labels.feedback.text)}</p>` : "";
  if (!rows.length) { container.innerHTML = `${feedback}<div class="empty-state"><strong>No labels match these filters</strong></div>`; return; }
  const all = state.labels.data.labels;
  container.innerHTML = `${feedback}<p class="job-meta wrap">Showing ${number(rows.length)} of ${number(all.length)}. "Then" is the model's verdict when you labeled; "Now" is its verdict in the latest comparison.</p>
    ${marketTable(["Requirement", "Kind", "Then", "Now", "Yours", "State", "Labeled", "Change"], rows.map((row) => labelRow(row, all.indexOf(row))).join(""))}`;
}

function verdictCell(status) {
  if (!status) return `<span class="job-meta">—</span>`;
  const glyph = VERDICT_ICONS[status] || "○";
  return `<span class="label-verdict ${escapeHtml(status)}" title="${escapeHtml(formatLabel(status))}">${glyph} ${escapeHtml(formatLabel(status))}</span>`;
}

function labelRow(row, index) {
  const posting = row.title ? `${row.title}${row.employer ? ` · ${row.employer}` : ""}` : row.job_id;
  const openable = state.opportunities.some((item) => item.job_id === row.job_id);
  const history = row.history.length ? `<details class="label-history"><summary>${row.history.length} earlier</summary><ul>${row.history.map((entry) => `<li>${formatTimestamp(entry.labeled_at)}: model ${verdictCell(entry.predicted_status)}, you ${verdictCell(entry.corrected_status)}</li>`).join("")}</ul></details>` : "";
  const editable = row.state === "current" || row.state === "stale";
  const busy = state.labels.busy === index;
  const buttons = editable ? Object.entries(VERDICT_ICONS).map(([status, glyph]) => `<button type="button" class="label-button${row.corrected_status === status ? " active" : ""}" data-relabel-index="${index}" data-label-status="${status}" aria-pressed="${row.corrected_status === status}" title="Should be ${formatLabel(status)}"${busy ? " disabled" : ""}>${glyph}</button>`).join("") : `<span class="job-meta" title="${escapeHtml(LABEL_STATE_HINTS[row.state])}">Not in the latest comparison</span>`;
  const agreement = row.agrees ? "Agreed" : "Corrected";
  return `<tr class="label-row ${escapeHtml(row.state)}">
    <td><span class="req-text">${escapeHtml(row.requirement)}</span><span class="job-meta wrap">${openable ? `<button type="button" class="text-button" data-open-job="${escapeHtml(row.job_id)}">${escapeHtml(posting)}</button>` : escapeHtml(posting)}${row.required === false ? " · preferred" : ""}</span>${history}</td>
    <td>${escapeHtml(formatLabel(row.kind))}</td>
    <td>${verdictCell(row.predicted_status)}</td>
    <td>${verdictCell(row.current_status)}</td>
    <td>${verdictCell(row.corrected_status)}<span class="job-meta">${agreement}</span></td>
    <td><span class="label-state ${escapeHtml(row.state)}" title="${escapeHtml(LABEL_STATE_HINTS[row.state])}">${escapeHtml(LABEL_STATE_NAMES[row.state] || formatLabel(row.state))}</span></td>
    <td>${escapeHtml(formatTimestamp(row.labeled_at))}<span class="job-meta">${escapeHtml(row.model_version ? `model ${row.model_version}` : "")}</span></td>
    <td><div class="req-label">${buttons}</div></td>
  </tr>`;
}

function handleLabelsFilter(event) {
  if (event.target.id === "labelsFilter") { state.labels.filter = event.target.value; renderLabels(); }
  if (event.target.id === "labelsKind") { state.labels.kind = event.target.value; renderLabelRows(); }
}

async function handleLabelsClick(event) {
  const filterButton = event.target.closest("[data-labels-filter]");
  if (filterButton) { state.labels.filter = filterButton.dataset.labelsFilter; renderLabels(); return; }
  const openButton = event.target.closest("[data-open-job]");
  if (openButton) { openDrawer(openButton.dataset.openJob); return; }
  const button = event.target.closest("button[data-relabel-index]");
  if (!button || state.labels.busy !== null) return;
  const index = Number(button.dataset.relabelIndex);
  const row = state.labels.data?.labels[index];
  if (!row) return;
  state.labels.busy = index;
  state.labels.feedback = null;
  renderLabelRows();
  try {
    const response = await fetch("/api/requirement-labels", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: row.job_id, requirement: row.requirement, corrected_status: button.dataset.labelStatus }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Label could not be saved");
    state.labels.busy = null;
    await loadLabels();
    // A new label pairs your verdict with the model's verdict today, so a stale line
    // becomes current and may leave the filtered list; say where it went.
    const saved = state.labels.data?.labels.find((item) => item.job_id === row.job_id && item.requirement === row.requirement);
    state.labels.feedback = { text: `Saved ${formatLabel(result.corrected_status)} for "${row.requirement.slice(0, 80)}"${saved ? `. It is now listed as ${LABEL_STATE_NAMES[saved.state] || formatLabel(saved.state)}${saved.agrees ? ", agreeing with the model" : ", disagreeing with the model"}.` : "."}` };
    renderLabelRows();
  } catch (error) {
    state.labels.busy = null;
    state.labels.feedback = { text: error.message, error: true };
    renderLabelRows();
  }
}
