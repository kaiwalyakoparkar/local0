"use strict";
// local0 dashboard — vanilla JS, no build step. Talks to the router's own JSON API.

const $ = (id) => document.getElementById(id);
const tokEl = $("admintok");
tokEl.value = localStorage.getItem("local0_admin_token") || "";
tokEl.addEventListener("change", () => localStorage.setItem("local0_admin_token", tokEl.value));

// --- theme toggle (persisted) ---
const savedTheme = localStorage.getItem("local0_theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
$("theme").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("local0_theme", next);
});

function adminHeaders(extra) {
  const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
  if (tokEl.value) h["X-Admin-Token"] = tokEl.value;
  return h;
}
async function jget(url) { const r = await fetch(url); return r.json(); }
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

// --- tabs ---
document.querySelectorAll("nav.tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    b.classList.add("active");
    $("view-" + b.dataset.view).classList.add("active");
    if (b.dataset.view === "documents") loadDocs();
    if (b.dataset.view === "learned") loadLearned();
    if (b.dataset.view === "routing") loadStats();
  });
});

// --- chat ---
function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.innerHTML = html;
  $("chatlog").appendChild(d);
  d.scrollIntoView({ behavior: "smooth", block: "end" });
  return d;
}
async function send() {
  const q = $("chatinput").value.trim();
  if (!q) return;
  $("chatinput").value = "";
  addMsg("user", esc(q));
  const btn = $("send"); btn.disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [{ role: "user", content: q }], stream: false }),
    });
    const ms = Math.round(performance.now() - t0);
    if (r.status === 424) {
      const bot = addMsg("bot", "<em class='muted'>No strong local match — escalating to cloud…</em>");
      await cloudFallback(q, bot, ms);
    } else if (r.ok) {
      const j = await r.json();
      const ans = j.choices?.[0]?.message?.content || "(empty)";
      const srcs = (j.sources || []).map((s) =>
        `<span class="chip src">${esc(s.source)}${s.section ? " › " + esc(s.section) : ""}</span>`).join(" ");
      addMsg("bot", esc(ans) +
        `<div class="meta"><span class="badge local">Local</span><span class="chip">${ms} ms</span>${srcs}</div>`);
    } else {
      addMsg("bot", `<em class='muted'>error ${r.status}</em>`);
    }
  } catch (e) {
    addMsg("bot", `<em class='muted'>request failed: ${esc(String(e))}</em>`);
  } finally { btn.disabled = false; }
}
async function cloudFallback(q, botEl, localMs) {
  // Relay through the live gateway path (admin-gated) to show the cloud answer.
  try {
    const r = await fetch("/demo/gateway-chat", {
      method: "POST", headers: adminHeaders(), body: JSON.stringify({ query: q }),
    });
    const j = await r.json();
    const ans = j.choices?.[0]?.message?.content || j.detail || JSON.stringify(j).slice(0, 400);
    botEl.innerHTML = esc(ans) +
      `<div class="meta"><span class="badge escalate">Escalated → cloud</span></div>`;
  } catch (e) {
    botEl.innerHTML = `<span class="badge escalate">Escalated</span>` +
      `<div class="meta muted">gateway relay unavailable (set admin token / start gateway)</div>`;
  }
}
$("send").addEventListener("click", send);
$("chatinput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

// --- documents ---
async function loadDocs() {
  const j = await jget("/documents");
  const rows = (j.items || []).map((d) =>
    `<tr><td class="q">${esc(d.source)}</td><td>${d.chunks}</td>
     <td><button class="btn danger" data-del="${esc(d.source)}">delete</button></td></tr>`).join("");
  $("doclist").innerHTML = rows || `<tr><td colspan="3" class="muted">No documents ingested yet.</td></tr>`;
  $("doclist").querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => delDoc(b.dataset.del)));
}
async function addDoc() {
  const name = $("docname").value.trim(), text = $("doctext").value.trim();
  if (!name || !text) { toast("name and text required"); return; }
  $("docstatus").textContent = "ingesting…";
  const r = await fetch("/documents", { method: "POST", headers: adminHeaders(),
    body: JSON.stringify({ name, text }) });
  const j = await r.json();
  if (r.ok) {
    $("docstatus").textContent = `stored ${j.chunks} chunks`;
    $("docname").value = ""; $("doctext").value = ""; loadDocs();
  } else { $("docstatus").textContent = j.detail || "failed"; }
}
async function delDoc(source) {
  const r = await fetch("/documents/" + encodeURIComponent(source),
    { method: "DELETE", headers: adminHeaders() });
  if (r.ok) { toast("deleted " + source); loadDocs(); } else { toast("delete failed"); }
}
$("adddoc").addEventListener("click", addDoc);

// --- learned ---
async function loadLearned() {
  const j = await jget("/learned");
  const rows = (j.items || []).map((r) =>
    `<tr><td class="q">${esc(r.query)}</td><td>${esc(r.answer)}</td></tr>`).join("");
  $("learnlist").innerHTML = rows || `<tr><td colspan="2" class="muted">Nothing learned yet.</td></tr>`;
}

// --- routing & stats ---
let threshold = 0.5;
async function loadStats() {
  const s = await jget("/stats");
  threshold = s.threshold;
  const tile = (l, n, cls) =>
    `<div class="tile ${cls || ""}"><div class="n">${n}</div><div class="l">${l}</div></div>`;
  $("tiles").innerHTML =
    tile("Total requests", s.total) +
    tile("Answered local", s.answered_local) +
    tile("Escalated", `${s.escalated_pct}<span class="unit">%</span>`, "accent") +
    tile("Cloud $ avoided", `<span class="unit">$</span>${s.est_usd_avoided}`) +
    tile("Learned", s.learned);

  // local vs escalated meter
  const localPct = s.total ? (100 * s.answered_local / s.total) : 0;
  $("meter").innerHTML =
    `<span class="m-local" style="width:${localPct}%"></span>` +
    `<span class="m-esc" style="width:${100 - localPct}%"></span>`;

  const hist = s.histogram || [], buckets = s.buckets || hist.length || 1;
  const max = Math.max(1, ...hist);
  const thrBucket = Math.round(threshold * buckets);
  $("hist").innerHTML =
    hist.map((v, i) =>
      `<div class="bar ${i < thrBucket ? "dim" : ""}" style="height:${(v / max) * 100}%" title="${v}"></div>`).join("") +
    `<div class="thmark" style="left:${threshold * 100}%" data-l="thr ${threshold.toFixed(2)}"></div>`;

  $("thr").value = threshold; $("thrval").textContent = threshold.toFixed(2);
  $("tags").value = (s.learn_tags || []).join(", ");

  const dbg = await jget("/debug").catch(() => ({}));
  const q = dbg.qdrant || {};
  // Reroute state: three levels, not a binary alarm. No escalations yet, or a
  // standalone run without a gateway, is normal — amber "info", never red.
  let rrState, rrLabel;
  if (!(dbg.escalated > 0)) { rrState = "neutral"; rrLabel = "no escalations yet"; }
  else if (dbg.learn_calls > 0) { rrState = "ok"; rrLabel = "gateway reroute active"; }
  else { rrState = "warn"; rrLabel = "awaiting gateway /learn callback"; }
  $("health").innerHTML =
    pill(q.reachable ? "ok" : "bad", `Qdrant ${q.reachable ? "up" : "down"}`) +
    pill(q.total > 0 ? "ok" : "neutral", `${q.total || 0} vectors`) +
    pill(rrState, rrLabel);
}
function pill(state, label) {
  const cls = { ok: "ok", bad: "bad", warn: "warn", neutral: "" }[state] || "";
  return `<span class="pill ${cls}"><span class="dot"></span>${esc(label)}</span>`;
}
$("thr").addEventListener("input", () => $("thrval").textContent = (+$("thr").value).toFixed(2));
$("savethr").addEventListener("click", async () => {
  const r = await fetch("/config", { method: "POST", headers: adminHeaders(),
    body: JSON.stringify({ threshold: +$("thr").value }) });
  toast(r.ok ? "threshold saved" : "save failed (admin token?)");
});
$("savetags").addEventListener("click", async () => {
  const tags = $("tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  const r = await fetch("/config", { method: "POST", headers: adminHeaders(),
    body: JSON.stringify({ tags }) });
  toast(r.ok ? "tags saved" : "save failed (admin token?)");
});
$("resetstats").addEventListener("click", async () => {
  const r = await fetch("/stats/reset", { method: "POST", headers: adminHeaders() });
  if (r.ok) { toast("stats reset"); loadStats(); } else { toast("reset failed (admin token?)"); }
});
