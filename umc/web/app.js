/* updatemanager-checkmk – Weboberflaeche (ohne Fremdbibliotheken) */
"use strict";

/* ================================================================ Grundlagen */
const S = { me: null, csrf: null, version: "", host: "", ov: null, jobs: null, dry: false, timers: [] };
try { S.dry = localStorage.getItem("umc-dry") === "1"; } catch (e) { /* ohne Speicher */ }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of kids.flat(3)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const SVGNS = "http://www.w3.org/2000/svg";
function icon(id, cls = "ic") {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("class", cls); s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVGNS, "use");
  u.setAttribute("href", "#" + id); s.append(u); return s;
}
function mark(cls = "mark") { return icon("mark", cls); }
const $ = (sel, root = document) => root.querySelector(sel);

async function api(method, path, body) {
  const opt = { method, headers: { "Accept": "application/json" }, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  if (method !== "GET" && S.csrf) opt.headers["X-CSRF-Token"] = S.csrf;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), method === "GET" ? 30000 : 45000);
  opt.signal = ctl.signal;
  let r;
  try { r = await fetch(path, opt); }
  catch (e) {
    throw new Error(e.name === "AbortError" ? "Der Update-Server antwortet nicht (Zeitüberschreitung). Bitte gleich noch einmal versuchen."
      : "Keine Verbindung zum Update-Server – läuft der SSH-Tunnel bzw. der Dienst noch?");
  } finally { clearTimeout(timer); }
  if (r.status === 401 && path !== "/api/login") { S.me = null; renderLogin(); throw new Error("Bitte anmelden"); }
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `Fehler ${r.status}`);
  return data;
}

/* ================================================================ Formatierung */
const DAY = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
function fmtTime(iso, withDay) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2, "0");
  return `${withDay ? DAY[d.getDay()] + " " : ""}${p(d.getDate())}.${p(d.getMonth() + 1)}. ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 0) return until(iso);
  if (s < 90) return "gerade eben";
  if (s < 3600) return `vor ${Math.round(s / 60)} Min.`;
  if (s < 86400 * 1.5) return `vor ${Math.round(s / 3600)} Std.`;
  return `vor ${Math.round(s / 86400)} Tagen`;
}
function until(iso) {
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "jetzt";
  if (s < 3600) return `in ${Math.round(s / 60)} Min.`;
  if (s < 86400) return `in ${Math.floor(s / 3600)} Std. ${Math.round((s % 3600) / 60)} Min.`;
  return `in ${Math.floor(s / 86400)} T ${Math.round((s % 86400) / 3600)} Std.`;
}
function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
const STATUS = {
  ok: "aktuell", pending: "Updates stehen an", error: "Fehler beim letzten Lauf", unknown: "noch nicht erfasst / nicht erreichbar",
  disabled: "deaktiviert", blocked: "Update blockiert",
};
const KIND = { patch: "Patch", catchup: "Major, nachholen", major: "Major" };
const RUNKIND = { os: "OS-Update", checkmk: "Checkmk-Update", edition: "Editionswechsel", error: "Fehler", check: "Vorprüfung" };

/* ================================================================ Meldungen und Dialoge */
function toast(msg, kind = "") {
  let box = $(".toasts");
  if (!box) { box = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
  const t = h("div", { class: `toast ${kind}`, text: msg });
  box.append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}
function dialog({ title, body, ok = "OK", cancel = "Abbrechen", danger = false, wide = false, onOk }) {
  return new Promise(resolve => {
    const err = h("div", { class: "err small" });
    const okBtn = h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "submit", text: ok });
    const d = h("dialog", { class: wide ? "wide" : "" },
      h("form", { method: "dialog", onsubmit: async e => {
        e.preventDefault();
        if (onOk) {
          okBtn.disabled = true; err.textContent = "";
          try { const r = await onOk(d); if (r === false) { okBtn.disabled = false; return; } }
          catch (x) { err.textContent = x.message; okBtn.disabled = false; return; }
        }
        d.close("ok");
      } },
        h("div", { class: "d-head" }, h("h2", { text: title })),
        h("div", { class: "d-body" }, body, err),
        h("div", { class: "d-foot" },
          cancel ? h("button", { class: "btn ghost", type: "button", text: cancel, onclick: () => d.close("cancel") }) : null,
          okBtn)));
    d.addEventListener("close", () => { resolve(d.returnValue === "ok"); d.remove(); });
    document.body.append(d); d.showModal();
    const first = d.querySelector("input, select, textarea"); if (first) first.focus();
  });
}
const confirmAsk = (title, text, ok, danger) => dialog({ title, body: h("p", { text }), ok, danger });

/* ================================================================ Laeufe starten */
const DRY_OK = new Set(["update-os", "update-checkmk", "maintenance", "edition-switch", "checkmk-check", "checkmk-fetch",
  "checkmk-delete", "checkmk-cleanup", "repo-cleanup", "repo-rollback"]);
async function startJob(body, confirmText) {
  if (DRY_OK.has(body.cmd) && S.dry) body.dry_run = true;
  if (confirmText && !body.dry_run) {
    if (!await confirmAsk("Bitte bestätigen", confirmText, "Starten")) return null;
  }
  try {
    const job = await api("POST", "/api/jobs", body);
    toast(`Gestartet: ${job.title}`);
    refreshJobs();
    location.hash = `#/runs/${job.id}`;
    return job;
  } catch (e) { toast(e.message, "bad"); return null; }
}

/* ================================================================ Anmeldung */
function loginBackdrop() {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("class", "login-bg"); s.setAttribute("viewBox", "0 0 1200 800"); s.setAttribute("preserveAspectRatio", "xMidYMid slice");
  const pts = [[600, 120], [240, 420], [480, 470], [720, 470], [960, 420], [130, 690], [330, 720], [870, 720], [1070, 690]];
  const edges = [[0, 1], [0, 2], [0, 3], [0, 4], [1, 5], [1, 6], [4, 7], [4, 8]];
  for (const [a, b] of edges) {
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", `M${pts[a][0]} ${pts[a][1]} L${pts[b][0]} ${pts[b][1]}`); s.append(p);
  }
  pts.forEach(([x, y], i) => {
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", i ? 5 : 9); s.append(c);
  });
  return s;
}
function brand() {
  return h("a", { class: "brand", href: "#/" }, mark(),
    h("span", { class: "brand-word" }, h("b", { text: "updatemanager" }), h("span", { text: "checkmk" })));
}
function renderLogin(msg) {
  stopTimers();
  const err = h("div", { class: "err small", text: msg || "" });
  const user = h("input", { id: "lu", autocomplete: "username", required: true });
  const pw = h("input", { id: "lp", type: "password", autocomplete: "current-password", required: true });
  const btn = h("button", { class: "btn primary", type: "submit", text: "Anmelden" });
  const app = $("#app");
  app.replaceChildren(h("div", { class: "login" }, loginBackdrop(),
    h("div", { class: "login-card" }, brand(),
      h("form", { onsubmit: async e => {
        e.preventDefault(); btn.disabled = true; err.textContent = "";
        try { await api("POST", "/api/login", { user: user.value, password: pw.value }); await boot(); }
        catch (x) { err.textContent = x.message; btn.disabled = false; pw.select(); }
      } },
        h("label", { class: "field", for: "lu" }, "Benutzer", user),
        h("label", { class: "field", for: "lp" }, "Passwort", pw), err, btn),
      h("div", { class: "foot", text: `Update-Server ${S.host || ""} · Version ${S.version || ""}`.trim() }))));
  user.focus();
}

/* ================================================================ Rahmen */
const NAV = [
  ["#/", "i-grid", "Übersicht"], ["#/systems", "i-net", "Verbindungen"], ["#/repo", "i-box", "Repository"],
  ["#/runs", "i-play", "Läufe & Logs"], ["#/schedule", "i-clock", "Zeitplan"], ["#/settings", "i-gear", "Einstellungen"],
  ["#/help", "i-book", "Hilfe"],
];
function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Hauptnavigation" },
    NAV.map(([href, ic, label]) => h("a", { href, "data-nav": href }, icon(ic), h("span", { text: label }),
      href === "#/" ? h("span", { class: "count", id: "nav-count" }) : null)));
  const rail = h("aside", { class: "rail", id: "rail" }, brand(), nav,
    h("div", { class: "rail-foot" },
      h("div", { class: "row" }, h("span", { text: S.me }), h("button", { class: "linkbtn", text: "Abmelden", onclick: logout })),
      h("div", { class: "row" }, h("span", { text: `Version ${S.version}` }),
        h("button", { class: "linkbtn", text: "Hell/Dunkel", onclick: toggleTheme }))));
  const dry = h("label", { class: "switch", title: "Im Testmodus wird nichts verändert – die Läufe zeigen nur, was passieren würde." },
    h("input", { type: "checkbox", id: "drysw", checked: S.dry, onchange: e => setDry(e.target.checked) }), "Testmodus");
  const top = h("header", { class: "topbar" },
    h("button", { class: "btn icon ghost menu-toggle", "aria-label": "Menü", onclick: () => $("#rail").classList.toggle("open") }, icon("i-grid")),
    h("div", { class: "crumbs", id: "crumbs" }), h("div", { class: "spacer" }),
    h("span", { class: "pill window", id: "pill-window" }), h("span", { id: "pill-job" }), dry);
  $("#app").replaceChildren(h("div", { class: "shell" }, rail,
    h("div", { class: "main" }, top, h("div", { id: "drybar" }), h("main", { class: "content", id: "content" }))));
  setDry(S.dry);
}
function setDry(on) {
  S.dry = on;
  try { localStorage.setItem("umc-dry", on ? "1" : "0"); } catch (e) { /* egal */ }
  const bar = $("#drybar");
  if (bar) bar.replaceChildren(on ? h("div", { class: "drybar", text: "Testmodus ist an: Updates, Downloads und Löschungen werden nur simuliert." }) : "");
}
function toggleTheme() {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = cur === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("umc-theme", next); } catch (e) { /* egal */ }
}
try { const t = localStorage.getItem("umc-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* egal */ }
async function logout() { try { await api("POST", "/api/logout"); } catch (e) { /* egal */ } S.me = null; renderLogin(); }
function crumbs(...parts) {
  const c = $("#crumbs"); if (!c) return;
  c.replaceChildren(...parts.map((p, i) => i === parts.length - 1 ? h("b", { text: p }) : h("span", { text: p + "  /  " })));
  document.title = `${parts[parts.length - 1]} – updatemanager-checkmk`;
}
function updateTopbar() {
  const ov = S.ov; if (!ov) return;
  const pw = $("#pill-window");
  if (pw) {
    const sc = ov.schedule;
    pw.className = "pill window" + (sc.inside ? " window-now" : "");
    pw.replaceChildren(h("span", { class: "dot" }),
      sc.inside ? "Wartungsfenster läuft" : sc.next_window ? `Wartungsfenster ${fmtTime(sc.next_window, true)} · ${until(sc.next_window)}` : "kein Wartungsfenster");
  }
  const nav = $(".nav");
  if (nav) {
    const lab = nav.querySelector('[data-nav="#/lab"]');
    if (ov.lab && !lab) nav.insertBefore(h("a", { href: "#/lab", "data-nav": "#/lab", class: "nav-lab" }, icon("i-refresh"), h("span", { text: "Labor" })),
      nav.querySelector('[data-nav="#/help"]'));
    if (!ov.lab && lab) lab.remove();
  }
  const n = ov.summary.cmk_pending + ov.summary.os_pending_hosts;
  const nc = $("#nav-count"); if (nc) nc.textContent = n ? String(n) : "";
}
function updateJobPill() {
  const el = $("#pill-job"); if (!el || !S.jobs) return;
  const cur = S.jobs.current;
  if (cur && cur.status === "running") {
    el.replaceChildren(h("a", { class: "pill live", href: `#/runs/${cur.id}` }, h("span", { class: "dot" }), cur.title));
  } else if (S.jobs.external_run) {
    el.replaceChildren(h("span", { class: "pill live", title: "Ein Lauf aus dem Zeitplan oder dem Terminal ist aktiv." },
      h("span", { class: "dot" }), "Lauf aktiv (Zeitplan/Terminal)"));
  } else el.replaceChildren();
}
async function refreshJobs() {
  try { S.jobs = await api("GET", "/api/jobs"); updateJobPill(); } catch (e) { /* still */ }
}
async function refreshOverview() {
  try {
    S.ov = await api("GET", "/api/overview");
    if (S.loadedVersion && S.ov.version && S.ov.version !== S.loadedVersion) { location.reload(); return S.ov; }
    updateTopbar();
  } catch (e) { /* still */ }
  return S.ov;
}
function stopTimers() { S.timers.forEach(clearInterval); S.timers = []; }
function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }

/* ================================================================ Router */
const ROUTES = [
  [/^#?\/?$/, () => pageDashboard()],
  [/^#\/systems$/, () => pageSystems()],
  [/^#\/systems\/(.+)$/, m => pageSystem(decodeURIComponent(m[1]))],
  [/^#\/repo$/, () => pageRepo()],
  [/^#\/runs$/, () => pageRuns()],
  [/^#\/runs\/([0-9a-f-]+)$/, m => pageRuns(m[1])],
  [/^#\/logs\/(.+)$/, m => pageRuns(null, decodeURIComponent(m[1]))],
  [/^#\/schedule$/, () => pageSchedule()],
  [/^#\/lab$/, () => pageLab()],
  [/^#\/settings(?:\/(\w+))?$/, m => pageSettings(m[1])],
  [/^#\/help(?:\/(.+))?$/, m => pageHelp(m[1] ? decodeURIComponent(m[1]) : null)],
];
let pageTimers = [];
function route() {
  if (!S.me) return;
  pageTimers.forEach(clearInterval); pageTimers = [];
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach(a => {
    const href = a.getAttribute("data-nav");
    const on = href === "#/" ? (hash === "#/" || hash === "") : hash.startsWith(href);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const rail = $("#rail"); if (rail) rail.classList.remove("open");
  const content = $("#content");
  content.replaceChildren(h("div", { class: "skeleton", text: "Lade …" }));
  window.scrollTo(0, 0);
  for (const [re, fn] of ROUTES) {
    const m = hash.match(re);
    if (m) {
      Promise.resolve(fn(m)).catch(e => content.replaceChildren(h("div", { class: "banner bad" }, e.message)));
      return;
    }
  }
  content.replaceChildren(h("div", { class: "empty", text: "Diese Seite gibt es nicht." }));
}
function pageEvery(ms, fn) { pageTimers.push(setInterval(fn, ms)); }
window.addEventListener("hashchange", route);

/* ================================================================ Bausteine */
function led(status) { return h("span", { class: `led ${status}`, title: STATUS[status] || status }); }
function verRail(ck, hasSite) {
  if (!hasSite) return h("span", { class: "mute small", text: "keine Remote Site" });
  if (!ck.version) return h("span", { class: "ver none", text: "unbekannt" });
  const kids = [h("span", { class: "ver", text: ck.version })];
  if (ck.target) {
    const arr = document.createElementNS(SVGNS, "svg");
    arr.setAttribute("class", "arr"); arr.setAttribute("viewBox", "0 0 24 10"); arr.setAttribute("aria-label", "wird aktualisiert auf");
    const u = document.createElementNS(SVGNS, "use"); u.setAttribute("href", "#i-arrow"); arr.append(u);
    kids.push(arr, h("span", { class: "ver to", text: ck.target }), h("span", { class: "kind", text: KIND[ck.kind] || "" }));
  } else kids.push(h("span", { class: "uptodate", text: "aktuell" }));
  return h("div", { class: "rail-v" }, kids);
}
function osCell(os) {
  if (!os.label) return h("div", { class: "oscell" }, h("span", { class: "mute", text: "noch nicht erfasst" }));
  const p = os.pending;
  return h("div", { class: "oscell" },
    h("span", { class: "lbl", text: os.label, title: `${os.label}\nKernel ${os.kernel || "?"}` }),
    h("span", {}, p === null || p === undefined ? h("span", { class: "tag", text: "offene Updates unbekannt" })
      : p > 0 ? h("span", { class: "tag amber", text: `${p} Update${p === 1 ? "" : "s"} offen` })
        : h("span", { class: "tag ok", text: "keine Updates offen" })));
}
function lastCell(last) {
  const os = last.os, ck = last.checkmk;
  const line = (label, e) => e
    ? h("div", {}, h("b", { text: label + " " }), h("span", { class: e.ok === false ? "bad" : "", text: e.ok === false ? `Fehler ${ago(e.time)}` : ago(e.time) }))
    : h("div", {}, h("b", { text: label + " " }), "–");
  return h("div", { class: "when" }, line("OS", os), line("Checkmk", ck));
}
function nodeMaster(s) {
  return h("div", { class: "node-master", role: "link", tabindex: "0",
    onclick: () => { location.hash = `#/systems/${encodeURIComponent(s.name)}`; },
    onkeydown: e => { if (e.key === "Enter") location.hash = `#/systems/${encodeURIComponent(s.name)}`; } },
  h("div", {}, h("div", { class: "role", text: "Master" }),
    h("div", { class: "nm-line" }, led(s.status), h("span", { class: "nm", text: s.name })),
    h("div", { class: "addr", text: `${s.host}${s.site ? "  site " + s.site : ""}` })),
  h("div", {}, h("div", { class: "cell-k", text: "Betriebssystem" }), osCell(s.os)),
  h("div", {}, h("div", { class: "cell-k", text: "Checkmk" }), verRail(s.checkmk, !!s.site),
    s.checkmk.edition ? h("div", { class: "small mute", text: `Edition ${s.checkmk.edition}` }) : null),
  lastCell(s.last));
}
function nodeCustomer(s) {
  const go = () => { location.hash = `#/systems/${encodeURIComponent(s.name)}`; };
  return h("li", { class: `st-${s.status}` },
    h("div", { class: `node${s.enabled ? "" : " off"}`, role: "link", tabindex: "0", onclick: go, onkeydown: e => { if (e.key === "Enter") go(); } },
      h("div", {}, h("div", { class: "nm-line" }, led(s.status), h("span", { class: "nm", text: s.name })),
        h("div", { class: "addr" }, s.host, s.enabled ? null : h("span", { class: "tag", text: "deaktiviert" }))),
      h("div", { class: "c-os" }, osCell(s.os)),
      h("div", { class: "c-cmk" }, h("div", { class: "cell-k", text: s.site ? `Remote Site ${s.site}` : "Remote Site" }), verRail(s.checkmk, !!s.site)),
      h("div", { class: "c-last" }, lastCell(s.last))));
}
function noticesList(ov) {
  const items = [];
  for (const s of [ov.master, ...ov.customers].filter(Boolean)) {
    for (const [k, e] of Object.entries(s.last)) if (e && e.ok === false)
      items.push(["bad", `${s.name}: ${RUNKIND[k] || k} fehlgeschlagen ${ago(e.time)}`, e.msg]);
    if (s.status === "unknown" && s.enabled) items.push(["amber", `${s.name}: noch nicht erfasst`, "Unter Läufe »Systeme erfassen« starten."]);
  }
  const setupOpen = (ov.setup || []).some(i => !i.ok);
  if (ov.timers && ov.timers.state !== "neu" && !setupOpen)
    items.unshift(ov.timers.state === "alt"
      ? ["amber", "Zeitplan läuft noch mit den alten Timern – Änderungen an den Uhrzeiten wirken erst nach der Umstellung",
        "Einmalig im Terminal: updatemanager-checkmk → 4 Wartungsfenster & Zeitplan → 2 Zeitplan aktivieren."]
      : ["bad", "Zeitplan ist nicht aktiv – es laufen keine automatischen Updates und kein täglicher Repo-Sync",
        "Einschalten im Terminal: updatemanager-checkmk → 4 Wartungsfenster & Zeitplan → 2 Zeitplan aktivieren."]);
  for (const b of ov.plan.blocked || []) items.push(["amber", b, null]);
  if (ov.plan.error) items.push(["amber", ov.plan.error, null]);
  for (const m of ov.checkmk.majors || []) items.push(["info", `Neue Checkmk-Major-Version ${m} beim Hersteller`,
    ov.checkmk.major_auto ? "Wird nach den Regeln automatisch eingespielt." : "Automatische Major-Updates sind aus."]);
  for (const [v, pids] of Object.entries(ov.checkmk.unsupported || {})) items.push(["amber", `Checkmk ${v} gibt es nicht für ${pids.join(", ")}`, "Dafür ist ein OS-Upgrade nötig."]);
  if (ov.runs.repo_sync && ov.runs.repo_sync.ok === false) items.push(["bad", `Repo-Aktualisierung fehlgeschlagen ${ago(ov.runs.repo_sync.time)}`, null]);
  if (!items.length) return h("div", { class: "empty", text: "Keine Hinweise – alles in Ordnung." });
  return h("ul", { class: "list" }, items.map(([k, t, sub]) => h("li", {},
    h("span", { class: `sym ${k}`, text: k === "bad" ? "■" : k === "info" ? "◆" : "▲" }),
    h("div", { class: "grow" }, h("div", { text: t }), sub ? h("div", { class: "small mute", text: sub }) : null))));
}

function setupPanel(ov) {
  const items = ov.setup || [];
  const open = items.filter(i => !i.ok);
  if (!open.length) return "";
  const done = items.length - open.length;
  return h("div", { class: "panel setup-panel" },
    h("div", { class: "panel-head" },
      h("h2", { text: `Einrichtung abschließen · ${done} von ${items.length} erledigt` }),
      h("button", { class: "btn", onclick: () => startJob({ cmd: "check" }) }, icon("i-refresh"), "Erneut prüfen")),
    h("div", { class: "setup-bar" }, h("span", { class: "setup-fill", "data-pct": String(Math.round(100 * done / items.length)) })),
    h("ul", { class: "list" }, [...open, { ok: true, text: `${done} Schritt${done === 1 ? "" : "e"} erledigt`, sum: true }].filter(i => !i.sum || done).map(i => h("li", {},
      h("span", { class: `sym ${i.ok ? "ok" : "bad"}`, text: i.ok ? "✓" : "○" }),
      h("div", { class: "grow" }, h("div", { text: i.text }),
        !i.ok && i.fix ? h("div", { class: "small mute", text: `Im Terminal: updatemanager-checkmk → ${i.fix}` }) : null),
      !i.ok && i.web ? h("button", { class: "btn", onclick: () => startJob({ cmd: i.web }) },
        i.web === "repo-sync" ? "Repo aktualisieren" : "Verbindung testen") : null))),
    h("div", { class: "small mute setup-foot", text: "Vorbereiten von Master und Kunden braucht das Admin-Passwort der VM und der Zeitplan sudo – das geht im Terminal: updatemanager-checkmk → e (führt durch alle offenen Schritte)." }));
}

/* ================================================================ Seite: Uebersicht */
async function pageDashboard() {
  crumbs("Übersicht");
  const ov = await refreshOverview();
  const content = $("#content");
  const draw = ov => {
    const s = ov.summary;
    const head = h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Übersicht" }),
        h("p", { text: s.systems ? `Master und ${ov.customers.length} Kunden. Stand aus der letzten Erfassung; »Jetzt erfassen« holt den aktuellen Zustand per SSH.` : "Noch keine Systeme angelegt." })),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: () => startJob({ cmd: "inventory" }) }, icon("i-refresh"), "Jetzt erfassen"),
        h("button", { class: "btn primary", onclick: () => startJob({ cmd: "maintenance", force: true },
          "Wartungslauf jetzt starten? OS- und Checkmk-Updates werden auf allen Systemen eingespielt (Remote Sites zuerst, Master zuletzt).") }, icon("i-play"), "Wartungslauf jetzt")));
    const strip = h("div", { class: "status-strip" },
      h("div", {}, h("div", { class: "k", text: "Systeme" }),
        h("div", { class: `v${s.errors ? " bad" : ""}` }, String(s.systems),
          h("small", { text: s.errors ? `  ${s.errors} mit Fehler` : s.unreachable ? `  ${s.unreachable} nicht erfasst` : "  alle erfasst" }))),
      h("div", {}, h("div", { class: "k", text: "OS-Updates offen" }),
        h("div", { class: `v${s.os_pending_hosts ? " amber" : ""}` }, String(s.os_pending_total),
          h("small", { text: s.os_pending_hosts ? `  auf ${s.os_pending_hosts} System${s.os_pending_hosts === 1 ? "" : "en"}` : "  alles aktuell" }))),
      h("div", {}, h("div", { class: "k", text: "Checkmk-Updates" }),
        h("div", { class: `v${s.cmk_pending ? " amber" : ""}` }, String(s.cmk_pending),
          h("small", { text: s.cmk_pending ? `  auf ${s.cmk_targets.join(", ")}` : "  alle Sites aktuell" }))),
      h("div", {}, h("div", { class: "k", text: "Nächstes Wartungsfenster" }),
        h("div", { class: "v" }, ov.schedule.inside ? "läuft gerade" : fmtTime(ov.schedule.next_window, true),
          h("small", { text: `  bis ${ov.schedule.end}` }))));
    const topo = h("section", { class: "topo", "aria-label": "Master und Kunden" },
      ov.master ? nodeMaster(ov.master) : h("div", { class: "banner amber" }, "Noch kein Master eingetragen. ",
        h("a", { href: "#/systems", text: "Master eintragen" })),
      ov.customers.length ? h("ul", { class: "branch" }, ov.customers.map(nodeCustomer))
        : h("div", { class: "topo-empty" }, "Noch keine Kunden. ", h("a", { href: "#/systems", text: "Kunden anlegen" })));
    const side = h("div", { class: "cols" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Hinweise" })), noticesList(ov)),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Letzte Läufe" }),
        h("a", { href: "#/runs", class: "small", text: "Alle Läufe" })), recentRuns(ov)));
    content.replaceChildren(ov.external_run ? h("div", { class: "banner amber" }, "Gerade läuft ein Vorgang aus dem Zeitplan oder im Terminal.") : "",
      head, setupPanel(ov), strip, topo, h("div", { class: "stack" }, h("div"), side));
    const fill = content.querySelector(".setup-fill");
    if (fill) fill.style.width = fill.dataset.pct + "%";
  };
  draw(ov);
  pageEvery(20000, async () => { const n = await refreshOverview(); if (n && location.hash.match(/^#?\/?$/)) draw(n); });
}
function recentRuns(ov) {
  const items = [];
  if (ov.runs.maintenance) items.push(["Wartungslauf", ov.runs.maintenance]);
  if (ov.runs.repo_sync) items.push(["Repo-Aktualisierung", ov.runs.repo_sync]);
  const jobs = (S.jobs && S.jobs.history || []).slice(0, 5);
  const list = h("ul", { class: "list" },
    items.map(([t, e]) => h("li", {}, h("span", { class: `sym ${e.ok ? "ok" : "bad"}`, text: e.ok ? "●" : "■" }),
      h("div", { class: "grow" }, h("div", { text: t }), h("div", { class: "small mute", text: `${fmtTime(e.time, true)} · ${e.ok ? "erfolgreich" : "mit Fehlern"}` })))),
    jobs.map(j => h("li", {}, h("span", { class: `sym ${j.status === "ok" ? "ok" : j.status === "running" ? "amber" : "bad"}`, text: j.status === "running" ? "▶" : j.status === "ok" ? "●" : "■" }),
      h("div", { class: "grow" }, h("a", { href: `#/runs/${j.id}`, text: j.title }),
        h("div", { class: "small mute", text: `${fmtTime(j.start, true)} · ${j.user}` })))));
  return items.length || jobs.length ? list : h("div", { class: "empty", text: "Noch keine Läufe." });
}

/* ================================================================ Seite: Verbindungen */
async function pageSystems() {
  crumbs("Verbindungen");
  const ov = await refreshOverview();
  const all = [ov.master, ...ov.customers].filter(Boolean);
  const q = h("input", { type: "text", placeholder: "Suchen: Name, Adresse, Betriebssystem …", "aria-label": "Suchen" });
  const sel = h("select", { "aria-label": "Status" }, h("option", { value: "", text: "Alle Zustände" }),
    Object.entries(STATUS).map(([k, v]) => h("option", { value: k, text: v })));
  const tbody = h("tbody");
  const draw = () => {
    const term = q.value.toLowerCase().trim();
    const rows = all.filter(s => (!sel.value || s.status === sel.value) &&
      (!term || [s.name, s.host, s.site, s.os.label, s.checkmk.version].join(" ").toLowerCase().includes(term)));
    tbody.replaceChildren(...rows.map(s => h("tr", { class: "click", onclick: () => { location.hash = `#/systems/${encodeURIComponent(s.name)}`; } },
      h("td", {}, led(s.status)),
      h("td", {}, h("b", { text: s.name }), h("div", { class: "small mute", text: s.role === "master" ? "Master" : "Kunde" })),
      h("td", { class: "mono", text: s.host }),
      h("td", {}, osCell(s.os)),
      h("td", {}, s.site ? h("span", { class: "mono", text: s.site }) : h("span", { class: "mute", text: "–" })),
      h("td", {}, verRail(s.checkmk, !!s.site)),
      h("td", { class: "small mute", text: s.os.seen ? ago(s.os.seen) : "nie" }))));
    if (!rows.length) tbody.replaceChildren(h("tr", {}, h("td", { colspan: 7, class: "empty", text: all.length ? "Nichts gefunden." : "Noch keine Systeme." })));
  };
  q.addEventListener("input", draw); sel.addEventListener("change", draw);
  $("#content").replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Verbindungen" }), h("p", { text: "Alle Systeme, die der Update-Server per SSH erreicht. Ein Klick zeigt Details, Verlauf und Aktionen." })),
      h("div", { class: "actions" },
        ov.master ? null : h("button", { class: "btn", text: "Master eintragen", onclick: () => editSystem(null, true) }),
        h("button", { class: "btn primary", text: "Kunden anlegen", onclick: () => editSystem(null, false) }))),
    h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "filter" }, q, sel), h("span", { class: "small mute", text: `${all.length} Systeme` })),
      h("div", { class: "tablewrap" }, h("table", { class: "t" },
        h("thead", {}, h("tr", {}, ["", "Name", "Adresse", "VM", "Remote Site", "Checkmk", "Erfasst"].map(t => h("th", { text: t })))), tbody))));
  draw();
}

function field(label, input, hint) {
  return h("label", { class: "field" }, label, input, hint ? h("span", { class: "hint", text: hint }) : null);
}
async function editSystem(s, isMaster) {
  const v = s || {};
  const inp = {
    name: h("input", { value: v.name || "", required: true }),
    host: h("input", { value: v.host || "", required: true, class: "mono" }),
    site: h("input", { value: v.site || "", class: "mono" }),
    checkmk_host: h("input", { value: v.checkmk_host || "" }),
    ssh_port: h("input", { type: "number", value: v.ssh_port || 22, min: 1, max: 65535 }),
    jumphost: h("input", { value: v.jumphost || "", class: "mono", placeholder: "user@host[:port]" }),
  };
  const grid = [field("Name", inp.name, isMaster ? "Anzeigename" : "eindeutig, z. B. kunde_a"),
    field("IP / Hostname", inp.host, "vom Update-Server aus"),
    field(isMaster ? "Checkmk-Site" : "Remote Site", inp.site, isMaster ? "" : "leer = nur OS-Updates"),
    field("Hostname in Checkmk", inp.checkmk_host, "für die Downtime"),
    field("SSH-Port", inp.ssh_port), field("Jumphost", inp.jumphost, "leer = Standard")];
  if (isMaster) {
    inp.api_url = h("input", { value: (v.api && v.api.url) || "", class: "mono", placeholder: "http://host/site/check_mk/api/1.0" });
    inp.api_user = h("input", { value: (v.api && v.api.user) || "svc-update" });
    inp.api_verify_tls = h("input", { type: "checkbox", checked: v.api ? v.api.verify_tls : true });
    grid.push(h("div", { class: "wide" }, field("REST-API-URL", inp.api_url, "leer = aus Adresse und Site ableiten")),
      field("Automation-User", inp.api_user),
      h("label", { class: "check" }, inp.api_verify_tls, h("span", {}, "TLS-Zertifikat prüfen", h("small", { text: "nur bei https" }))));
  } else {
    inp.notes = h("input", { value: v.notes || "" });
    grid.push(h("div", { class: "wide" }, field("Notiz", inp.notes)));
  }
  const ok = await dialog({
    title: s ? `${s.name} bearbeiten` : isMaster ? "Master eintragen" : "Kunden anlegen", wide: true, ok: "Speichern",
    body: h("div", { class: "form-grid" }, grid),
    onOk: async () => {
      const data = {};
      for (const [k, el] of Object.entries(inp)) data[k] = el.type === "checkbox" ? el.checked : el.value;
      if (!isMaster) data.enabled = s ? s.enabled : true;
      let r;
      if (isMaster) r = await api("PUT", "/api/master", data);
      else if (s) r = await api("PUT", `/api/customers/${encodeURIComponent(s.name)}`, data);
      else r = await api("POST", "/api/customers", data);
      v.__newname = r.name || data.name;
    },
  });
  if (ok) {
    toast("Gespeichert.", "ok");
    if (!s && !isMaster) toast("Als Nächstes: Vorbereitungs-Skript herunterladen und auf der VM ausführen.");
    location.hash = `#/systems/${encodeURIComponent(v.__newname)}`;
    if (s && v.__newname === s.name) route();
  }
}

/* ================================================================ Seite: ein System */
async function pageSystem(name) {
  const s = await api("GET", `/api/systems/${encodeURIComponent(name)}`);
  crumbs("Verbindungen", s.name);
  const only = { only: [s.name] };
  const isM = s.role === "master";
  const actions = h("div", { class: "actions" },
    h("button", { class: "btn", text: "Vorprüfung", onclick: () => startJob({ cmd: "check", ...only }) }),
    h("button", { class: "btn", text: "Erfassen", onclick: () => startJob({ cmd: "inventory", ...only }) }),
    h("button", { class: "btn", text: "OS-Update", onclick: () => startJob({ cmd: "update-os", ...only },
      `OS-Updates jetzt auf ${s.name} einspielen? Das System startet bei Bedarf neu.`) }),
    s.site ? h("button", { class: "btn primary", text: "Checkmk-Update", onclick: () => startJob({ cmd: "update-checkmk", ...only },
      isM ? "Den Master jetzt aktualisieren? Er wird nur aktualisiert, wenn alle Remote Sites bereits auf dem Ziel sind."
        : `Checkmk auf ${s.name} jetzt aktualisieren? Vorher wird ein Backup der Site erstellt.`) }) : null);
  const head = h("div", { class: "page-head" },
    h("div", { class: "sys-head" },
      h("h1", {}, led(s.status), s.name),
      h("span", { class: `tag ${isM ? "amber" : ""}`, text: isM ? "Master" : "Kunde" }),
      h("span", { class: "mute", text: STATUS[s.status] })), actions);

  const ck = s.checkmk;
  const cmkPanel = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: isM ? "Checkmk" : "Remote Site" }),
      h("p", { text: s.site ? `Site ${s.site}` : "Auf diesem System ist keine Site eingetragen." }))),
    h("div", { class: "panel-body" },
      s.site ? h("div", { class: "stack" },
        verRail(ck, true),
        h("dl", { class: "dl" },
          h("dt", { text: "Edition" }), h("dd", { text: ck.edition || "–" }),
          h("dt", { text: "Version (voll)" }), h("dd", { class: "mono", text: ck.full || "–" }),
          h("dt", { text: "Installiert" }), h("dd", {}, ck.installed && ck.installed.length
            ? ck.installed.map(x => h("span", { class: "ver", text: x })).reduce((a, b) => [...a, " ", b], []) : "–"),
          h("dt", { text: "Nächster Schritt" }), h("dd", { text: ck.target ? `${KIND[ck.kind]}-Update auf ${ck.target} im nächsten Wartungsfenster` : "keiner – aktuell" })),
        s.blocked.length ? h("div", { class: "banner amber" }, h("div", {}, s.blocked.map(b => h("div", { text: b })))) : null)
        : h("p", { class: "mute", text: "Es werden nur OS-Updates eingespielt." })));
  const os = s.os;
  const osPanel = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: isM ? "System" : "VM" }), h("p", { text: os.seen ? `erfasst ${ago(os.seen)}` : "noch nicht erfasst" }))),
    h("div", { class: "panel-body" }, os.label ? h("dl", { class: "dl" },
      h("dt", { text: "Betriebssystem" }), h("dd", { text: os.label }),
      h("dt", { text: "Offene Updates" }), h("dd", {}, osCell(os).lastChild),
      h("dt", { text: "Kernel" }), h("dd", { class: "mono", text: os.kernel || "–" }),
      h("dt", { text: "Architektur" }), h("dd", { text: os.arch || "–" }),
      h("dt", { text: "Repo-Profil" }), h("dd", {}, h("a", { href: "#/repo", class: "mono", text: os.profile || "–" })))
      : h("p", { class: "mute", text: "Noch keine Daten – »Erfassen« starten." })));
  const hist = Object.entries(s.last);
  const histPanel = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { text: "Letzte Ergebnisse" })),
    h("div", { class: "panel-body" }, hist.length ? h("ul", { class: "history" }, hist.map(([k, e]) => h("li", {},
      h("div", {}, h("div", { text: RUNKIND[k] || k }), h("div", { class: "small mute", text: fmtTime(e.time, true) })),
      h("div", {}, h("span", { class: `tag ${e.ok === false ? "bad" : e.ok ? "ok" : ""}`, text: e.ok === false ? "Fehler" : e.ok ? "OK" : "–" }), " ", e.msg,
        e.packages && e.packages.length ? h("details", { class: "pkgs" }, h("summary", { text: `${e.packages.length} Pakete anzeigen` }),
          h("div", { class: "keybox" }, e.packages.map(p => h("div", { text: p[1] ? `${p[0]}  ${p[1]} → ${p[2]}` : `${p[0]}  ${p[2] || ""}` })))) : null))))
      : h("p", { class: "mute", text: "Noch keine Läufe für dieses System." })));
  const conn = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { text: "Verbindung" }),
      h("button", { class: "btn sm", text: "Bearbeiten", onclick: () => editSystem(s, isM) })),
    h("div", { class: "panel-body" }, h("dl", { class: "dl" },
      h("dt", { text: "Adresse" }), h("dd", { class: "mono", text: s.host }),
      h("dt", { text: "SSH-Port" }), h("dd", { text: s.ssh_port }),
      h("dt", { text: "Jumphost" }), h("dd", { text: s.jumphost || "Standard" }),
      h("dt", { text: "Hostname in Checkmk" }), h("dd", { text: s.checkmk_host || "– (keine Downtime)" }),
      !isM ? h("dt", { text: "Notiz" }) : null, !isM ? h("dd", { text: s.notes || "–" }) : null)));
  const prep = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Vorbereitung" }),
      h("p", { text: "Einmalig: Service-User, sudo-Rechte, Repo-Quellen und Zeit auf dem System einrichten." }))),
    h("div", { class: "panel-body" },
      h("p", { class: "small", text: "Skript herunterladen, auf das System kopieren und dort mit sudo bash ausführen (oder im Terminal: Menü Kunden → vorbereiten)." }),
      h("a", { class: "btn", href: `/api/systems/${encodeURIComponent(s.name)}/prepare`, download: true, text: "Vorbereitungs-Skript herunterladen" })));
  const apiPanel = isM ? h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "REST-API" }), h("p", { text: "für Downtimes und die Version des Masters" }))),
    h("div", { class: "panel-body stack" },
      h("dl", { class: "dl" }, h("dt", { text: "URL" }), h("dd", { class: "mono", text: s.api.url || "–" }),
        h("dt", { text: "User" }), h("dd", { text: s.api.user }),
        h("dt", { text: "Secret" }), h("dd", {}, s.api.secret ? h("span", { class: "tag ok", text: "hinterlegt" }) : h("span", { class: "tag bad", text: "fehlt" }))),
      h("div", { class: "actions" },
        h("button", { class: "btn sm", text: "Verbindung testen", onclick: async e => {
          e.target.disabled = true;
          try { const r = await api("POST", "/api/master/test"); toast(r.ok ? `REST-API erreichbar – Checkmk ${r.version}` : `Nicht erreichbar: ${r.error}`, r.ok ? "ok" : "bad"); }
          catch (x) { toast(x.message, "bad"); } e.target.disabled = false;
        } }),
        h("button", { class: "btn sm", text: "Secret ändern", onclick: () => secretDialog("api", "Automation-Secret", "Das Secret des Automation-Users aus dem Master.") })))) : null;
  const logs = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { text: "Logs mit diesem System" })),
    s.logs.length ? h("ul", { class: "list" }, s.logs.map(l => h("li", {}, h("div", { class: "grow" },
      h("a", { href: `#/logs/${encodeURIComponent(l.name)}`, class: "mono small", text: l.name }))))) : h("div", { class: "empty", text: "Keine." }));
  const danger = !isM ? h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", { text: "Verwalten" })),
    h("div", { class: "panel-body actions" },
      h("button", { class: "btn", text: s.enabled ? "Deaktivieren" : "Aktivieren", onclick: async () => {
        try { await api("PUT", `/api/customers/${encodeURIComponent(s.name)}`, { ...s, enabled: !s.enabled }); route(); toast(s.enabled ? "Deaktiviert – wird bei Läufen übersprungen." : "Aktiviert.", "ok"); }
        catch (x) { toast(x.message, "bad"); }
      } }),
      h("button", { class: "btn danger", onclick: async () => {
        if (!await confirmAsk(`${s.name} löschen?`, "Der Kunde wird aus dem Update-Server entfernt. Auf der VM selbst ändert sich nichts.", "Löschen", true)) return;
        try { await api("DELETE", `/api/customers/${encodeURIComponent(s.name)}`); toast("Gelöscht.", "ok"); location.hash = "#/systems"; }
        catch (x) { toast(x.message, "bad"); }
      } }, icon("i-trash"), "Löschen"))) : null;
  $("#content").replaceChildren(head,
    h("div", { class: "cols" },
      h("div", { class: "stack" }, cmkPanel, osPanel, histPanel),
      h("div", { class: "stack" }, conn, apiPanel, prep, logs, danger)));
}
async function secretDialog(kind, title, text, placeholder) {
  const inp = h("input", { type: "password", autocomplete: "off", placeholder: placeholder || "" });
  const ok = await dialog({ title, ok: "Speichern", body: h("div", { class: "stack" }, h("p", { text }), field("Wert (wird nicht wieder angezeigt)", inp)),
    onOk: () => api("POST", `/api/secrets/${kind}`, { value: inp.value }) });
  if (ok) { toast("Gespeichert.", "ok"); route(); }
}

/* ================================================================ Seite: Repository */
async function pageRepo() {
  crumbs("Repository");
  const [rp, ov] = await Promise.all([api("GET", "/api/repo?fresh=1"), refreshOverview()]);
  const content = $("#content");
  const targets = {};
  for (const s of [ov.master, ...ov.customers].filter(Boolean)) if (s.checkmk.target) {
    const k = `${s.os.profile}|${s.checkmk.target}`; (targets[k] = targets[k] || []).push(s.name);
  }
  const removable = rp.checkmk.filter(u => u.removable);
  const head = h("div", { class: "page-head" },
    h("div", {}, h("h1", { text: "Repository" }),
      h("p", { text: "Was der Update-Server für die Systeme bereithält: Checkmk-Pakete je Betriebssystem und gespiegelte OS-Updates. Die Systeme holen alles über den SSH-Tunnel von hier." })),
    h("div", { class: "actions" },
      h("button", { class: "btn", onclick: () => startJob({ cmd: "checkmk-check" }) }, "Nach neuen Versionen suchen"),
      h("button", { class: "btn", onclick: fetchDialog }, "Version laden"),
      h("button", { class: "btn primary", onclick: () => startJob({ cmd: "repo-sync" }) }, icon("i-refresh"), "Repo jetzt aktualisieren")));
  const chk = rp.check || {};
  const banners = [];
  for (const m of chk.majors || []) banners.push(h("div", { class: "banner" }, `Neue Major-Version beim Hersteller: ${m}.`));
  for (const [v, pids] of Object.entries(chk.unsupported || {})) banners.push(h("div", { class: "banner amber" }, `Checkmk ${v} gibt es nicht für ${pids.join(", ")} – dort ist ein OS-Upgrade nötig.`));

  /* Matrix Version x Betriebssystem */
  const profs = rp.profiles.filter(p => rp.checkmk.some(u => u.profile === p.id) || p.used_by.length);
  const rows = {};
  for (const u of rp.checkmk) { const k = `${u.version}|${u.edition}`; (rows[k] = rows[k] || {})[u.profile] = u; }
  const vkey = s => s.split("|")[0].split(/[.p]/).map(Number);
  const order = Object.keys(rows).sort((a, b) => { const x = vkey(a), y = vkey(b); for (let i = 0; i < 4; i++) if ((x[i] || 0) !== (y[i] || 0)) return (y[i] || 0) - (x[i] || 0); return 0; });
  const cell = (u, pid) => {
    if (!u) return h("td", {}, h("div", { class: "pkg-none", text: "–" }));
    const tg = targets[`${pid}|${u.version}`];
    let cls = "pkg", top, sub;
    if (u.used_by.length) { cls += " used"; top = "installiert"; sub = u.used_by.join(", "); }
    else if (tg && u.edition === rp.settings.edition) { cls += " target"; top = `Ziel für ${tg.length}`; sub = tg.join(", "); }
    else if (u.removable) { cls += " removable"; top = "entbehrlich"; sub = u.reason; }
    else { top = "bereit"; sub = u.reason; }
    return h("td", {}, h("div", { class: cls, title: `${u.file}\n${bytes(u.size)}\n${u.reason}` },
      h("div", { class: "what" }, h("b", { text: top }), h("span", { text: sub })),
      h("button", { class: "btn icon sm ghost", "aria-label": `${u.version} für ${pid} löschen`, onclick: () => deletePkg(u) }, icon("i-trash"))));
  };
  const matrix = order.length ? h("div", { class: "tablewrap" }, h("table", { class: "matrix" },
    h("thead", {}, h("tr", {}, h("th", { text: "Version" }), profs.map(p => h("th", {}, p.label, h("span", { class: "sub", text: p.id }))))),
    h("tbody", {}, order.map(k => {
      const [v, ed] = k.split("|");
      return h("tr", {}, h("td", { class: "v" }, v, ed !== rp.settings.edition ? h("div", { class: "small mute", text: ed }) : null),
        profs.map(p => cell(rows[k][p.id], p.id)));
    })))) : h("div", { class: "empty", text: "Noch keine Checkmk-Pakete im Repo – »Nach neuen Versionen suchen« lädt die passenden." });
  const autoSw = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: rp.settings.repo_cleanup, onchange: async e => {
    try { await api("PUT", "/api/settings/checkmk", { repo_cleanup: e.target.checked }); toast(e.target.checked ? "Automatisches Bereinigen ist an." : "Automatisches Bereinigen ist aus.", "ok"); }
    catch (x) { toast(x.message, "bad"); e.target.checked = !e.target.checked; }
  } }), "Automatisch bereinigen");
  const cmk = h("div", { class: "panel" },
    h("div", { class: "panel-head" },
      h("div", {}, h("h2", { text: `Checkmk-Pakete (${rp.settings.edition})` }),
        h("p", { text: `${rp.checkmk.length} Pakete, ${bytes(rp.checkmk_bytes)}. Entbehrlich ist ein Paket, sobald alle Sites eine neuere Version haben; die Version des Masters bleibt für neue Kunden.` })),
      h("div", { class: "actions" }, autoSw,
        h("button", { class: "btn sm", disabled: !removable.length, onclick: () => startJob({ cmd: "checkmk-cleanup" },
          `${removable.length} entbehrliche Pakete löschen (${bytes(removable.reduce((a, u) => a + u.size, 0))})?`) },
        removable.length ? `${removable.length} entbehrliche löschen` : "Nichts zu bereinigen"))),
    matrix);
  const disk = rp.disk ? (() => {
    const pct = Math.round(rp.disk.used / rp.disk.total * 100);
    return h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Speicherplatz" }), h("span", { class: "small mute mono", text: rp.disk.path })),
      h("div", { class: "panel-body stack" },
        h("div", { class: "meter", role: "meter", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 },
          (() => { const i = h("i", { class: pct > 90 ? "crit" : pct > 75 ? "warn" : "" }); i.style.width = pct + "%"; return i; })()),
        h("div", { class: "small" }, `${bytes(rp.disk.free)} frei von ${bytes(rp.disk.total)} (${pct} % belegt)`)));
  })() : null;
  const sync = h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Letzte Aktualisierung" })),
    h("div", { class: "panel-body" }, h("dl", { class: "dl" },
      h("dt", { text: "Repo-Sync" }), h("dd", {}, rp.repo_sync ? [fmtTime(rp.repo_sync.time, true), " ", h("span", { class: `tag ${rp.repo_sync.ok ? "ok" : "bad"}`, text: rp.repo_sync.ok ? "OK" : "Fehler" })] : "noch nie"),
      h("dt", { text: "Versionssuche" }), h("dd", { text: chk.time ? fmtTime(chk.time, true) : "noch nie" }),
      h("dt", { text: "Neuester Patch" }), h("dd", { class: "mono", text: chk.patch || "–" }),
      h("dt", { text: "Zeitplan" }), h("dd", { text: `täglich ${ov.schedule.repo_sync} Uhr` }))));
  if (!rp.snapshots_live) banners.push(h("div", { class: "banner amber" },
    `Gerade läuft ein Vorgang, der das Repository sperrt. Die Repo-Stände zeigen den Stand vom ${fmtTime(rp.snapshots_time, true)}.`));
  const osPanels = rp.profiles.map(p => profilePanel(p));
  content.replaceChildren(head, ...banners, h("div", { class: "stack" }, cmk,
    h("div", { class: "cols-2" }, sync, disk),
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Betriebssysteme" }),
      h("p", { text: "Für jedes erkannte Betriebssystem spiegelt der Update-Server die nötigen Pakete. Jeder Abgleich ergibt einen Stand; ältere Stände lassen sich wieder veröffentlichen." })),
      h("div", { class: "actions" }, h("button", { class: "btn", onclick: () => startJob({ cmd: "repo-cleanup" }, "Alte Repo-Stände aufräumen? Die neuesten Stände bleiben erhalten.") }, "Alte Stände aufräumen"))),
    h("div", { class: "cols-2" }, osPanels)));
}
function profilePanel(p) {
  const snaps = [...p.snapshots].reverse();
  const srcs = p.family === "apt" ? p.mirrors.map(m => `${m.dist}  ${m.url}  ${m.components.join(" ")}`)
    : p.repos.map(r => `${r.name}  ${r.url}${r.enabled ? "" : "  (aus)"}`);
  return h("div", { class: "panel" },
    h("div", { class: "panel-head" },
      h("div", {}, h("h2", { text: p.label }), h("p", {}, h("span", { class: "mono", text: p.id }), `  ${p.family}  ·  genutzt von ${p.used_by.join(", ") || "niemandem"}`)),
      h("label", { class: "switch" }, h("input", { type: "checkbox", checked: p.enabled, onchange: async e => {
        try { await api("PUT", `/api/profiles/${encodeURIComponent(p.id)}`, { enabled: e.target.checked }); toast("Gespeichert.", "ok"); }
        catch (x) { toast(x.message, "bad"); e.target.checked = !e.target.checked; }
      } }), "aktiv")),
    h("div", { class: "panel-body stack" },
      p.note ? h("div", { class: "banner amber small", text: p.note }) : null,
      h("div", {}, h("div", { class: "cell-k", text: "Veröffentlichter Stand" }), h("span", { class: "mono", text: p.current || "noch keiner" })),
      snaps.length ? h("ul", { class: "snap-list" }, snaps.map(sn => h("li", {}, h("span", { class: "grow", text: sn }),
        sn === p.current ? h("span", { class: "tag ok", text: "aktiv" })
          : h("button", { class: "btn sm", text: "Veröffentlichen", onclick: () => startJob({ cmd: "repo-rollback", profile_id: p.id, snapshot: sn },
            `Stand ${sn} für ${p.label} veröffentlichen? Die Systeme bekommen danach diesen (älteren) Paketstand.`) })))) : null,
      h("div", {}, h("div", { class: "cell-k", text: p.family === "apt" ? "Spiegel" : "Repos" }),
        srcs.length ? h("div", { class: "keybox" }, srcs.map(s => h("div", { text: s }))) : h("span", { class: "mute", text: "keine eingetragen" })),
      h("div", { class: "actions" }, h("button", { class: "btn sm", text: "Quellen bearbeiten", onclick: () => editSources(p) }))));
}
async function editSources(p) {
  const apt = p.family === "apt";
  const ta = h("textarea", { rows: 7 });
  ta.value = apt ? p.mirrors.map(m => `${m.dist} ${m.url} ${m.components.join(",")}`).join("\n")
    : p.repos.map(r => `${r.name} ${r.url} ${r.enabled ? "an" : "aus"}`).join("\n");
  const ok = await dialog({ title: `Quellen für ${p.label}`, wide: true, ok: "Speichern",
    body: h("div", { class: "stack" },
      h("p", { class: "small", text: apt ? "Eine Zeile je Spiegel: Distribution  URL  Komponenten (mit Komma), z. B. noble-security http://archive.ubuntu.com/ubuntu main,universe"
        : "Eine Zeile je Repo: Name  URL  an|aus, z. B. baseos https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/ an" }), ta),
    onOk: async () => {
      const lines = ta.value.split("\n").map(l => l.trim()).filter(Boolean).map(l => l.split(/\s+/));
      const body = apt ? { mirrors: lines.map(([dist, url, comps]) => ({ dist, url, components: (comps || "").split(",").filter(Boolean) })) }
        : { repos: lines.map(([name, url, en]) => ({ name, url, enabled: en !== "aus" })) };
      await api("PUT", `/api/profiles/${encodeURIComponent(p.id)}`, body);
    } });
  if (ok) { toast("Gespeichert – wirkt beim nächsten Repo-Sync.", "ok"); route(); }
}
async function fetchDialog() {
  const inp = h("input", { class: "mono", placeholder: "2.5.0p15", pattern: "\\d+\\.\\d+\\.\\d+(p\\d+)?", required: true });
  let body;
  const ok = await dialog({ title: "Checkmk-Version laden", ok: "Laden",
    body: h("div", { class: "stack" }, h("p", { text: "Lädt die Version in der eingestellten Edition für alle erkannten Betriebssysteme ins Repo." }), field("Version", inp)),
    onOk: () => { if (!/^\d+\.\d+\.\d+(p\d+)?$/.test(inp.value.trim())) throw new Error("Format 2.5.0p15"); body = { cmd: "checkmk-fetch", version: inp.value.trim() }; } });
  if (ok) startJob(body);
}
async function deletePkg(u) {
  const warn = u.removable ? "" : ` Achtung: ${u.reason}.`;
  if (!await confirmAsk(`Checkmk ${u.version} löschen?`, `Das Paket (${u.edition}) wird für ${u.label} aus dem Repo entfernt.${warn}`, "Löschen", true)) return;
  startJob({ cmd: "checkmk-delete", version: u.version, profile: u.profile, edition: u.edition });
}

/* ================================================================ Seite: Laeufe und Logs */
function ansi(text) {
  const out = document.createDocumentFragment();
  const re = /\x1b\[([0-9;]*)m/g;
  let last = 0, cls = [], m;
  const push = s => { if (!s) return; out.append(cls.length ? h("span", { class: cls.map(c => "a-" + c).join(" "), text: s }) : document.createTextNode(s)); };
  while ((m = re.exec(text))) {
    push(text.slice(last, m.index)); last = re.lastIndex;
    const codes = m[1].split(";").filter(Boolean);
    if (!codes.length || codes.includes("0")) cls = [];
    for (const c of codes) if (c !== "0" && !cls.includes(c)) cls.push(c);
  }
  push(text.slice(last));
  return out;
}
async function pageRuns(jobId, logName) {
  crumbs("Läufe & Logs");
  await Promise.all([refreshJobs(), refreshOverview()]);
  const ov = S.ov;
  const names = [ov.master, ...ov.customers].filter(Boolean).map(s => s.name);
  const pick = new Set();
  const chips = h("div", { class: "actions" }, names.map(n => {
    const b = h("button", { class: "btn sm", "aria-pressed": "false", text: n, onclick: () => {
      if (pick.has(n)) pick.delete(n); else pick.add(n);
      b.setAttribute("aria-pressed", pick.has(n)); b.classList.toggle("primary", pick.has(n));
    } });
    return b;
  }));
  const noMaster = h("input", { type: "checkbox" });
  const opts = () => ({ only: [...pick], no_master: noMaster.checked });
  const L = (cmd, title, text, extra = {}, confirm, hot) => h("button", { class: hot ? "hot" : "", onclick: () => startJob({ cmd, ...extra(), }, confirm) },
    h("b", { text: title }), h("span", { text }));
  const all = () => opts();
  const none = () => ({});
  const launcher = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Aktion starten" }),
      h("p", { text: S.dry ? "Testmodus ist an: Updates werden nur simuliert." : "Läufe laufen auf dem Update-Server weiter, auch wenn diese Seite geschlossen wird." }))),
    h("div", { class: "panel-body stack" },
      h("div", {}, h("div", { class: "group-title", text: "Auf bestimmte Systeme beschränken (sonst alle aktiven)" }), chips,
        h("label", { class: "check", style: null }, noMaster, h("span", {}, "Master auslassen"))),
      h("div", {}, h("div", { class: "group-title", text: "Prüfen" }), h("div", { class: "launch" },
        L("check", "Vorprüfung", "SSH, sudo, Zeit, Tunnel und Platz auf den Systemen prüfen.", all),
        L("inventory", "Systeme erfassen", "Betriebssystem, offene Updates und Checkmk-Version abfragen.", all))),
      h("div", {}, h("div", { class: "group-title", text: "Aktualisieren" }), h("div", { class: "launch" },
        L("update-os", "OS-Updates einspielen", "Kunden zuerst, Master zuletzt; Neustart bei Bedarf.", all,
          "OS-Updates jetzt einspielen? Systeme starten bei Bedarf neu.", true),
        L("update-checkmk", "Checkmk aktualisieren", "Remote Sites zuerst, Master nur, wenn alle erfolgreich waren.", all,
          "Checkmk jetzt aktualisieren? Vor jedem Update wird ein Backup der Site erstellt.", true),
        L("maintenance", "Wartungslauf jetzt", "Kompletter Ablauf wie im Wartungsfenster (OS, dann Checkmk).", () => ({ force: true }),
          "Kompletten Wartungslauf jetzt starten – auch außerhalb des Wartungsfensters?", true),
        h("button", { onclick: editionDialog }, h("b", { text: "Edition wechseln" }), h("span", { text: "Alle Sites auf eine höhere Edition umstellen (gleiche Version)." })))),
      h("div", {}, h("div", { class: "group-title", text: "Repository und Benachrichtigung" }), h("div", { class: "launch" },
        L("repo-sync", "Repo aktualisieren", "Systeme erfassen, Checkmk prüfen, Spiegel abgleichen, bereinigen.", none),
        L("checkmk-cleanup", "Checkmk-Pakete bereinigen", "Pakete löschen, die keine Site mehr braucht.", none),
        L("notify-test", "Teams-Testnachricht", "Prüft, ob Nachrichten im Teams-Kanal ankommen.", none)))));
  const consoleBox = h("div", { class: "console", id: "console", tabindex: "0", "aria-live": "off" });
  const jobHead = h("div", { class: "job-head" });
  const consolePanel = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, jobHead),
    h("div", { class: "panel-body" }, consoleBox));
  const histBody = h("tbody");
  const drawHist = () => {
    const hs = (S.jobs && S.jobs.history) || [];
    histBody.replaceChildren(...(hs.length ? hs.map(j => h("tr", { class: "click", onclick: () => { location.hash = `#/runs/${j.id}`; } },
      h("td", {}, h("span", { class: `tag ${j.status === "ok" ? "ok" : j.status === "running" ? "amber" : j.status === "failed" ? "bad" : ""}`,
        text: { ok: "OK", running: "läuft", failed: "Fehler", unknown: "unbekannt", timeout: "Zeitlimit", cancelled: "abgebrochen" }[j.status] || j.status })),
      h("td", { text: j.title }), h("td", { class: "small mute", text: fmtTime(j.start, true) }), h("td", { class: "small mute", text: j.user })))
      : [h("tr", {}, h("td", { colspan: 4, class: "empty", text: "Noch keine Läufe über die Weboberfläche." }))]));
  };
  const logsBody = h("tbody");
  const hist = h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Läufe über die Weboberfläche" })),
    h("div", { class: "tablewrap" }, h("table", { class: "t" }, histBody)));
  const logsPanel = h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Logs" }),
    h("p", { text: "Die neuesten Protokolle – auch aus dem Zeitplan und dem Terminal." })),
    h("button", { class: "btn sm", text: "Alle Logs", onclick: allLogs })),
  h("div", { class: "tablewrap" }, h("table", { class: "t" }, logsBody)));
  $("#content").replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Läufe & Logs" }),
      h("p", { text: "Alles, was das Terminal-Menü kann – als Knopf. Die Ausgabe erscheint live." }))),
    h("div", { class: "stack" }, (jobId || logName) ? consolePanel : null, launcher, h("div", { class: "cols-2" }, hist, logsPanel)));
  drawHist();
  api("GET", "/api/logs").then(ls => logsBody.replaceChildren(...ls.slice(0, 14).map(l => h("tr", { class: "click", onclick: () => { location.hash = `#/logs/${encodeURIComponent(l.name)}`; } },
    h("td", { class: "mono small", text: l.name }), h("td", { class: "num", text: bytes(l.size) }),
    h("td", { class: "small mute", text: fmtTime(new Date(l.mtime * 1000).toISOString(), true) }))))).catch(() => { });
  if (logName) {
    const lg = await api("GET", `/api/logs/${encodeURIComponent(logName)}`);
    jobHead.replaceChildren(h("h2", { class: "mono", text: lg.name }), h("span", { class: "small mute", text: `${bytes(lg.size)}${lg.truncated ? " – nur das Ende" : ""}` }));
    consoleBox.replaceChildren(ansi(lg.text));
  }
  if (jobId) {
    let offset = 0, done = false;
    const poll = async () => {
      if (done) return;
      let r;
      try { r = await api("GET", `/api/jobs/${jobId}?offset=${offset}`); } catch (e) { return; }
      const atBottom = consoleBox.scrollTop + consoleBox.clientHeight >= consoleBox.scrollHeight - 30;
      if (r.text) { consoleBox.append(ansi(r.text)); offset = r.offset; if (atBottom) consoleBox.scrollTop = consoleBox.scrollHeight; }
      const j = r.job;
      const st = { running: ["amber", "läuft …"], ok: ["ok", "erfolgreich beendet"], failed: ["bad", `mit Fehlern beendet (Code ${j.rc})`],
        unknown: ["", "Ausgang unbekannt (Server neu gestartet)"], timeout: ["bad", "Zeitlimit überschritten – abgebrochen"],
        cancelled: ["bad", `abgebrochen von ${j.cancelled_by || "?"}`] }[j.status] || ["", j.status];
      const stop = j.status === "running" ? h("button", { class: "btn sm danger", text: j.cancelled_by ? "Wird abgebrochen …" : "Abbrechen", disabled: !!j.cancelled_by, onclick: async () => {
        const risky = ["update-os", "update-checkmk", "maintenance", "edition-switch"].includes(j.cmd);
        if (!await confirmAsk("Lauf abbrechen?", risky ? "Achtung: Wird ein Update mittendrin abgebrochen, kann ein System halb aktualisiert zurückbleiben. Nur abbrechen, wenn der Lauf hängt." : "Der Lauf wird beendet. Das ist unkritisch und kann jederzeit neu gestartet werden.", "Abbrechen", true)) return;
        try { await api("POST", `/api/jobs/${j.id}/cancel`); toast("Lauf wird abgebrochen."); } catch (x) { toast(x.message, "bad"); }
      } }) : null;
      jobHead.replaceChildren(h("h2", { text: j.title }), h("span", { class: `tag ${st[0]} job-state`, text: st[1] }),
        h("span", { class: "small mute", text: `gestartet ${fmtTime(j.start, true)} von ${j.user}` }), h("span", { class: "spacer-flex" }), stop);
      if (j.status !== "running") { done = true; refreshJobs().then(drawHist); refreshOverview(); }
    };
    await poll();
    pageEvery(1000, poll);
  }
}
async function allLogs() {
  const ls = await api("GET", "/api/logs");
  const q = h("input", { placeholder: "Filtern, z. B. update-checkmk oder 20261012" });
  const body = h("tbody");
  const draw = () => body.replaceChildren(...ls.filter(l => l.name.includes(q.value.trim())).slice(0, 300).map(l => h("tr", { class: "click", onclick: () => { document.querySelector("dialog").close(); location.hash = `#/logs/${encodeURIComponent(l.name)}`; } },
    h("td", { class: "mono small", text: l.name }), h("td", { class: "num", text: bytes(l.size) }))));
  q.addEventListener("input", draw); draw();
  dialog({ title: `Alle Logs (${ls.length})`, wide: true, ok: "Schließen", cancel: null,
    body: h("div", { class: "stack" }, q, h("div", { class: "tablewrap loglist" }, h("table", { class: "t" }, body))) });
}
async function editionDialog() {
  const sel = h("select", {}, ["pro", "ultimate", "enterprise", "cloud"].map(e => h("option", { value: e, text: e })));
  const info = h("div", { class: "small" });
  const check = async () => {
    try {
      const r = await api("GET", `/api/edition-check?edition=${encodeURIComponent(sel.value)}`);
      info.replaceChildren(...(r.checks.length ? r.checks.map(c => h("div", { class: c.allowed ? "okc" : "err", text: `${c.from} → ${r.edition}: ${c.allowed ? "möglich" : c.reason}` }))
        : [h("div", { class: "mute", text: "Noch keine Sites erfasst." })]));
    } catch (e) { info.textContent = e.message; }
  };
  sel.addEventListener("change", check); check();
  let body;
  const ok = await dialog({ title: "Edition wechseln", ok: "Wechsel starten", wide: true,
    body: h("div", { class: "stack" },
      h("p", { text: "Stellt alle Sites auf die gekaufte Edition um – bei gleicher Version, Remote Sites zuerst, der Master zuletzt. Ein Wechsel auf eine kleinere Edition ist gesperrt. Für kommerzielle Editionen müssen die Download-Zugangsdaten hinterlegt sein (Einstellungen → Checkmk)." }),
      field("Neue Edition", sel), info),
    onOk: () => { body = { cmd: "edition-switch", edition: sel.value }; } });
  if (ok) startJob(body, `Alle Sites jetzt auf ${body.edition} umstellen?`);
}

/* ================================================================ Seite: Labor */
async function pageLab() {
  crumbs("Labor");
  const content = $("#content");
  content.replaceChildren(h("div", { class: "skeleton", text: "Suche ältere Checkmk-Versionen beim Hersteller …" }));
  const [lab, ov] = await Promise.all([api("GET", "/api/lab"), refreshOverview()]);
  if (!lab.enabled) {
    content.replaceChildren(h("div", { class: "banner" }, "Der Labormodus ist aus. Einschalten nur in Testumgebungen, im Terminal-Menü unter l (Labor) → 1."));
    return;
  }
  const systems = [...ov.customers, ov.master].filter(Boolean).filter(s => s.enabled);
  const pick = new Set(systems.map(s => s.name));
  const chips = h("div", { class: "actions" }, systems.map(s => {
    const b = h("button", { class: "btn sm primary", "aria-pressed": "true", onclick: () => {
      if (pick.has(s.name)) pick.delete(s.name); else pick.add(s.name);
      b.setAttribute("aria-pressed", pick.has(s.name)); b.classList.toggle("primary", pick.has(s.name));
    } }, `${s.name}${s.role === "master" ? " (Master)" : ""}`, h("span", { class: "chip-ver", text: s.checkmk.version ? `  ${s.checkmk.version}` : "" }));
    return b;
  }));
  const sel = h("select", {}, h("option", { value: "", text: "Checkmk nicht ändern" }),
    lab.versions.map(v => h("option", { value: v, text: v })));
  const osBox = h("input", { type: "checkbox", checked: true });
  const repoBox = h("input", { type: "checkbox", checked: true });
  const start = async () => {
    const body = { cmd: "lab-rollback", only: [...pick], checkmk: sel.value, os: osBox.checked, clear_repo: repoBox.checked };
    if (!body.only.length) { toast("Bitte mindestens ein System wählen.", "bad"); return; }
    if (!body.checkmk && !body.os && !body.clear_repo) { toast("Bitte auswählen, was zurückgesetzt werden soll.", "bad"); return; }
    const what = [body.checkmk ? `Checkmk auf ${body.checkmk}` : null, body.os ? "OS-Pakete auf Release-Stand" : null,
      body.clear_repo ? "Checkmk-Pakete aus dem Repo löschen" : null].filter(Boolean).join(", ");
    if (!await confirmAsk("Testsysteme zurücksetzen?", `${body.only.join(", ")}: ${what}. Vorher wird jede Site gesichert; schlägt das Zurücksetzen fehl, wird die Sicherung zurückgespielt.`, "Zurücksetzen", true)) return;
    startJob(body);
  };
  content.replaceChildren(
    h("div", { class: "banner bad" }, h("div", {}, h("b", { text: "Labormodus – nur für Testumgebungen. " }),
      "Setzt Systeme absichtlich auf einen alten Stand zurück, damit Repo-Sync und Wartungsfenster zeigen können, dass sie alles wieder aktualisieren. Ausschalten im Terminal-Menü unter l (Labor) → 1.")),
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Testsysteme zurücksetzen" }),
      h("p", { text: "Danach am besten den Zeitplan kurz vorziehen und zuschauen: Repo-Sync lädt die neuen Versionen, das Wartungsfenster spielt alles ein, Teams meldet das Ergebnis." }))),
    h("div", { class: "stack" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Systeme" })),
        h("div", { class: "panel-body" }, chips)),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Was zurücksetzen?" }),
        h("p", { text: `Neueste Version auf den Systemen: ${lab.newest || "unbekannt"} (${lab.edition}). Zur Auswahl stehen ältere Versionen desselben Zweigs beim Hersteller.` }))),
      h("div", { class: "panel-body stack" },
        field("Remote Sites und Master auf diese Checkmk-Version", sel,
          "Liegt auf dem System ein Backup dieser Version, wird es zurückgesetzt – sonst ein Downgrade per omd update (von Checkmk nicht offiziell unterstützt)."),
        h("label", { class: "check" }, osBox, h("span", {}, "OS: harmlose Pakete auf den Release-Stand zurücksetzen",
          h("small", { text: "nano, curl, wget, less, rsync, jq, tzdata, vim-tiny – lädt der Update-Server aus dem Internet; geht nur, wenn er dasselbe Betriebssystem hat." }))),
        h("label", { class: "check" }, repoBox, h("span", {}, "Danach alle Checkmk-Pakete aus dem Repo löschen",
          h("small", { text: "Dann muss der Repo-Sync die neue Version selbst beim Hersteller finden und laden." }))),
        h("div", { class: "form-foot" }, h("button", { class: "btn danger", text: "Zurücksetzen starten", onclick: start }))))));
}

/* ================================================================ Seite: Zeitplan */
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WDDE = { Mon: "Mo", Tue: "Di", Wed: "Mi", Thu: "Do", Fri: "Fr", Sat: "Sa", Sun: "So" };
function weekStrip(sc) {
  const days = sc.weekdays.split(",").map(s => s.trim());
  const toMin = t => { const [a, b] = t.split(":").map(Number); return a * 60 + b; };
  const now = new Date(); const todayIdx = (now.getDay() + 6) % 7;
  const pct = m => `${(m / 1440 * 100).toFixed(2)}%`;
  return h("div", {},
    h("div", { class: "week" }, WD.map((d, i) => {
      const tr = h("div", { class: "track" });
      if (days.includes(d)) { const w = h("div", { class: "win", title: `Wartungsfenster ${sc.start}–${sc.end}` }); w.style.left = pct(toMin(sc.start)); w.style.width = pct(toMin(sc.end) - toMin(sc.start)); tr.append(w); }
      const s = h("div", { class: "sync", title: `Repo-Sync ${sc.repo_sync}` }); s.style.left = pct(toMin(sc.repo_sync)); tr.append(s);
      if (i === todayIdx) { const n = h("div", { class: "nowm", title: "jetzt" }); n.style.left = pct(now.getHours() * 60 + now.getMinutes()); tr.append(n); }
      return h("div", { class: `day${i === todayIdx ? " today" : ""}` }, h("div", { class: "dn" }, h("span", { text: WDDE[d] }), h("span", { text: i === todayIdx ? "heute" : "" })), tr);
    })),
    h("div", { class: "legend" }, h("span", {}, h("i", { class: "lw" }), "Wartungsfenster (OS + Checkmk)"),
      h("span", {}, h("i", { class: "ls" }), "Repo-Sync (täglich)"), h("span", { text: "Raster: 0 · 6 · 12 · 18 Uhr" })));
}
const okT = v => /^([01]?\d|2[0-3]):[0-5]\d$/.test(v.trim()) ? v.trim() : null;
async function pageSchedule() {
  crumbs("Zeitplan");
  const st = await api("GET", "/api/settings");
  const sc = st.schedule, up = st.updates;
  const daysSel = WD.map(d => h("label", { class: "check" }, h("input", { type: "checkbox", value: d, checked: sc.weekdays.split(",").includes(d) }), WDDE[d]));
  const f = {
    start: h("input", { value: sc.start, class: "mono", placeholder: "HH:MM", inputmode: "numeric" }),
    end: h("input", { value: sc.end, class: "mono", placeholder: "HH:MM", inputmode: "numeric" }),
    timezone: h("input", { value: sc.timezone }),
    repo_sync: h("input", { value: sc.repo_sync, class: "mono", placeholder: "HH:MM", inputmode: "numeric" }),
    os_update: h("input", { type: "checkbox", checked: sc.os_update }), include_master_os: h("input", { type: "checkbox", checked: sc.include_master_os }),
    checkmk_update: h("input", { type: "checkbox", checked: sc.checkmk_update }),
    min_minutes_left: h("input", { type: "number", value: sc.min_minutes_left, min: 0, max: 600 }),
  };
  const u = {
    batch_size: h("input", { type: "number", value: up.batch_size, min: 0, max: 500 }),
    downtime_min: h("input", { type: "number", value: up.downtime_min, min: 5, max: 600 }),
    reboot_timeout_min: h("input", { type: "number", value: up.reboot_timeout_min, min: 2, max: 120 }),
    site_start_timeout_min: h("input", { type: "number", value: up.site_start_timeout_min, min: 1, max: 60 }),
  };
  const msg = h("span", { class: "msg" });
  const preview = h("div");
  const drawPreview = () => preview.replaceChildren(weekStrip({
    weekdays: daysSel.filter(l => l.firstChild.checked).map(l => l.firstChild.value).join(",") || "",
    start: okT(f.start.value) || "00:00", end: okT(f.end.value) || "00:00", repo_sync: okT(f.repo_sync.value) || "00:00" }));
  [...daysSel.map(l => l.firstChild), f.start, f.end, f.repo_sync].forEach(el => el.addEventListener("input", drawPreview));
  drawPreview();
  const save = async () => {
    msg.className = "msg"; msg.textContent = "";
    try {
      const wd = daysSel.filter(l => l.firstChild.checked).map(l => l.firstChild.value).join(",");
      const r = await api("PUT", "/api/settings/schedule", { weekdays: wd, start: f.start.value, end: f.end.value, timezone: f.timezone.value,
        repo_sync: f.repo_sync.value, os_update: f.os_update.checked, include_master_os: f.include_master_os.checked,
        checkmk_update: f.checkmk_update.checked, min_minutes_left: f.min_minutes_left.value });
      await api("PUT", "/api/settings/updates", Object.fromEntries(Object.entries(u).map(([k, el]) => [k, el.value])));
      msg.className = "msg okc"; msg.textContent = r.message; refreshOverview();
    } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
  };
  const tm = st.timers;
  $("#content").replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Zeitplan" }),
      h("p", { text: "Im Wartungsfenster laufen zuerst die OS-Updates, danach Checkmk – Remote Sites zuerst, der Master zuletzt. Der Repo-Sync lädt täglich neue Pakete." }))),
    h("div", { class: "stack" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Woche" }),
        h("span", { class: `tag ${tm.state === "neu" ? "ok" : tm.state === "alt" ? "amber" : "bad"}`,
          text: tm.state === "neu" ? "Zeitplan aktiv – Änderungen gelten sofort" : tm.state === "alt" ? "alte Timer – Umstellung nötig" : "Zeitplan aus" })),
      h("div", { class: "panel-body" }, preview,
        h("div", { class: "small mute next-runs" }, st.next_runs.map(t => h("div", { text: t }))),
        tm.state !== "neu" ? h("div", { class: "banner amber small" }, tm.state === "alt"
          ? "Der Zeitplan läuft noch mit den alten Timern: Geänderte Uhrzeiten wirken erst nach einmaliger Umstellung im Terminal: updatemanager-checkmk → 4 → 2."
          : "Der Zeitplan ist nicht aktiv. Einmalig einschalten im Terminal: updatemanager-checkmk → 4 → 2.") : null)),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Wartungsfenster" })),
        h("div", { class: "panel-body" },
          h("div", { class: "group-title", text: "Wochentage" }), h("div", { class: "days" }, daysSel),
          h("div", { class: "form-grid" },
            field("Beginn", f.start), field("Ende", f.end), field("Zeitzone", f.timezone), field("Repo-Sync täglich um", f.repo_sync),
            field("Kein neuer Host, wenn weniger als … Minuten übrig", f.min_minutes_left)),
          h("div", { class: "stack" }, h("div"),
            h("label", { class: "check" }, f.os_update, h("span", {}, "OS-Updates im Wartungsfenster")),
            h("label", { class: "check" }, f.include_master_os, h("span", {}, "Auch den Master per OS-Update aktualisieren", h("small", { text: "als Letztes" }))),
            h("label", { class: "check" }, f.checkmk_update, h("span", {}, "Checkmk-Updates im Wartungsfenster"))))),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Ablauf" })),
        h("div", { class: "panel-body" }, h("div", { class: "form-grid" },
          field("Kunden gleichzeitig (OS und Checkmk)", u.batch_size, "0 = alle auf einmal, der Master immer zuletzt"), field("Downtime pro Host (Minuten)", u.downtime_min),
          field("Max. Wartezeit auf Neustart (Minuten)", u.reboot_timeout_min), field("Max. Wartezeit auf Site-Start (Minuten)", u.site_start_timeout_min)),
          h("div", { class: "form-foot" }, msg, h("button", { class: "btn primary", text: "Zeitplan speichern", onclick: save }))))));
}

/* ================================================================ Seite: Einstellungen */
const TABS = [["checkmk", "Checkmk"], ["ssh", "SSH & Tunnel"], ["repo", "Repository"], ["notify", "Benachrichtigungen"], ["web", "Weboberfläche & Konto"]];
async function pageSettings(tab) {
  tab = TABS.some(t => t[0] === tab) ? tab : "checkmk";
  crumbs("Einstellungen", TABS.find(t => t[0] === tab)[1]);
  const st = await api("GET", "/api/settings");
  const tabs = h("div", { class: "tabs", role: "tablist" }, TABS.map(([k, l]) => h("button", { role: "tab", "aria-selected": String(k === tab), text: l, onclick: () => { location.hash = `#/settings/${k}`; } })));
  const body = { checkmk: setCheckmk, ssh: setSsh, repo: setRepo, notify: setNotify, web: setWeb }[tab](st);
  $("#content").replaceChildren(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Einstellungen" }))), tabs, body);
}
function form(section, defs, values, extraFoot) {
  const els = {};
  const grid = h("div", { class: "form-grid" });
  const checks = h("div", { class: "stack" });
  for (const d of defs) {
    const v = values[d.k];
    let el;
    if (d.t === "bool") { el = h("input", { type: "checkbox", checked: v }); checks.append(h("label", { class: "check" }, el, h("span", {}, d.l, d.hint ? h("small", { text: d.hint }) : null))); }
    else {
      if (d.t === "int") el = h("input", { type: "number", value: v, min: d.min, max: d.max });
      else if (d.t === "choice") el = h("select", {}, d.opts.map(([ov, ol]) => h("option", { value: ov, text: ol, selected: ov === v })));
      else if (d.t === "minpatch") el = h("input", { class: "mono", value: Object.entries(v || {}).map(([a, b]) => `${a}=${b}`).join(", "), placeholder: "2.6.0=2.5.0p20" });
      else el = h("input", { value: v ?? "", class: d.mono ? "mono" : "" });
      grid.append(h("div", { class: d.wide ? "wide" : "" }, field(d.l, el, d.hint)));
    }
    els[d.k] = [d, el];
  }
  const msg = h("span", { class: "msg" });
  const save = async (extra = {}) => {
    msg.className = "msg"; msg.textContent = "";
    const data = { ...extra };
    for (const [k, [d, el]] of Object.entries(els)) {
      if (d.t === "bool") data[k] = el.checked;
      else if (d.t === "minpatch") data[k] = Object.fromEntries(el.value.split(",").map(x => x.trim()).filter(Boolean).map(x => x.split("=").map(y => y.trim())));
      else data[k] = el.value;
    }
    try {
      const r = await api("PUT", `/api/settings/${section}`, data);
      msg.className = "msg okc"; msg.textContent = r.message || "Gespeichert.";
    } catch (e) {
      if (section === "checkmk" && /Edition hier/.test(e.message) &&
        await confirmAsk("Nur die gesuchte Edition ändern?", e.message, "Trotzdem ändern")) return save({ confirm_edition: true });
      msg.className = "msg err"; msg.textContent = e.message;
    }
  };
  return h("div", { class: "panel" }, h("div", { class: "panel-body" }, grid, grid.children.length && checks.children.length ? h("div", { class: "group-title", text: " " }) : null, checks,
    h("div", { class: "form-foot" }, msg, extraFoot || null, h("button", { class: "btn primary", text: "Speichern", onclick: () => save() }))));
}
function secretLine(label, exists, kind, text, placeholder) {
  return h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: label }), h("p", { text })),
    h("div", { class: "actions" }, h("span", { class: `tag ${exists ? "ok" : "bad"}`, text: exists ? "hinterlegt" : "fehlt" }),
      h("button", { class: "btn sm", text: exists ? "Ändern" : "Hinterlegen", onclick: () => secretDialog(kind, label, text, placeholder) }))));
}
function setCheckmk(st) {
  const c = st.checkmk;
  return h("div", { class: "stack" },
    form("checkmk", [
      { k: "edition", l: "Gekaufte Edition", t: "choice", opts: st.editions.map(e => [e, e]), hint: "nach dieser Edition wird gesucht – nie eine kleinere" },
      { k: "backup", l: "Backup vor dem Update", t: "choice", opts: [["no-past", "Konfiguration ohne Historie (empfohlen)"], ["full", "Komplett inkl. Historie"], ["none", "Kein Backup"]] },
      { k: "keep_backups", l: "Backups pro Site behalten", t: "int", min: 1, max: 20 },
      { k: "min_patch", l: "Mindest-Patchstand vor Major", t: "minpatch", hint: "z. B. 2.6.0=2.5.0p20, leer = keiner", wide: true },
      { k: "auto_patch", l: "Neue Patch-Versionen automatisch laden und einspielen", t: "bool" },
      { k: "major_download", l: "Neue Major-Versionen sofort ins Repo laden", t: "bool" },
      { k: "major_auto", l: "Major-Updates automatisch einspielen", t: "bool", hint: "nur ein Zweig pro Lauf, erst nach dem neuesten Patch, nur wenn das OS unterstützt wird" },
      { k: "auto_rollback", l: "Bei Fehler automatisch auf das Backup zurückrollen", t: "bool" },
      { k: "repo_cleanup", l: "Checkmk-Pakete automatisch aus dem Repo löschen, sobald keine Site sie mehr braucht", t: "bool" },
      { k: "omd_cleanup", l: "Nach erfolgreichem Update alte Checkmk-Versionen auf den Systemen entfernen", t: "bool", hint: "omd cleanup – entfernt nur Versionen, die keine Site nutzt" },
      { k: "require_hash", l: "Download nur mit Prüfsumme des Herstellers", t: "bool" },
    ], c),
    secretLine("Download-Zugang für kommerzielle Editionen", st.secrets.download, "download", "Zugang zum Checkmk-Downloadportal im Format user:passwort.", "user:passwort"));
}
function setSsh(st) {
  return h("div", { class: "stack" },
    form("ssh", [
      { k: "user", l: "Service-User auf den Systemen", t: "text" },
      { k: "connect_timeout", l: "Verbindungs-Timeout (Sekunden)", t: "int", min: 3, max: 120 },
      { k: "jumphost", l: "Standard-Jumphost", t: "text", mono: true, hint: "user@host[:port], leer = keiner" },
    ], st.ssh),
    form("repo", [
      { k: "listen_host", l: "Repo-Adresse aus Sicht dieses Servers", t: "text", mono: true },
      { k: "listen_port", l: "Repo-Port (nginx)", t: "int", min: 1, max: 65535 },
      { k: "tunnel_port", l: "Tunnel-Port auf den Systemen", t: "int", min: 1024, max: 65535 },
    ], st.repo),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Öffentlicher Schlüssel des Update-Servers" }),
      h("p", { text: "Steht im Vorbereitungs-Skript und wird auf jedem System beim Service-User hinterlegt." }))),
    h("div", { class: "panel-body" }, st.pubkey ? h("div", { class: "keybox", text: st.pubkey }) : h("p", { class: "err", text: "Noch kein Schlüssel – im Terminal unter SSH & Tunnel erzeugen." }))));
}
function setRepo(st) {
  return h("div", { class: "stack" },
    form("repo", [
      { k: "filter_mode", l: "Umfang der apt-Spiegel", t: "choice", opts: [["packages", "nur benötigte Pakete (klein)"], ["full", "komplette Distribution (groß)"]] },
      { k: "keep_snapshots", l: "Repo-Stände je Betriebssystem behalten", t: "int", min: 1, max: 60 },
      { k: "sync_retries", l: "Wiederholungen bei Download-Fehlern", t: "int", min: 0, max: 10 },
      { k: "sync_retry_wait_s", l: "Wartezeit zwischen Wiederholungen (Sekunden)", t: "int", min: 0, max: 3600 },
    ], st.repo),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Zeitserver für die Systeme" }),
      h("p", { text: "Wird ins Vorbereitungs-Skript übernommen. Leer = Update-Server selbst bzw. Standard des Systems." }))),
    (() => { const inp = h("input", { class: "mono", value: st.ntp_server }); const m = h("span", { class: "msg" });
      return h("div", { class: "panel-body" }, field("NTP-Server", inp), h("div", { class: "form-foot" }, m,
        h("button", { class: "btn primary", text: "Speichern", onclick: async () => {
          try { await api("PUT", "/api/settings/ntp_server", { value: inp.value }); m.className = "msg okc"; m.textContent = "Gespeichert."; }
          catch (e) { m.className = "msg err"; m.textContent = e.message; } } }))); })()));
}
function setNotify(st) {
  return h("div", { class: "stack" },
    secretLine("Teams-Kanal (Webhook)", st.secrets.teams, "teams",
      "Adresse aus Teams → Workflows → »Beim Empfang einer Webhookanforderung …«. Wer sie kennt, kann in den Kanal schreiben.", "https://…"),
    form("notify", [
      { k: "mode", l: "Wann melden", t: "choice", opts: [["always", "nach jedem Lauf"], ["problems", "nur bei Fehlern oder Hinweisen"]] },
      { k: "teams", l: "Teams-Benachrichtigung eingeschaltet", t: "bool" },
      { k: "manual_runs", l: "Auch Läufe melden, die von Hand gestartet werden", t: "bool", hint: "aus dem Menü oder dieser Oberfläche" },
    ], st.notify, h("button", { class: "btn", text: "Testnachricht senden", onclick: () => startJob({ cmd: "notify-test" }) })),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Checkmk-Services im Master" }),
      h("p", { text: st.notify.checkmk_localcheck ? "Eingerichtet: Der Agent auf diesem Server liefert die Services »Updatemanager …«." : "Nicht eingerichtet. Einrichten im Terminal: updatemanager-checkmk → b → 4 (braucht den Checkmk-Agenten auf diesem Server)." })),
    h("span", { class: `tag ${st.notify.checkmk_localcheck ? "ok" : ""}`, text: st.notify.checkmk_localcheck ? "aktiv" : "aus" }))));
}
function setWeb(st) {
  const w = st.web;
  const old = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const nw2 = h("input", { type: "password", autocomplete: "new-password" });
  const m = h("span", { class: "msg" });
  return h("div", { class: "stack" },
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h2", { text: "Erreichbarkeit" }),
      h("p", { text: "Aus Sicherheitsgründen nur im Terminal änderbar." }))),
    h("div", { class: "panel-body stack" },
      h("dl", { class: "dl" }, h("dt", { text: "Lauscht auf" }), h("dd", { class: "mono", text: `${w.listen}:${w.port}` }),
        h("dt", { text: "HTTPS" }), h("dd", {}, h("span", { class: `tag ${w.tls ? "ok" : "amber"}`, text: w.tls ? "an" : "aus" })),
        h("dt", { text: "Sitzungsdauer" }), h("dd", { text: `${w.session_hours} Stunden` })),
      h("div", { class: "keybox" },
        h("div", { text: "updatemanager-checkmk web listen 0.0.0.0     # im Netz erreichbar" }),
        h("div", { text: "updatemanager-checkmk web tls                # HTTPS mit eigenem Zertifikat" }),
        h("div", { text: "updatemanager-checkmk web passwd NAME        # weiteren Benutzer anlegen" }),
        h("div", { text: "updatemanager-checkmk web off                # Oberfläche ausschalten" })))),
    h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: `Passwort ändern (${S.me})` })),
      h("div", { class: "panel-body" }, h("div", { class: "form-grid" }, field("Bisheriges Passwort", old), field("Neues Passwort", nw, "mindestens 10 Zeichen"), field("Wiederholen", nw2)),
        h("div", { class: "form-foot" }, m, h("button", { class: "btn primary", text: "Passwort ändern", onclick: async () => {
          m.className = "msg err";
          if (nw.value !== nw2.value) { m.textContent = "Die neuen Passwörter stimmen nicht überein."; return; }
          try { await api("POST", "/api/password", { old: old.value, new: nw.value }); m.className = "msg okc"; m.textContent = "Passwort geändert."; old.value = nw.value = nw2.value = ""; }
          catch (e) { m.textContent = e.message; }
        } })))));
}

/* ================================================================ Seite: Hilfe (Markdown) */
function mdInline(text) {
  const frag = document.createDocumentFragment();
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|(?<![*\w])\*[^*\n]+\*(?![*\w]))/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const t = m[0];
    if (t.startsWith("`")) frag.append(h("code", { text: t.slice(1, -1) }));
    else if (t.startsWith("**")) frag.append(h("strong", {}, mdInline(t.slice(2, -2))));
    else if (t.startsWith("[")) { const mm = t.match(/^\[([^\]]+)\]\(([^)]+)\)$/); const safe = /^(https?:|#|mailto:)/.test(mm[2]); frag.append(h("a", { href: safe ? mm[2] : "#", rel: "noopener", target: mm[2].startsWith("http") ? "_blank" : null, text: mm[1] })); }
    else frag.append(h("em", { text: t.slice(1, -1) }));
    last = re.lastIndex;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}
const slug = s => s.toLowerCase().replace(/[^a-z0-9äöüß]+/g, "-").replace(/^-|-$/g, "");
function mdRender(md) {
  const root = h("div", { class: "md" });
  const lines = md.split("\n");
  let i = 0;
  const toc = [];
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; root.append(h("pre", {}, h("code", { text: buf.join("\n") }))); continue;
    }
    const hm = l.match(/^(#{1,3}) (.*)$/);
    if (hm) {
      const lv = hm[1].length, id = slug(hm[2]);
      root.append(h("h" + lv, { id }, mdInline(hm[2])));
      if (lv === 2) toc.push([hm[2], id]);
      i++; continue;
    }
    if (/^\s*\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = rows.filter(r => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r)).map(r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
      const [head, ...rest] = cells;
      root.append(h("table", {}, h("thead", {}, h("tr", {}, head.map(c => h("th", {}, mdInline(c))))),
        h("tbody", {}, rest.map(r => h("tr", {}, r.map(c => h("td", {}, mdInline(c))))))));
      continue;
    }
    if (/^\s*([-*]|\d+\.) /.test(l)) {
      const ordered = /^\s*\d+\. /.test(l);
      const list = h(ordered ? "ol" : "ul");
      const stack = [[list, l.search(/\S/)]];
      while (i < lines.length && (/^\s*([-*]|\d+\.) /.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && !/^\s*\|/.test(lines[i])))) {
        const cur = lines[i];
        const ind = cur.search(/\S/);
        const item = cur.match(/^\s*(?:[-*]|\d+\.) (.*)$/);
        if (!item) { const last = stack[stack.length - 1][0].lastChild; if (last) last.append(" ", mdInline(cur.trim())); i++; continue; }
        while (stack.length > 1 && ind < stack[stack.length - 1][1]) stack.pop();
        if (ind > stack[stack.length - 1][1] + 1) {
          const parentLi = stack[stack.length - 1][0].lastChild;
          const sub = h(/^\s*\d+\. /.test(cur) ? "ol" : "ul");
          if (parentLi) parentLi.append(sub); stack.push([sub, ind]);
        }
        stack[stack.length - 1][0].append(h("li", {}, mdInline(item[1])));
        i++;
      }
      root.append(list); continue;
    }
    if (/^\s*$/.test(l) || /^---\s*$/.test(l)) { i++; continue; }
    const buf = [l]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,3} |```|\s*\||\s*([-*]|\d+\.) |---\s*$)/.test(lines[i])) buf.push(lines[i++]);
    root.append(h("p", {}, mdInline(buf.join(" "))));
  }
  return [root, toc];
}
let HELP_CACHE = null;
async function pageHelp(anchor) {
  if (!HELP_CACHE) HELP_CACHE = (await api("GET", "/api/help")).text;
  const parts = HELP_CACHE.split(/^(?=## )/m);
  const secs = parts.map((p, i) => {
    const title = i === 0 ? "Überblick" : p.split("\n")[0].replace(/^## /, "").trim();
    return { title, id: slug(title), md: p };
  });
  const cur = secs.find(s => s.id === anchor) || secs[0];
  crumbs("Hilfe", cur.title);
  const idx = secs.indexOf(cur);
  const nav = h("nav", { class: "help-toc", "aria-label": "Kapitel" }, secs.map(s =>
    h("a", { href: `#/help/${encodeURIComponent(s.id)}`, class: s === cur ? "on" : "", text: s.title })));
  const [doc] = mdRender(cur.md);
  const pager = h("div", { class: "pager" },
    idx > 0 ? h("a", { class: "btn", href: `#/help/${encodeURIComponent(secs[idx - 1].id)}`, text: `Zurück: ${secs[idx - 1].title}` }) : h("span"),
    idx < secs.length - 1 ? h("a", { class: "btn", href: `#/help/${encodeURIComponent(secs[idx + 1].id)}`, text: `Weiter: ${secs[idx + 1].title}` }) : h("span"));
  $("#content").replaceChildren(h("div", { class: "help" }, nav, h("div", {}, doc, pager)));
}

/* ================================================================ Start */
async function boot() {
  let s;
  try { s = await api("GET", "/api/session"); } catch (e) { $("#app").replaceChildren(h("div", { class: "login" }, h("div", { class: "banner bad", text: `Server nicht erreichbar: ${e.message}` }))); return; }
  // Nach einem Update des Programms die Seite einmal neu laden, damit auch die neue Oberflaeche geladen wird
  if (S.loadedVersion && S.loadedVersion !== s.version) { location.reload(); return; }
  S.loadedVersion = s.version;
  S.version = s.version; S.host = s.host;
  if (!s.user) { renderLogin(); return; }
  S.me = s.user; S.csrf = s.csrf;
  renderShell();
  await Promise.all([refreshJobs(), refreshOverview()]);
  stopTimers();
  every(5000, refreshJobs);
  every(60000, () => { refreshOverview(); });
  route();
}
document.addEventListener("DOMContentLoaded", boot);
