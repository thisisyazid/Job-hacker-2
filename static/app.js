// Job Hacker frontend — no build step, plain ES modules.

const STAGES = [
  { id: "saved", label: "Saved" },
  { id: "referral", label: "Referral" },  // referred by a person
  { id: "applied", label: "Applied" },
  { id: "interviewing", label: "Interviewing" },
  { id: "offer", label: "Offer" },
  { id: "closed", label: "Closed" },
];
const STAGE = Object.fromEntries(STAGES.map((s) => [s.id, s]));
const stageColor = (id) => `var(--st-${id})`;

const state = {
  session: null,
  jobs: null,
  filter: localGet("jh.filter") || "active",
  sort: localGet("jh.sort") || "updated",
  dir: localGet("jh.dir") || "desc",
  q: "",
  shown: 0,  // how many rows of the job table are drawn; more are added as you scroll
  selected: new Set(),  // job ids ticked in the select column, for bulk actions
};
let lastRows = [];
// Filters combine: tap one to switch it on, tap again to switch it off. Stages are OR'd together (Saved or Applied),
// and Tonight / Reposted / No description narrow that down. All shows everything.
state.filters = new Set((localGet("jh.filters") || state.filter || "active").split(",").filter(Boolean));
function fOn(id) { return state.filters.has(id); }
function fOnly(id) { return state.filters.size === 1 && state.filters.has(id); }
function setFilters(ids) {
  state.filters = new Set(ids.length ? ids : ["active"]);
  state.filter = state.filters.size === 1 ? [...state.filters][0] : "multi";
  localSet("jh.filters", [...state.filters].join(","));
  localSet("jh.filter", state.filter);
}
// Tap a filter to switch it on, tap again to switch it off. All resets. Active is the default:
// the first filter tapped replaces it, a stage replaces it, and tapping Active clears the stages.
function toggleFilter(id) {
  const f = new Set(state.filters);
  const stageIds = STAGES.map((st) => st.id);
  if (id === "all") return ["all"];
  if (f.has(id)) { f.delete(id); return [...f]; }
  f.delete("all");
  if (id === "active") stageIds.forEach((x) => f.delete(x));
  else if (stageIds.includes(id) || (f.size === 1 && f.has("active"))) f.delete("active");
  f.add(id);
  return [...f];
}
function jobPasses(j) {
  const f = state.filters;
  if (f.has("all")) return true;
  if (f.has("archived") ? !j.archived_at : j.archived_at) return false;
  const stages = STAGES.map((st) => st.id).filter((id) => f.has(id));
  if (f.has("active")) stages.push(...STAGES.map((st) => st.id).filter((id) => id !== "closed"));
  if (stages.length && !stages.includes(j.status)) return false;
  if (f.has("tonight") && !j.queued) return false;
  if (f.has("reposted") && !j.reposted_at) return false;
  if (f.has("nodesc") && (j.has_description || looksLikeUrl(j.title))) return false;
  return true;
}  // the job table's current rows (filtered and sorted), for "select all"
const PAGE = 100;

const $main = document.getElementById("main");

// ---------- utilities ----------

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function html(strings, ...vals) {
  return strings.reduce((out, s, i) => {
    let v = vals[i - 1];
    const isRaw = (x) => x !== null && typeof x === "object" && "__raw" in x;
    if (isRaw(v)) v = v.__raw;
    else if (Array.isArray(v)) v = v.map((x) => isRaw(x) ? x.__raw : esc(x)).join("");
    else v = esc(v);
    return out + v + s;
  });
}
const raw = (s) => ({ __raw: s });

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", headers: {}, ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch("/api" + path, init);
  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("json")) data = await res.json();
  if (res.status === 401 && !path.startsWith("/login") && !path.startsWith("/session")) {
    state.session = { ...(state.session || {}), logged_in: false };
    renderLogin();
    throw new Error("Log in to continue.");
  }
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.reason)) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, kind = "") {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = kind), Math.max(kind === "error" ? 5000 : 2600, String(msg).length * 55));
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return "now";
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  const d = Math.floor(s / 86400);
  if (d < 60) return `${d}d`;
  return `${Math.floor(d / 30)}mo`;
}
// Same rule as the server: a title that is really a link means the posting was taken down.
const looksLikeUrl = (t) => /(^\s*(https?:\/\/|www\.)|:\/\/|[?&](trackingId|refId|eBP|currentJobId)=|^[\w.-]+\.(com|sg|net|org|io|co)\/\S*$)/i.test(t || "");

async function removeDeadJobs(n) {
  if (!confirm(`Remove ${n} job${n === 1 ? "" : "s"} whose title is just a link? These are postings that were taken down. This can't be undone.`)) return false;
  try {
    const r = await api("/cleanup/dead-links", { method: "POST" });
    toast(`Removed ${r.removed} taken-down job${r.removed === 1 ? "" : "s"}`);
    state.jobs = null;
    return true;
  } catch (e) { toast(e.message, "error"); return false; }
}

const isoDate = (s) => (/^\d{4}-\d{2}-\d{2}$/.test(s || "") ? s : "");
// "27 Apr" this year, "27 Apr 2025" otherwise; text that isn't a date is shown as is.
function fmtPosted(s, long = false) {
  if (!isoDate(s)) return s || "";
  const d = new Date(s + "T00:00:00");
  if (long) return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  // earlier years: month and year is enough in a list ("Dec 2025")
  if (d.getFullYear() !== new Date().getFullYear()) return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
// A UTC timestamp as the viewer's local calendar day (YYYY-MM-DD).
function localDay(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function longDate(iso) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
}
function fitColor(n) { return n >= 7 ? "var(--fit-high)" : n >= 5 ? "var(--fit-mid)" : "var(--fit-low)"; }

function busy(btn, label) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${esc(label)}</span>`;
  return () => { btn.disabled = false; btn.innerHTML = old; };
}

// Full-screen "working" card for AI calls, so a slow request never looks like a crash.
// Shows a spinning donut, what's happening, a typical duration and a live timer.
function aiWait(title, detail, typical, who = "Claude", slowAfter = 90) {
  const el = document.createElement("div");
  el.className = "aiwait";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.innerHTML = html`
    <div class="aiwait-card">
      <svg class="donut" viewBox="0 0 80 80" aria-hidden="true">
        <circle class="donut-track" cx="40" cy="40" r="32"/>
        <circle class="donut-arc" cx="40" cy="40" r="32"/>
      </svg>
      <h2>${title}</h2>
      <p class="aiwait-detail">${detail}</p>
      <p class="aiwait-time"><span id="aw-clock">0:00</span><span class="aiwait-typ">${typical ? ` · usually ${typical}` : ""}</span></p>
      <p class="aiwait-slow" hidden>Taking longer than usual. Still waiting for ${who}, so keep this open.</p>
    </div>`;
  document.body.append(el);
  document.body.classList.add("waiting");
  const t0 = Date.now();
  const clock = el.querySelector("#aw-clock");
  const slow = el.querySelector(".aiwait-slow");
  const timer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    if (s >= slowAfter) slow.hidden = false;
  }, 250);
  requestAnimationFrame(() => el.classList.add("show"));
  return () => {
    clearInterval(timer);
    document.body.classList.remove("waiting");
    el.classList.remove("show");
    setTimeout(() => el.remove(), 200);
  };
}

// Run an AI request with the waiting card up; always takes it down again.
// opts: { title, detail: (who) => text, typical: { claude, local }, engine }
async function withAI(opts, fn) {
  const eng = opts.engine || currentEngine();
  const who = eng === "local" ? "your local model" : "Claude";
  const Who = who[0].toUpperCase() + who.slice(1);
  const detail = typeof opts.detail === "function" ? opts.detail(Who, who) : opts.detail;
  const typical = typeof opts.typical === "object" ? opts.typical[eng] : opts.typical;
  const stop = aiWait(opts.title, detail, typical, who, eng === "local" ? 300 : 90);
  try { return await fn(eng); } finally { stop(); }
}

// Ask before filling in a job: may AI be used if free reading finds nothing?
// Resolves to "local" | "claude" | "none", or null when cancelled.
function askFillEngine(count = 1, intro = "") {
  const all = engines();
  const opts = [
    all.local?.available && { id: "local", name: `Local (${all.local.model})`, note: "Free. Runs on your own computer, so it's slower." },
    all.claude?.available && { id: "claude", name: "Claude", note: "Fast. Uses a few cents of API credit for this job." },
    { id: "none", name: "No AI", note: "Only the free reading. If that finds nothing, paste the details yourself." },
  ].filter(Boolean);
  const saved = localGet("jh.fillEngine");
  let pick = opts.some((o) => o.id === saved) ? saved : (opts.find((o) => o.id === currentEngine()) || opts[0]).id;
  const dlg = document.createElement("dialog");
  dlg.className = "pick";
  dlg.innerHTML = html`
    <form method="dialog">
      <h2>${count > 1 ? `Fill in ${count} jobs` : "Fill in missing details"}</h2>
      ${intro ? raw(html`<p><strong>${intro}</strong></p>`) : ""}
      <p>Job Hacker reads the posting first. That's free and works for LinkedIn, MyCareersFuture and most career pages.</p>
      <p><strong>If that finds nothing, may it use AI as a last resort?</strong></p>
      <div class="pick-opts" role="radiogroup" aria-label="AI as a last resort">
        ${opts.map((o) => raw(html`<button type="button" role="radio" data-pick="${o.id}" aria-checked="${o.id === pick}"><span class="pick-n">${o.name}</span><span class="pick-d">${o.note}</span></button>`))}
      </div>
      <div class="row pick-actions">
        <button class="btn btn-ghost" value="cancel">Cancel</button>
        <button class="btn btn-primary" value="go">Fill in</button>
      </div>
    </form>`;
  document.body.append(dlg);
  dlg.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
    pick = b.dataset.pick;
    dlg.querySelectorAll("[data-pick]").forEach((x) => x.setAttribute("aria-checked", x === b));
  }));
  dlg.showModal();
  dlg.querySelector('[aria-checked="true"]')?.focus();
  return new Promise((resolve) => dlg.addEventListener("close", () => {
    const go = dlg.returnValue === "go";
    dlg.remove();
    if (go) localSet("jh.fillEngine", pick);
    resolve(go ? pick : null);
  }));
}

const FILL_LABELS = { title: "title", description: "description", company: "company", location: "location", salary: "salary", employment_type: "job type", posted_date: "posted date" };

// Re-read a job's posting and fill only its empty fields (nothing already there
// is changed). Asks first whether AI may be used. Returns the updated job, or null.
async function fillJob(id) {
  const eng = await askFillEngine();
  if (!eng) return null;
  const who = eng === "local" ? "your local model" : "Claude";
  const stop = aiWait("Filling in missing details",
    eng === "none" ? "Reading the posting. AI won't be used." : `Reading the posting. If that finds nothing, ${who} picks the details out of the page.`,
    eng === "local" ? "5 seconds to a few minutes" : "5–30 seconds", eng === "none" ? "the job site" : who, eng === "local" ? 300 : 60);
  let r;
  try { r = await api(`/jobs/${id}/refresh`, { method: "POST", json: { engine: eng } }); }
  catch (e) { stop(); toast(e.message, "error"); return null; }
  stop();
  const via = r.method ? (/AI/.test(r.method) ? ` (with ${who})` : " (no AI needed)") : "";
  if (r.status === "updated") {
    toast(`Filled in${via}: ${r.filled.map((k) => FILL_LABELS[k] || k.replace("_", " ")).join(", ")}`);
    return r.job;
  }
  toast(r.status === "unchanged" ? `Nothing new on the posting${via}. Paste the details with Edit details instead.`
    : `${r.reason}${r.ai_allowed === false ? " Try again with AI, or" : ""} Open the posting, copy the text and paste it with Edit details.`,
    r.status === "failed" ? "error" : "");
  return null;
}

// Fill in several jobs one after another, with one question about AI up front,
// a progress card with a Stop button, and a summary at the end. Returns the updated jobs.
async function fillJobs(ids, intro = "") {
  if (!ids.length) return [];
  const eng = await askFillEngine(ids.length, intro);
  if (!eng) return [];
  const who = eng === "local" ? "your local model" : "Claude";
  const stop = aiWait(`Filling in ${ids.length} job${ids.length === 1 ? "" : "s"}`, "Starting…",
    eng === "local" ? "up to a few minutes each" : "5–30 seconds each", eng === "none" ? "the job site" : who, 100000);
  const card = document.querySelector(".aiwait-card");
  const detail = card?.querySelector(".aiwait-detail");
  let stopped = false;
  card?.insertAdjacentHTML("beforeend", '<div class="row" style="justify-content:center;margin-top:14px"><button class="btn btn-s" id="fill-stop">Stop after this job</button></div>');
  card?.querySelector("#fill-stop")?.addEventListener("click", (e) => { stopped = true; e.target.disabled = true; e.target.textContent = "Stopping…"; });
  const updated = [];
  let nothing = 0, failed = 0, done = 0, ai = 0;
  for (const id of ids) {
    if (stopped) break;
    const j = state.jobs?.find((x) => x.id === id);
    if (detail) detail.textContent = `${done + 1} of ${ids.length}: ${j ? [j.title, j.company].filter(Boolean).join(", ") : ""}`;
    try {
      const r = await api(`/jobs/${id}/refresh`, { method: "POST", json: { engine: eng } });
      if (r.status === "updated") { updated.push(r.job); if (/AI/.test(r.method || "")) ai++; }
      else if (r.status === "unchanged") nothing++;
      else failed++;
    } catch { failed++; }
    done++;
    if (done < ids.length && !stopped) await new Promise((res) => setTimeout(res, 600));  // be gentle with job sites
  }
  stop();
  const parts = [`Filled in ${updated.length} of ${done} job${done === 1 ? "" : "s"}${ai ? ` (${ai} with ${who})` : ""}`];
  if (nothing) parts.push(`${nothing} had nothing new`);
  if (failed) parts.push(`${failed} couldn't be read`);
  if (stopped && done < ids.length) parts.push(`stopped with ${ids.length - done} left`);
  toast(parts.join(", ") + (nothing || failed ? ". Details are in today's log (Settings → Log files)." : "."), failed && !updated.length ? "error" : "");
  return updated;
}

// ---------- AI engine choice (Claude or the local Ollama model) ----------

function engines() { return state.session?.engines || {}; }
function engineName(e) { return e === "local" ? `Local (${engines().local?.model || "Ollama"})` : "Claude"; }
function currentEngine() {
  const all = engines();
  const saved = localGet("jh.engine");
  if (saved && all[saved]?.available) return saved;
  return all.default || null;
}
function enginePicker() {
  const all = engines();
  const cur = currentEngine();
  if (all.claude?.available && all.local?.available) {
    return html`<div class="engine" role="radiogroup" aria-label="AI model">
      <span class="engine-l">Use</span>
      <button type="button" role="radio" data-engine="claude" aria-checked="${cur === "claude"}">Claude</button>
      <button type="button" role="radio" data-engine="local" aria-checked="${cur === "local"}">Local <span class="engine-m">${all.local.model}</span></button>
    </div>`;
  }
  return cur ? html`<p class="engine-one">Using ${engineName(cur)}</p>` : "";
}
function bindEnginePickers(root = document) {
  root.querySelectorAll(".engine [data-engine]").forEach((b) => b.addEventListener("click", () => {
    localSet("jh.engine", b.dataset.engine);
    document.querySelectorAll(".engine [data-engine]").forEach((x) => x.setAttribute("aria-checked", x.dataset.engine === b.dataset.engine));
  }));
}

function setView(markup, { keep = false } = {}) {
  $main.innerHTML = markup;
  if (!keep) {
    $main.firstElementChild?.classList.add("view-enter");
    window.scrollTo(0, 0);
  }
}

const icon = {
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
  down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
  ext: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
};

// ---------- routing ----------

function parseHash() {
  const h = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = h.split("?");
  return { parts: path.split("/").filter(Boolean), params: new URLSearchParams(qs || "") };
}

async function route() {
  if (!state.session) await loadSession();
  if (state.session.auth_required && !state.session.logged_in) return renderLogin();
  const { parts, params } = parseHash();
  document.querySelectorAll("[data-nav]").forEach((a) => a.removeAttribute("aria-current"));
  document.body.classList.toggle("wide", !parts.length || !["job", "add", "settings"].includes(parts[0]));
  try {
    if (parts[0] === "job" && parts[1]) return await renderJob(Number(parts[1]));
    if (parts[0] === "add") { markNav("add"); return renderAdd(params); }
    if (parts[0] === "settings") { markNav("settings"); return await renderSettings(params); }
    return await renderList();
  } catch (e) {
    if (e.status === 401) return;
    setView(html`<div class="empty"><h2>Something went wrong</h2><p>${e.message}</p><a class="btn" href="#/">Back to jobs</a></div>`);
  }
}
function markNav(n) { document.querySelector(`[data-nav=${n}]`)?.setAttribute("aria-current", "page"); }

async function loadSession() {
  state.session = await api("/session");
}

// ---------- login ----------

function renderLogin() {
  setView(html`
    <form class="login" id="login">
      <h1>Welcome back</h1>
      <p>Enter the password you set for this app.</p>
      <div class="field">
        <label for="pw">Password</label>
        <input id="pw" type="password" autocomplete="current-password" required autofocus>
      </div>
      <button class="btn btn-primary btn-wide" type="submit">Log in</button>
      <p class="hint" id="login-err" role="alert"></p>
    </form>`);
  document.getElementById("login").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.submitter;
    const done = busy(btn, "Logging in");
    try {
      await api("/login", { json: { password: document.getElementById("pw").value } });
      state.session = null;
      route();
    } catch (err) {
      document.getElementById("login-err").textContent = err.message;
      done();
    }
  });
}

// ---------- list ----------

async function renderList(keep = false) {
  if (!state.jobs) {
    setView('<p class="hint">Loading your jobs…</p>');
    state.jobs = await api("/jobs");
  }
  const jobs = state.jobs;
  // Archived jobs are kept for searching later but left out of the everyday counts.
  const live = jobs.filter((j) => !j.archived_at);
  const archived = jobs.length - live.length;
  const counts = Object.fromEntries(STAGES.map((s) => [s.id, 0]));
  live.forEach((j) => counts[j.status] = (counts[j.status] || 0) + 1);
  const active = live.length - counts.closed;
  const picked = live.filter((j) => j.queued).length;
  const reposted = live.filter((j) => j.reposted_at).length;
  const nodesc = live.filter((j) => !j.has_description && !looksLikeUrl(j.title)).length;
  const y = scrollY;
  const focusQ = document.activeElement?.id === "q";

  setView(html`
    <div>
      <div class="listbar">
        <form class="quickadd" id="quickadd" autocomplete="off">
          <input type="url" inputmode="url" name="url" placeholder="Paste a job link to add it" aria-label="Job link" required>
          <button class="btn btn-primary btn-s" type="submit">Add</button>
        </form>
        ${jobs.length ? raw(html`<input type="search" id="q" class="search" placeholder="Search jobs" enterkeyhint="search" autocomplete="off" value="${state.q}" aria-label="Search title, company, location">`) : ""}
      </div>

      ${jobs.filter((j) => looksLikeUrl(j.title)).length ? raw(html`
      <div class="callout dead-callout"><p><strong>${(() => { const n = jobs.filter((j) => looksLikeUrl(j.title)).length; return n === 1 ? "1 job looks taken down:" : `${n} jobs look taken down:`; })()}</strong> the title is just a link.</p>
        <button class="btn btn-s" id="rm-dead">Remove them</button></div>`) : ""}
      ${jobs.length ? raw(html`
      <section class="flow ${fOnly("all") || fOnly("active") ? "" : "filtered"}" aria-label="Pipeline">
        <div class="flow-bar">
          ${STAGES.filter((s) => counts[s.id]).map((s) => raw(html`<button class="flow-seg ${fOn(s.id) ? "on" : ""}" data-st="${s.id}" data-filter="${s.id}" style="--n:${counts[s.id]};--c:${raw(stageColor(s.id))}" aria-label="${s.label}: ${counts[s.id]}"></button>`))}
        </div>
        <div class="flow-legend" role="group" aria-label="Filter by stage">
          <button class="stage" data-filter="active" aria-pressed="${fOn("active")}">Active <span class="n">${active}</span></button>
          <button class="stage stage-tn" data-filter="tonight" aria-pressed="${fOn("tonight")}"><span class="dot"></span>Tonight <span class="n">${picked}</span></button>
          ${reposted || fOn("reposted") ? raw(html`<button class="stage stage-rp" data-filter="reposted" aria-pressed="${fOn("reposted")}" title="Jobs an import showed were posted again, in any stage"><span class="dot"></span>Reposted <span class="n">${reposted}</span></button>`) : ""}
          ${STAGES.map((s) => raw(html`<button class="stage" data-filter="${s.id}" aria-pressed="${fOn(s.id)}" style="--c:${raw(stageColor(s.id))}"><span class="dot"></span>${s.label} <span class="n">${counts[s.id]}</span></button>`))}
          ${nodesc || fOn("nodesc") ? raw(html`<button class="stage stage-nd" data-filter="nodesc" aria-pressed="${fOn("nodesc")}" title="Jobs without a description. Select them and use Fill in"><span class="dot"></span>No description <span class="n">${nodesc}</span></button>`) : ""}
          ${archived || fOn("archived") ? raw(html`<button class="stage stage-ar" data-filter="archived" aria-pressed="${fOn("archived")}" title="Jobs you've archived, kept for searching later"><span class="dot"></span>Archived <span class="n">${archived}</span></button>`) : ""}
          <button class="stage" data-filter="all" aria-pressed="${fOn("all")}" title="Every job, archived ones included">All <span class="n">${jobs.length}</span></button>
        </div>
      </section>
      ${fOnly("closed") && counts.closed ? raw(html`<div class="archive-bar"><p>Done with these? Archive them to clear them out of the way. They stay searchable under <strong>Archived</strong>.</p>
        <button class="btn btn-s" id="ar-closed">Archive all ${counts.closed} closed job${counts.closed === 1 ? "" : "s"}</button></div>`) : ""}
      ${fOn("nodesc") && nodesc ? raw(html`<div class="archive-bar"><p>Tick the jobs to fill in (or the box at the top of the table for all of them), then click <strong>Fill in</strong> in the bar at the bottom.</p></div>`) : ""}
      ${fOn("archived") && archived ? raw(html`<div class="archive-bar"><p>Archived jobs keep their stage and everything in them. Search them here, or open one to unarchive it.</p></div>`) : ""}
      <div class="tonight-bar" id="tonight-bar">
        <p class="tn-sum"><strong>Tonight's list:</strong> <span id="tn-sum">${picked ? `${picked} job${picked === 1 ? "" : "s"} picked.` : "empty. Tick jobs in the Tonight column."}</span></p>
        <div class="tn-tools">
          <button class="btn btn-ghost btn-s" id="tn-pick">Pick saved jobs without a resume</button>
          ${picked ? raw('<button class="btn btn-ghost btn-s" id="tn-clear">Clear</button>') : ""}
          <button class="btn btn-s" id="tn-run" ${picked ? "" : "disabled"}>Run now</button>
          <a class="btn btn-ghost btn-s" href="#/settings?at=nightly">Schedule</a>
        </div>
      </div>
      <div class="sel-bar" id="sel-bar" hidden role="region" aria-label="Selected jobs">
        <p><strong id="sel-n">0</strong> <span id="sel-what">selected</span></p>
        <div class="sel-tools">
          <select id="sel-stage" class="sel-stage" aria-label="Move the selected jobs to a stage">
            <option value="">Move to…</option>
            ${STAGES.map((st) => raw(html`<option value="${st.id}">${st.label}</option>`))}
          </select>
          <button class="btn btn-s btn-primary" id="sel-fill">Fill in</button>
          <button class="btn btn-s" id="sel-tn-add">Tonight</button>
          <button class="btn btn-s" id="sel-tn-rm">Off tonight</button>
          <button class="btn btn-s" id="sel-archive">Archive</button>
          <button class="btn btn-s" id="sel-unarchive">Unarchive</button>
          <button class="btn btn-ghost btn-s" id="sel-clear">Clear</button>
        </div>
      </div>
      <p class="hint sel-hint">Click a row to select it, <span class="kbd">Ctrl</span>+click to add more, <span class="kbd">Shift</span>+click for a range, then right-click for actions. Click a title, or double-click a row, to open the job. Tap several filters above to combine them; tap one again to switch it off.</p>
      <div class="sheet-wrap">
        <table class="sheet">
          <thead><tr>
            ${SHEET_COLS.map((c) => raw(c.cls === "c-sel"
              ? html`<th class="c-sel"><input type="checkbox" id="sel-all" aria-label="Select every job shown" title="Select every job shown"></th>`
              : c.sort
              ? html`<th class="${c.cls}" aria-sort="${state.sort === c.sort ? (state.dir === "asc" ? "ascending" : "descending") : "none"}"><button data-sort="${c.sort}">${c.label}<span class="arr" aria-hidden="true">${state.sort === c.sort ? (state.dir === "asc" ? "↑" : "↓") : ""}</span></button></th>`
              : html`<th class="${c.cls}">${c.label}</th>`))}
          </tr></thead>
          <tbody id="jobs"></tbody>
        </table>
      </div>
      <p class="hint sheet-count" id="sheet-count"></p>`) : raw(html`
      <div class="empty">
        <h2>Your pipeline is empty</h2>
        <p>Paste a link to a job posting above. LinkedIn, MyCareersFuture, company career pages and most job boards work. If a site won't share its details, you can paste the post's text instead.</p>
        <p>Have a list already? Use the <a href="#/add?mode=bulk">Bulk</a> tab to paste many links or import a CSV.</p>
      </div>`)}
    </div>`, { keep });

  document.getElementById("quickadd").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = e.target.url.value.trim();
    if (url) location.hash = "#/add?url=" + encodeURIComponent(url);
  });
  document.getElementById("rm-dead")?.addEventListener("click", async () => {
    if (await removeDeadJobs(jobs.filter((j) => looksLikeUrl(j.title)).length)) renderList(true);
  });
  if (!jobs.length) return;
  $main.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    setFilters(toggleFilter(b.dataset.filter));
    state.shown = 0;
    renderList(true);
  }));
  const q = document.getElementById("q");
  let qTimer;
  q.addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.q = q.value; state.shown = 0; drawRows(); scrollTo(0, 0); }, 180);
  });
  if (focusQ) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  $main.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.sort;
    if (state.sort === k) state.dir = state.dir === "asc" ? "desc" : "asc";
    else { state.sort = k; state.dir = ["updated", "fit", "posted", "applied", "tonight"].includes(k) ? "desc" : "asc"; }
    localSet("jh.sort", state.sort); localSet("jh.dir", state.dir);
    state.shown = 0;
    renderList(true);
  }));
  const tbody = document.getElementById("jobs");
  tbody.addEventListener("click", (e) => {
    const sel = e.target.closest("td.c-sel");
    if (sel) { if (e.target.tagName !== "INPUT") sel.querySelector("input")?.click(); return; }
    const tn = e.target.closest("td.c-tn");
    if (tn) { if (e.target.tagName !== "INPUT") tn.querySelector("input:not(:disabled)")?.click(); return; }
    const fill = e.target.closest("[data-fillrow]");
    if (fill) {
      const id = Number(fill.dataset.fillrow);
      fillJob(id).then((updated) => {
        if (!updated) return;
        const i = state.jobs.findIndex((x) => x.id === id);
        if (i >= 0) state.jobs[i] = { ...state.jobs[i], ...updated };
        renderList(true);
      });
      return;
    }
    if (e.target.closest("select, a, button, input")) return;
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    // Touch screens: a tap opens the job (select with the tick boxes). Mouse: click selects,
    // like a spreadsheet; the title link or a double-click opens the job.
    if (!fineMouse()) { location.hash = `#/job/${tr.dataset.id}`; return; }
    selectRow(Number(tr.dataset.id), e);
  });
  tbody.addEventListener("dblclick", (e) => {
    if (e.target.closest("select, a, button, input, td.c-sel, td.c-tn")) return;
    const tr = e.target.closest("tr[data-id]");
    if (tr) location.hash = `#/job/${tr.dataset.id}`;
  });
  tbody.addEventListener("mousedown", (e) => {  // no text highlighting on shift-click
    if (e.shiftKey && !e.target.closest("input, select, textarea")) e.preventDefault();
  });
  tbody.addEventListener("contextmenu", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || e.target.closest("input, select, textarea")) return;
    e.preventDefault();
    const id = Number(tr.dataset.id);
    if (!state.selected.has(id)) { state.selected.clear(); state.selected.add(id); selAnchor = id; syncSel(); }
    openRowMenu(e.clientX, e.clientY);
  });
  tbody.addEventListener("change", (e) => {
    const pick = e.target.closest("input[data-sel]");
    if (pick) {
      const id = Number(pick.dataset.sel);
      if (pick.checked) state.selected.add(id); else state.selected.delete(id);
      selAnchor = id;
      pick.closest("tr").classList.toggle("picked", pick.checked);
      return drawSelBar();
    }
    const box = e.target.closest("input[data-q]");
    if (box) queueJobs([Number(box.dataset.q)], box.checked);
  });
  document.getElementById("ar-closed")?.addEventListener("click", async (e) => {
    const ids = live.filter((j) => j.status === "closed").map((j) => j.id);
    if (!confirm(`Archive ${ids.length} closed job${ids.length === 1 ? "" : "s"}? You can find them under Archived and unarchive any of them later.`)) return;
    e.currentTarget.disabled = true;
    try {
      const r = await api("/jobs/archive", { json: { job_ids: ids, archived: true } });
      const at = new Date().toISOString();
      state.jobs.forEach((j) => { if (ids.includes(j.id)) { j.archived_at = at; j.queued = false; } });
      toast(`Archived ${r.changed} job${r.changed === 1 ? "" : "s"}`);
    } catch (err) { toast(err.message, "error"); }
    state.shown = 0;
    renderList();
  });
  document.getElementById("tn-pick").addEventListener("click", () => {
    const ids = live.filter((j) => j.status === "saved" && !j.has_resume && j.has_description && !j.queued).map((j) => j.id);
    if (!ids.length) return toast("No saved jobs without a resume (that have a description) to add.");
    queueJobs(ids, true);
  });
  document.getElementById("tn-clear")?.addEventListener("click", () => queueJobs(jobs.filter((j) => j.queued).map((j) => j.id), false));
  document.getElementById("tn-run").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api("/nightly/run", { method: "POST" }); toast("Started. Resumes appear on each job as they're made."); }
    catch (err) { toast(err.message, "error"); }
    drawTonightBar();
  });
  drawTonightBar();
  document.getElementById("sel-all").addEventListener("change", (e) => {
    const ids = lastRows.map((j) => j.id);
    if (e.target.checked) ids.forEach((id) => state.selected.add(id)); else ids.forEach((id) => state.selected.delete(id));
    tbody.querySelectorAll("input[data-sel]").forEach((b) => { b.checked = e.target.checked; b.closest("tr").classList.toggle("picked", b.checked); });
    drawSelBar();
  });
  document.getElementById("sel-clear").addEventListener("click", () => { state.selected.clear(); syncSel(); });
  document.getElementById("sel-archive").addEventListener("click", () => bulkArchive(true));
  document.getElementById("sel-unarchive").addEventListener("click", () => bulkArchive(false));
  document.getElementById("sel-fill").addEventListener("click", bulkFill);
  document.getElementById("sel-tn-add").addEventListener("click", () => bulkTonight(true));
  document.getElementById("sel-tn-rm").addEventListener("click", () => bulkTonight(false));
  document.getElementById("sel-stage").addEventListener("change", (e) => { if (e.target.value) bulkStage(e.target.value); e.target.value = ""; });
  tbody.addEventListener("change", async (e) => {
    const sel = e.target.closest("select[data-stage]");
    if (!sel) return;
    const id = Number(sel.dataset.stage);
    const job = state.jobs.find((x) => x.id === id);
    const before = job.status;
    try {
      const r = await api(`/jobs/${id}`, { method: "PATCH", json: { status: sel.value } });
      Object.assign(job, { status: r.status, applied_at: r.applied_at, updated_at: r.updated_at });
      toast(`${job.title}: ${STAGE[r.status].label}`);
      renderList(true);
    } catch (err) { sel.value = before; toast(err.message, "error"); }
  });
  drawRows();
  if (keep) scrollTo(0, y);  // after the rows are back, or the short page snaps to the top
}

// Put jobs on / take them off tonight's list from the job table, then redraw it.
async function queueJobs(ids, queued) {
  try {
    const r = await api("/nightly/queue", { json: { job_ids: ids, queued } });
    state.jobs.forEach((j) => { if (ids.includes(j.id) && (!queued || j.has_description)) j.queued = queued; });
    if (ids.length > 1) toast(queued ? `Added ${r.changed} to tonight's list` : "Tonight's list cleared");
  } catch (err) { toast(err.message, "error"); }
  renderList(true);
}

// Fill in what tonight's run will make, when it runs, and whether one is going now.
async function drawTonightBar() {
  const bar = document.getElementById("tonight-bar");
  if (!bar) return;
  let n;
  try { n = await api("/nightly"); } catch { return; }
  if (!bar.isConnected) return;
  const st = n.settings;
  const sum = bar.querySelector("#tn-sum");
  const run = bar.querySelector("#tn-run");
  if (n.running) {
    sum.textContent = "a run is going now.";
    run.disabled = true;
    clearTimeout(nightlyTimer);
    nightlyTimer = setTimeout(drawTonightBar, 5000);
    return;
  }
  const when = st.enabled ? ` Runs at ${st.run_time} with ${st.engine === "local" ? engineName("local") : "Claude"}.` : " Nightly runs are off (Schedule).";
  if (n.pending.queued) sum.innerHTML = pendingText(n.pending) + esc(when);
  run.disabled = !n.pending.jobs;
}

const SHEET_COLS = [
  { cls: "c-sel", label: "" },
  { cls: "c-stage", label: "Stage", sort: "stage" },
  { cls: "c-job", label: "Job", sort: "title" },
  { cls: "c-co", label: "Company", sort: "company" },
  { cls: "c-loc", label: "Location" },
  { cls: "c-fit num", label: "Fit", sort: "fit" },
  { cls: "c-res", label: "Resume", sort: "resume" },
  { cls: "c-tn", label: "Tonight", sort: "tonight" },
  { cls: "c-post num", label: "Posted", sort: "posted" },
  { cls: "c-app num", label: "Applied", sort: "applied" },
  { cls: "c-upd num", label: "Updated", sort: "updated" },
];
const STAGE_ORDER = Object.fromEntries(STAGES.map((s, i) => [s.id, i]));

function drawRows() {
  const needle = state.q.trim().toLowerCase();
  let rows = state.jobs.filter((j) => {
    if (!jobPasses(j)) return false;
    if (needle && !`${j.title} ${j.company} ${j.location} ${j.tags}`.toLowerCase().includes(needle)) return false;
    return true;
  });
  const dir = state.dir === "asc" ? 1 : -1;
  const txt = (a, b) => (a || "").localeCompare(b || "", undefined, { sensitivity: "base" });
  const cmp = {
    updated: (a, b) => (a.updated_at || "").localeCompare(b.updated_at || ""),
    fit: (a, b) => (a.fit_score ?? -1) - (b.fit_score ?? -1),
    // undated jobs sort as oldest, so newest postings come first
    posted: (a, b) => (isoDate(a.posted_date) || "0").localeCompare(isoDate(b.posted_date) || "0"),
    // not applied yet sorts as oldest, so the latest applications come first
    applied: (a, b) => (a.applied_at || "0").localeCompare(b.applied_at || "0"),
    company: (a, b) => txt(a.company, b.company) || txt(a.title, b.title),
    title: (a, b) => txt(a.title, b.title),
    resume: (a, b) => (a.has_resume - b.has_resume) || (b.updated_at || "").localeCompare(a.updated_at || ""),
    tonight: (a, b) => (a.queued - b.queued) || (a.has_description - b.has_description) || (b.updated_at || "").localeCompare(a.updated_at || ""),
    stage: (a, b) => STAGE_ORDER[a.status] - STAGE_ORDER[b.status] || (b.updated_at || "").localeCompare(a.updated_at || ""),
  }[state.sort] || ((a, b) => (a.updated_at || "").localeCompare(b.updated_at || ""));
  rows.sort((a, b) => dir * cmp(a, b));
  lastRows = rows;

  const tbody = document.getElementById("jobs");
  const count = document.getElementById("sheet-count");
  count.textContent = `${rows.length} job${rows.length === 1 ? "" : "s"}${rows.length !== state.jobs.length ? ` of ${state.jobs.length}` : ""}`;
  if (!rows.length) {
    tbody.innerHTML = html`<tr class="none"><td colspan="${SHEET_COLS.length}">${fOnly("tonight") && !state.q ? "Nothing on tonight's list. Tick jobs in the Tonight column, or use Pick saved jobs without a resume." : fOnly("reposted") && !state.q ? "No reposted jobs. When a CSV import shows a saved job was posted again, it appears here." : fOnly("nodesc") && !state.q ? "Every job has a description." : fOnly("archived") && !state.q ? "Nothing archived yet. Open a job and click Archive, or use Archive all closed jobs on the Closed filter." : `No jobs match.${!fOnly("all") ? " Try the All filter." : ""}`}</td></tr>`;
    const m = document.getElementById("sheet-more");
    if (m) m.hidden = true;
    drawSelBar();
    return;
  }
  // Drawing thousands of rows at once freezes the browser, so draw a page
  // and add the next one as the end of the table scrolls into view.
  state.shown = Math.min(rows.length, Math.max(state.shown, PAGE));
  tbody.innerHTML = rows.slice(0, state.shown).map(rowHtml).join("");
  let more = document.getElementById("sheet-more");
  if (!more) {
    count.insertAdjacentHTML("beforebegin", '<div id="sheet-more" class="sheet-more"><button class="btn btn-ghost btn-s" type="button"></button></div>');
    more = document.getElementById("sheet-more");
  }
  const btn = more.querySelector("button");
  const update = () => {
    const left = rows.length - state.shown;
    more.hidden = left <= 0;
    btn.textContent = `Show ${Math.min(PAGE, left)} more (${left} not shown)`;
  };
  const addPage = () => {
    if (state.shown >= rows.length) return;
    const next = rows.slice(state.shown, state.shown + PAGE);
    state.shown += next.length;
    tbody.insertAdjacentHTML("beforeend", next.map(rowHtml).join(""));
    update();
  };
  btn.onclick = addPage;
  update();
  drawSelBar();
  sheetObserver?.disconnect();
  if ("IntersectionObserver" in window) {
    sheetObserver = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) addPage(); }, { rootMargin: "600px" });
    sheetObserver.observe(more);
  }
}

let sheetObserver;

let selAnchor = null;  // the row a Shift+click range starts from
function fineMouse() { return matchMedia("(hover: hover) and (pointer: fine)").matches; }
function tonightable(j) { return j.has_description && !j.archived_at && !j.queued; }
function selectedJobs() { return (state.jobs || []).filter((j) => state.selected.has(j.id)); }

// Click = select only this row (click it again to clear), Ctrl/Cmd+click = add or remove,
// Shift+click = everything from the last clicked row to this one, like a spreadsheet.
function selectRow(id, e) {
  const ids = lastRows.map((j) => j.id);
  if (e.shiftKey && selAnchor != null && ids.includes(selAnchor)) {
    const a = ids.indexOf(selAnchor), b = ids.indexOf(id);
    if (!(e.ctrlKey || e.metaKey)) state.selected.clear();
    ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => state.selected.add(x));
  } else if (e.ctrlKey || e.metaKey) {
    if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    selAnchor = id;
  } else {
    const only = state.selected.size === 1 && state.selected.has(id);
    state.selected.clear();
    if (!only) state.selected.add(id);
    selAnchor = id;
  }
  syncSel();
}

// Update the ticks, highlights and the bar without redrawing the table.
function syncSel() {
  document.querySelectorAll("#jobs tr[data-id]").forEach((tr) => {
    const on = state.selected.has(Number(tr.dataset.id));
    tr.classList.toggle("picked", on);
    const cb = tr.querySelector("input[data-sel]");
    if (cb) cb.checked = on;
  });
  drawSelBar();
}

function mergeJobs(list) {
  list.forEach((u) => { const i = state.jobs.findIndex((x) => x.id === u.id); if (i >= 0) state.jobs[i] = { ...state.jobs[i], ...u }; });
}

async function bulkStage(status) {
  const ids = selectedJobs().filter((j) => j.status !== status).map((j) => j.id);
  if (!ids.length) return toast(`Already ${STAGE[status].label}.`);
  try {
    const r = await api("/jobs/stage", { json: { job_ids: ids, status } });
    mergeJobs(r.jobs);
    toast(`Moved ${r.changed} job${r.changed === 1 ? "" : "s"} to ${STAGE[status].label}`);
  } catch (err) { toast(err.message, "error"); }
  renderList(true);
}

async function bulkTonight(on) {
  const pick = selectedJobs();
  const ids = (on ? pick.filter(tonightable) : pick.filter((j) => j.queued)).map((j) => j.id);
  if (!ids.length) return toast(on ? "None of these can go on tonight's list (they need a description and can't be archived)." : "None of these are on tonight's list.");
  try {
    const r = await api("/nightly/queue", { json: { job_ids: ids, queued: on } });
    state.jobs.forEach((j) => { if (ids.includes(j.id)) j.queued = on; });
    const skipped = on ? pick.length - ids.length - pick.filter((j) => j.queued).length : 0;
    toast(`${on ? "Added" : "Removed"} ${r.changed} ${on ? "to" : "from"} tonight's list${skipped > 0 ? `. ${skipped} skipped (no description or archived).` : ""}`);
  } catch (err) { toast(err.message, "error"); }
  renderList(true);
}

async function bulkArchive(archived) {
  const ids = selectedJobs().filter((j) => !!j.archived_at !== archived).map((j) => j.id);
  if (!ids.length) return;
  try {
    const r = await api("/jobs/archive", { json: { job_ids: ids, archived } });
    const at = new Date().toISOString();
    state.jobs.forEach((j) => { if (ids.includes(j.id)) { j.archived_at = archived ? at : null; if (archived) j.queued = false; } });
    toast(`${archived ? "Archived" : "Unarchived"} ${r.changed} job${r.changed === 1 ? "" : "s"}${archived ? ". Find them under Archived." : ""}`);
    state.selected.clear();
  } catch (err) { toast(err.message, "error"); }
  renderList(true);
}

async function bulkFill() {
  const ids = selectedJobs().filter(fillable).map((j) => j.id);
  const updated = await fillJobs(ids);
  mergeJobs(updated);
  updated.forEach((u) => state.selected.delete(u.id));
  if (updated.length) renderList(true);
}

// Right-click menu for the selected rows.
function closeRowMenu() { document.querySelector(".ctx-menu")?.remove(); }
function openRowMenu(x, y) {
  closeRowMenu();
  const pick = selectedJobs();
  if (!pick.length) return;
  const n = pick.length;
  const fill = pick.filter(fillable).length, tnAdd = pick.filter(tonightable).length, tnRm = pick.filter((j) => j.queued).length;
  const arch = pick.filter((j) => j.archived_at).length;
  const one = n === 1 ? pick[0] : null;
  const m = document.createElement("div");
  m.className = "ctx-menu";
  m.setAttribute("role", "menu");
  m.innerHTML = html`
    <p class="ctx-h">${one ? one.title : `${n} jobs selected`}</p>
    ${one ? raw('<button role="menuitem" data-act="open">Open job</button><hr>') : ""}
    <p class="ctx-sub">Move to</p>
    ${STAGES.map((st) => raw(html`<button role="menuitem" data-act="stage" data-st="${st.id}" style="--c:${raw(stageColor(st.id))}"><span class="dot"></span>${st.label}${pick.every((j) => j.status === st.id) ? raw(' <span class="ctx-cur">current</span>') : ""}</button>`))}
    <hr>
    ${fill ? raw(html`<button role="menuitem" data-act="fill">Fill in missing details${n > 1 ? ` (${fill})` : ""}</button>`) : ""}
    ${tnAdd ? raw(html`<button role="menuitem" data-act="tn-add">Add to tonight's list${n > 1 ? ` (${tnAdd})` : ""}</button>`) : ""}
    ${tnRm ? raw(html`<button role="menuitem" data-act="tn-rm">Remove from tonight's list${n > 1 ? ` (${tnRm})` : ""}</button>`) : ""}
    ${arch < n ? raw(html`<button role="menuitem" data-act="archive">Archive${n > 1 ? ` (${n - arch})` : ""}</button>`) : ""}
    ${arch ? raw(html`<button role="menuitem" data-act="unarchive">Unarchive${n > 1 ? ` (${arch})` : ""}</button>`) : ""}
    <hr>
    <button role="menuitem" data-act="clear">Clear selection</button>`;
  document.body.append(m);
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.max(8, Math.min(x, innerWidth - r.width - 8))}px`;
  m.style.top = `${Math.max(8, Math.min(y, innerHeight - r.height - 8))}px`;
  m.querySelector("button")?.focus();
  m.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    closeRowMenu();
    const act = b.dataset.act;
    if (act === "open") location.hash = `#/job/${one.id}`;
    else if (act === "stage") bulkStage(b.dataset.st);
    else if (act === "fill") bulkFill();
    else if (act === "tn-add") bulkTonight(true);
    else if (act === "tn-rm") bulkTonight(false);
    else if (act === "archive") bulkArchive(true);
    else if (act === "unarchive") bulkArchive(false);
    else if (act === "clear") { state.selected.clear(); syncSel(); }
  });
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest(".ctx-menu")) closeRowMenu(); });
addEventListener("scroll", closeRowMenu, { passive: true });
addEventListener("resize", closeRowMenu);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (document.querySelector(".ctx-menu")) return closeRowMenu();
  if (state.selected.size && document.getElementById("jobs") && !document.querySelector("dialog[open]")) { state.selected.clear(); syncSel(); }
});

// A job the Fill in action can help: it has a link and no description.
function fillable(j) { return !!j.url && !j.has_description && !looksLikeUrl(j.title); }

// Show the bulk-action bar while jobs are ticked, and keep the select-all box in step.
function drawSelBar() {
  const bar = document.getElementById("sel-bar");
  if (!bar) return;
  const known = new Set(state.jobs.map((j) => j.id));
  [...state.selected].forEach((id) => { if (!known.has(id)) state.selected.delete(id); });
  const picked = state.jobs.filter((j) => state.selected.has(j.id));
  bar.hidden = !picked.length;
  bar.querySelector("#sel-n").textContent = picked.length;
  const off = picked.filter((j) => !lastRows.includes(j)).length;
  bar.querySelector("#sel-what").textContent = `job${picked.length === 1 ? "" : "s"} selected${off ? ` (${off} not in this view)` : ""}`;
  const arch = picked.filter((j) => j.archived_at).length;
  const canFill = picked.filter(fillable).length;
  const tnAdd = picked.filter(tonightable).length, tnRm = picked.filter((j) => j.queued).length;
  bar.querySelector("#sel-tn-add").hidden = !tnAdd;
  bar.querySelector("#sel-tn-add").textContent = `Tonight ${tnAdd}`;
  bar.querySelector("#sel-tn-rm").hidden = !tnRm;
  bar.querySelector("#sel-tn-rm").textContent = `Off tonight ${tnRm}`;
  bar.querySelector("#sel-fill").hidden = !canFill;
  bar.querySelector("#sel-fill").textContent = `Fill in ${canFill}`;
  bar.querySelector("#sel-fill").title = "Read the missing description from each job's posting";
  bar.querySelector("#sel-archive").hidden = arch === picked.length;
  bar.querySelector("#sel-unarchive").hidden = arch === 0;
  bar.querySelector("#sel-archive").textContent = `Archive ${picked.length - arch}`;
  bar.querySelector("#sel-unarchive").textContent = `Unarchive ${arch}`;
  const all = document.getElementById("sel-all");
  if (all) {
    const inView = lastRows.filter((j) => state.selected.has(j.id)).length;
    all.checked = inView > 0 && inView === lastRows.length;
    all.indeterminate = inView > 0 && inView < lastRows.length;
  }
}

function rowHtml(j) {
  return html`
    <tr data-id="${j.id}" class="${state.selected.has(j.id) ? "picked" : ""}" style="--c:${raw(stageColor(j.status))}">
      <td class="c-sel"><input type="checkbox" data-sel="${j.id}" ${state.selected.has(j.id) ? "checked" : ""} aria-label="Select ${j.title}"></td>
      <td class="c-stage"><span class="st-dot" aria-hidden="true"></span><select data-stage="${j.id}" aria-label="Stage for ${j.title}">
        ${STAGES.map((s) => raw(html`<option value="${s.id}" ${s.id === j.status ? "selected" : ""}>${s.label}</option>`))}
      </select></td>
      <td class="c-job">
        <a class="t" href="#/job/${j.id}"><span class="st-dot m" aria-hidden="true"></span><span>${j.title}</span></a>
        <span class="sub">${[j.company, j.location].filter(Boolean).join(", ")}</span>
        ${j.has_description ? "" : j.url
          ? raw(html`<button type="button" class="flag warn flag-fill" data-fillrow="${j.id}" title="Read the description from the posting">No description · Fill in</button>`)
          : raw('<span class="flag warn" title="No link saved, so it can\'t be filled in automatically">No description</span>')}
        ${j.archived_at ? raw('<span class="flag ar">Archived</span>') : ""}
        ${j.reposted_at ? raw(html`<span class="flag rp" title="${j.first_posted ? `First posted ${fmtPosted(j.first_posted, true)}` : "Posted again"}">Reposted</span>`) : ""}
        <span class="m-tags">${j.has_resume ? raw('<span class="flag ok">Resume</span>') : ""}${j.applied_at ? raw(html`<span class="m-applied">Applied ${fmtPosted(localDay(j.applied_at))}</span>`) : ""}</span>
      </td>
      <td class="c-co">${j.company || ""}</td>
      <td class="c-loc">${j.location || ""}</td>
      <td class="c-fit num">${j.fit_score ? raw(html`<span class="fitn" style="--fc:${raw(fitColor(j.fit_score))}" aria-label="Fit ${j.fit_score} out of 10">${j.fit_score}</span>`) : raw('<span class="nil">–</span>')}</td>
      <td class="c-res">${j.has_resume ? raw('<span class="flag ok">Ready</span>') : raw('<span class="nil">–</span>')}</td>
      <td class="c-tn"><input type="checkbox" data-q="${j.id}" ${j.queued ? "checked" : ""} ${j.has_description && !j.archived_at ? "" : "disabled"} aria-label="Tonight's list: ${j.title}" title="${j.archived_at ? "Archived" : j.has_description ? "Make overnight" : "Needs a description first"}"></td>
      <td class="c-post num" title="${j.posted_date ? `Posted ${fmtPosted(j.posted_date, true)}${j.first_posted ? `, first posted ${fmtPosted(j.first_posted, true)}` : ""}` : "Posting date unknown"}">${j.posted_date ? fmtPosted(j.posted_date) : raw('<span class="nil">–</span>')}</td>
      <td class="c-app num" title="${j.applied_at ? `Moved to Applied ${longDate(j.applied_at)}` : "Not applied yet"}">${j.applied_at ? fmtPosted(localDay(j.applied_at)) : raw('<span class="nil">–</span>')}</td>
      <td class="c-upd num" title="Changed in the app ${longDate(j.updated_at)}">${ago(j.updated_at)}</td>
    </tr>`;
}

// ---------- add ----------

const EMPTY = { title: "", company: "", location: "", salary: "", employment_type: "", posted_date: "", description: "", url: "" };

function renderAdd(params) {
  const url = params.get("url") || "";
  const mode = params.get("mode") || (url ? "link" : "link");
  setView(html`
    <div>
      <a class="btn btn-ghost back" href="#/">${raw(icon.back)}Jobs</a>
      <h1>Add a job</h1>
      <div class="tabs" role="tablist" aria-label="How to add">
        <button role="tab" data-mode="link" aria-selected="${mode === "link"}">Link</button>
        <button role="tab" data-mode="paste" aria-selected="${mode === "paste"}">Paste</button>
        <button role="tab" data-mode="manual" aria-selected="${mode === "manual"}">Manual</button>
        <button role="tab" data-mode="bulk" aria-selected="${mode === "bulk"}">Bulk</button>
      </div>
      <div id="add-body"></div>
    </div>`);
  $main.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    $main.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-selected", x === b));
    showMode(b.dataset.mode, { url: document.querySelector("#add-body [name=url]")?.value || url });
  }));
  showMode(mode, { url, auto: !!url });
}

function selectTab(mode) {
  $main.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-selected", x.dataset.mode === mode));
}

function showMode(mode, { url = "", auto = false, note = "" } = {}) {
  const body = document.getElementById("add-body");
  selectTab(mode);
  if (mode === "link") {
    body.innerHTML = html`
      <form id="f-link">
        <div class="field">
          <label for="u">Job link</label>
          <input id="u" name="url" type="url" inputmode="url" placeholder="https://…" value="${url}" required>
          <p class="hint">Works with LinkedIn, MyCareersFuture, JobStreet, Indeed and most company career pages.</p>
        </div>
        <button class="btn btn-primary btn-wide" type="submit">Get job details</button>
      </form>`;
    const form = document.getElementById("f-link");
    form.addEventListener("submit", (e) => { e.preventDefault(); fetchLink(form.url.value.trim(), e.submitter); });
    if (auto && url) fetchLink(url, form.querySelector("button"));
    else form.url.focus();
  } else if (mode === "paste") {
    body.innerHTML = html`
      ${note ? raw(html`<div class="callout"><p>${note}</p><p>Open the posting, select all the text, copy it, and paste it below.</p></div>`) : ""}
      <form id="f-paste">
        <div class="field">
          <label for="t">Job post</label>
          <textarea id="t" name="text" class="tall" placeholder="Paste everything from the posting: title, company, description…" required></textarea>
        </div>
        <div class="field">
          <label for="pu">Link to the posting <span class="hint" style="display:inline">(optional)</span></label>
          <input id="pu" name="url" type="url" inputmode="url" value="${url}">
        </div>
        ${state.session.ai_enabled ? raw(enginePicker()) : ""}
        <button class="btn btn-primary btn-wide" type="submit">${state.session.ai_enabled ? "Fill in the details" : "Continue"}</button>
        ${state.session.ai_enabled ? "" : raw('<p class="hint">AI is off, so you\'ll fill in title and company yourself on the next step.</p>')}
      </form>`;
    const form = document.getElementById("f-paste");
    bindEnginePickers(form);
    form.text.focus();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const done = busy(e.submitter, "Reading the post");
      try {
        const r = state.session.ai_enabled
          ? await withAI({ title: "Reading the job post", detail: (w) => `${w} is picking out the title, company, salary and description from your paste.`, typical: { claude: "10–30 seconds", local: "30–90 seconds" } },
              (engine) => api("/extract/text", { json: { text: form.text.value, url: form.url.value, engine } }))
          : await api("/extract/text", { json: { text: form.text.value, url: form.url.value } });
        showReview(r.draft, r.method === "AI" ? `Filled in by ${r.engine || "AI"} from your paste. Check it over.` : (r.note || "Fill in the title and company."));
      } catch (err) { toast(err.message, "error"); done(); }
    });
  } else if (mode === "bulk") {
    renderBulk(body);
  } else {
    showReview({ ...EMPTY, url }, "");
  }
}

// ---------- bulk add ----------

let bulkCancel = false;

function findLinks(text) {
  const out = [];
  for (const tok of (text || "").split(/[\s,;<>"'()]+/)) {
    let t = tok.trim().replace(/[.)\]]+$/, "");
    if (!t) continue;
    if (/^www\./i.test(t) || /^[a-z0-9-]+(\.[a-z0-9-]+)+\/\S/i.test(t)) t = "https://" + t;
    if (/^https?:\/\/[^/\s]+\.[^/\s]+/i.test(t) && !out.includes(t)) out.push(t);
  }
  return out;
}

async function renderBulk(body) {
  if (!state.jobs) state.jobs = await api("/jobs");
  const missing = state.jobs.filter((j) => j.url && !j.has_description && !j.archived_at && !looksLikeUrl(j.title));
  body.innerHTML = html`
    <section class="bulk">
      <h2>Paste many links</h2>
      <p class="hint">One per line, or paste any text with links in it. Each one is read and saved, and links you've already saved are skipped.</p>
      <div class="field" style="margin-top:12px">
        <label for="bulk-links" class="sr">Job links</label>
        <textarea id="bulk-links" class="tall" placeholder="https://www.linkedin.com/jobs/view/…&#10;https://www.mycareersfuture.gov.sg/job/…"></textarea>
        <p class="hint" id="bulk-count">No links yet.</p>
      </div>
      <div class="actions"><button class="btn btn-primary" id="bulk-go" disabled>Save all links</button></div>
      <div id="bulk-out"></div>

      <h2>Fill in missing details</h2>
      ${missing.length
        ? raw(html`<p class="hint">${missing.length} saved job${missing.length === 1 ? " has" : "s have"} a link but no description, usually from a CSV import. Each one is read from its link; nothing you've already filled in is changed.</p>
          <div class="actions"><button class="btn" id="fill-go">Fill in ${missing.length} job${missing.length === 1 ? "" : "s"}</button></div>`)
        : raw('<p class="hint">Every saved job with a link already has a description.</p>')}
      <div id="fill-out"></div>

      ${state.jobs.some((j) => looksLikeUrl(j.title)) ? raw(html`
        <h2>Remove taken-down jobs</h2>
        <p class="hint">${state.jobs.filter((j) => looksLikeUrl(j.title)).length} saved jobs have a link where the title should be. That usually means the posting was taken down, so there's nothing to apply to.</p>
        <div class="actions"><button class="btn" id="bulk-dead">Remove them</button></div>`) : ""}

      <h2>Import a CSV</h2>
      <p class="hint">The same format as the old tracker: <code>Job URL</code>, <code>Job title</code>, <code>Company Name</code>, <code>Location</code>, <code>Published At</code>, <code>Status</code>. A title or a link is enough: jobs with only a link get their title and description read from the posting right after the import.</p>
      <div class="actions"><label class="btn" style="margin:0">Choose CSV file<input type="file" id="bulk-csv" accept=".csv,text/csv" hidden></label></div>
    </section>`;

  const ta = document.getElementById("bulk-links");
  const go = document.getElementById("bulk-go");
  const count = document.getElementById("bulk-count");
  ta.addEventListener("input", () => {
    const n = findLinks(ta.value).length;
    count.textContent = n ? `${n} link${n === 1 ? "" : "s"} found.` : "No links yet.";
    go.disabled = !n;
  });
  go.addEventListener("click", () => {
    const links = findLinks(ta.value);
    runBatch({
      items: links, out: document.getElementById("bulk-out"), btn: go, verb: "Saving",
      label: (u) => u.replace(/^https?:\/\/(www\.)?/, "").slice(0, 60),
      call: (u) => api("/jobs/from-url", { json: { url: u, engine: currentEngine() } }),
      done: (failed) => {
        ta.value = failed.join("\n");
        ta.dispatchEvent(new Event("input"));
        if (failed.length) count.textContent = `${failed.length} link${failed.length === 1 ? "" : "s"} couldn't be read and ${failed.length === 1 ? "is" : "are"} left above.`;
      },
    });
  });

  document.getElementById("fill-go")?.addEventListener("click", (e) => {
    runBatch({
      items: missing, out: document.getElementById("fill-out"), btn: e.currentTarget, verb: "Filling in",
      label: (j) => [j.title, j.company].filter(Boolean).join(", "),
      call: (j) => api(`/jobs/${j.id}/refresh`, { method: "POST", json: { engine: currentEngine() } }).then((r) => ({ ...r, job: r.job || j, url: j.url })),
    });
  });

  document.getElementById("bulk-dead")?.addEventListener("click", async () => {
    if (await removeDeadJobs(state.jobs.filter((j) => looksLikeUrl(j.title)).length)) renderBulk(body);
  });

  const csv = document.getElementById("bulk-csv");
  csv.addEventListener("change", async () => {
    const file = csv.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api("/import/csv", { method: "POST", body: fd });
      toast(importSummary(r));
      state.jobs = null;
      renderBulk(body);
      await fillAfterImport(r);
    } catch (err) { toast(err.message, "error"); }
    csv.value = "";
  });
}

const RESULT = {
  added: { cls: "ok", text: "Saved" },
  updated: { cls: "ok", text: "Filled in" },
  duplicate: { cls: "skip", text: "Already saved" },
  unchanged: { cls: "skip", text: "Nothing new found" },
  failed: { cls: "fail", text: "Couldn't read it" },
};

async function runBatch({ items, out, btn, verb, label, call, done }) {
  bulkCancel = false;
  btn.disabled = true;
  out.innerHTML = html`
    <div class="batch">
      <div class="batch-head"><strong id="b-status">${verb} 0 of ${items.length}</strong><button class="btn btn-ghost" id="b-stop">Stop</button></div>
      <div class="batch-bar"><span id="b-bar"></span></div>
      <ul class="batch-list" id="b-list"></ul>
    </div>`;
  const list = out.querySelector("#b-list");
  const status = out.querySelector("#b-status");
  const bar = out.querySelector("#b-bar");
  out.querySelector("#b-stop").addEventListener("click", (e) => { bulkCancel = true; e.target.disabled = true; e.target.textContent = "Stopping…"; });
  const tally = { ok: 0, skip: 0, fail: 0 };
  const failed = [];
  let i = 0;
  for (const item of items) {
    if (bulkCancel) break;
    const li = document.createElement("li");
    li.className = "busy";
    li.innerHTML = html`<span class="b-dot"></span><span class="b-main"><span class="b-title">${label(item)}</span><span class="b-note">Reading…</span></span>`;
    list.prepend(li);
    let r;
    try { r = await call(item); } catch (e) { r = { status: "failed", reason: e.message, url: typeof item === "string" ? item : item.url }; }
    const res = RESULT[r.status] || RESULT.failed;
    tally[res.cls]++;
    if (res.cls === "fail") failed.push(typeof item === "string" ? item : item.url);
    li.className = res.cls;
    const j = r.job;
    const title = j ? [j.title, j.company].filter(Boolean).join(", ") : label(item);
    let note = res.text;
    if (r.status === "updated") note += `: ${r.filled.map((f) => f.replace("_", " ")).join(", ")}`;
    if (r.status === "failed") note += `. ${r.reason || ""}`;
    const fix = r.status === "failed"
      ? (j ? html` <a href="#/job/${j.id}">Open it</a>` : html` <a href="#/add?mode=paste&url=${encodeURIComponent(r.url || "")}">Paste text instead</a>`)
      : "";
    li.innerHTML = html`<span class="b-dot"></span><span class="b-main">${j ? raw(html`<a class="b-title" href="#/job/${j.id}">${title}</a>`) : raw(html`<span class="b-title">${title}</span>`)}<span class="b-note">${note}${raw(fix)}</span></span>`;
    i++;
    status.textContent = `${verb} ${i} of ${items.length}`;
    bar.style.width = `${(i / items.length) * 100}%`;
    if (i < items.length) await new Promise((res2) => setTimeout(res2, 600)); // be gentle with job sites
  }
  state.jobs = null;
  const parts = [];
  if (tally.ok) parts.push(`${tally.ok} ${verb === "Saving" ? "saved" : "filled in"}`);
  if (tally.skip) parts.push(`${tally.skip} skipped`);
  if (tally.fail) parts.push(`${tally.fail} couldn't be read`);
  status.textContent = (bulkCancel ? "Stopped. " : "Done. ") + (parts.join(", ") || "Nothing to do") + ".";
  out.querySelector("#b-stop").remove();
  btn.disabled = false;
  if (tally.fail) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "For the ones that couldn't be read, use “Paste text instead” and paste the post yourself.";
    out.querySelector(".batch").append(p);
  }
  if (!bulkCancel || i) done?.(bulkCancel ? [...failed, ...items.slice(i)] : failed);
}

async function fetchLink(url, btn) {
  if (!url) return;
  const done = busy(btn, "Reading the page");
  try {
    const r = await withAI({ title: "Reading the job page", detail: (W, w) => state.session.ai_enabled
        ? `Opening the link and looking for the job details. If the page doesn't list them clearly, ${w} reads it.`
        : "Opening the link and looking for the job details.", typical: { claude: "5–30 seconds", local: "5–90 seconds" } },
      (engine) => api("/extract/url", { json: { url, engine } }));
    if (r.existing_id) {
      document.getElementById("add-body").innerHTML = html`
        <div class="callout info"><p><strong>Already saved.</strong> ${r.title}${r.company ? ` at ${r.company}` : ""} is in your list.</p></div>
        <a class="btn btn-primary" href="#/job/${r.existing_id}">Open it</a>`;
      return;
    }
    showReview(r.draft, `Found via ${r.method}. Check it over, then save.`);
  } catch (err) {
    if (err.data?.needs_manual) {
      showMode("paste", { url: err.data.url || url, note: err.data.reason });
    } else { toast(err.message, "error"); done(); }
  }
}

function jobForm(d, submitLabel) {
  return html`
    <form id="f-job">
      <div class="field"><label for="jt">Job title</label><input id="jt" name="title" type="text" value="${d.title}" required></div>
      <div class="grid2">
        <div class="field"><label for="jc">Company</label><input id="jc" name="company" type="text" value="${d.company}"></div>
        <div class="field"><label for="jl">Location</label><input id="jl" name="location" type="text" value="${d.location}"></div>
        <div class="field"><label for="js">Salary</label><input id="js" name="salary" type="text" value="${d.salary}"></div>
        <div class="field"><label for="je">Job type</label><input id="je" name="employment_type" type="text" value="${d.employment_type}" placeholder="Full time, contract…"></div>
        <div class="field"><label for="jp">Posted</label><input id="jp" name="posted_date" type="text" value="${d.posted_date}"></div>
        <div class="field"><label for="ju">Link</label><input id="ju" name="url" type="url" inputmode="url" value="${d.url || ""}"></div>
      </div>
      <div class="field">
        <label for="jd">Description</label>
        <textarea id="jd" name="description" class="tall" placeholder="Paste the job description here. It's what the fit check and tailored resume work from.">${d.description}</textarea>
      </div>
      <div class="sticky-actions"><button class="btn btn-primary btn-wide" type="submit">${submitLabel}</button></div>
    </form>`;
}

function formData(form) {
  const out = {};
  ["title", "company", "location", "salary", "employment_type", "posted_date", "url", "description"].forEach((k) => out[k] = form[k].value.trim());
  return out;
}

function showReview(draft, note) {
  const body = document.getElementById("add-body");
  body.innerHTML = (note ? html`<p class="method">${note}</p>` : "") + jobForm({ ...EMPTY, ...draft }, "Save job");
  const form = document.getElementById("f-job");
  if (!draft.title) form.title.focus();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const done = busy(e.submitter, "Saving");
    try {
      const j = await api("/jobs", { json: { ...formData(form), source: draft.source || "app" } });
      state.jobs = null;
      toast(j.duplicate ? "Already saved, details filled in" : "Saved");
      location.hash = `#/job/${j.id}`;
    } catch (err) { toast(err.message, "error"); done(); }
  });
}

// ---------- job detail ----------

async function renderJob(id) {
  const j = await api(`/jobs/${id}`);
  const personas = state.session.ai_enabled ? await api("/personas") : [];
  const facts = [["Location", j.location], ["Salary", j.salary], ["Job type", j.employment_type], ["Posted", fmtPosted(j.posted_date, true) + (j.reposted_at ? (j.first_posted ? ` (reposted; first posted ${fmtPosted(j.first_posted, true)})` : " (reposted)") : "")], ["Saved", longDate(j.created_at)]].filter(([, v]) => v);
  const desc = j.description || "";
  const FILLABLE = FILL_LABELS;
  const missing = Object.keys(FILLABLE).filter((k) => !(j[k] || "").trim());
  const canFill = !!j.url && missing.length > 0;

  setView(html`
    <article>
      <a class="btn btn-ghost back" href="#/">${raw(icon.back)}Jobs</a>
      ${j.archived_at ? raw(html`<div class="callout archived-note"><p><strong>Archived ${longDate(j.archived_at)}.</strong> It's kept for searching, but left out of your active list and overnight runs.</p><button class="btn btn-s" data-archive="0">Unarchive</button></div>`) : ""}
      <header class="detail-head">
        <h1>${j.title}</h1>
        ${j.company ? raw(html`<div class="company">${j.company}</div>`) : ""}
        <dl class="facts">${facts.map(([k, v]) => raw(html`<div><dt>${k}</dt><dd>${v}</dd></div>`))}</dl>
        <div class="row">
          ${j.url ? raw(html`<a class="btn" href="${j.url}" target="_blank" rel="noopener">Open posting ${raw(icon.ext)}</a>`) : ""}
          ${canFill ? raw(html`<button class="btn" data-fill title="Missing: ${missing.map((k) => FILLABLE[k]).join(", ")}">Fill in missing details</button>`) : ""}
          <button class="btn" id="edit">Edit details</button>
        </div>
      </header>

      <section class="section" aria-labelledby="h-stage">
        <h2 id="h-stage">Stage</h2>
        <div class="stages-pick" role="group" aria-labelledby="h-stage">
          ${STAGES.map((s) => raw(html`<button data-st="${s.id}" aria-pressed="${j.status === s.id}" style="--c:${raw(stageColor(s.id))}">${s.label}</button>`))}
        </div>
        ${j.applied_at ? raw(html`<p class="applied-note">Applied ${longDate(j.applied_at)}, ${ago(j.applied_at) === "now" ? "today" : ago(j.applied_at) + " ago"}.</p>`) : ""}
      </section>

      ${state.session.ai_enabled ? raw(html`
      <section class="section" aria-labelledby="h-fit">
        <h2 id="h-fit">Fit</h2>
        <div id="fit"></div>
      </section>

      <section class="section" aria-labelledby="h-docs">
        <h2 id="h-docs">Resume and cover letter</h2>
        <div class="panel">
          ${personas.length > 1 ? raw(html`<div class="field"><label for="persona">Base resume</label><select id="persona">${personas.map((p) => raw(html`<option value="${p.id}" ${p.is_default ? "selected" : ""}>${p.name}</option>`))}</select></div>`) : ""}
          ${!personas.length || !personas.some((p) => p.resume_text.trim()) ? raw('<div class="callout"><p>Add your base resume in <a href="#/settings">Settings</a> to tailor documents.</p></div>') : ""}
          <div class="field">
            <label for="instr">Anything to emphasise? <span class="hint" style="display:inline">(optional)</span></label>
            <input id="instr" type="text" placeholder="e.g. lead with my Citibank transformation work">
          </div>
          ${raw(enginePicker())}
          <div class="row">
            <button class="btn btn-primary" data-gen="resume">Tailor my resume</button>
            <button class="btn" data-gen="cover_letter">Write a cover letter</button>
          </div>
          <div class="queue-row">
            <button class="btn btn-ghost" id="queue" aria-pressed="${!!j.queued}" ${desc ? "" : "disabled"}>${j.queued ? "On tonight's list. Remove" : "Add to tonight's list"}</button>
            <span class="hint">Made overnight instead, as set up in <a href="#/settings">Settings</a>.</span>
          </div>
        </div>
        <div id="docs" style="margin-top:14px"></div>
      </section>`) : ""}

      <section class="section" aria-labelledby="h-notes">
        <h2 id="h-notes">Notes</h2>
        <textarea id="notes" placeholder="Recruiter name, interview dates, follow-ups…">${j.notes || ""}</textarea>
        <p class="hint" id="notes-state">Saves when you leave the box.</p>
      </section>

      <section class="section" aria-labelledby="h-desc">
        <h2 id="h-desc">Job description</h2>
        ${desc ? raw(html`<div class="prose ${desc.length > 1400 ? "clamped" : ""}" id="desc">${desc}</div>
          ${desc.length > 1400 ? raw('<button class="btn btn-ghost" id="more">Show the full description</button>') : ""}`)
          : raw(html`<div class="callout"><p>No description saved yet. ${j.url ? "Read it from the posting, or use" : "Use"} <strong>Edit details</strong> to paste it in, so the fit check and tailoring have something to work from.</p>
            ${j.url ? raw('<div class="row"><button class="btn btn-primary" data-fill>Fill in from the posting</button></div>') : ""}</div>`)}
      </section>

      <section class="section">
        <div class="row">
          ${j.archived_at ? raw('<button class="btn" data-archive="0">Unarchive this job</button>') : raw('<button class="btn" data-archive="1">Archive this job</button>')}
          <button class="btn btn-danger" id="del">Delete this job</button>
        </div>
        <p class="hint">${j.archived_at ? "Unarchiving puts it back in your active list." : "Archive keeps the job, its resumes and notes, and moves it out of the way. Find it later under Archived."}</p>
      </section>
    </article>`);

  $main.querySelectorAll("[data-st]").forEach((b) => b.addEventListener("click", async () => {
    if (b.getAttribute("aria-pressed") === "true") return;
    try {
      await api(`/jobs/${id}`, { method: "PATCH", json: { status: b.dataset.st } });
      state.jobs = null;
      toast(`Moved to ${STAGE[b.dataset.st].label}`);
      renderJob(id);
    } catch (e) { toast(e.message, "error"); }
  }));

  document.getElementById("edit").addEventListener("click", () => renderEdit(j));
  $main.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", async () => {
    if (await fillJob(id)) { state.jobs = null; renderJob(id); }
  }));
  document.getElementById("more")?.addEventListener("click", (e) => {
    document.getElementById("desc").classList.remove("clamped"); e.target.remove();
  });

  const notes = document.getElementById("notes");
  notes.addEventListener("change", async () => {
    const st = document.getElementById("notes-state");
    try { await api(`/jobs/${id}`, { method: "PATCH", json: { notes: notes.value } }); st.textContent = "Notes saved."; }
    catch (e) { st.textContent = e.message; }
  });

  $main.querySelectorAll("[data-archive]").forEach((b) => b.addEventListener("click", async () => {
    const want = b.dataset.archive === "1";
    try {
      await api(`/jobs/${id}`, { method: "PATCH", json: { archived: want } });
      state.jobs = null;
      toast(want ? "Archived. Find it under Archived on the job list." : "Unarchived");
      renderJob(id);
    } catch (e) { toast(e.message, "error"); }
  }));

  document.getElementById("del").addEventListener("click", async () => {
    if (!confirm(`Delete “${j.title}”? Its resume and cover letters go too.`)) return;
    await api(`/jobs/${id}`, { method: "DELETE" });
    state.jobs = null;
    toast("Deleted");
    location.hash = "#/";
  });

  document.getElementById("queue")?.addEventListener("click", async (e) => {
    const b = e.currentTarget;
    const want = b.getAttribute("aria-pressed") !== "true";
    try {
      await api(`/jobs/${id}`, { method: "PATCH", json: { queued: want } });
      j.queued = want;
      state.jobs = null;
      b.setAttribute("aria-pressed", want);
      b.textContent = want ? "On tonight's list. Remove" : "Add to tonight's list";
      toast(want ? "Added to tonight's list" : "Removed from tonight's list");
    } catch (err) { toast(err.message, "error"); }
  });

  if (state.session.ai_enabled) {
    bindEnginePickers($main);
    drawFit(j);
    drawDocs(j.documents);
    $main.querySelectorAll("[data-gen]").forEach((b) => b.addEventListener("click", async () => {
      const kind = b.dataset.gen;
      const done = busy(b, kind === "resume" ? "Tailoring" : "Writing");
      try {
        const at = `${j.title}${j.company ? ` at ${j.company}` : ""}`;
        const doc = await withAI(kind === "resume"
          ? { title: "Tailoring your resume", detail: (w) => `${w} is matching your resume to ${at} and rewriting the summary and bullets.`, typical: { claude: "30–90 seconds", local: "1–4 minutes" } }
          : { title: "Writing your cover letter", detail: (w) => `${w} is writing a letter for ${at} from your resume.`, typical: { claude: "15–45 seconds", local: "1–2 minutes" } },
          (engine) => api(`/jobs/${id}/documents`, { json: {
            kind, engine, persona_id: Number(document.getElementById("persona")?.value) || null,
            instructions: document.getElementById("instr").value,
          } }));
        j.documents.unshift(doc);
        drawDocs(j.documents, doc.id);
        toast(kind === "resume" ? "Resume ready" : "Cover letter ready");
      } catch (e) { toast(e.message, "error"); }
      done();
    }));
  }
}

function drawFit(j) {
  const box = document.getElementById("fit");
  const f = j.fit;
  if (!f) {
    box.innerHTML = html`<div class="panel"><p>Get a 1 to 10 score against your resume and what you're looking for, with strengths, gaps and keywords to use.</p>${raw(enginePicker())}<button class="btn btn-primary" id="dofit">Check my fit</button></div>`;
  } else {
    box.innerHTML = html`
      <div class="panel fitbox">
        <span class="fit big" style="--fc:${raw(fitColor(f.score))}" aria-label="Fit ${f.score} out of 10">${f.score}</span>
        <div><p class="verdict">${f.verdict}</p>${f.engine ? raw(html`<p class="hint" style="margin:-4px 0 6px">Checked by ${f.engine}</p>`) : ""}<button class="btn btn-ghost" id="dofit" style="margin-left:-10px">Check again</button></div>
        <div class="fitlists">
          <div><h3>Strengths</h3><ul>${(f.strengths || []).map((s) => raw(html`<li>${s}</li>`))}</ul></div>
          <div><h3>Gaps</h3><ul>${(f.gaps || []).map((s) => raw(html`<li>${s}</li>`))}</ul></div>
        </div>
        ${f.keywords?.length ? raw(html`<div class="kw" aria-label="Keywords to mirror">${f.keywords.map((k) => raw(html`<span>${k}</span>`))}</div>`) : ""}
        <div class="fit-again">${raw(enginePicker())}</div>
      </div>`;
  }
  bindEnginePickers(box);
  document.getElementById("dofit").addEventListener("click", async (e) => {
    const done = busy(e.currentTarget, "Checking");
    try {
      const nj = await withAI({ title: "Checking your fit", detail: (w) => `${w} is comparing the job with your resume and what you're looking for.`, typical: { claude: "10–30 seconds", local: "30–90 seconds" } },
        (engine) => api(`/jobs/${j.id}/fit`, { method: "POST", json: { engine } }));
      state.jobs = null;
      drawFit(nj);
    } catch (err) { toast(err.message, "error"); done(); }
  });
}

// Resumes come back as light Markdown (#, ##, ###, "- ", **bold**).
const isMd = (t) => /^#{1,3}\s/m.test(t || "");
function inline(t) { return esc(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
function mdToHtml(md) {
  const out = [];
  let list = false, head = 0;
  const close = () => { if (list) { out.push("</ul>"); list = false; } };
  for (const raw of md.split("\n")) {
    const l = raw.trimEnd();
    if (!l.trim()) { close(); continue; }
    let m;
    if ((m = l.match(/^# (.*)/))) { close(); out.push(`<h3 class="r-name">${inline(m[1])}</h3>`); head = 1; }
    else if ((m = l.match(/^## (.*)/))) { close(); head = 0; out.push(`<h4 class="r-sec">${inline(m[1])}</h4>`); }
    else if ((m = l.match(/^### (.*)/))) {
      close();
      const i = m[1].lastIndexOf(" | ");
      out.push(i > 0 ? `<p class="r-role"><strong>${inline(m[1].slice(0, i))}</strong><span>${inline(m[1].slice(i + 3))}</span></p>` : `<p class="r-role"><strong>${inline(m[1])}</strong></p>`);
    }
    else if ((m = l.match(/^\s*[-*]\s+(.*)/))) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(m[1])}</li>`); }
    else if (head && head < 3) { out.push(`<p class="r-head${head === 1 ? " r-contact" : ""}">${inline(l)}</p>`); head++; }
    else if (/^\*\*.+\*\*$/.test(l.trim())) { close(); out.push(`<p class="r-title">${esc(l.trim().replace(/^\*\*|\*\*$/g, ""))}</p>`); }
    else { close(); out.push(`<p>${inline(l)}</p>`); }
  }
  close();
  return out.join("");
}
function toPlain(md) {
  return md.split("\n").map((l) => {
    if (l.startsWith("# ")) return l.slice(2);
    if (l.startsWith("## ")) return "\n" + l.slice(3).toUpperCase();
    if (l.startsWith("### ")) return "\n" + l.slice(4);
    if (/^\s*[-*]\s+/.test(l)) return "• " + l.replace(/^\s*[-*]\s+/, "");
    return l;
  }).join("\n").replace(/\*\*(.+?)\*\*/g, "$1").replace(/\n{3,}/g, "\n\n").trim();
}

function drawDocs(docs, openId) {
  const box = document.getElementById("docs");
  box.innerHTML = docs.map((d) => html`
    <details class="doc" data-doc="${d.id}" ${d.id === openId ? "open" : ""}>
      <summary><span><span class="t">${d.kind === "resume" ? "Tailored resume" : "Cover letter"}</span><br><span class="m">${longDate(d.created_at)}${d.persona_name ? `, from ${d.persona_name}` : ""}${d.engine ? `, by ${d.engine}` : ""}${d.nightly ? ", made overnight" : ""}</span></span>${raw(icon.down)}</summary>
      <div class="doc-body">
        ${isMd(d.content) ? raw(html`<div class="doc-text resume-view">${raw(mdToHtml(d.content))}</div>`) : raw(html`<div class="doc-text">${d.content}</div>`)}
        <div class="row">
          <button class="btn btn-primary" data-act="copy">Copy text</button>
          <a class="btn" href="/api/documents/${d.id}/pdf" download>Download PDF</a>
          <a class="btn" href="/api/documents/${d.id}/docx" download>Download Word</a>
          <button class="btn" data-act="edit">Edit</button>
          <button class="btn btn-ghost btn-danger" data-act="del">Delete</button>
        </div>
      </div>
    </details>`).join("");
  box.querySelectorAll(".doc").forEach((el) => {
    const id = Number(el.dataset.doc);
    const d = docs.find((x) => x.id === id);
    el.querySelector("[data-act=copy]").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(isMd(d.content) ? toPlain(d.content) : d.content); toast("Copied"); }
      catch { toast("Couldn't copy. Select the text and copy it instead.", "error"); }
    });
    el.querySelector("[data-act=del]").addEventListener("click", async () => {
      if (!confirm("Delete this document?")) return;
      await api(`/documents/${id}`, { method: "DELETE" });
      docs.splice(docs.indexOf(d), 1);
      drawDocs(docs);
    });
    el.querySelector("[data-act=edit]").addEventListener("click", (e) => {
      const text = el.querySelector(".doc-text");
      if (e.target.dataset.editing) {
        const val = text.querySelector("textarea").value;
        api(`/documents/${id}`, { method: "PATCH", json: { content: val } }).then(() => {
          d.content = val; toast("Saved"); drawDocs(docs, id);
        }).catch((err) => toast(err.message, "error"));
        return;
      }
      e.target.dataset.editing = "1";
      e.target.textContent = "Save changes";
      text.innerHTML = html`<textarea class="tall" aria-label="Document text">${d.content}</textarea>`;
      text.classList.remove("resume-view");
      text.style.padding = "0"; text.style.background = "none"; text.style.maxHeight = "none";
    });
  });
}

function renderEdit(j) {
  setView(html`<div>
    <button class="btn btn-ghost back" id="cancel">${raw(icon.back)}Back</button>
    <h1 style="margin-bottom:18px">Edit details</h1>
    ${raw(jobForm(j, "Save changes"))}
  </div>`);
  document.getElementById("cancel").addEventListener("click", () => renderJob(j.id));
  const form = document.getElementById("f-job");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const done = busy(e.submitter, "Saving");
    try {
      await api(`/jobs/${j.id}`, { method: "PATCH", json: formData(form) });
      state.jobs = null;
      toast("Saved");
      renderJob(j.id);
    } catch (err) { toast(err.message, "error"); done(); }
  });
}

// ---------- settings ----------

async function renderSettings(params) {
  await loadSession();
  const s = state.session;
  const personas = await api("/personas");
  const origin = location.origin;

  setView(html`
    <div class="settings">
      <a class="btn btn-ghost back" href="#/">${raw(icon.back)}Jobs</a>
      <h1>Settings</h1>

      <h2>Your base resume</h2>
      <p class="lead">The starting point for every tailored resume and fit check. Paste your fullest version; tailoring trims it per job.</p>
      <div class="list" id="personas">
        ${personas.map((p) => raw(html`
          <div class="list-item">
            <div><strong>${p.name}</strong>${p.is_default ? raw('<span class="badge">Default</span>') : ""}<div class="meta">${p.resume_text.trim() ? `${p.resume_text.trim().split(/\s+/).length} words` : "Empty"}</div></div>
            <button class="btn" data-persona="${p.id}">Edit</button>
          </div>`))}
      </div>
      <div class="actions"><button class="btn" id="add-persona">${personas.length ? "Add another version" : "Add your resume"}</button></div>
      <div id="persona-editor"></div>

      <h2>What you're looking for</h2>
      <p class="lead">Used by the fit check. Roles, seniority, salary floor, location, and anything that rules a job out.</p>
      <textarea id="prefs" placeholder="e.g. Senior Scrum Master or Agile Coach in Singapore. Hybrid or remote. Not NCS. Minimum S$9k a month.">${s.user.preferences || ""}</textarea>
      <div class="actions"><button class="btn btn-primary" id="save-prefs">Save</button></div>

      <h2 id="h-nightly">Overnight resumes</h2>
      <p class="lead">Pick the jobs you want on the job list, and their resumes are made overnight so they're ready in the morning. Only jobs on the list are done, so nothing is spent on jobs you don't care about. Runs on the Mac mini, nothing to keep open.</p>
      <div id="nightly"><p class="hint">Loading…</p></div>

      <h2>Profile</h2>
      <p class="lead">Each profile has its own jobs and resumes. You're using <strong>${s.user.name}</strong>.</p>
      <div class="field"><label for="pname">Profile name</label><input id="pname" type="text" value="${s.user.name}"></div>
      <div class="actions"><button class="btn" id="save-name">Rename</button></div>
      ${s.users.length > 1 ? raw(html`
        <div class="field" style="margin-top:18px"><label for="switch">Switch profile</label>
          <select id="switch">${s.users.map((u) => raw(html`<option value="${u.id}" ${u.id === s.user.id ? "selected" : ""}>${u.name}</option>`))}</select></div>`) : ""}
      <div class="actions">
        <button class="btn" id="new-user">Add a profile</button>
        ${s.users.length > 1 ? raw('<button class="btn btn-ghost btn-danger" id="del-user">Delete this profile</button>') : ""}
      </div>

      <h2>Use it from your phone</h2>
      <p class="lead">Open this site on your phone, then use <strong>Add to Home Screen</strong>. On Android, Job Hacker then appears in the share menu: share a job from LinkedIn or Chrome and it opens ready to save.</p>
      <p class="lead">On iPhone, make a Shortcut that opens <code>${origin}/?add=</code> followed by the shared URL, and turn on “Show in Share Sheet”.</p>

      <h2>Backup and import</h2>
      <p class="lead">Download everything as one file, or bring in a backup or the export from the old tracker. Jobs already saved (same link) are skipped; if a CSV shows one was reposted, its posted date moves to the newest date in the file.</p>
      <div class="actions">
        <a class="btn" href="/api/export" download>Download backup</a>
        <label class="btn" style="margin:0">Import backup<input type="file" id="imp-json" accept=".json,application/json" hidden></label>
        <label class="btn" style="margin:0">Import CSV<input type="file" id="imp-csv" accept=".csv,text/csv" hidden></label>
      </div>
      <p class="hint">CSV needs a <code>Job title</code> or <code>Job URL</code> column; <code>Company Name</code>, <code>Location</code>, <code>Status</code> and <code>Description</code> are used when present. New jobs with a link but no description can be filled in from their postings right after the import.</p>

      <h2>Automations</h2>
      <p class="lead">n8n or other tools can add jobs by sending a POST to <code>${origin}/api/jobs</code> with the header <code>X-API-Key</code> set to your <code>API_TOKEN</code>. Send <code>title</code>, <code>url</code>, <code>company</code>, <code>description</code> and so on as JSON.</p>
      <p class="lead">AI models: Claude is <strong>${engines().claude?.available ? "on" : "off"}</strong>, and the local model (${engines().local?.model || "Ollama"}) is <strong>${engines().local?.available ? "on" : "off"}</strong>.${engines().local?.available || !engines().local?.reason ? "" : ` ${engines().local.reason}`}</p>

      <h2>Log files</h2>
      <p class="lead">Job Hacker keeps one log file per day of what it did: imports (and what each row became), links read, AI calls, overnight runs and errors. If something looks wrong, download that day's file and send it along. Logs never include your API key, resumes or job descriptions.</p>
      <div id="logs"><p class="hint">Loading…</p></div>

      ${s.auth_required ? raw('<h2>Session</h2><div class="actions"><button class="btn" id="logout">Log out</button></div>') : ""}
    </div>`);

  const reload = () => { state.jobs = null; state.session = null; route(); };
  drawNightly(document.getElementById("nightly"));
  drawLogs(document.getElementById("logs"));
  if (params?.get("at") === "nightly") document.getElementById("h-nightly")?.scrollIntoView({ block: "start" });

  $main.querySelectorAll("[data-persona]").forEach((b) => b.addEventListener("click", () => editPersona(personas.find((p) => p.id === Number(b.dataset.persona)), personas)));
  document.getElementById("add-persona").addEventListener("click", () => editPersona(null, personas));

  document.getElementById("save-prefs").addEventListener("click", async () => {
    await api("/me", { method: "PATCH", json: { preferences: document.getElementById("prefs").value } });
    toast("Saved");
  });
  document.getElementById("save-name").addEventListener("click", async () => {
    try { await api("/me", { method: "PATCH", json: { name: document.getElementById("pname").value } }); toast("Renamed"); reload(); }
    catch (e) { toast(e.message, "error"); }
  });
  document.getElementById("switch")?.addEventListener("change", async (e) => {
    await api(`/users/${e.target.value}/select`, { method: "POST" });
    toast("Switched profile"); reload();
  });
  document.getElementById("new-user").addEventListener("click", async () => {
    const name = prompt("Name for the new profile");
    if (!name) return;
    try { await api("/users", { json: { name } }); toast(`Now using ${name}`); reload(); }
    catch (e) { toast(e.message, "error"); }
  });
  document.getElementById("del-user")?.addEventListener("click", async () => {
    if (!confirm(`Delete the profile “${s.user.name}” and all its jobs? This can't be undone.`)) return;
    await api(`/users/${s.user.id}`, { method: "DELETE" });
    toast("Profile deleted"); reload();
  });
  const upload = (input, path) => input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api(path, { method: "POST", body: fd });
      toast(importSummary(r));
      state.jobs = null;
      await fillAfterImport(r);
    } catch (e) { toast(e.message, "error"); }
    input.value = "";
  });
  upload(document.getElementById("imp-json"), "/import/backup");
  upload(document.getElementById("imp-csv"), "/import/csv");
  document.getElementById("logout")?.addEventListener("click", async () => {
    await api("/logout", { method: "POST" }); state.session = null; state.jobs = null; route();
  });
}

// After a CSV import, offer to read the new jobs' details from their links straight away.
async function fillAfterImport(r) {
  const ids = r?.to_fill || [];
  if (!ids.length) return;
  await new Promise((res) => setTimeout(res, 900));  // let the import summary be seen first
  const done = await fillJobs(ids, `${ids.length} imported job${ids.length === 1 ? " has" : "s have"} a link but no description yet. Read ${ids.length === 1 ? "it" : "them"} from the posting${ids.length === 1 ? "" : "s"} now? Cancel to do it later from the No description filter.`);
  if (done.length) state.jobs = null;
}

function importSummary(r) {
  const added = r.jobs ?? r.added ?? 0;
  const parts = [`Imported ${added} job${added === 1 ? "" : "s"}`];
  if (r.duplicates) parts.push(`${r.duplicates} already saved`);
  if (r.reposted) parts.push(`${r.reposted} reposted (posted date updated)`);
  if (r.dead) parts.push(`${r.dead} taken down left out`);
  if (r.skipped) parts.push(`${r.skipped} without a title skipped`);
  return parts.join(", ") + ". Each row is listed in today's log (Settings → Log files).";
}

async function drawLogs(box) {
  if (!box) return;
  let r;
  try { r = await api("/logs"); } catch (e) { box.innerHTML = html`<p class="hint">${e.message}</p>`; return; }
  const size = (b) => (b < 1024 ? `${b} B` : b < 1048576 ? `${Math.round(b / 1024)} KB` : `${(b / 1048576).toFixed(1)} MB`);
  const day = (d) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  box.innerHTML = r.files.length
    ? html`<ul class="log-files">${r.files.slice(0, 14).map((f) => raw(html`<li><a href="/api/logs/${f.name}" download>${day(f.date)}</a> <span class="hint">${size(f.size)}</span></li>`))}</ul>
      <p class="hint">Kept for ${r.keep_days} days in <code>${r.folder}</code>.</p>`
    : html`<p class="hint">No log files yet.</p>`;
}

// ---------- overnight runs ----------

const TASK = { resume: "Tailored resume", fit: "Fit check", cover_letter: "Cover letter" };
let nightlyTimer;

async function drawNightly(box) {
  clearTimeout(nightlyTimer);
  if (!box || !box.isConnected) return;
  let n;
  try { n = await api("/nightly"); } catch (e) { box.innerHTML = html`<p class="hint">${e.message}</p>`; return; }
  const st = n.settings;
  const all = engines();
  const p = n.pending;
  if (!state.jobs) state.jobs = await api("/jobs");
  const noDesc = state.jobs.filter((j) => j.status !== "closed" && !j.has_description).length;
  const engineOk = all[st.engine]?.available;
  const last = n.runs[0];

  box.innerHTML = html`
    <div class="panel nightly">
      <label class="switch"><input type="checkbox" id="n-on" ${st.enabled ? "checked" : ""}><span>Run every night</span></label>
      <div class="grid2" style="margin-top:14px">
        <div class="field"><label for="n-time">Time</label><input id="n-time" type="time" value="${st.run_time}" step="300"><p class="hint">${n.timezone} time</p></div>
        <div class="field"><label for="n-engine">Model</label>
          <select id="n-engine">
            <option value="local" ${st.engine === "local" ? "selected" : ""}>${engineName("local")}</option>
            <option value="claude" ${st.engine === "claude" ? "selected" : ""}>Claude</option>
          </select>
          ${engineOk ? "" : raw(html`<p class="hint warn-text">${all[st.engine]?.reason || "Not available right now."}</p>`)}
        </div>
      </div>
      <div class="grid2" style="margin-top:4px">
        <div class="field"><label for="n-max">Jobs per night</label><input id="n-max" type="number" min="1" max="2000" step="1" value="${st.max_jobs}">
          <p class="hint">About 2 minutes a job with the local model, under a minute with Claude.</p></div>
        <div class="field"><label for="n-stop-at">Stop by <span class="hint" style="display:inline">(optional)</span></label>
          <div class="row" style="gap:6px;flex-wrap:nowrap"><input id="n-stop-at" type="time" value="${st.stop_at || ""}" step="300">${st.stop_at ? raw('<button class="btn btn-ghost btn-s" id="n-stop-clear" type="button">Clear</button>') : ""}</div>
          <p class="hint">${st.stop_at ? `Finishes the job it's on, then stops at ${st.stop_at}. The rest wait for the next night.` : "Leave empty to keep going until the night's jobs are done."}</p></div>
      </div>
      <fieldset class="checks"><legend class="label">Make</legend>
        <label><input type="checkbox" data-opt="do_resume" ${st.do_resume ? "checked" : ""}> Tailored resumes</label>
        <label><input type="checkbox" data-opt="do_fit" ${st.do_fit ? "checked" : ""}> Fit checks</label>
        <label><input type="checkbox" data-opt="do_cover" ${st.do_cover ? "checked" : ""}> Cover letters</label>
      </fieldset>
      <h3 class="pick-h">Tonight's list</h3>
      <p class="pick-sum" id="n-sum">${raw(pendingText(p))}</p>
      <p><a href="#/" id="n-pick">Pick jobs on the job list</a>: tick the Tonight column, and sort or filter by it.</p>
      ${noDesc ? raw(html`<p class="hint">${noDesc} job${noDesc === 1 ? " can't" : "s can't"} be picked because ${noDesc === 1 ? "it has" : "they have"} no description yet. Fill ${noDesc === 1 ? "it" : "them"} in from the Bulk tab on Add job.</p>`) : ""}
      <div class="actions">
        ${n.running
          ? raw('<button class="btn" id="n-stop">Stop the run</button>')
          : raw(html`<button class="btn" id="n-run" ${p.jobs ? "" : "disabled"}>Run the list now</button>`)}
      </div>
    </div>
    ${last ? raw(runHtml(last, n.running)) : ""}
    ${n.runs.length > 1 ? raw(html`<details class="runs-old"><summary>Earlier runs</summary>${n.runs.slice(1).map((r) => raw(runHtml(r, false, true)))}</details>`) : ""}`;

  const save = async (patch) => {
    try { await api("/nightly", { method: "PATCH", json: patch }); toast("Saved"); drawNightly(box); }
    catch (e) { toast(e.message, "error"); }
  };
  box.querySelector("#n-on").addEventListener("change", (e) => save({ enabled: e.target.checked }));
  box.querySelector("#n-time").addEventListener("change", (e) => e.target.value && save({ run_time: e.target.value }));
  box.querySelector("#n-engine").addEventListener("change", (e) => save({ engine: e.target.value }));
  box.querySelector("#n-max").addEventListener("change", (e) => {
    const v = Math.round(Number(e.target.value));
    if (v >= 1 && v <= 2000) save({ max_jobs: v }); else { toast("Pick a number from 1 to 2000.", "error"); e.target.value = st.max_jobs; }
  });
  box.querySelector("#n-stop-at").addEventListener("change", (e) => save({ stop_at: e.target.value || "" }));
  box.querySelector("#n-stop-clear")?.addEventListener("click", () => save({ stop_at: "" }));
  box.querySelectorAll("[data-opt]").forEach((c) => c.addEventListener("change", () => save({ [c.dataset.opt]: c.checked })));
  box.querySelector("#n-pick").addEventListener("click", () => setFilters(["tonight"]));
  box.querySelector("#n-run")?.addEventListener("click", async () => {
    try { await api("/nightly/run", { method: "POST" }); toast("Started"); setTimeout(() => drawNightly(box), 800); }
    catch (e) { toast(e.message, "error"); }
  });
  box.querySelector("#n-stop")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Stopping after this one…";
    await api("/nightly/stop", { method: "POST" });
  });
  if (n.running) nightlyTimer = setTimeout(() => drawNightly(box), 5000);
}

function pendingText(p) {
  if (!p.queued) return "Empty. Tick jobs in the Tonight column on the job list, or use “Add to tonight's list” on a job.";
  const parts = [p.resume && `${p.resume} resume${p.resume === 1 ? "" : "s"}`, p.fit && `${p.fit} fit check${p.fit === 1 ? "" : "s"}`,
    p.cover_letter && `${p.cover_letter} cover letter${p.cover_letter === 1 ? "" : "s"}`].filter(Boolean);
  const capped = p.limit && p.ready > p.jobs;
  const nights = capped ? Math.ceil(p.ready / p.limit) : 1;
  return esc(`${p.queued} job${p.queued === 1 ? "" : "s"} picked. `) +
    (parts.length
      ? (capped ? esc(`Each run does up to ${p.limit} jobs, so the next run makes `) : "") + `<strong>${esc(parts.join(", "))}</strong>` +
        (capped ? esc(`. The other ${p.ready - p.jobs} wait for later runs (about ${nights - 1} more night${nights - 1 === 1 ? "" : "s"}); raise “Jobs per night” in Settings to do more at once.`) : esc(" to make."))
      : esc("Nothing to make: turn on resumes, fit checks or cover letters above."));
}

function runHtml(r, running, compact = false) {
  const when = new Date(r.started_at).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  const head = running
    ? `Running now: ${r.log.length} of ${r.total} done`
    : { done: "Finished", failed: "Couldn't run", stopped: "Stopped", running: "Interrupted" }[r.status] || r.status;
  const counts = [r.made && `${r.made} made`, r.failed && `${r.failed} failed`].filter(Boolean).join(", ");
  return html`
    <div class="run ${compact ? "compact" : ""}">
      <p class="run-head"><strong>${head}</strong> <span class="hint" style="display:inline">${when}${r.trigger === "manual" ? ", started by hand" : ""}${r.engine ? `, ${r.engine}` : ""}${counts ? `. ${counts}.` : ""}</span></p>
      ${running ? raw(html`<div class="batch-bar"><span style="width:${r.total ? (r.log.length / r.total) * 100 : 0}%"></span></div>`) : ""}
      ${r.log.length ? raw(html`<ul class="batch-list">${r.log.slice().reverse().map((e) => raw(html`
        <li class="${e.ok ? "ok" : "fail"}"><span class="b-dot"></span><span class="b-main">
          ${e.job_id ? raw(html`<a class="b-title" href="#/job/${e.job_id}">${[e.title, e.company].filter(Boolean).join(", ")}</a>`) : ""}
          <span class="b-note">${e.task ? TASK[e.task] : ""}${e.note ? `${e.task ? ": " : ""}${e.note}` : ""}${e.seconds ? ` (${e.seconds}s)` : ""}</span></span></li>`))}</ul>`) : ""}
    </div>`;
}

function editPersona(p, personas) {
  const box = document.getElementById("persona-editor");
  box.innerHTML = html`
    <form class="panel" id="f-persona" style="margin-top:16px">
      <div class="field"><label for="pn">Name</label><input id="pn" name="name" type="text" value="${p ? p.name : (personas.length ? "" : "Main resume")}" placeholder="e.g. Scrum Master" required></div>
      <div class="field"><label for="pr">Resume text</label><textarea id="pr" name="resume_text" class="tall" placeholder="Paste your full resume as plain text.">${p ? p.resume_text : ""}</textarea></div>
      <div class="actions">
        <button class="btn btn-primary" type="submit">Save resume</button>
        ${p && !p.is_default ? raw('<button class="btn" type="button" id="p-default">Make default</button>') : ""}
        ${p ? raw('<button class="btn btn-ghost btn-danger" type="button" id="p-del">Delete</button>') : ""}
      </div>
    </form>`;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  const form = document.getElementById("f-persona");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: form.name.value, resume_text: form.resume_text.value };
    try {
      if (p) await api(`/personas/${p.id}`, { method: "PATCH", json: body });
      else await api("/personas", { json: body });
      toast("Resume saved"); renderSettings();
    } catch (err) { toast(err.message, "error"); }
  });
  document.getElementById("p-default")?.addEventListener("click", async () => { await api(`/personas/${p.id}/default`, { method: "POST" }); renderSettings(); });
  document.getElementById("p-del")?.addEventListener("click", async () => {
    if (!confirm(`Delete the resume “${p.name}”?`)) return;
    await api(`/personas/${p.id}`, { method: "DELETE" }); renderSettings();
  });
}

// ---------- boot ----------

function handleShareIntent() {
  // Android share target (?share_url / ?share_text) and iOS Shortcut (?add=)
  const q = new URLSearchParams(location.search);
  const cand = q.get("add") || q.get("share_url") || q.get("share_text") || "";
  const m = cand.match(/https?:\/\/\S+/);
  if (m || q.has("add") || q.has("share_text") || q.has("share_url")) {
    history.replaceState(null, "", "/");
    if (m) location.hash = "#/add?url=" + encodeURIComponent(m[0]);
    else if (cand) location.hash = "#/add?mode=paste";
  }
}

// Column headers stick just under the top bar, whatever its height.
const setBarH = () => document.documentElement.style.setProperty("--topbar-h", `${document.querySelector(".topbar").offsetHeight}px`);
window.addEventListener("resize", setBarH);
setBarH();

window.addEventListener("hashchange", route);
window.addEventListener("scroll", () => document.querySelector(".topbar").classList.toggle("scrolled", scrollY > 4), { passive: true });
handleShareIntent();
route();

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
