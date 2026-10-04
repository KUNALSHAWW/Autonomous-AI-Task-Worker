// proof: run console. No framework: a small event reducer and string templates.
// Every screen is built from the run's event stream (live over SSE, or replayed from disk).

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
};

/* ------------------------------------------------------------------ icons */
const I = (d, s = 16) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  browser_goto: I('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
  browser_click: I('<path d="M5 3l14 7-6 2-2 6z"/>'),
  browser_fill_form: I('<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>'),
  browser_read: I('<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'),
  read_file: I('<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6M9 13h7M9 17h7"/>'),
  http_request: I('<path d="M8 4c-2 0-3 1-3 3v3l-2 2 2 2v3c0 2 1 3 3 3M16 4c2 0 3 1 3 3v3l2 2-2 2v3c0 2-1 3-3 3"/>'),
  remember: I('<path d="M6 3h12v18l-6-4-6 4z"/>'),
  update_plan: I('<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/>'),
  ask_user: I('<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5V14M12 17.5v.01"/>'),
  notify_user: I('<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>'),
  finish: I('<path d="M5 21V4M5 4h12l-2 4 2 4H5"/>'),
  ok: I('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 14),
  err: I('<path d="M6 6l12 12M18 6L6 18"/>', 14),
  wait: I('<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>', 14),
  shield: I('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>', 18),
  alert: I('<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.01"/>', 18),
  x: I('<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>', 18),
  prev: I('<path d="M15 6l-6 6 6 6"/>', 14),
  next: I('<path d="M9 6l6 6-6 6"/>', 14),
  app: I('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>', 15),
  mail: I('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>', 15),
  erp: I('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>', 15),
  portal: I('<path d="M3 21h18M5 21V8l7-5 7 5v13M9 21v-6h6v6"/>', 15),
  drive: I('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', 15),
};
const APP_ICON = { Mail: ICON.mail, ERP: ICON.erp, "Supplier portal": ICON.portal, Drive: ICON.drive };

/* ------------------------------------------------------------------ state */
const S = { runs: [], sandbox: null, health: null, examples: [], run: null, es: null, timer: null };

function newRun(id) {
  return {
    id, task: "", status: "queued", outcome: "", model: "", dry: false, t0: null, t1: null, phase: "",
    contract: null, steps: {}, items: [], pending: null, checks: [], verification: null, report: null,
    claimed: null, facts: {}, error: "", selected: null, follow: true, tab: "screen", open: new Set(),
  };
}

function apply(R, ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "created": R.task = d.task; R.model = d.model; R.dry = d.dry_run; R.t0 = ev.ts; break;
    case "status":
      R.status = d.status; if (d.outcome) R.outcome = d.outcome;
      if (["done", "failed", "stopped"].includes(d.status)) R.t1 = ev.ts;
      break;
    case "phase":
      if (d.phase === "verify") {
        R.verifyRounds = (R.verifyRounds || 0) + 1;
        R.items.push({ kind: "phase", text: R.verifyRounds > 1 ? `Independent verification, round ${R.verifyRounds}` : "Independent verification" });
      } else if (d.phase === "act" && R.phase === "verify") {
        R.items.push({ kind: "phase", text: "Back to work on what verification found" });
      }
      R.phase = d.phase;
      break;
    case "contract": R.contract = d.contract; break;
    case "step_started":
      R.steps[d.n] = { ...d, running: true, ts: ev.ts, phase: R.phase === "verify" ? "verify" : "act" };
      R.items.push({ kind: "step", n: d.n });
      break;
    case "step": {
      const st = { ...(R.steps[d.n] || { ts: ev.ts }), ...d, running: false };
      if (!R.steps[d.n]) R.items.push({ kind: "step", n: d.n });
      R.steps[d.n] = st;
      if (d.tool === "remember" && d.ok && d.args && d.args.facts) {
        for (const [k, v] of Object.entries(d.args.facts)) R.facts[k] = { key: k, value: v, source: d.args.source, quote: d.args.quote };
      }
      if (d.tool === "update_plan" && R.contract && d.args && d.args.done) {
        for (const it of R.contract.checklist) if (d.args.done.includes(it.id)) it.done = true;
      }
      if (R.follow && d.screenshot) R.selected = d.n;
      break;
    }
    case "note": R.items.push({ kind: "note", text: d.message }); break;
    case "error": R.error = d.message; R.items.push({ kind: "note", text: d.message, bad: true }); break;
    case "question": R.pending = d; break;
    case "answer":
      R.items.push({ kind: "msg", who: "You answered", text: d.answer });
      R.pending = null;
      break;
    case "notify": R.items.push({ kind: "msg", who: "Message from the worker", text: d.message }); break;
    case "claimed": R.claimed = d; break;
    case "check": R.checks.push(d); break;
    case "verification":
      R.verification = d;
      if ((d.problems || []).length && R.claimed && R.claimed.status === "completed" && d.repairable)
        R.items.push({ kind: "note", text: `Verification rejected the finish: ${d.problems.join("; ")}`, bad: true });
      R.checks = [];
      break;
    case "report":
      R.report = d;
      for (const f of d.facts || []) R.facts[f.key] = f;
      if (R.tab === "screen" && R.follow) R.tab = "checks";
      break;
  }
}

/* ------------------------------------------------------------------ helpers */
const OUTCOME = {
  verified: ["ok", "Verified", "Every check passed against the live systems."],
  partially_verified: ["warn", "Partly verified", "Some checks failed. Review before relying on it."],
  unverified: ["warn", "Done, not fully verified", "Some checks could not run. Spot check the result."],
  needs_attention: ["warn", "Needs your attention", "The worker stopped because it needs a decision or input."],
  dry_run: ["warn", "Rehearsal complete", "Nothing was changed. The writes it would have made are listed."],
  failed: ["bad", "Not completed", "The task was not finished."],
  stopped: ["", "Stopped", "You stopped this run."],
};

function statusBadge(R) {
  if (R.status === "running") return `<span class="badge live"><span class="pulse"></span>Working</span>`;
  if (R.status === "waiting_for_user") return `<span class="badge warn">Waiting for you</span>`;
  if (R.status === "queued") return `<span class="badge">Queued</span>`;
  const o = OUTCOME[R.outcome] || OUTCOME.failed;
  return `<span class="badge ${o[0]}">${o[1]}</span>`;
}

function dotClass(r) {
  if (r.status === "running") return "live";
  if (r.status === "waiting_for_user") return "warn";
  const o = OUTCOME[r.outcome];
  return o ? o[0] : r.status === "failed" ? "bad" : "";
}

const pad = (n) => String(Math.floor(n)).padStart(2, "0");
const clock = (s) => (s >= 3600 ? `${Math.floor(s / 3600)}:` : "") + `${pad((s % 3600) / 60)}:${pad(s % 60)}`;
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};

function shortUrl(u) {
  if (!u) return "";
  try { const x = new URL(u, location.href); return x.pathname + x.search; } catch (e) { return u; }
}

function appFor(url) {
  const path = shortUrl(url);
  const apps = (S.sandbox && S.sandbox.apps) || [];
  let best = null;
  for (const a of apps) if (path.startsWith(a.path.replace(/\/$/, "")) && (!best || a.path.length > best.path.length)) best = a;
  return best ? best.name : "Browser";
}

function how(st) {
  const a = st.args || {};
  switch (st.tool) {
    case "browser_goto": return `open ${shortUrl(a.url)}`;
    case "browser_click": return /^\d+$/.test(String(a.element)) ? `click [${a.element}]` : `click "${a.element}"`;
    case "browser_fill_form": {
      const n = a.fields ? Object.keys(a.fields).length : 0;
      return `fill ${n} field${n === 1 ? "" : "s"}${a.submit ? " and submit" : ""}`;
    }
    case "browser_read": return a.find ? `search page for "${a.find}"` : `read page${a.part > 1 ? ` part ${a.part}` : ""}`;
    case "read_file": return `read ${a.path || "workspace files"}`;
    case "http_request": return `${a.method || "GET"} ${shortUrl(a.url)}`;
    case "remember": return `save ${Object.keys(a.facts || {}).join(", ")}`;
    case "update_plan": return a.done && a.done.length ? `tick ${a.done.join(", ")}` : "update plan";
    case "ask_user": return "ask you";
    case "notify_user": return "message you";
    case "finish": return `finish as ${a.status}`;
    default: return st.tool;
  }
}

const sentence = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const titleFor = (st) => sentence(st.reason) || sentence(how(st));

function stepState(st) {
  if (st.running) return ["run", '<span class="pulse"></span>'];
  if (st.error_kind === "needs_approval") return ["wait", ICON.wait];
  return st.ok ? ["ok", ICON.ok] : ["err", ICON.err];
}

/* ------------------------------------------------------------------ rail */
async function loadRuns() {
  try { S.runs = await api("/api/runs"); } catch (e) { return; }
  const cur = S.run && S.run.id;
  $("#runs").innerHTML = S.runs.length ? S.runs.slice(0, 40).map((r) => `
    <li><button data-run="${esc(r.id)}" aria-current="${r.id === cur}">
      <span class="dot ${dotClass(r)}"></span><span class="t">${esc(r.task)}</span>
      <span class="sub">${esc(ago(r.created_at))}${r.dry_run ? ", rehearsal" : ""}</span>
    </button></li>`).join("") : `<li class="empty">No runs yet</li>`;
}

function renderApps() {
  const apps = (S.sandbox && S.sandbox.apps) || [];
  $("#apps").innerHTML = apps.map((a) => `<li><a href="${esc(a.path)}" target="_blank" rel="noopener">${APP_ICON[a.name] || ICON.app}${esc(a.name)}</a></li>`).join("");
}

async function loadHealth() {
  try { S.health = await api("/api/health"); } catch (e) { S.health = null; }
  const h = S.health;
  $("#model-dot").className = "dot " + (h && h.llm_configured ? "ok" : "warn");
  $("#model-name").textContent = h ? (h.llm_configured ? `${h.model} via ${h.provider === "ollama" ? "Ollama" : h.provider}` : "No model configured") : "Server unreachable";
  $("#model-name").title = $("#model-name").textContent;
}

/* ------------------------------------------------------------------ composer */
const EXAMPLE_TAGS = ["Invoice by email", "Supplier portal", "Needs approval", "New vendor", "Fraud check", "Ambiguous name", "Report by email"];

function renderComposer() {
  closeStream();
  S.run = null;
  loadRuns();
  const sb = S.sandbox || { chaos: {}, chaos_help: {} };
  const configured = S.health && S.health.llm_configured;
  $("#main").innerHTML = `
  <div class="composer"><div class="inner">
    <h1>What should the worker do?</h1>
    <p class="lede">Describe the outcome you want. It works through the company apps in a real browser, asks before anything risky, and checks the result on its own before telling you it is done.</p>
    <form class="ask" id="ask">
      <label class="sr" for="task">Task</label>
      <textarea id="task" placeholder="Find the latest invoice from Globex, enter it into the ERP and tell me when it's done" required></textarea>
      <div class="bar">
        <label class="check"><input type="checkbox" id="dry"> Rehearse only, change nothing</label>
        <span class="spacer"></span>
        <button class="btn primary" id="start" type="submit">Start task <kbd>Ctrl ↵</kbd></button>
      </div>
    </form>
    ${configured === false ? `<div class="notice warn">No model is configured yet. Set <code>OLLAMA_API_KEY</code> and <code>OLLAMA_MODEL</code> on the server, then restart it.</div>` : ""}
    <div class="section-h"><h2>Try one of these</h2><p>Names and numbers change every time the company is rebuilt</p></div>
    <ul class="examples">${S.examples.map((e, i) => `<li><button data-example="${i}"><span class="tag">${esc(EXAMPLE_TAGS[i] || "Task")}</span><span>${esc(e)}</span></button></li>`).join("")}</ul>
    <div class="section-h"><h2>The company</h2><p>Acme Corp is a sandbox: real web apps, generated from a seed</p></div>
    <form class="world" id="world">
      <div class="row"><span class="lbl">Seed<small>Different seed, different vendors, invoices, labels and date formats</small></span><input type="number" id="seed" min="0" value="${esc(sb.seed ?? 7)}"></div>
      ${Object.entries(sb.chaos_help || {}).map(([k, help]) => `
      <div class="row"><span class="lbl">${esc(k.replace(/_/g, " "))}<small>${esc(help)}</small></span>
        <label class="switch"><input type="checkbox" name="${esc(k)}" ${sb.chaos && sb.chaos[k] ? "checked" : ""} aria-label="${esc(k)}"><span></span></label></div>`).join("")}
      <div class="row"><span class="lbl"><small id="world-msg">Rebuilding resets mail, bills and vendors to a fresh state.</small></span><button class="btn" type="submit">Rebuild company</button></div>
    </form>
    <div class="section-h"><h2>How a run works</h2><p>A LangGraph state graph</p></div>
    <div class="flow"><span>intake</span><i>to</i><span>act</span><i>loops with</i><span>human</span><i>then</i><span>verify</span><i>then</i><span>report</span></div>
    <p class="lede" style="margin-top:10px">Questions and approvals pause the graph with <code>interrupt()</code> and resume it with your answer. A policy gate in code blocks risky writes until you approve them. Verification re-reads the sources and probes the live systems; the worker's own claim is not taken on trust.</p>
  </div></div>`;
  const ta = $("#task");
  ta.focus();
  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); startTask(); });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startTask(); } });
  document.querySelectorAll("[data-example]").forEach((b) => b.addEventListener("click", () => {
    ta.value = S.examples[+b.dataset.example]; ta.focus();
  }));
  $("#world").addEventListener("submit", rebuildWorld);
}

async function startTask() {
  const task = $("#task").value.trim();
  if (task.length < 3) { $("#task").focus(); return; }
  $("#start").disabled = true;
  try {
    const r = await api("/api/runs", { method: "POST", body: JSON.stringify({ task, dry_run: $("#dry").checked }) });
    location.hash = `#/run/${r.id}`;
  } catch (e) {
    toast(`Could not start the task: ${e.message}`);
    $("#start").disabled = false;
  }
}

async function rebuildWorld(e) {
  e.preventDefault();
  const chaos = {};
  document.querySelectorAll("#world .switch input").forEach((i) => { chaos[i.name] = i.checked; });
  const msg = $("#world-msg");
  msg.textContent = "Rebuilding...";
  try {
    const r = await api("/api/sandbox/reset", { method: "POST", body: JSON.stringify({ seed: +$("#seed").value || 0, chaos }) });
    msg.textContent = `Rebuilt from seed ${r.seed}.`;
    await loadSandbox();
    S.examples = await api("/api/examples").catch(() => S.examples);
    renderComposer();
  } catch (err) {
    msg.textContent = `Not rebuilt: ${err.message}`;
  }
}

/* ------------------------------------------------------------------ run view */
function openRun(id) {
  closeStream();
  S.run = newRun(id);
  $("#main").innerHTML = `
  <div class="run">
    <header class="run-head" id="run-head"></header>
    <div class="run-body">
      <section class="ledger-col" aria-label="Steps">
        <div id="verdict"></div>
        <div class="ledger-scroll" id="ledger-scroll"><div id="plan"></div><ul class="ledger" id="ledger"></ul></div>
        <div id="sheet"></div>
      </section>
      <section class="evidence" aria-label="Evidence">
        <div class="tabs" role="tablist" id="tabs"></div>
        <div class="pane" id="pane"></div>
      </section>
    </div>
  </div>`;
  loadRuns();
  const es = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
  S.es = es;
  const handle = (msg) => {
    if (!S.run || S.run.id !== id) return;
    const ev = JSON.parse(msg.data);
    apply(S.run, ev);
    schedule(ev.type);
  };
  for (const t of ["created", "status", "phase", "contract", "step_started", "step", "note", "error", "question", "answer", "notify", "claimed", "check", "verification", "report"]) es.addEventListener(t, handle);
  es.addEventListener("end", () => { es.close(); loadRuns(); });
  es.onerror = () => { if (S.run && ["done", "failed", "stopped"].includes(S.run.status)) es.close(); };
  S.timer = setInterval(renderHead, 1000);
}

function closeStream() {
  if (S.es) { S.es.close(); S.es = null; }
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
}

let queued = new Set();
function schedule(type) {
  queued.add(type);
  if (queued.size > 1) return;
  requestAnimationFrame(() => {
    const types = queued; queued = new Set();
    renderHead(); renderLedger(); renderEvidence();
    if (types.has("question") || types.has("answer") || types.has("status")) renderSheet();
    if (types.has("status")) loadRuns();
  });
}

function renderHead() {
  const R = S.run; if (!R) return;
  const end = R.t1 || (["done", "failed", "stopped"].includes(R.status) ? R.t0 : Date.now() / 1000);
  const secs = R.t0 ? Math.max(0, end - R.t0) : 0;
  const steps = Object.values(R.steps).filter((s) => s.phase !== "verify").length;
  const live = ["running", "waiting_for_user", "queued"].includes(R.status);
  $("#run-head").innerHTML = `
    <div class="task"><h1>${esc(R.task || "Loading run")}</h1>
      <div class="meta"><span>Elapsed <span class="mono">${clock(secs)}</span></span><span>Steps <span class="mono">${steps}</span></span>
      ${R.model ? `<span>Model <span class="mono">${esc(R.model.split(" @ ")[0])}</span></span>` : ""}${R.dry ? "<span>Rehearsal, nothing is written</span>" : ""}</div></div>
    ${statusBadge(R)}
    ${live ? `<button class="btn ghost danger" id="stop">Stop</button>` : `<button class="btn" id="again">Run again</button>`}`;
  const stop = $("#stop");
  if (stop) stop.onclick = async () => { await api(`/api/runs/${R.id}/cancel`, { method: "POST" }).catch(() => {}); };
  const again = $("#again");
  if (again) again.onclick = () => { location.hash = "#/"; setTimeout(() => { const t = $("#task"); if (t) t.value = R.task; }, 0); };
}

function renderVerdict(R) {
  const rep = R.report;
  if (!rep) return "";
  const [cls, title, fallback] = OUTCOME[rep.outcome] || OUTCOME.failed;
  const crit = (rep.verification && rep.verification.criteria) || [];
  const passed = crit.filter((c) => c.status === "pass").length;
  const sub = crit.length ? `${passed} of ${crit.length} checks passed against the live systems` : fallback;
  const mark = cls === "ok" ? ICON.shield : cls === "bad" ? ICON.x : ICON.alert;
  const st = rep.stats || {};
  return `<div class="verdict ${cls}">
    <div class="v-line"><span class="v-mark">${mark}</span><div><h2>${esc(title)}</h2><div class="v-sub">${esc(sub)}</div></div></div>
    <p class="summary">${esc(rep.summary)}</p>
    <div class="stats">
      <div><b>${st.steps ?? 0}</b><span>steps</span></div>
      <div><b>${st.recovered_failures ?? 0}</b><span>failures recovered</span></div>
      <div><b>${st.repair_rounds ?? 0}</b><span>repair rounds</span></div>
      <div><b>${st.llm_calls ?? 0}</b><span>model calls</span></div>
      <div><b>${clock(st.duration_s || 0)}</b><span>duration</span></div>
    </div></div>`;
}

function renderPlan(R) {
  const c = R.contract;
  if (!c) return `<div class="block"><h3>Plan</h3><p class="goal" style="color:var(--text-2)"><span class="pulse"></span> Reading the request</p></div>`;
  return `<div class="block"><h3>Goal</h3><p class="goal">${esc(c.goal)}</p>
    <h3>Plan</h3><ul class="plan">${c.checklist.map((it) => `<li class="${it.done ? "done" : ""}"><span class="box">${it.done ? ICON.ok : ""}</span>${esc(it.text)}</li>`).join("")}</ul></div>`;
}

function renderLedger() {
  const R = S.run; if (!R) return;
  const box = $("#ledger-scroll");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  $("#verdict").innerHTML = renderVerdict(R);
  $("#plan").innerHTML = renderPlan(R);
  const rows = [];
  for (const it of R.items) {
    if (it.kind === "phase") { rows.push(`<li class="phase-row">${esc(it.text)}</li>`); continue; }
    if (it.kind === "note") { rows.push(`<li class="note-row ${it.bad ? "" : "info"}">${esc(it.text)}</li>`); continue; }
    if (it.kind === "msg") { rows.push(`<li class="msg-row"><b>${esc(it.who)}</b>${esc(it.text)}</li>`); continue; }
    const st = R.steps[it.n]; if (!st) continue;
    const [cls, glyph] = stepState(st);
    const t = R.t0 && st.ts ? clock(Math.max(0, st.ts - R.t0)) : "";
    const sel = R.selected === st.n;
    rows.push(`<li><button class="step ${cls}" data-step="${st.n}" aria-selected="${sel}" aria-expanded="${R.open.has(st.n)}">
      <span class="when">${t}</span><span class="ico">${ICON[st.tool] || ICON.browser_goto}</span>
      <span class="what"><span class="title">${esc(titleFor(st))}</span><span class="how">${esc(how(st))}</span></span>
      <span class="st">${glyph}</span></button>
      ${R.open.has(st.n) ? `<div class="step-detail">${esc(st.summary || "")}${st.observation ? `<pre>${esc(st.observation)}</pre>` : ""}</div>` : ""}</li>`);
  }
  $("#ledger").innerHTML = rows.join("");
  if (nearBottom && R.follow) box.scrollTop = box.scrollHeight;
}

function renderSheet() {
  const R = S.run; if (!R) return;
  const p = R.pending;
  const el = $("#sheet");
  if (!p) { el.innerHTML = ""; return; }
  if (p.kind === "approval") {
    const ctx = p.context || {};
    const args = ctx.args || {};
    const fields = ctx.fields || args.fields || args.body || null;
    const table = fields && typeof fields === "object" ? `<table class="kv"><thead><tr><th>Field</th><th>Value to be written</th></tr></thead><tbody>
      ${Object.entries(fields).filter(([k, v]) => String(v ?? "").trim() !== "").map(([k, v]) => `<tr><td>${esc(k)}</td><td class="mono">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</tbody></table>` : "";
    const why = (p.question.split("\n")[0] || "").replace(/^Approval needed\.\s*/, "");
    el.innerHTML = `<div class="sheet approval" role="dialog" aria-label="Approval needed">
      <header>${ICON.alert}<h3>Approve this action?</h3><span class="policy">${esc(ctx.rule || "")}</span></header>
      <div class="q">${esc(why)}</div>${table}
      <div class="actions"><input id="note" placeholder="Optional note for the worker">
        <button class="btn" data-answer="deny">Deny <kbd>D</kbd></button>
        <button class="btn primary" data-answer="approve">Approve <kbd>A</kbd></button></div></div>`;
  } else {
    el.innerHTML = `<div class="sheet" role="dialog" aria-label="Question from the worker">
      <header>${ICON.ask_user}<h3>The worker has a question</h3></header>
      <div class="q">${esc(p.question)}</div>
      ${p.options && p.options.length ? `<div class="chips">${p.options.map((o) => `<button class="btn" data-answer="${esc(o)}">${esc(o)}</button>`).join("")}</div>` : ""}
      <div class="actions"><input id="note" placeholder="Type your answer"><button class="btn primary" data-answer="__typed">Send answer</button></div></div>`;
    setTimeout(() => { const n = $("#note"); if (n) n.focus(); }, 0);
  }
}

async function answer(value) {
  const R = S.run; if (!R || !R.pending) return;
  const note = ($("#note") && $("#note").value.trim()) || "";
  let text = value;
  if (value === "__typed") { if (!note) { $("#note").focus(); return; } text = note; }
  else if (note) text = `${value} - ${note}`;
  try { await api(`/api/runs/${R.id}/reply`, { method: "POST", body: JSON.stringify({ answer: text }) }); }
  catch (e) { toast(`Answer not delivered: ${e.message}`); }
}

/* ------------------------------------------------------------------ evidence pane */
function shotSteps(R) { return Object.values(R.steps).filter((s) => s.screenshot).sort((a, b) => a.n - b.n); }

function renderEvidence() {
  const R = S.run; if (!R) return;
  const facts = Object.values(R.facts);
  const crit = (R.verification && R.verification.criteria) || [];
  const changes = (R.report && R.report.changes_made) || [];
  const tabs = [["screen", "Screen", ""], ["facts", "Facts", facts.length], ["checks", "Checks", crit.length || R.checks.length], ["changes", "Changes", changes.length]];
  $("#tabs").innerHTML = tabs.map(([k, label, n]) => `<button role="tab" data-tab="${k}" aria-selected="${R.tab === k}">${label}${n !== "" && n ? `<span class="count">${n}</span>` : ""}</button>`).join("");
  const pane = $("#pane");
  if (R.tab === "screen") pane.innerHTML = paneScreen(R);
  else if (R.tab === "facts") pane.innerHTML = paneFacts(R, facts);
  else if (R.tab === "checks") pane.innerHTML = paneChecks(R);
  else pane.innerHTML = paneChanges(R, changes);
}

function paneScreen(R) {
  const shots = shotSteps(R);
  if (!shots.length) return `<div class="frame"><div class="chrome"><span class="app">Browser</span><span class="url">about:blank</span></div><div class="blank">${R.status === "running" ? "The browser view appears with the first page the worker opens." : "No screens were captured in this run."}</div></div>`;
  let st = R.steps[R.selected];
  if (!st || !st.screenshot) st = shots[shots.length - 1];
  const idx = shots.indexOf(st);
  const src = `/api/runs/${encodeURIComponent(R.id)}/files/shots/${encodeURIComponent(st.screenshot)}`;
  return `<div class="frame">
    <div class="chrome"><span class="app">${esc(appFor(st.url))}</span><span class="url" title="${esc(st.url)}">${esc(st.url)}</span>
      <span class="nav"><button class="btn ghost" data-shot="prev" aria-label="Previous screen" ${idx <= 0 ? "disabled" : ""}>${ICON.prev}</button>
      <span>${idx + 1} / ${shots.length}</span>
      <button class="btn ghost" data-shot="next" aria-label="Next screen" ${idx >= shots.length - 1 ? "disabled" : ""}>${ICON.next}</button></span></div>
    <img src="${src}" alt="Screen after step ${st.n}: ${esc(titleFor(st))}" loading="lazy"></div>
    <p class="caption"><b>Step ${st.n}.</b> ${esc(titleFor(st))}. <span class="mono" style="color:var(--text-3)">${esc(st.summary || "")}</span></p>
    ${R.follow ? "" : `<button class="btn" data-follow>Follow live</button>`}`;
}

function highlight(quote, value) {
  const q = esc(quote || "");
  const v = String(value ?? "").trim();
  if (!v) return q;
  const variants = [v, Number(v).toLocaleString("en-US", { minimumFractionDigits: 2 })].filter((x) => x && x !== "NaN");
  for (const x of variants) {
    const i = q.indexOf(esc(x));
    if (i >= 0) return q.slice(0, i) + `<mark>${esc(x)}</mark>` + q.slice(i + esc(x).length);
  }
  return q;
}

function paneFacts(R, facts) {
  if (!facts.length) return `<div class="empty-pane">Facts appear here as the worker finds them. Each one carries the exact text it came from, and is checked against that source later.</div>`;
  const fid = {};
  for (const f of (R.verification && R.verification.fidelity) || []) fid[f.key] = f;
  return `<h3>What the worker found</h3><p class="hint">Every value was saved with a verbatim quote from its source. After the run, a separate model re-read each source without seeing these values.</p>
  <table class="kv"><thead><tr><th>Field</th><th>Value</th><th>Source quote</th><th>Re-read</th></tr></thead><tbody>
  ${facts.map((f) => {
    const v = fid[f.key];
    const re = !v ? `<span style="color:var(--text-3)">pending</span>` : v.status === "match" ? `<span class="res pass">matches</span>` : v.status === "mismatch" ? `<span class="res fail">source says ${esc(v.source_value)}</span>` : `<span class="res unknown">not found</span>`;
    return `<tr><td>${esc(f.key)}</td><td class="mono">${esc(f.value)}</td><td><div class="quote">${highlight(f.quote, f.value)}</div><div style="color:var(--text-3);font-size:11px;margin-top:3px">${esc(f.source)}</div></td><td>${re}</td></tr>`;
  }).join("")}</tbody></table>`;
}

function paneChecks(R) {
  const v = R.verification;
  if (!v) {
    if (R.checks.length) return `<h3>Checking now</h3><ul class="check-list">${R.checks.map(checkRow).join("")}</ul>`;
    return `<div class="empty-pane">When the worker says it is done, an independent verifier checks the live systems. Results land here.</div>`;
  }
  const crit = v.criteria || [];
  return `<h3>Success criteria</h3><p class="hint">Turned into read-only probes before anything is trusted. Code runs them against the real systems.</p>
    ${crit.length ? `<ul class="check-list">${crit.map((c) => checkRow({ criterion: c.id, status: c.status, detail: c.detail, text: c.text })).join("")}</ul>` : `<div class="empty-pane">${esc(v.summary || "No criteria were checked.")}</div>`}
    ${(v.problems || []).length ? `<h3>Problems found</h3><ul class="check-list">${v.problems.map((p) => `<li><span class="ic-fail">${ICON.err}</span><div class="crit">${esc(p)}</div><span></span></li>`).join("")}</ul>` : ""}
    ${(v.ledger && v.ledger.warnings && v.ledger.warnings.length) ? `<h3>From the write ledger</h3><p class="hint">${v.ledger.warnings.map((w) => esc(w.replace(/https?:\/\/[^/\s]+/g, ""))).join("<br>")}</p>` : ""}`;
}

function checkRow(c) {
  const label = { pass: "Passed", fail: "Failed", unknown: "Could not check" }[c.status] || c.status;
  const ic = c.status === "pass" ? ICON.ok : c.status === "fail" ? ICON.err : ICON.wait;
  return `<li><span class="ic-${esc(c.status)}">${ic}</span><div><div class="crit">${esc(c.text || c.criterion)}</div><div class="det">${esc(c.detail)}</div></div><span class="res ${esc(c.status)}">${label}</span></li>`;
}

function paneChanges(R, changes) {
  if (!R.report) return `<div class="empty-pane">Every write the worker makes passes through the policy gate. The full list appears here when the run ends.</div>`;
  const ap = R.report.approvals || [];
  return `<h3>Writes to company systems</h3><p class="hint">Recorded by the policy gate, not reported by the worker.</p>
    ${changes.length ? `<table class="kv"><thead><tr><th>Step</th><th>Request</th><th>Result</th><th>Fields</th></tr></thead><tbody>
    ${changes.map((w) => `<tr><td class="mono">${w.step}</td><td class="mono">${esc(w.method)} ${esc(shortUrl(w.url))}</td><td>${esc(w.outcome)}${w.status ? ` <span class="mono" style="color:var(--text-3)">${w.status}</span>` : ""}</td><td class="quote">${esc(Object.entries(w.fields || {}).map(([k, v]) => `${k}: ${v}`).join("\n")).replace(/\n/g, "<br>")}</td></tr>`).join("")}</tbody></table>` : `<div class="empty-pane">Nothing was written.</div>`}
    ${ap.length ? `<h3>Approvals</h3><table class="kv"><tbody>${ap.map((a) => `<tr><td>${a.approved ? '<span class="res pass">Approved</span>' : '<span class="res fail">Denied</span>'}</td><td class="mono">${esc(a.rule)}</td><td>${esc(a.answer)}</td></tr>`).join("")}</tbody></table>` : ""}
    ${(R.report.assumptions || []).length ? `<h3>Assumptions</h3><ul class="plan">${R.report.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}`;
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-run],[data-step],[data-tab],[data-shot],[data-answer],[data-follow]");
  if (!t) return;
  const R = S.run;
  if (t.dataset.run) { location.hash = `#/run/${t.dataset.run}`; return; }
  if (!R) return;
  if (t.dataset.step) {
    const n = +t.dataset.step;
    if (R.selected === n) { R.open.has(n) ? R.open.delete(n) : R.open.add(n); } else R.selected = n;
    R.follow = false; R.tab = "screen";
    renderLedger(); renderEvidence();
  } else if (t.dataset.tab) { R.tab = t.dataset.tab; renderEvidence(); }
  else if (t.dataset.shot) { stepShot(t.dataset.shot === "next" ? 1 : -1); }
  else if (t.dataset.answer) { answer(t.dataset.answer); }
  else if ("follow" in t.dataset) { R.follow = true; const s = shotSteps(R); if (s.length) R.selected = s[s.length - 1].n; renderLedger(); renderEvidence(); }
});

function stepShot(dir) {
  const R = S.run; if (!R) return;
  const shots = shotSteps(R);
  let i = shots.findIndex((s) => s.n === R.selected);
  if (i < 0) i = shots.length - 1;
  const nx = shots[Math.min(shots.length - 1, Math.max(0, i + dir))];
  if (nx) { R.selected = nx.n; R.follow = false; R.tab = "screen"; renderLedger(); renderEvidence(); }
}

document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement && document.activeElement.tagName);
  if (e.key === "Enter" && document.activeElement && document.activeElement.id === "note") {
    e.preventDefault();
    answer(S.run && S.run.pending && S.run.pending.kind === "approval" ? "deny" : "__typed");
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "n" || e.key === "N") { e.preventDefault(); location.hash = "#/"; }
  const R = S.run;
  if (R && R.pending && R.pending.kind === "approval") {
    if (e.key === "a" || e.key === "A") answer("approve");
    if (e.key === "d" || e.key === "D") answer("deny");
  }
  if (R && (e.key === "[" || e.key === "]")) stepShot(e.key === "]" ? 1 : -1);
});

$("#new-task").addEventListener("click", () => { location.hash = "#/"; if (location.hash === "#/") renderComposer(); });
$("#theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("proof-theme", next); } catch (e) {}
});
$("#menu").addEventListener("click", () => $("#rail").classList.toggle("open"));

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast"; el.setAttribute("role", "status"); el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

/* ------------------------------------------------------------------ boot */
async function loadSandbox() {
  try { S.sandbox = await api("/api/sandbox"); } catch (e) { S.sandbox = null; }
  renderApps();
}

function route() {
  $("#rail").classList.remove("open");
  const m = location.hash.match(/^#\/run\/([\w-]+)/);
  if (m) openRun(m[1]); else renderComposer();
}

window.addEventListener("hashchange", route);
(async function boot() {
  await Promise.all([loadHealth(), loadSandbox(), api("/api/examples").then((x) => { S.examples = x; }).catch(() => {})]);
  route();
  setInterval(loadRuns, 6000);
})();
