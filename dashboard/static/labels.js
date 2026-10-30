"use strict";

// Labels tab (Phase 7.1). Loaded after app.js and shares its `state`, `elements`, and
// formatting helpers. Lists the newest owner label per requirement line across every
// posting, flags labels that went stale or orphaned against the latest comparison, and
// re-labels or withdraws a line through the same append-only endpoint the drawer uses.
// Withdrawn lines stay findable under their own filter but are left out of every count.
// The Review queue view serves lines drawn at random by the server; only labels saved
// there are marked sampled, which is what Phase 7 accuracy estimates use.

const LABEL_STATE_NAMES = { current: "Current", stale: "Stale", orphaned: "Orphaned", removed: "Removed by parser fix", closed: "Posting closed" };
const LABEL_STATE_HINTS = {
  current: "The model still gives the verdict you labeled",
  stale: "The model's verdict changed after you labeled this line",
  orphaned: "The posting is still compared, but this line is not (usually a parser change reworded it)",
  removed: "You marked this line not a requirement, and a boilerplate rule now keeps it out of the comparison",
  closed: "The posting is no longer active; the label still counts but cannot be changed",
};
const LABEL_FILTERS = [["all", "All labels"], ["disagree", "Disagreements"], ["not_requirement", "Not a requirement"], ["stale", "Stale"], ["orphaned", "Orphaned"], ["removed", "Removed by parser fix"], ["closed", "Posting closed"], ["current", "Current"], ["withdrawn", "Withdrawn"]];
const LABEL_SOURCE_NAMES = { drawer: "Drawer", labels_tab: "Labels tab", queue: "Review queue" };

state.labels = { data: null, loading: false, error: null, filter: "all", kind: "", search: "", busy: null, feedback: null, view: "all" };
state.queue = { data: null, loading: false, error: null, busy: false, feedback: null };
state.changes = { data: null, loading: false, error: null, busy: false, feedback: null };
state.scorecard = { data: null, loading: false, error: null };

function bindLabels() {
  elements.refreshLabels.addEventListener("click", () => { loadLabels(); if (state.labels.view === "queue") loadQueue(); if (state.labels.view === "changes") loadChanges(); if (state.labels.view === "scorecard") loadScorecard(); });
  elements.labelsTabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-labels-view]");
    if (!button) return;
    state.labels.view = button.dataset.labelsView;
    elements.labelsTabs.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    if (state.labels.view === "queue" && !state.queue.data && !state.queue.loading) loadQueue();
    if (state.labels.view === "changes" && !state.changes.loading) loadChanges();
    if (state.labels.view === "scorecard" && !state.scorecard.loading) loadScorecard();
    renderLabels();
  });
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
  if (state.labels.view === "queue") { renderQueue(); return; }
  if (state.labels.view === "changes") { renderChanges(); return; }
  if (state.labels.view === "scorecard") { renderScorecard(); return; }
  const labels = state.labels;
  const summary = labels.data?.summary;
  if (labels.loading && !labels.data) { elements.labelsSummary.textContent = "Loading labels"; elements.labelsContent.innerHTML = `<div class="loading-state"><span>Loading labels</span></div>`; return; }
  if (labels.error) { elements.labelsSummary.textContent = "Labels unavailable"; elements.labelsContent.innerHTML = `<div class="error-state"><strong>Labels could not be loaded</strong><span>${escapeHtml(labels.error)}</span></div>`; return; }
  if (!summary) return;
  const model = labels.data.comparison_model;
  elements.labelsSummary.textContent = labels.data.comparison_generated_at ? `${number(summary.total)} labeled lines · checked against the comparison from ${formatTimestamp(labels.data.comparison_generated_at)}${model?.version ? ` (model ${model.version})` : ""}` : `${number(summary.total)} labeled lines · no comparison results yet; run scripts/sync_data.py`;
  if (!summary.total && !summary.withdrawn) { elements.labelsContent.innerHTML = `<div class="empty-state"><strong>No labels yet</strong><span>Open a posting and agree with or correct a ✓/◐/✗ verdict to save your first label.</span></div>`; return; }
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
    ${tile("all", "Labeled lines", summary.total, `Drawer ${number(sources.drawer || 0)} · Tab ${number(sources.labels_tab || 0)} · Queue ${number(sources.queue || 0)}`)}
    ${tile("disagree", "Disagreements", summary.disagree, `${number(summary.agree)} agreed with the model`)}
    ${tile("not_requirement", "Not a requirement", summary.not_requirement || 0, `Parser picked these up by mistake${summary.states.removed ? ` · ${number(summary.states.removed)} now filtered` : ""}`)}
    ${tile("stale", "Stale", summary.states.stale, "Model verdict changed since")}
    ${tile("orphaned", "Orphaned", summary.states.orphaned, `Alert at ${number(alert.alert_count)}${alert.alert_share ? ` or ${(alert.alert_share * 100).toFixed(0)}%` : ""}${share}`, alert.active ? " alert" : "")}
    ${tile("closed", "Posting closed", summary.states.closed, "Kept as evidence")}
    ${tile("withdrawn", "Withdrawn", summary.withdrawn || 0, "Not counted anywhere")}
  </div>`;
}

function filteredLabelRows() {
  const { filter, kind, search } = state.labels;
  const query = search.trim().toLowerCase();
  return (state.labels.data?.labels || []).filter((row) => {
    if ((filter === "withdrawn") !== Boolean(row.withdrawn)) return false;
    if (filter === "disagree" && (row.agrees || row.not_requirement)) return false;
    if (filter === "not_requirement" && !row.not_requirement) return false;
    if (!["all", "disagree", "not_requirement", "withdrawn"].includes(filter) && row.state !== filter) return false;
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
  const shown = all.filter((row) => (state.labels.filter === "withdrawn") === Boolean(row.withdrawn)).length;
  container.innerHTML = `${feedback}<p class="job-meta wrap">Showing ${number(rows.length)} of ${number(shown)}${state.labels.filter === "withdrawn" ? " withdrawn" : ""}. "Then" is the model's verdict when you labeled; "Now" is its verdict in the latest comparison. A note is saved with your next change on that line.</p>
    ${marketTable(["Requirement", "Kind", "Then", "Now", "Yours", "State", "Labeled", "Change"], rows.map((row) => labelRow(row, all.indexOf(row))).join(""))}`;
}

function verdictCell(status) {
  if (!status) return `<span class="job-meta">—</span>`;
  const glyph = VERDICT_ICONS[status] || "○";
  return `<span class="label-verdict ${escapeHtml(status)}" title="${escapeHtml(verdictName(status))}">${glyph} ${escapeHtml(verdictName(status))}</span>`;
}

function verdictName(status) { return status === "not_requirement" ? "Not a requirement" : formatLabel(status); }

function labelRow(row, index) {
  const posting = row.title ? `${row.title}${row.employer ? ` · ${row.employer}` : ""}` : row.job_id;
  const openable = state.opportunities.some((item) => item.job_id === row.job_id);
  const historyEntry = (entry) => `<li>${formatTimestamp(entry.labeled_at)}: ${entry.action === "withdraw" ? "withdrawn" : `model ${verdictCell(entry.predicted_status)}, you ${verdictCell(entry.corrected_status)}`} · ${escapeHtml(LABEL_SOURCE_NAMES[entry.source] || formatLabel(entry.source))}${entry.note ? ` · “${escapeHtml(entry.note)}”` : ""}</li>`;
  const history = row.history.length ? `<details class="label-history"><summary>${row.history.length} earlier</summary><ul>${row.history.map(historyEntry).join("")}</ul></details>` : "";
  const editable = row.state === "current" || row.state === "stale";
  const busy = state.labels.busy === index ? " disabled" : "";
  const verdictButtons = editable ? Object.entries(VERDICT_ICONS).map(([status, glyph]) => `<button type="button" class="label-button${!row.withdrawn && row.corrected_status === status ? " active" : ""}" data-relabel-index="${index}" data-label-status="${status}" aria-pressed="${!row.withdrawn && row.corrected_status === status}" title="${escapeHtml(row.withdrawn ? `Label again as ${verdictName(status)}` : verdictTitle(status))}"${busy}>${glyph}</button>`).join("") : `<span class="job-meta" title="${escapeHtml(LABEL_STATE_HINTS[row.state])}">Not in the latest comparison</span>`;
  // Any active label can be withdrawn, including orphaned and closed ones: the log, not
  // the comparison, decides. Re-labeling still needs the line in the latest comparison.
  const withdraw = row.withdrawn ? "" : `<button type="button" class="label-button withdraw" data-withdraw-index="${index}" title="Take this label back; it stays in the history"${busy}>Withdraw</button>`;
  const noteInput = editable || !row.withdrawn ? `<input class="label-note-input" type="text" maxlength="500" data-note-index="${index}" placeholder="Note (optional)" aria-label="Note for your next change to this label">` : "";
  const agreement = row.withdrawn ? `Withdrawn ${formatTimestamp(row.withdrawn_at)}${row.withdrawal_note ? ` · “${escapeHtml(row.withdrawal_note)}”` : ""}` : `${row.not_requirement ? "Parser mistake" : row.agrees ? "Agreed" : "Corrected"}${row.note ? ` · “${escapeHtml(row.note)}”` : ""}`;
  return `<tr class="label-row ${escapeHtml(row.state)}${row.withdrawn ? " withdrawn-row" : ""}">
    <td><span class="req-text">${escapeHtml(row.requirement)}</span><span class="job-meta wrap">${openable ? `<button type="button" class="text-button" data-open-job="${escapeHtml(row.job_id)}">${escapeHtml(posting)}</button>` : escapeHtml(posting)}${row.required === false ? " · preferred" : ""}</span>${history}</td>
    <td>${escapeHtml(formatLabel(row.kind))}</td>
    <td>${verdictCell(row.predicted_status)}</td>
    <td>${verdictCell(row.current_status)}</td>
    <td>${verdictCell(row.corrected_status)}<span class="job-meta wrap">${agreement}</span></td>
    <td><span class="label-state ${escapeHtml(row.state)}" title="${escapeHtml(LABEL_STATE_HINTS[row.state])}">${escapeHtml(LABEL_STATE_NAMES[row.state] || formatLabel(row.state))}</span>${row.withdrawn ? `<span class="label-state withdrawn">Withdrawn</span>` : ""}</td>
    <td>${escapeHtml(formatTimestamp(row.labeled_at))}<span class="job-meta">${escapeHtml([LABEL_SOURCE_NAMES[row.source] || formatLabel(row.source), row.model_version ? `model ${row.model_version}` : ""].filter(Boolean).join(" · "))}</span></td>
    <td><div class="req-label">${verdictButtons}${withdraw}</div>${noteInput}</td>
  </tr>`;
}

function handleLabelsFilter(event) {
  if (event.target.id === "labelsFilter") { state.labels.filter = event.target.value; renderLabels(); }
  if (event.target.id === "labelsKind") { state.labels.kind = event.target.value; renderLabelRows(); }
}

async function handleLabelsClick(event) {
  const filterButton = event.target.closest("[data-labels-filter]");
  if (filterButton) { state.labels.filter = filterButton.dataset.labelsFilter; renderLabels(); return; }
  if (event.target.closest("button[data-save-snapshot]")) { saveSnapshot(); return; }
  const queueButton = event.target.closest("button[data-queue-status]");
  if (queueButton) { saveQueueLabel(queueButton.dataset.queueStatus); return; }
  const openButton = event.target.closest("[data-open-job]");
  if (openButton) { openDrawer(openButton.dataset.openJob); return; }
  const button = event.target.closest("button[data-relabel-index], button[data-withdraw-index]");
  if (!button || state.labels.busy !== null) return;
  const withdrawing = button.dataset.withdrawIndex !== undefined;
  const index = Number(withdrawing ? button.dataset.withdrawIndex : button.dataset.relabelIndex);
  const row = state.labels.data?.labels[index];
  if (!row) return;
  const note = elements.labelsContent.querySelector(`input[data-note-index="${index}"]`)?.value.trim() || null;
  const body = withdrawing ? { action: "withdraw" } : { action: "label", corrected_status: button.dataset.labelStatus };
  state.labels.busy = index;
  state.labels.feedback = null;
  renderLabelRows();
  try {
    const response = await fetch("/api/requirement-labels", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: row.job_id, requirement: row.requirement, source: "labels_tab", note, ...body }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Label could not be saved");
    state.labels.busy = null;
    await loadLabels();
    // A new label pairs your verdict with the model's verdict today, so a stale line
    // becomes current and may leave the filtered list; say where it went.
    const saved = state.labels.data?.labels.find((item) => item.job_id === row.job_id && item.requirement === row.requirement);
    const line = `"${row.requirement.slice(0, 80)}"`;
    state.labels.feedback = withdrawing ? { text: `Withdrew the label on ${line}. It is no longer counted; find it under Withdrawn to label it again.` } : { text: `Saved ${verdictName(result.corrected_status)} for ${line}${saved ? `. It is now listed as ${LABEL_STATE_NAMES[saved.state] || formatLabel(saved.state)}${saved.not_requirement ? ", counted as a parser mistake" : saved.agrees ? ", agreeing with the model" : ", disagreeing with the model"}.` : "."}` };
    renderLabelRows();
  } catch (error) {
    state.labels.busy = null;
    state.labels.feedback = { text: error.message, error: true };
    renderLabelRows();
  }
}

// Review queue: one randomly drawn line at a time. There is no skip button on purpose:
// skipping the hard or dull lines would bias the sample the accuracy estimate rests on.
async function loadQueue() {
  const queue = state.queue;
  queue.loading = true;
  queue.error = null;
  if (state.view === "labels" && state.labels.view === "queue") renderQueue();
  try {
    const response = await fetch("/api/labels/queue", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The review queue could not be loaded");
    queue.data = payload;
  } catch (error) {
    queue.error = error.message;
  }
  queue.loading = false;
  if (state.view === "labels" && state.labels.view === "queue") renderQueue();
}

function renderQueue() {
  const queue = state.queue;
  if (queue.loading && !queue.data) { elements.labelsContent.innerHTML = `<div class="loading-state"><span>Drawing the next line</span></div>`; return; }
  if (queue.error) { elements.labelsContent.innerHTML = `<div class="error-state"><strong>The review queue could not be loaded</strong><span>${escapeHtml(queue.error)}</span></div>`; return; }
  const data = queue.data;
  if (!data) return;
  if (!data.groups.length) { elements.labelsContent.innerHTML = `<div class="empty-state"><strong>Nothing to sample yet</strong><span>The queue draws from the latest comparison. Approve your resume and run scripts/sync_data.py first.</span></div>`; return; }
  const percent = data.target ? Math.min(100, (data.labeled / data.target) * 100) : 0;
  const feedback = queue.feedback ? `<p class="save-feedback ${queue.feedback.error ? "error" : "success"}">${escapeHtml(queue.feedback.text)}</p>` : "";
  elements.labelsContent.innerHTML = `<div class="queue-progress"><div><strong>${number(data.labeled)} of ${number(data.target)} sampled labels</strong><span>Up to ${number(data.target_per_group)} per group, drawn at random (seed ${escapeHtml(data.seed)}). ${escapeHtml(formatLabel(data.exclude_kinds.join(", ") || "none"))} lines are left out.</span></div><div class="queue-bar"><span style="--progress:${percent}%"></span></div></div>
    ${feedback}${data.next ? queueCard(data.next) : `<div class="empty-state"><strong>Every group has reached its target</strong><span>Raise queue.target_per_group in config/labels.yaml to collect more.</span></div>`}
    <section class="intel-block wide queue-groups"><div class="intel-heading"><h2>Progress by group</h2><span>Sampled labels / target · lines available now</span></div>${queueGroupsTable(data.groups)}</section>`;
}

function queueCard(next) {
  const busy = state.queue.busy ? " disabled" : "";
  const posting = next.title ? `${next.title}${next.employer ? ` · ${next.employer}` : ""}` : next.job_id;
  const openable = state.opportunities.some((item) => item.job_id === next.job_id);
  const predicted = next.status;
  const button = (status, text, title) => `<button type="button" class="label-button" data-queue-status="${status}" title="${escapeHtml(title)}"${busy}>${text}</button>`;
  const others = Object.entries(VERDICT_ICONS).filter(([status]) => status !== predicted).map(([status, glyph]) => button(status, glyph, verdictTitle(status))).join("");
  return `<article class="queue-card ${escapeHtml(predicted)}">
    <span class="drawer-eyebrow">${escapeHtml(formatLabel(next.kind))} · model says ${escapeHtml(formatLabel(predicted))}${next.item.required === false ? " · preferred" : ""}</span>
    <p class="queue-requirement"><span class="req-icon">${VERDICT_ICONS[predicted] || "–"}</span> ${escapeHtml(next.requirement)}</p>
    <span class="job-meta wrap">${openable ? `<button type="button" class="text-button" data-open-job="${escapeHtml(next.job_id)}">${escapeHtml(posting)}</button>` : escapeHtml(posting)}</span>
    <div class="req-evidence">${requirementEvidence(next.item)}</div>
    <div class="req-label">${button(predicted, "Agree", `Agree: ${formatLabel(predicted)}`)}<span class="label-prompt">should be</span>${others}</div>
    <input class="label-note-input" id="queueNote" type="text" maxlength="500" placeholder="Note (optional)" aria-label="Note for this label">
    <p class="job-meta wrap">Use ⊘ when the line is not a requirement at all (benefits, a company blurb); it still counts as a sampled label. Please label every line the queue shows, even easy ones. Lines are drawn at random so the accuracy estimate is fair; skipping would bias it.</p>
  </article>`;
}

function queueGroupsTable(groups) {
  const kinds = [...new Set(groups.map((group) => group.kind))].sort();
  const cell = (kind, status) => {
    const group = groups.find((item) => item.kind === kind && item.status === status);
    if (!group) return `<td class="number-cell"><span class="job-meta">—</span></td>`;
    const done = group.target && group.labeled >= group.target;
    return `<td class="number-cell${done ? " queue-done" : ""}">${number(group.labeled)} / ${number(group.target)}<span class="job-meta">${number(group.population)} available</span></td>`;
  };
  return marketTable(["Kind", ...QUEUE_STATUSES.map((status) => `${VERDICT_ICONS[status]} ${formatLabel(status)}`)], kinds.map((kind) => `<tr><td>${escapeHtml(formatLabel(kind))}</td>${QUEUE_STATUSES.map((status) => cell(kind, status)).join("")}</tr>`).join(""));
}
const QUEUE_STATUSES = ["met", "partial", "missing"];

async function saveQueueLabel(status) {
  const queue = state.queue;
  const next = queue.data?.next;
  if (!next || queue.busy) return;
  const note = document.getElementById("queueNote")?.value.trim() || null;
  queue.busy = true;
  queue.feedback = null;
  renderQueue();
  try {
    const response = await fetch("/api/labels/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: next.job_id, requirement: next.requirement, corrected_status: status, note }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Label could not be saved");
    queue.feedback = { text: `Saved ${verdictName(result.corrected_status)} for "${next.requirement.slice(0, 80)}".` };
  } catch (error) {
    queue.feedback = { text: error.message, error: true };
  }
  queue.busy = false;
  await loadQueue();
  loadLabels();
}

// Changes: what happened to the labeled data, frozen snapshots of it, and how close the
// tracking decisions are to what the simulator and Phase 8 B3 need.
const CHANGE_NAMES = { added: "Labeled", changed: "Changed", confirmed: "Confirmed", withdrawn: "Withdrawn", restored: "Restored", model_version: "New model version" };

async function loadChanges() {
  const changes = state.changes;
  changes.loading = true;
  changes.error = null;
  if (state.view === "labels" && state.labels.view === "changes") renderChanges();
  try {
    const response = await fetch("/api/labels/changes", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Label changes could not be loaded");
    changes.data = payload;
  } catch (error) {
    changes.error = error.message;
  }
  changes.loading = false;
  if (state.view === "labels" && state.labels.view === "changes") renderChanges();
}

function renderChanges() {
  const changes = state.changes;
  if (changes.loading && !changes.data) { elements.labelsContent.innerHTML = `<div class="loading-state"><span>Loading label changes</span></div>`; return; }
  if (changes.error) { elements.labelsContent.innerHTML = `<div class="error-state"><strong>Label changes could not be loaded</strong><span>${escapeHtml(changes.error)}</span></div>`; return; }
  const data = changes.data;
  if (!data) return;
  const feedback = changes.feedback ? `<p class="save-feedback ${changes.feedback.error ? "error" : "success"}">${escapeHtml(changes.feedback.text)}</p>` : "";
  elements.labelsContent.innerHTML = `${trackingProgressBlock(data.tracking)}
    <section class="intel-block wide"><div class="intel-heading"><h2>Snapshots</h2><button type="button" class="secondary-button" data-save-snapshot${changes.busy ? " disabled" : ""}>Save snapshot</button></div>
      <p class="job-meta wrap">A snapshot freezes the labels as they are now, so a Phase 7 number can name exactly which set it used. It stores counts and a fingerprint of the log, not the labels themselves; "Verified" means the log still matches.</p>${feedback}
      ${data.snapshots.length ? marketTable(["Snapshot", "Saved", "Labels", "Sampled", "Agree", "Log lines", "Check"], data.snapshots.map((item) => `<tr><td><code>${escapeHtml(item.snapshot_id)}</code></td><td>${escapeHtml(formatTimestamp(item.created_at))}</td><td class="number-cell">${number(item.active_labels)}</td><td class="number-cell">${number(item.sampled_labels)}</td><td class="number-cell">${number(item.agree)}</td><td class="number-cell">${number(item.log_lines)}</td><td>${item.verified ? `<span class="label-state current">Verified</span>` : `<span class="label-state orphaned" title="The label log was edited after this snapshot">Does not match</span>`}</td></tr>`).join("")) : `<p class="job-meta">No snapshots yet.</p>`}
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>Activity by day</h2><span>${number(data.event_count)} label events in total (UTC dates)</span></div>
      ${data.days.length ? marketTable(["Date", "Labeled", "Changed", "Confirmed", "Withdrawn", "Restored", "From the queue"], data.days.map((day) => `<tr><td>${escapeHtml(day.date)}</td><td class="number-cell">${number(day.added)}</td><td class="number-cell">${number(day.changed)}</td><td class="number-cell">${number(day.confirmed)}</td><td class="number-cell">${number(day.withdrawn)}</td><td class="number-cell">${number(day.restored)}</td><td class="number-cell">${number(day.sampled)}</td></tr>`).join("")) : `<p class="job-meta">No label activity yet.</p>`}
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>Recent changes</h2><span>Newest first${data.event_count > data.events.length ? `, latest ${number(data.events.length)}` : ""}</span></div>
      ${data.events.length ? `<ul class="change-log">${data.events.map(changeEntry).join("")}</ul>` : `<p class="job-meta">No label activity yet.</p>`}
    </section>`;
}

function changeEntry(event) {
  const when = `<span class="change-when">${escapeHtml(formatTimestamp(event.at))}</span>`;
  if (event.type === "model_version") return `<li class="change-entry model">${when}<strong>${CHANGE_NAMES.model_version}</strong><span>Labels from here on were made against comparison model ${escapeHtml(event.model_version)}.</span></li>`;
  const verdicts = event.type === "withdrawn" ? `was ${verdictCell(event.from_status)}` : event.type === "changed" ? `${verdictCell(event.from_status)} → ${verdictCell(event.to_status)}` : verdictCell(event.to_status);
  const source = LABEL_SOURCE_NAMES[event.source] || formatLabel(event.source);
  return `<li class="change-entry ${escapeHtml(event.type)}">${when}<strong>${escapeHtml(CHANGE_NAMES[event.type] || formatLabel(event.type))}</strong><span>${verdicts} · ${escapeHtml(event.requirement)}<span class="job-meta">${escapeHtml(formatLabel(event.kind))} · model said ${escapeHtml(formatLabel(event.predicted_status))} · ${escapeHtml(source)}${event.sampled ? " (sampled)" : ""}${event.note ? ` · “${escapeHtml(event.note)}”` : ""}</span></span></li>`;
}

function trackingProgressBlock(tracking) {
  const bar = (value, goal) => `<div class="queue-bar"><span style="--progress:${Math.min(100, goal ? (value / goal) * 100 : 0)}%"></span></div>`;
  const simulator = tracking.simulator;
  const b3 = tracking.b3;
  return `<section class="intel-block wide"><div class="intel-heading"><h2>Training labels from application tracking</h2><span>${number(tracking.positive)} positive · ${number(tracking.negative)} archived</span></div>
    <div class="tracking-progress">
      <div><strong>Weight simulator calibration: ${number(tracking.total)} of ${number(simulator.minimum)}</strong>${bar(tracking.total, simulator.minimum)}<span class="job-meta wrap">${simulator.ready ? "Ready: the simulator can recommend weights." : `Needs ${number(simulator.minimum)} decisions, with at least ${number(simulator.minimum_each)} positive and ${number(simulator.minimum_each)} archived.`}</span></div>
      <div><strong>Phase 8 B3 relevance model: ${number(tracking.total)} of ${number(b3.minimum)}–${number(b3.target)}</strong>${bar(tracking.total, b3.target)}<span class="job-meta wrap">${b3.ready ? "Enough to start B3." : `B3 can start at ${number(b3.minimum)} decisions and is comfortable at ${number(b3.target)}.`}</span></div>
    </div>
    <p class="job-meta wrap">Positive: ${escapeHtml(tracking.positive_statuses.map(formatLabel).join(", "))}. Negative: ${escapeHtml(tracking.negative_statuses.map(formatLabel).join(", "))}. Other statuses, including rejections (an outcome, not a preference), count as neither. Set in config/labels.yaml under tracking.</p>
  </section>`;
}

async function saveSnapshot() {
  const changes = state.changes;
  if (changes.busy) return;
  changes.busy = true;
  changes.feedback = null;
  renderChanges();
  try {
    const response = await fetch("/api/labels/snapshots", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Snapshot could not be saved");
    changes.feedback = { text: result.created ? `Saved snapshot ${result.snapshot.snapshot_id}.` : `Nothing changed since snapshot ${result.snapshot.snapshot_id}; no new snapshot saved.` };
  } catch (error) {
    changes.feedback = { text: error.message, error: true };
  }
  changes.busy = false;
  await loadChanges();
}

// Scorecard (Phase 7.2): how often the model's ✓/◐/✗ agrees with your labels, as
// Beta-Binomial posteriors with credible intervals. Only randomly sampled queue labels
// estimate accuracy; labels from postings you chose are shown on their own.
async function loadScorecard() {
  const card = state.scorecard;
  card.loading = true;
  card.error = null;
  if (state.view === "labels" && state.labels.view === "scorecard") renderScorecard();
  try {
    const response = await fetch("/api/labels/scorecard", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The scorecard could not be loaded");
    card.data = payload;
  } catch (error) {
    card.error = error.message;
  }
  card.loading = false;
  if (state.view === "labels" && state.labels.view === "scorecard") renderScorecard();
}

const pct = (value) => `${(Number(value) * 100).toFixed(0)}%`;

// A rate with its interval: "81% (74–87%)" plus a bar, or counts alone below the minimum.
function rateCell(rate, countKey = "agree") {
  if (!rate || !rate.total) return `<span class="job-meta">No labels</span>`;
  const counts = `${number(rate[countKey])} of ${number(rate.total)}`;
  if (rate.mean === undefined) return `<span class="job-meta" title="Too few labels for an interval">${counts}</span>`;
  return `<span class="rate-cell"><strong>${pct(rate.mean)}</strong> <span class="job-meta">${pct(rate.low)}–${pct(rate.high)} · ${counts}</span><span class="interval-bar" aria-hidden="true"><span style="--low:${rate.low * 100}%;--high:${rate.high * 100}%"></span><i style="--mean:${rate.mean * 100}%"></i></span></span>`;
}

function renderScorecard() {
  const card = state.scorecard;
  if (card.loading && !card.data) { elements.labelsContent.innerHTML = `<div class="loading-state"><span>Computing the scorecard</span></div>`; return; }
  if (card.error) { elements.labelsContent.innerHTML = `<div class="error-state"><strong>The scorecard could not be loaded</strong><span>${escapeHtml(card.error)}</span></div>`; return; }
  const data = card.data;
  if (!data) return;
  const level = pct(data.credible_level);
  const sampled = data.sampled;
  const overall = sampled.overall;
  const citation = data.snapshot_id ? `These numbers come from snapshot <code>${escapeHtml(data.snapshot_id)}</code>.` : `No snapshot covers the current labels; save one under Changes to cite these numbers.`;
  const headline = overall.mean === undefined
    ? `<div class="empty-state"><strong>Not enough sampled labels yet</strong><span>The estimate needs at least ${number(data.minimum_labels)} labels from the review queue.</span></div>`
    : `<div class="scorecard-headline"><div><span class="drawer-eyebrow">Model agrees with you</span><strong>${pct(overall.mean)}</strong><span>${level} credible interval ${pct(overall.low)}–${pct(overall.high)}</span></div>
      <p class="job-meta wrap">From ${number(overall.total)} randomly sampled labels in ${number(overall.groups_used)} of ${number(overall.groups)} groups, each group weighted by how many lines it has in the latest comparison (${pct(overall.coverage)} of lines covered). Lines you marked not a requirement are counted below, not here.</p></div>`;
  elements.labelsContent.innerHTML = `${headline}
    <p class="job-meta wrap">${citation} Prior Beta(${data.prior.map(number).join(", ")}); a group shows an interval once it has ${number(data.minimum_labels)} labels.</p>
    <section class="intel-block wide"><div class="intel-heading"><h2>By kind</h2><span>Random review queue · ${level} intervals</span></div>
      ${marketTable(["Kind", "Agreement"], Object.entries(sampled.by_kind).map(([kind, rate]) => `<tr><td>${escapeHtml(formatLabel(kind))}</td><td>${rateCell(rate)}</td></tr>`).join(""))}
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>By group</h2><span>Kind and the model's verdict when you labeled</span></div>
      ${marketTable(["Kind", "Model said", "Agreement", "⊘ Not a requirement", "Lines now"], sampled.groups.map((group) => `<tr><td>${escapeHtml(formatLabel(group.kind))}</td><td>${verdictCell(group.status)}</td><td>${rateCell(group)}</td><td class="number-cell">${number(group.not_requirement)}</td><td class="number-cell">${number(group.population)}</td></tr>`).join(""))}
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>Parser mistakes</h2><span>Sampled lines you marked ⊘</span></div>
      <p class="job-meta wrap">Share of randomly drawn lines that were not requirements at all: ${rateCell(sampled.not_requirement, "count")}</p>
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>Model verdict vs yours</h2><span>Random review queue</span></div>${confusionTable(sampled.confusion)}</section>
    ${replayBlock(data.replay, data.current_model_version)}
    <section class="intel-block wide"><div class="intel-heading"><h2>Postings you chose</h2><span>Drawer and table labels · not an accuracy estimate</span></div>
      <p class="job-meta wrap">These come from postings you opened, which lean toward strong matches, so they are shown apart. Agreement: ${rateCell(data.chosen.overall)}</p>
      ${data.chosen.labels ? confusionTable(data.chosen.confusion) : ""}
    </section>
    <section class="intel-block wide"><div class="intel-heading"><h2>By model version</h2><span>Agreement when each label was made</span></div>
      ${marketTable(["Model version", "Random queue", "Postings you chose"], data.by_model_version.map((row) => `<tr><td>${escapeHtml(row.model_version)}</td><td>${rateCell(row.sampled)}</td><td>${rateCell(row.chosen)}</td></tr>`).join(""))}
    </section>`;
}

function confusionTable(confusion) {
  const columns = ["met", "partial", "missing", "not_requirement"];
  return marketTable(["Model said ↓ · You said →", ...columns.map((status) => `${VERDICT_ICONS[status]} ${verdictName(status)}`)], Object.entries(confusion).map(([predicted, row]) => `<tr><td>${verdictCell(predicted)}</td>${columns.map((status) => `<td class="number-cell${status === predicted ? " confusion-agree" : ""}">${number(row[status])}</td>`).join("")}</tr>`).join(""));
}

function replayBlock(replay, version) {
  if (!replay.available) return `<section class="intel-block wide"><div class="intel-heading"><h2>Replay</h2></div><p class="job-meta wrap">${escapeHtml(replay.reason)}.</p></section>`;
  const change = (row) => `<li>${verdictCell(row.then_status)} → ${verdictCell(row.now_status)} · you ${verdictCell(row.your_status)} · ${escapeHtml(row.requirement)}${row.sampled ? ` <span class="job-meta">(sampled)</span>` : ""}</li>`;
  return `<section class="intel-block wide"><div class="intel-heading"><h2>Replay of the current model</h2><span>Model ${escapeHtml(version)} on ${number(replay.lines)} labeled lines</span></div>
    <p class="job-meta wrap">Every labeled line is judged again with today's vocabulary and your approved resume, including lines from postings that have closed. "Then" is agreement when you labeled; "Now" is the current model on the same lines.${replay.dropped ? ` ${number(replay.dropped)} lines the model now leaves to self-assess are left out of both.` : ""}</p>
    ${marketTable(["Labels", "Then", "Now"], [["Random queue", replay.sampled], ["Postings you chose", replay.chosen], ["All", replay.all]].map(([name, pair]) => `<tr><td>${name}</td><td>${rateCell(pair.then)}</td><td>${rateCell(pair.now)}</td></tr>`).join(""))}
    <div class="replay-changes"><div><h3>Fixed (${number(replay.fixed_count)})</h3>${replay.fixed.length ? `<ul class="change-log">${replay.fixed.map(change).join("")}</ul>` : `<p class="job-meta">None</p>`}</div>
    <div><h3>Regressed (${number(replay.regressed_count)})</h3>${replay.regressed.length ? `<ul class="change-log">${replay.regressed.map(change).join("")}</ul>` : `<p class="job-meta">None</p>`}</div></div>
  </section>`;
}
