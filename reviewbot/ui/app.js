// ReviewBot web UI. Plain DOM, no framework, no build step.
// Everything that comes from the API (and therefore from reviewed code and model output) is inserted
// with textContent, never innerHTML.
"use strict";

const SEVERITIES = ["critical", "high", "medium", "low", "nit"];
const ACTIVE = new Set(["queued", "planning", "reviewing"]);
const TOKEN_KEY = "reviewbot.token";

const state = {
  info: null,
  token: readToken(),
  jobId: null,
  job: null,
  timer: null,
  filters: { severities: new Set(SEVERITIES), category: "", file: "", query: "" },
};

// ---------------------------------------------------------------- helpers
function readToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
function saveToken(value) {
  try { value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "style") Object.assign(node.style, v); // CSSOM, allowed by the page's CSP
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const $ = (sel) => document.querySelector(sel);
const fmtInt = (n) => Number(n || 0).toLocaleString("en-US");
const fmtUsd = (n) => (n === null || n === undefined ? "n/a" : `$${Number(n).toFixed(2)}`);
const words = (s) => (s || "").split(/[\s,]+/).map((w) => w.trim()).filter(Boolean);

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    $("#token-row").hidden = false;
    throw new Error("This server needs an access token. Enter it below the form and try again.");
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try { const body = await resp.json(); if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail); } catch { /* not JSON */ }
    throw new Error(detail);
  }
  const type = resp.headers.get("content-type") || "";
  return type.includes("application/json") ? resp.json() : resp.text();
}

// GitHub links are built only from validated pieces.
function githubLink(plan, path, line) {
  if (!plan || !plan.repo_full_name || !plan.commit) return null;
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(plan.repo_full_name)) return null;
  if (!/^[0-9a-f]{7,40}$/.test(plan.commit)) return null;
  const safePath = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${plan.repo_full_name}/blob/${plan.commit}/${safePath}#L${Number(line)}`;
}

// ---------------------------------------------------------------- header + rail
async function loadInfo() {
  const engine = $("#engine");
  try {
    state.info = await api("/api/info");
  } catch (err) {
    engine.textContent = "Can't reach the ReviewBot server.";
    return;
  }
  const i = state.info;
  engine.replaceChildren(
    i.mock
      ? el("span", { class: "mock" }, "Mock reviewer: set ANTHROPIC_API_KEY for real reviews")
      : el("span", {}, "Reviewing with ", el("strong", {}, i.model)),
    el("br"),
    `Budget ${fmtUsd(i.budget_usd)} per review, up to ${i.max_files} files`,
  );
  $("#token-row").hidden = !i.token_required;
  if (i.token_required) $("#token").value = state.token;
}

async function loadJobs() {
  let jobs = [];
  try { jobs = await api("/repo-reviews"); } catch { return; }
  const list = $("#jobs");
  list.replaceChildren(...jobs.map((j) => {
    const pips = el("span", { class: "pips", "aria-hidden": "true" },
      ...SEVERITIES.flatMap((s) => Array.from({ length: Math.min(j.counts?.[s] || 0, 4) },
        () => el("span", { class: "pip", dataset: { sev: s } }))));
    const total = j.counts ? Object.values(j.counts).reduce((a, b) => a + b, 0) : null;
    return el("li", {},
      el("button", { type: "button", "aria-current": j.id === state.jobId ? "true" : "false", onclick: () => openJob(j.id) },
        el("span", { class: "name", title: j.label || j.repo }, j.label || j.repo),
        el("span", { class: "meta" }, el("span", {}, statusWord(j.status)),
          total !== null ? el("span", {}, `${total} ${total === 1 ? "finding" : "findings"}`) : null, pips)));
  }));
  $("#jobs-empty").hidden = jobs.length > 0;
}

function statusWord(status) {
  return {
    queued: "Waiting", planning: "Downloading", planned: "Ready to review", reviewing: "Reviewing",
    done: "Done", rejected: "Stopped", failed: "Failed",
  }[status] || status;
}

// ---------------------------------------------------------------- form
function readForm() {
  const f = new FormData($("#form"));
  const body = { repo: (f.get("repo") || "").trim(), plan_only: true };
  const ref = (f.get("ref") || "").trim();
  const path = (f.get("path") || "").trim();
  if (ref) body.ref = ref;
  if (path) body.path = path;
  const include = words(f.get("include"));
  const exclude = words(f.get("exclude"));
  if (include.length) body.include = include;
  if (exclude.length) body.exclude = exclude;
  const maxFiles = parseInt(f.get("max_files"), 10);
  const budget = parseFloat(f.get("budget_usd"));
  if (maxFiles > 0) body.max_files = maxFiles;
  if (budget > 0) body.budget_usd = budget;
  return body;
}

function showFormError(message) {
  const p = $("#form-error");
  p.textContent = message || "";
  p.hidden = !message;
}

async function submitForm(event) {
  event.preventDefault();
  showFormError("");
  if (state.info?.token_required) {
    state.token = $("#token").value.trim();
    saveToken(state.token);
  }
  const body = readForm();
  if (!body.repo) {
    showFormError("Enter a repository, like pallets/itsdangerous.");
    $("#repo").focus();
    return;
  }
  const button = $("#plan-btn");
  button.disabled = true;
  try {
    const created = await api("/repo-reviews", { method: "POST", body: JSON.stringify(body) });
    resetFilters();
    await openJob(created.id);
  } catch (err) {
    showFormError(err.message);
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- job lifecycle
async function openJob(id) {
  clearTimeout(state.timer);
  if (state.jobId !== id) resetFilters();
  state.jobId = id;
  if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  await poll();
}

async function poll() {
  clearTimeout(state.timer);
  if (!state.jobId) return;
  try {
    state.job = await api(`/repo-reviews/${encodeURIComponent(state.jobId)}`);
  } catch (err) {
    renderMessage("We couldn't load this review", err.message);
    loadJobs();
    return;
  }
  renderJob(state.job);
  loadJobs();
  if (ACTIVE.has(state.job.status)) state.timer = setTimeout(poll, 1000);
}

async function startReview() {
  const button = $("#start-btn");
  if (button) button.disabled = true;
  try {
    await api(`/repo-reviews/${encodeURIComponent(state.jobId)}/start`, { method: "POST" });
    await poll();
  } catch (err) {
    if (button) button.disabled = false;
    const note = $("#start-note");
    if (note) { note.textContent = err.message; note.classList.add("warn"); }
  }
}

async function downloadReport() {
  try {
    const text = await api(`/repo-reviews/${encodeURIComponent(state.jobId)}/report`, { headers: { Accept: "text/markdown" } });
    const name = (state.job.plan?.label || state.job.repo).replace(/[^A-Za-z0-9_.-]+/g, "_") + ".md";
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
    const a = el("a", { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) {
    alertInline(err.message);
  }
}

function alertInline(message) {
  const box = $("#result-note");
  if (box) { box.textContent = message; box.hidden = false; }
}

function editAndRetry() {
  $("#options").open = true;
  $("#repo").focus();
  window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

// ---------------------------------------------------------------- rendering
function renderMessage(title, text) {
  const section = $("#job");
  section.hidden = false;
  section.replaceChildren(el("div", { class: "panel error-panel" }, el("h3", {}, title), el("p", {}, text)));
}

function jobHeader(job) {
  const title = job.plan?.label || job.repo;
  let status = statusWord(job.status);
  if (job.status === "reviewing" && job.progress?.total) status = `Reviewing: ${job.progress.done} of ${job.progress.total} batches`;
  return el("div", { class: "job-head" }, el("h2", {}, title), el("span", { class: "status" }, status));
}

function renderJob(job) {
  const section = $("#job");
  section.hidden = false;
  const parts = [jobHeader(job)];

  if (job.status === "queued" || job.status === "planning") {
    parts.push(el("div", { class: "panel" },
      el("p", { class: "note" }, job.status === "queued" ? "Waiting for the current review to finish." : `Downloading ${job.repo} and picking out the source files.`),
      el("div", { class: "progress-track indeterminate", role: "progressbar", "aria-label": "Downloading" }, el("div", { class: "progress-fill" }))));
  }
  if (job.status === "rejected" || job.status === "failed") {
    parts.push(el("div", { class: "panel error-panel" },
      el("h3", {}, job.status === "rejected" ? "This review didn't start" : "This review failed"),
      el("p", {}, job.error || "No details were returned."),
      el("button", { type: "button", class: "secondary", onclick: editAndRetry }, "Change the options")));
  }
  if (job.plan && job.status !== "done") parts.push(planPanel(job));
  if (job.status === "reviewing") parts.push(progressPanel(job));
  if (job.status === "done") parts.push(...resultsPanels(job));
  section.replaceChildren(...parts);
  if (job.status === "done") renderList(job);
}

function planPanel(job) {
  const p = job.plan;
  const ready = job.status === "planned";
  const figures = el("dl", { class: "figures" },
    figure("Files to review", fmtInt(p.files.length)),
    figure("Lines of code", fmtInt(p.lines)),
    figure("Model calls", fmtInt(p.calls)),
    figure("Estimated cost", p.est_cost_usd === null ? "Free" : `≤ ${fmtUsd(p.est_cost_usd)}`));

  let meter = null;
  if (p.est_cost_usd !== null) {
    const pct = Math.min(100, (p.est_cost_usd / p.budget_usd) * 100);
    meter = el("div", { class: "meter" },
      el("div", { class: "meter-track", role: "meter", "aria-valuemin": "0", "aria-valuemax": String(p.budget_usd),
        "aria-valuenow": String(p.est_cost_usd), "aria-label": "Estimated cost against budget" },
        el("div", { class: `meter-fill${p.over_budget ? " over" : ""}`, style: { width: `${pct.toFixed(1)}%` } })),
      el("div", { class: "meter-legend" },
        el("span", {}, "Estimate assumes no prompt caching, so the real cost is usually lower."),
        el("span", {}, "Budget ", el("strong", {}, fmtUsd(p.budget_usd)))));
  }

  const skipped = Object.entries(p.skipped).sort((a, b) => b[1] - a[1]);
  const lists = el("div", { class: "lists" },
    el("details", {}, el("summary", {}, `${p.files.length} files will be reviewed`),
      el("ul", { class: "files" }, p.files.map((f) => el("li", {}, f)))),
    el("div", {}, el("p", { class: "note" }, skipped.length ? "Skipped" : "Nothing was skipped."),
      skipped.length ? el("ul", {}, skipped.map(([why, n]) => el("li", {}, `${fmtInt(n)} ${why}`))) : null));

  const actions = ready
    ? el("div", { class: "actions" },
        el("button", { type: "button", class: "primary", id: "start-btn", disabled: p.over_budget, onclick: startReview }, "Start review"),
        el("button", { type: "button", class: "secondary", onclick: editAndRetry }, "Change the options"),
        el("p", { class: `note${p.over_budget ? " warn" : ""}`, id: "start-note" },
          p.over_budget
            ? "The estimate is over the budget. Narrow it to a folder or file patterns, or raise the budget."
            : `Runs ${p.chunks} ${p.chunks === 1 ? "batch" : "batches"} through ${state.info?.specialists?.length || 3} specialist reviewers.`))
    : null;

  return el("div", { class: "panel" },
    el("h3", {}, ready ? "Ready to review" : "Review plan"),
    p.commit ? el("p", { class: "note" }, "Pinned to commit ", el("code", {}, p.commit.slice(0, 12))) : null,
    figures, meter, lists, actions);
}

function figure(label, value) {
  return el("div", {}, el("dt", {}, label), el("dd", {}, value));
}

function progressPanel(job) {
  const { done = 0, total = 0 } = job.progress || {};
  const pct = total ? Math.round((done / total) * 100) : 0;
  return el("div", { class: "panel" },
    el("h3", {}, "Reviewing"),
    el("div", { class: "progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct) },
      el("div", { class: "progress-fill", style: { width: `${pct}%` } })),
    el("p", { class: "note" }, `${done} of ${total} batches finished. Each batch goes to every specialist reviewer.`));
}

// ---------------------------------------------------------------- results
function resetFilters() {
  state.filters = { severities: new Set(SEVERITIES), category: "", file: "", query: "" };
}

function resultsPanels(job) {
  const counts = job.counts || {};
  const total = SEVERITIES.reduce((a, s) => a + (counts[s] || 0), 0);
  const f = state.filters;

  const bar = el("div", { class: "severity-bar", role: "group", "aria-label": "Show severities" },
    SEVERITIES.map((s) => el("button", {
      type: "button", class: "sev-toggle", dataset: { sev: s }, disabled: !(counts[s] > 0),
      "aria-pressed": String(f.severities.has(s)),
      onclick: () => { f.severities.has(s) ? f.severities.delete(s) : f.severities.add(s); renderJob(job); },
    }, el("span", { class: "n" }, String(counts[s] || 0)), s)));

  const categories = [...new Set(job.findings.map((x) => x.category))].sort();
  const files = [...new Set(job.findings.map((x) => x.path))].sort();
  const search = el("input", { type: "text", placeholder: "Search findings", value: f.query, "aria-label": "Search findings" });
  search.addEventListener("input", () => { f.query = search.value; renderList(job); });
  const filters = el("div", { class: "filters" },
    select("All categories", categories, f.category, (v) => { f.category = v; renderList(job); }),
    select("All files", files, f.file, (v) => { f.file = v; renderList(job); }),
    search);

  const head = el("div", { class: "result-actions" },
    el("p", { class: "note" }, total === 0
      ? "No issues above the confidence threshold."
      : `${total} ${total === 1 ? "finding" : "findings"} in ${files.length} ${files.length === 1 ? "file" : "files"}.`),
    el("button", { type: "button", class: "secondary", onclick: downloadReport }, "Download report"));

  const errors = job.errors?.length
    ? el("div", { class: "panel error-panel" }, el("h3", {}, "Some review calls failed"),
        el("p", {}, "These batches weren't fully reviewed, so the results may be incomplete."),
        el("ul", {}, job.errors.map((e) => el("li", {}, e))))
    : null;

  const u = job.usage || {};
  const coverage = el("div", { class: "coverage" },
    el("span", {}, `${fmtInt(job.plan?.files.length)} files reviewed at commit `, el("code", {}, (job.plan?.commit || "").slice(0, 12))),
    el("span", {}, `${fmtInt(u.calls)} model calls, ${fmtInt(u.input_tokens)} input and ${fmtInt(u.output_tokens)} output tokens, ${fmtInt(u.cache_read_input_tokens)} read from cache. Model: ${job.model || "unknown"}.`));

  return [
    head,
    errors,
    el("p", { id: "result-note", class: "form-error", hidden: true }),
    total ? bar : null,
    total ? filters : null,
    el("ol", { class: "findings", id: "findings" }),
    coverage,
  ].filter(Boolean);
}

function select(allLabel, options, value, onChange) {
  const s = el("select", { "aria-label": allLabel }, el("option", { value: "" }, allLabel),
    options.map((o) => el("option", { value: o, selected: o === value }, o)));
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

function renderList(job) {
  const list = $("#findings");
  if (!list) return;
  const f = state.filters;
  const q = f.query.trim().toLowerCase();
  const shown = job.findings
    .filter((x) => f.severities.has(x.severity))
    .filter((x) => !f.category || x.category === f.category)
    .filter((x) => !f.file || x.path === f.file)
    .filter((x) => !q || `${x.title} ${x.explanation} ${x.path}`.toLowerCase().includes(q))
    .sort((a, b) => SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity) || a.path.localeCompare(b.path) || a.line - b.line);

  if (!job.findings.length) {
    list.replaceChildren(el("li", { class: "empty" }, "Nothing to report at this confidence level."));
    return;
  }
  if (!shown.length) {
    list.replaceChildren(el("li", { class: "empty" }, "No findings match these filters."));
    return;
  }
  list.replaceChildren(...shown.map((x) => findingItem(job, x)));
}

function findingItem(job, x) {
  const href = githubLink(job.plan, x.path, x.line);
  const loc = `${x.path}:${x.line}`;
  return el("li", { class: "finding", dataset: { sev: x.severity } },
    el("div", { class: "gutter", "aria-hidden": "true" }, el("span", { class: "line" }, String(x.line)), el("span", { class: "mark" })),
    el("div", { class: "finding-body" },
      el("h4", {}, x.title),
      el("p", { class: "where" },
        href ? el("a", { class: "loc", href, target: "_blank", rel: "noopener noreferrer" }, loc) : el("span", { class: "loc" }, loc),
        el("span", { class: "sev" }, x.severity),
        el("span", {}, x.category),
        el("span", {}, `${x.reviewers.join(", ")}, confidence ${Number(x.confidence).toFixed(2)}`)),
      el("p", {}, x.explanation),
      x.suggestion && x.suggestion.trim() ? el("pre", {}, el("code", {}, x.suggestion)) : null));
}

// ---------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", async () => {
  $("#form").addEventListener("submit", submitForm);
  $("#token").addEventListener("change", (e) => { state.token = e.target.value.trim(); saveToken(state.token); loadJobs(); });
  await loadInfo();
  await loadJobs();
  const id = location.hash.slice(1);
  if (/^[0-9a-f]{12}$/.test(id)) openJob(id);
});
