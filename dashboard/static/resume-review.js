"use strict";

// Resume review (Phase 6). Loaded after app.js and shares its `state`, `elements`, and
// formatting helpers. Facts are edited locally, saved as a draft (PATCH), and approved
// (PUT) only when all four sections are approved. Editing a section clears its approval.

const REVIEW_SECTIONS = ["skills", "experience", "education", "projects"];
const REVIEW_LABELS = { skills: "Skills", experience: "Experience", education: "Education", projects: "Projects" };
const REVIEW_BLANK_ROWS = {
  skills: () => ({ name: "", canonical: null, category: null, source: "reviewer", evidence: [] }),
  experience: () => ({ title: "", employer: null, location: null, start: null, end: null, current: false, highlights: [], evidence: null }),
  education: () => ({ institution: "", degree: null, field_of_study: null, graduation: null, expected: false, evidence: null }),
  projects: () => ({ name: "", description: null, technologies: [], evidence: null }),
};
const REVIEW_REQUIRED = { skills: "name", experience: "title", education: "institution", projects: "name" };

state.review = { list: null, selectedId: null, detail: null, draft: null, approved: new Set(), comment: "", dirty: false, busy: false, invalid: new Set(), mark: null, scrollToMark: false, feedback: null, loading: false, error: null };

function bindResumeReview() {
  elements.resumeReviewSelect.addEventListener("change", () => selectResumeReview(elements.resumeReviewSelect.value));
  elements.resumeReviewContent.addEventListener("input", handleReviewInput);
  elements.resumeReviewContent.addEventListener("change", handleReviewInput);
  elements.resumeReviewContent.addEventListener("click", handleReviewClick);
}

async function loadResumeReviews(preferredId = null) {
  const review = state.review;
  review.loading = true;
  review.error = null;
  renderResumeReview();
  try {
    const response = await fetch("/api/resume-reviews", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Resume reviews could not be loaded");
    review.list = payload;
    const items = payload.items || [];
    const chosen = items.find((item) => item.extraction_id === preferredId) || items.find((item) => item.can_review) || items[0];
    review.loading = false;
    if (chosen) await loadReviewDetail(chosen.extraction_id);
    else renderResumeReview();
  } catch (error) {
    review.loading = false;
    review.error = error.message;
    renderResumeReview();
  }
}

async function selectResumeReview(extractionId) {
  if (state.review.dirty && !window.confirm("Discard unsaved review changes?")) {
    elements.resumeReviewSelect.value = state.review.selectedId || "";
    return;
  }
  await loadReviewDetail(extractionId);
}

async function loadReviewDetail(extractionId) {
  const review = state.review;
  try {
    const response = await fetch(`/api/resume-reviews/${encodeURIComponent(extractionId)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Review could not be loaded");
    review.selectedId = extractionId;
    review.detail = payload;
    review.draft = structuredClone(payload.extraction.facts);
    review.approved = new Set(payload.extraction.approved_sections);
    review.comment = payload.extraction.review_comment || "";
    review.dirty = false;
    review.invalid = new Set();
    review.mark = null;
    review.feedback = null;
  } catch (error) {
    review.error = error.message;
  }
  renderMetrics();
  renderResumeReview();
}

function renderResumeReview() {
  const review = state.review;
  const items = review.list?.items || [];
  elements.resumeReviewSelect.replaceChildren(...items.map((item) => {
    const option = document.createElement("option");
    option.value = item.extraction_id;
    option.textContent = `${item.resume_version.label}${item.resume_version.is_active ? " (active)" : ""} · ${formatLabel(item.review_status)} · ${formatTimestamp(item.created_at)}`;
    return option;
  }));
  elements.resumeReviewSelect.value = review.selectedId || "";
  elements.resumeReviewSelect.disabled = items.length === 0;
  if (review.loading) { elements.resumeReviewContent.innerHTML = `<div class="intel-empty">Loading resume reviews</div>`; return; }
  if (review.error) { elements.resumeReviewSummary.textContent = review.error; elements.resumeReviewContent.innerHTML = `<div class="ops-status-line failed"><div><strong>Resume review is unavailable</strong><span>${escapeHtml(review.error)}</span></div></div>`; return; }
  if (!items.length) {
    elements.resumeReviewSummary.textContent = "No resumes ingested yet";
    elements.resumeReviewContent.innerHTML = `<div class="rr-empty"><strong>Add a resume to start</strong><p>Resumes stay on this computer in <code>data/private</code>. Ingest a PDF or DOCX, then review its extracted facts here.</p><pre>.\\.venv\\Scripts\\python.exe scripts\\manage_resumes.py ingest "C:\\path\\to\\resume.pdf" --label "Data science v1" --activate</pre></div>`;
    return;
  }
  const detail = review.detail;
  if (!detail) { elements.resumeReviewContent.innerHTML = ""; return; }
  const extraction = detail.extraction;
  const editable = extraction.can_review;
  elements.resumeReviewSummary.textContent = `${number(review.list.pending_count)} pending · ${number(review.list.approved_count)} approved · parser ${extraction.parser_name} ${extraction.parser_version}`;
  const allApproved = REVIEW_SECTIONS.every((section) => review.approved.has(section));
  const statusLine = extraction.status === "failed"
    ? `<div class="ops-status-line failed wide"><div><strong>Text extraction failed quality checks</strong><span>This file may be scanned or image-only. Re-export it as a text PDF or DOCX and ingest it again.</span></div></div>`
    : extraction.review_status === "approved"
      ? `<div class="ops-status-line wide"><div><strong>Approved ${escapeHtml(formatTimestamp(extraction.approved_at))}</strong><span>These facts are read-only. Re-extract the resume to review a newer parse.</span></div></div>`
      : `<div class="ops-status-line ${extraction.status === "needs_attention" ? "stale" : ""} wide"><div><strong>${extraction.status === "needs_attention" ? "Review carefully: some quality checks did not pass" : "Check each section against the source text, then approve it"}</strong><span>Editing a section clears its approval. The review is final once all four sections are approved.</span></div></div>`;
  const warnings = (extraction.warnings || []).map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  const checks = (extraction.quality_checks || []).map((check) => `<li class="${check.passed ? "" : "failed"}"><span>${escapeHtml(check.message)}</span><b>${check.passed ? "Pass" : "Fail"}</b></li>`).join("");
  elements.resumeReviewContent.innerHTML = `${statusLine}
    <div class="rr-layout">
      <div class="rr-sections">${REVIEW_SECTIONS.map((section) => reviewSectionMarkup(section, editable)).join("")}
        <section class="rr-card">
          <label class="tracking-field"><span>Reviewer note (optional)</span><textarea data-review-comment maxlength="5000" rows="2" ${editable ? "" : "disabled"}>${escapeHtml(review.comment)}</textarea></label>
          <div class="rr-actions">
            <button class="secondary-button" type="button" data-review-action="save" ${editable && !review.busy ? "" : "disabled"}>Save draft</button>
            <button class="save-button" type="button" data-review-action="approve" ${editable && allApproved && !review.busy && !review.invalid.size ? "" : "disabled"}>Approve review</button>
            <span class="save-feedback ${review.feedback?.kind || ""}" role="status" aria-live="polite">${escapeHtml(review.feedback?.text || (review.dirty ? "Unsaved changes" : ""))}</span>
          </div>
        </section>
      </div>
      <aside class="rr-source" aria-label="Extracted resume text">
        <div class="section-heading"><h3>Source text</h3><span class="job-meta">${number(extraction.character_count)} characters${extraction.page_count ? ` · ${extraction.page_count} page${extraction.page_count === 1 ? "" : "s"}` : ""}</span></div>
        <pre class="rr-text" id="rrSourceText">${sourceTextMarkup(detail.text, review.mark)}</pre>
        <details class="rr-details"><summary>Quality checks · ${formatPercent(extraction.quality_score)}</summary><ul class="rr-checks">${checks}</ul></details>
        ${warnings ? `<details class="rr-details" open><summary>Parser warnings (${(extraction.warnings || []).length})</summary><ul class="rr-warnings">${warnings}</ul></details>` : ""}
      </aside>
    </div>`;
  const mark = document.getElementById("rrMark");
  if (mark && review.scrollToMark) {
    const pane = document.getElementById("rrSourceText");
    pane.scrollTop = Math.max(0, mark.offsetTop - pane.offsetTop - pane.clientHeight / 2);
  }
  review.scrollToMark = false;
}

function reviewSectionMarkup(section, editable) {
  const rows = state.review.draft[section] || [];
  const approved = state.review.approved.has(section);
  const body = rows.length ? rows.map((row, index) => reviewRowMarkup(section, row, index, editable)).join("") : `<p class="job-meta rr-none">Nothing extracted for this section.</p>`;
  return `<section class="rr-card ${approved ? "approved" : ""}" data-section-card="${section}">
    <div class="rr-card-head"><h3>${REVIEW_LABELS[section]} <span class="job-meta">${rows.length}</span></h3>
      <label class="rr-approve"><input type="checkbox" data-approve-section="${section}" ${approved ? "checked" : ""} ${editable ? "" : "disabled"}><span>${approved ? "Approved" : "Approve section"}</span></label></div>
    <div class="rr-rows">${body}</div>
    ${editable ? `<button class="text-button" type="button" data-add-row="${section}">+ Add ${section === "experience" || section === "education" ? "entry" : section.slice(0, -1)}</button>` : ""}
  </section>`;
}

function reviewRowMarkup(section, row, index, editable) {
  const field = (name, label, value, kind = "text", wide = false) => {
    const key = `${section}.${index}.${name}`;
    const invalid = state.review.invalid.has(key) ? "invalid" : "";
    const attributes = `data-section="${section}" data-index="${index}" data-field="${name}" data-kind="${kind}" ${editable ? "" : "disabled"}`;
    if (kind === "bool") return `<label class="rr-check"><input type="checkbox" ${attributes} ${value ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`;
    if (kind === "lines") return `<label class="tracking-field rr-wide"><span>${escapeHtml(label)}</span><textarea rows="3" ${attributes}>${escapeHtml((value || []).join("\n"))}</textarea></label>`;
    if (kind === "longtext") return `<label class="tracking-field rr-wide"><span>${escapeHtml(label)}</span><textarea rows="2" ${attributes}>${escapeHtml(value || "")}</textarea></label>`;
    const shown = kind === "date" ? formatYearMonth(value) : kind === "list" ? (value || []).join(", ") : value || "";
    const placeholder = kind === "date" ? "YYYY-MM or YYYY" : "";
    return `<label class="tracking-field ${wide ? "rr-wide" : ""} ${invalid}"><span>${escapeHtml(label)}</span><input type="text" value="${escapeHtml(shown)}" placeholder="${placeholder}" maxlength="${kind === "list" ? 600 : 200}" ${attributes}></label>`;
  };
  const evidence = Array.isArray(row.evidence) ? row.evidence : row.evidence ? [row.evidence] : [];
  const sources = evidence.map((item, position) => `<button class="rr-evidence" type="button" data-mark-start="${item.start}" data-mark-end="${item.end}" title="Show in source text">${escapeHtml(truncateText(item.text, 60))}${evidence.length > 1 ? ` <small>${position + 1}/${evidence.length}</small>` : ""}</button>`).join("");
  const remove = editable ? `<button class="text-button rr-remove" type="button" data-remove-row="${section}" data-index="${index}" aria-label="Remove">Remove</button>` : "";
  let fields = "";
  if (section === "skills") {
    const mapped = row.canonical ? `<span class="skill-chip">${escapeHtml(row.canonical)}${row.category ? `<small>${escapeHtml(formatLabel(row.category))}</small>` : ""}</span>` : `<span class="skill-chip rr-unmapped">Not in taxonomy</span>`;
    fields = `${field("name", "Skill", row.name)}<div class="rr-meta">${mapped}<span class="job-meta">${escapeHtml(formatLabel(row.source))}</span></div>`;
  } else if (section === "experience") {
    fields = `${field("title", "Title", row.title)}${field("employer", "Employer", row.employer)}${field("location", "Location", row.location)}${field("start", "Start", row.start, "date")}${field("end", "End", row.end, "date")}${field("current", "Current role", row.current, "bool")}${field("highlights", "Highlights (one per line)", row.highlights, "lines")}`;
  } else if (section === "education") {
    fields = `${field("institution", "Institution", row.institution, "text", true)}${field("degree", "Degree", row.degree)}${field("field_of_study", "Field of study", row.field_of_study)}${field("graduation", "Graduation", row.graduation, "date")}${field("expected", "Expected", row.expected, "bool")}`;
  } else {
    fields = `${field("name", "Project", row.name)}${field("technologies", "Technologies (comma separated)", row.technologies, "list")}${field("description", "Description", row.description, "longtext")}`;
  }
  return `<div class="rr-row rr-${section}">${fields}<div class="rr-row-foot">${sources || `<span class="job-meta">Added by reviewer</span>`}${remove}</div></div>`;
}

function handleReviewInput(event) {
  const target = event.target;
  const review = state.review;
  if (target.matches("[data-review-comment]")) { review.comment = target.value; markReviewDirty(); return; }
  if (target.matches("[data-approve-section]")) {
    if (event.type !== "change") return;
    const section = target.dataset.approveSection;
    if (target.checked) review.approved.add(section); else review.approved.delete(section);
    markReviewDirty();
    renderResumeReview();
    return;
  }
  const section = target.dataset.section;
  if (!section) return;
  const row = review.draft[section][Number(target.dataset.index)];
  const fieldName = target.dataset.field;
  const key = `${section}.${target.dataset.index}.${fieldName}`;
  const kind = target.dataset.kind;
  if (kind === "bool") row[fieldName] = target.checked;
  else if (kind === "lines") row[fieldName] = target.value.split("\n").map((line) => line.trim()).filter(Boolean);
  else if (kind === "list") row[fieldName] = target.value.split(",").map((item) => item.trim()).filter(Boolean);
  else if (kind === "date") {
    const parsed = parseYearMonth(target.value);
    if (parsed === undefined) review.invalid.add(key); else { review.invalid.delete(key); row[fieldName] = parsed; }
    target.closest(".tracking-field").classList.toggle("invalid", parsed === undefined);
  } else row[fieldName] = target.value.trim() === "" && fieldName !== REVIEW_REQUIRED[section] ? null : target.value;
  if (fieldName === "current" && row.current) row.end = null;
  const wasApproved = review.approved.delete(section);
  markReviewDirty();
  if (wasApproved || event.type === "change" && kind === "bool") renderResumeReview();
  else updateReviewActions();
}

function handleReviewClick(event) {
  const review = state.review;
  const markButton = event.target.closest("[data-mark-start]");
  if (markButton) { review.mark = { start: Number(markButton.dataset.markStart), end: Number(markButton.dataset.markEnd) }; review.scrollToMark = true; renderResumeReview(); return; }
  const add = event.target.closest("[data-add-row]");
  if (add) { const section = add.dataset.addRow; review.draft[section].push(REVIEW_BLANK_ROWS[section]()); review.approved.delete(section); markReviewDirty(); renderResumeReview(); return; }
  const remove = event.target.closest("[data-remove-row]");
  if (remove) { const section = remove.dataset.removeRow; review.draft[section].splice(Number(remove.dataset.index), 1); review.invalid = new Set([...review.invalid].filter((key) => !key.startsWith(`${section}.`))); review.approved.delete(section); markReviewDirty(); renderResumeReview(); return; }
  const action = event.target.closest("[data-review-action]");
  if (action) submitResumeReview(action.dataset.reviewAction === "approve");
}

function markReviewDirty() { state.review.dirty = true; state.review.feedback = null; }

function updateReviewActions() {
  const review = state.review;
  const approve = elements.resumeReviewContent.querySelector("[data-review-action='approve']");
  if (approve) approve.disabled = !REVIEW_SECTIONS.every((section) => review.approved.has(section)) || review.busy || review.invalid.size > 0;
  const feedback = elements.resumeReviewContent.querySelector(".rr-actions .save-feedback");
  if (feedback && review.dirty && !review.feedback) { feedback.className = "save-feedback"; feedback.textContent = "Unsaved changes"; }
}

function reviewProblems() {
  const problems = [];
  if (state.review.invalid.size) problems.push("Fix the highlighted dates (use YYYY-MM or YYYY).");
  for (const section of REVIEW_SECTIONS) {
    state.review.draft[section].forEach((row, index) => {
      if (!String(row[REVIEW_REQUIRED[section]] || "").trim()) problems.push(`${REVIEW_LABELS[section]} row ${index + 1} needs a ${REVIEW_REQUIRED[section].replace("_", " ")}.`);
    });
  }
  return problems;
}

async function submitResumeReview(approve) {
  const review = state.review;
  const problems = reviewProblems();
  if (problems.length) { review.feedback = { kind: "error", text: problems[0] }; renderResumeReview(); return; }
  if (approve && !window.confirm("Approve all four sections? The review becomes read-only.")) return;
  review.busy = true;
  review.feedback = { kind: "", text: approve ? "Approving" : "Saving" };
  renderResumeReview();
  try {
    const body = { revision: review.detail.extraction.revision, facts: review.draft, approved_sections: REVIEW_SECTIONS.filter((section) => review.approved.has(section)), review_comment: review.comment };
    const response = await fetch(`/api/resume-reviews/${encodeURIComponent(review.selectedId)}`, { method: approve ? "PUT" : "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Review could not be saved");
    review.busy = false;
    if (approve) {
      await refreshCandidateProfile();
      await loadResumeReviews(review.selectedId);
      state.review.feedback = { kind: "success", text: "Approved" };
      renderResumeReview();
      return;
    }
    review.detail.extraction.revision = result.revision;
    review.dirty = false;
    review.feedback = { kind: "success", text: `Draft saved ${formatTimestamp(result.review_updated_at)}` };
  } catch (error) {
    review.busy = false;
    review.feedback = { kind: "error", text: error.message };
  }
  renderResumeReview();
}

async function refreshCandidateProfile() {
  try {
    const response = await fetch("/api/candidate-profile", { cache: "no-store" });
    if (response.ok) state.candidate = await response.json();
  } catch (error) {
    state.candidate = null;
  }
}

function sourceTextMarkup(text, mark) {
  if (!mark || mark.start < 0 || mark.end > text.length || mark.end <= mark.start) return escapeHtml(text);
  return `${escapeHtml(text.slice(0, mark.start))}<mark id="rrMark">${escapeHtml(text.slice(mark.start, mark.end))}</mark>${escapeHtml(text.slice(mark.end))}`;
}

function formatYearMonth(value) { return value ? (value.month ? `${value.year}-${String(value.month).padStart(2, "0")}` : String(value.year)) : ""; }

function parseYearMonth(raw) {
  const value = raw.trim();
  if (!value) return null;
  const match = value.match(/^(\d{4})(?:-(\d{1,2}))?$/);
  if (!match) return undefined;
  const year = Number(match[1]);
  const month = match[2] ? Number(match[2]) : null;
  if (year < 1950 || year > 2100 || (month !== null && (month < 1 || month > 12))) return undefined;
  return { year, month };
}

function truncateText(value, length) { const text = String(value || "").replace(/\s+/g, " "); return text.length > length ? `${text.slice(0, length - 1)}…` : text; }

function resumeVersionOptions(selectedId) {
  const versions = state.candidate?.resume_versions || [];
  const options = versions.map((version) => `<option value="${escapeHtml(version.resume_version_id)}" ${version.resume_version_id === selectedId ? "selected" : ""}>${escapeHtml(version.label)}${version.is_active ? " (active)" : ""}${version.approved ? "" : " · not reviewed"}</option>`).join("");
  return `<label class="tracking-field"><span>Resume used</span><select name="resume_version_id" id="resumeVersionId" ${versions.length ? "" : "disabled"}><option value="">${versions.length ? "Not recorded" : "No resumes ingested"}</option>${options}</select></label>`;
}
