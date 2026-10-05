"""The dashboard: one self-contained HTML page (markup, styles, scripts)."""


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataGuard</title>
<link rel="icon" href="data:,">
<style>
:root{color-scheme:light dark;--bg:#f4f6f9;--card:#fff;--ink:#101828;--mute:#667085;--line:#e4e7ec;--acc:#2563eb;--accink:#fff;--up:#9333ea;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--track:#e8ecf2}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f14;--card:#141a22;--ink:#e8edf3;--mute:#8b96a5;--line:#242d38;--acc:#5aa2ff;--accink:#06101f;--up:#c4a1ff;--ok:#3fb950;--warn:#e3a008;--bad:#f85149;--track:#243040}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:22px 16px 56px;display:grid;gap:14px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{margin:0;font-size:20px;letter-spacing:-.01em}
h2{margin:0 0 10px;font-size:11.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--mute)}
.chip{font-size:12.5px;padding:4px 11px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--mute)}
.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.warn{color:var(--warn);border-color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.hero{display:flex;gap:26px;align-items:center;flex-wrap:wrap}
#ring{width:170px;height:170px;flex:none}
.trk,.arc{fill:none;stroke-width:10}.trk{stroke:var(--track)}
.arc{stroke:var(--ok);stroke-linecap:round;stroke-dasharray:0 400;transition:stroke-dasharray .6s,stroke .3s}
.big{font-size:24px;font-weight:700;fill:var(--ink)}
.facts{flex:1;min-width:260px;display:grid;grid-template-columns:repeat(2,minmax(120px,1fr));gap:16px 24px}
.facts span{display:block;font-size:12.5px;color:var(--mute)}
.facts b{font-size:22px;letter-spacing:-.02em}
.facts small{display:block;color:var(--mute);font-size:12.5px}
.num{font-size:28px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--mute);font-size:13.5px}
.meter{height:8px;background:var(--track);border-radius:99px;overflow:hidden;margin:10px 0 6px}
.meter i{display:block;height:100%;width:0;background:var(--ok);border-radius:99px;transition:width .5s,background .3s}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
svg text{font-family:inherit}
#spark{width:100%;height:58px;display:block;margin-top:8px}
#spark polyline{fill:none;stroke-width:1.8;stroke-linejoin:round;vector-effect:non-scaling-stroke}
#sd{stroke:var(--acc)}#su{stroke:var(--up)}
#bars{width:100%;height:auto;display:block}
.col{fill:var(--acc);opacity:.5}.col.today{opacity:1}
.allow{stroke:var(--warn);stroke-width:1;stroke-dasharray:4 3}
.axis{font-size:10px;fill:var(--mute)}
button{font:inherit;padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc)}button:disabled{opacity:.6;cursor:default}
button.pri{background:var(--acc);color:var(--accink);border-color:transparent;font-weight:600}
ul{list-style:none;margin:0;padding:0;display:grid;gap:10px}
li .t{font-weight:600}li small{color:var(--mute);display:block}
.row{display:grid;grid-template-columns:minmax(90px,170px) 1fr auto;gap:10px;align-items:center;font-size:14px}
.row .meter{margin:0}
details summary{cursor:pointer;font-weight:600}
.apphead,.approw summary{display:grid;grid-template-columns:minmax(110px,200px) 1fr 76px 88px;gap:12px;align-items:center}
.apphead{font-size:11.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--mute);padding-bottom:4px}
.apphead[hidden]{display:none}
.apphead span:nth-child(n+3){text-align:right}
.approw{border-top:1px solid var(--line)}
.approw summary{cursor:pointer;font-size:14px;padding:9px 0;list-style:none}
.approw summary::-webkit-details-marker{display:none}
.approw .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.approw .car{display:inline-block;color:var(--mute);margin-right:6px;transition:transform .15s}
.approw[open] .car{transform:rotate(90deg)}
.approw .meter{margin:0}
.approw .v1,.approw .v2{text-align:right;font-variant-numeric:tabular-nums}
.approw .v1{color:var(--mute)}
.days{display:flex;gap:2px;align-items:flex-end;height:44px;margin:2px 0 6px}
.days i{flex:1;min-height:2px;background:var(--acc);opacity:.55;border-radius:2px}
.days i.z{background:var(--track);opacity:1}
.daylbl{display:flex;justify-content:space-between;font-size:11px;color:var(--mute);padding-bottom:8px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0}
label{display:grid;gap:4px;font-size:13px;color:var(--mute)}
label.chk{display:flex;gap:8px;align-items:center}
input,select{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);min-width:0}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
#msg{font-size:13px;color:var(--mute)}
.hint{margin:0;font-size:13px;color:var(--mute)}
footer{font-size:12.5px;color:var(--mute)}
</style></head>
<body><main>
<header><h1>DataGuard</h1><span id="chip" class="chip">connecting&hellip;</span></header>

<section class="card hero">
  <svg id="ring" viewBox="0 0 120 120" role="img" aria-label="Share of data budget used">
    <circle class="trk" cx="60" cy="60" r="52"/>
    <circle id="arc" class="arc" cx="60" cy="60" r="52" transform="rotate(-90 60 60)"/>
    <text id="pct" class="big" x="60" y="68" text-anchor="middle">-</text>
  </svg>
  <div class="facts">
    <div><span>Used this cycle</span><b id="used">-</b><small id="plan"></small></div>
    <div><span id="remk">Left in budget</span><b id="rem">-</b><small id="remn"></small></div>
    <div><span>Days to renewal</span><b id="days">-</b><small id="renew"></small></div>
    <div><span>Safe daily allowance</span><b id="allow">-</b><small>to last until renewal</small></div>
  </div>
</section>
<p class="hint" id="hint" hidden></p>

<div class="grid">
  <section class="card"><h2>Today</h2>
    <div class="num" id="today">-</div>
    <div class="meter"><i id="tbar"></i></div>
    <div class="sub" id="tnote"></div></section>
  <section class="card"><h2>Pace</h2>
    <span class="tag" id="ptag">-</span>
    <p class="sub" id="pnote" style="margin:10px 0 0"></p></section>
  <section class="card"><h2>Live</h2>
    <div><span class="num" id="down">-</span> <span class="sub">down</span></div>
    <div class="sub"><span id="up">-</span> up<span id="nc"></span></div>
    <svg id="spark" viewBox="0 0 300 56" preserveAspectRatio="none"><polyline id="sd"/><polyline id="su"/></svg></section>
</div>

<section class="card"><h2>Last 30 days</h2><svg id="bars" viewBox="0 0 600 160"></svg></section>

<section class="card"><h2>Who's using data right now?</h2>
  <div class="actions"><button id="whoBtn" class="pri">Measure (3 s)</button>
  <span class="sub" id="whoNote">Estimated from per-app activity on this computer.</span></div>
  <div id="who" style="margin-top:12px;display:grid;gap:8px"></div></section>

<section class="card"><h2>Data by app</h2>
  <div class="sub" id="appsNote">Loading&hellip;</div>
  <div class="apphead" id="appsHead" hidden><span>App</span><span>Share of this cycle</span><span>Today</span><span>This cycle</span></div>
  <div id="apps"></div></section>

<section class="card"><h2>Recent alerts</h2><ul id="alerts"></ul></section>

<section class="card"><details><summary>Settings &amp; calibration</summary>
  <div class="form">
    <label>Plan size (GB)<input id="f_plan" type="number" min="1" step="1"></label>
    <label>Renewal day of month<input id="f_day" type="number" min="1" max="31"></label>
    <label>Reserve (% kept untouched)<input id="f_res" type="number" min="0" max="50" step="1"></label>
    <label>Phone hotspot Wi-Fi name<input id="f_ssid" placeholder="empty = count the whole interface"></label>
    <label>Network interface<select id="f_iface"></select></label>
    <label>Alert at (% of budget, comma separated)<input id="f_alerts"></label>
    <label>Warn above (MB per minute, 0 = off)<input id="f_burst" type="number" min="0"></label>
    <label class="chk"><input id="f_bin" type="checkbox"> My carrier counts 1 GB = 1024 MB</label>
    <label class="chk"><input id="f_notify" type="checkbox"> Desktop notifications</label>
  </div>
  <div class="actions"><button class="pri" id="save">Save</button>
    <button id="useSsid">Use the Wi-Fi I'm on now</button>
    <button id="test">Test notification</button><span id="msg"></span></div>
  <div class="actions" style="margin-top:18px">
    <label>Carrier says I've used (GB) this cycle<input id="f_cal" type="number" min="0" step="0.1"></label>
    <button id="calBtn">Calibrate</button></div>
  <p class="sub">The laptop only sees its own traffic; your carrier also counts your phone's. Calibrate every few days
  (check your carrier's app) and the totals stay exact.</p>
</details></section>
<footer>Everything stays on this computer. DataGuard makes no internet connections of its own.</footer>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
let U = {gb: 1e9, mb: 1e6}, filled = false, timer = 0, msgTimer = 0;

const gb = b => { const v = b / U.gb; return (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)) + " GB"; };
const sz = b => {
  const a = Math.abs(b);
  if (a >= U.gb) return (b / U.gb).toFixed(2) + " GB";
  if (a >= U.mb) return (b / U.mb).toFixed(a >= 10 * U.mb ? 0 : 1) + " MB";
  return Math.round(b / (U.mb / 1000)) + " KB";
};
const dstr = iso => new Date(iso + "T12:00:00").toLocaleDateString(undefined, {month: "short", day: "numeric"});
const txt = (sel, t) => { $(sel).textContent = t; };
const tone = p => p >= 90 ? "var(--bad)" : p >= 75 ? "var(--warn)" : "var(--ok)";
const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const svg = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const msg = t => { txt("#msg", t); clearTimeout(msgTimer); msgTimer = setTimeout(() => txt("#msg", ""), 6000); };

function spark(down, up) {
  const W = 300, H = 56, max = Math.max(50000, ...down, ...up);
  const pts = a => a.map((v, i) => (i * W / (a.length - 1)).toFixed(1) + "," + (H - 2 - v / max * (H - 6)).toFixed(1)).join(" ");
  $("#sd").setAttribute("points", pts(down));
  $("#su").setAttribute("points", pts(up));
}

function bars(hist, allow) {
  const el = $("#bars"); el.replaceChildren();
  const W = 600, H = 160, pad = 16, n = hist.length, y0 = H - pad, bw = W / n;
  const top = Math.max(allow || 0, ...hist.map(h => h.b), 1) * 1.12;
  hist.forEach((h, i) => {
    const bh = h.b / top * (y0 - 4);
    const r = svg("rect", {class: i === n - 1 ? "col today" : "col", x: (i * bw + 1.5).toFixed(1), y: (y0 - bh).toFixed(1),
      width: Math.max(bw - 3, 1).toFixed(1), height: Math.max(bh, h.b > 0 ? 1 : 0).toFixed(1), rx: 2});
    const t = svg("title", {}); t.textContent = dstr(h.d) + ": " + sz(h.b); r.appendChild(t); el.appendChild(r);
  });
  if (allow > 0) {
    const y = (y0 - allow / top * (y0 - 4)).toFixed(1);
    el.appendChild(svg("line", {class: "allow", x1: 0, x2: W, y1: y, y2: y}));
  }
  [[0, "start", 0], [Math.floor(n / 2), "middle", W / 2], [n - 1, "end", W]].forEach(([i, a, x]) => {
    const t = svg("text", {class: "axis", x: x, y: H - 3, "text-anchor": a});
    t.textContent = dstr(hist[i].d); el.appendChild(t);
  });
}

function fillForm(s) {
  const c = s.cfg;
  $("#f_plan").value = c.plan_gb; $("#f_day").value = c.reset_day; $("#f_res").value = c.reserve_pct;
  $("#f_ssid").value = c.ssid; $("#f_alerts").value = c.alert_pcts.join(", "); $("#f_burst").value = c.burst_mb_per_min;
  $("#f_bin").checked = c.binary_gb; $("#f_notify").checked = c.notify;
  const sel = $("#f_iface"); sel.replaceChildren();
  ["auto"].concat(s.ifaces).forEach(n => sel.appendChild(mk("option", "", n === "auto" ? "auto (" + (s.iface || "?") + ")" : n)));
  Array.from(sel.options).forEach(o => { o.value = o.textContent.startsWith("auto (") ? "auto" : o.textContent; });
  sel.value = c.iface; filled = true;
}

function render(s) {
  U = {gb: s.unit_gb, mb: s.unit_mb};
  const C = 2 * Math.PI * 52, p = Math.max(0, Math.min(s.pct, 100));
  const arc = $("#arc");
  arc.style.strokeDasharray = (C * p / 100) + " " + C;
  arc.style.stroke = tone(s.pct);
  txt("#pct", Math.round(s.pct) + "%");
  txt("#used", sz(s.used));
  txt("#plan", "of " + gb(s.budget) + " budget (plan " + gb(s.plan) + ")");
  const left = s.budget - s.used;
  txt("#remk", left >= 0 ? "Left in budget" : "Over budget by");
  txt("#rem", sz(Math.abs(left)));
  txt("#remn", s.reserve_pct ? gb(s.plan - s.budget) + " reserve kept on top" : "");
  txt("#days", String(s.days_left));
  txt("#renew", "renews " + dstr(s.cycle_end));
  txt("#allow", gb(s.allowance));

  const chip = $("#chip");
  if (s.err) { chip.className = "chip warn"; chip.textContent = "Error: " + s.err; }
  else if (s.counting === null) { chip.className = "chip"; chip.textContent = "Starting\u2026"; }
  else if (s.counting) { chip.className = "chip ok"; chip.textContent = "Counting \u00b7 " + (s.cfg.ssid || s.iface || "?"); }
  else { chip.className = "chip"; chip.textContent = "Not counted \u00b7 on " + (s.ssid ? "\u201c" + s.ssid + "\u201d" : "another network"); }

  txt("#today", sz(s.today));
  const r = s.allowance > 0 ? s.today / s.allowance : (s.today > 0 ? 2 : 0);
  const tb = $("#tbar"); tb.style.width = Math.min(100, r * 100) + "%"; tb.style.background = tone(r * 100);
  txt("#tnote", s.allowance <= 0 ? "No allowance left this cycle"
    : s.today <= s.allowance ? sz(s.allowance - s.today) + " left of today's " + gb(s.allowance)
    : "Over today's " + gb(s.allowance) + " allowance by " + sz(s.today - s.allowance));

  const tag = $("#ptag"), note = $("#pnote");
  if (s.pct >= 100) { tag.className = "tag bad"; tag.textContent = "Over budget"; note.textContent = "The " + gb(s.budget) + " budget is used up with " + s.days_left + " days to go. Keep to essentials."; }
  else if (s.run_out) { tag.className = "tag bad"; tag.textContent = "Over pace"; note.textContent = "At ~" + gb(s.avg_daily) + "/day you'd run out around " + dstr(s.run_out) + ", " + s.days_early + " days early. Aim for " + gb(s.allowance) + "/day."; }
  else if (s.pace_reliable) { tag.className = "tag ok"; tag.textContent = "On track"; note.textContent = "At ~" + gb(s.avg_daily) + "/day you'll finish the cycle near " + gb(s.projected) + " (" + Math.round(100 * s.projected / s.budget) + "% of budget)."; }
  else { tag.className = "tag"; tag.textContent = "Learning"; note.textContent = "Needs a couple of full days of data before it can predict your pace."; }

  txt("#down", sz(s.down) + "/s"); txt("#up", sz(s.up) + "/s");
  txt("#nc", s.counting === false ? "  \u00b7  not counted" : "");
  spark(s.series_down, s.series_up);
  bars(s.history, s.allowance);

  const ul = $("#alerts"); ul.replaceChildren();
  if (!s.alerts.length) ul.appendChild(mk("li", "sub", "Nothing yet."));
  s.alerts.forEach(a => {
    const li = mk("li"); li.append(mk("span", "t", a.title), mk("small", "", a.msg + " \u00b7 " + new Date(a.t).toLocaleString()));
    ul.appendChild(li);
  });

  const tips = [];
  if (!s.cfg.ssid) tips.push("Tip: open Settings and enter your phone's hotspot name so only that traffic is counted.");
  if (!s.offset) tips.push("Tip: your phone's own traffic isn't visible here. Calibrate with your carrier's figure (Settings) to keep totals exact.");
  const hint = $("#hint"); hint.hidden = !tips.length; hint.textContent = tips[0] || "";
  if (!filled) fillForm(s);
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

async function who() {
  const b = $("#whoBtn"), box = $("#who"); b.disabled = true; b.textContent = "Measuring\u2026";
  try {
    const j = await (await fetch("/api/top")).json();
    if (j.error) throw new Error(j.error);
    box.replaceChildren();
    txt("#whoNote", !j.supported ? "The per-app view only works on Windows."
      : !j.items.length ? "Nothing noticeable is using the network right now."
      : "Estimated from per-app activity over " + j.window + " s \u2014 relative, not exact bytes.");
    j.items.forEach(it => {
      const row = mk("div", "row"), m = mk("div", "meter"), i = mk("i");
      i.style.width = Math.max(3, it.share * 100) + "%"; m.appendChild(i);
      row.append(mk("span", "", it.name + (it.pids > 1 ? " \u00d7" + it.pids : "")), m, mk("span", "sub", "\u2248 " + sz(it.rate) + "/s"));
      box.appendChild(row);
    });
    if (j.blind && j.blind.length) box.appendChild(mk("div", "sub", "Couldn't read: " + j.blind.join(", ") + ". Run DataGuard as administrator to include system services such as Windows Update."));
  } catch (e) { txt("#whoNote", "Couldn't measure: " + e.message); }
  b.disabled = false; b.textContent = "Measure again";
}

$("#whoBtn").onclick = who;

function renderApps(a) {
  const box = $("#apps"), note = $("#appsNote"), head = $("#appsHead");
  if (!a.supported) {
    head.hidden = true; box.replaceChildren();
    note.textContent = "Per-app history only works on Windows.";
    return;
  }
  note.textContent = a.err ? "Tracker problem: " + a.err
    : "Estimated from per-app I/O counters while DataGuard runs \u2014 not exact network bytes.";
  const rows = (a.apps || []).slice(0, 15), days = a.days || [];
  head.hidden = !rows.length;
  box.replaceChildren();
  if (!rows.length) { box.appendChild(mk("div", "sub", "Nothing recorded yet \u2014 totals build up in the background.")); return; }
  const share = a.total_cycle || 1;
  rows.forEach(r => {
    const d = mk("details", "approw"), s = mk("summary");
    const nm = mk("span", "nm");
    nm.append(mk("span", "car", "\u25b8"), document.createTextNode(r.app));
    const m = mk("div", "meter"), i = mk("i");
    i.style.width = Math.max(2, r.cycle / share * 100) + "%"; m.appendChild(i);
    s.append(nm, m, mk("span", "v1", sz(r.today)), mk("span", "v2", sz(r.cycle)));
    const barsBox = mk("div", "days"), top = Math.max(...Object.values(r.days), 1);
    days.forEach(day => {
      const b = r.days[day] || 0, bar = mk("i", b ? "" : "z");
      bar.style.height = (b ? Math.max(4, b / top * 100) : 4) + "%";
      bar.title = dstr(day) + ": " + sz(b);
      barsBox.appendChild(bar);
    });
    const lbl = mk("div", "daylbl");
    lbl.append(mk("span", "", days.length ? dstr(days[0]) : ""), mk("span", "", "last 30 days"),
      mk("span", "", days.length ? dstr(days[days.length - 1]) : ""));
    d.append(s, barsBox, lbl);
    box.appendChild(d);
  });
  if (a.count > rows.length)
    box.appendChild(mk("div", "sub", a.count + " apps recorded this cycle \u2014 showing the top " + rows.length + "."));
}
$("#save").onclick = async () => {
  try {
    await post("/api/config", {
      plan_gb: +$("#f_plan").value, reset_day: +$("#f_day").value, reserve_pct: +$("#f_res").value,
      ssid: $("#f_ssid").value, iface: $("#f_iface").value,
      alert_pcts: $("#f_alerts").value.split(",").map(x => parseInt(x, 10)).filter(x => x > 0),
      burst_mb_per_min: +$("#f_burst").value, binary_gb: $("#f_bin").checked, notify: $("#f_notify").checked});
    filled = false; msg("Saved."); tick();
  } catch (e) { msg(e.message); }
};
$("#useSsid").onclick = async () => {
  try {
    const j = await (await fetch("/api/ssid")).json();
    if (j.ssid) { $("#f_ssid").value = j.ssid; msg("Filled in \u201c" + j.ssid + "\u201d \u2014 press Save."); }
    else msg(j.ssid === "" ? "You're not on Wi-Fi right now." : "Couldn't read the Wi-Fi name on this system.");
  } catch (e) { msg(e.message); }
};
$("#test").onclick = async () => { try { await post("/api/notify-test", {}); msg("Sent \u2014 look for a notification."); } catch (e) { msg(e.message); } };
$("#calBtn").onclick = async () => {
  const v = parseFloat($("#f_cal").value);
  if (!(v >= 0)) return msg("Enter the GB your carrier reports.");
  try { await post("/api/calibrate", {gb: v}); msg("Calibrated."); tick(); } catch (e) { msg(e.message); }
};

async function tick() {
  try { render(await (await fetch("/api/status", {cache: "no-store"})).json()); }
  catch (e) { const c = $("#chip"); c.className = "chip warn"; c.textContent = "Can't reach DataGuard"; console.error(e); }
  try { renderApps(await (await fetch("/api/apps", {cache: "no-store"})).json()); }
  catch (e) { console.error(e); }
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(tick, 2000);   // no polling while the tab is hidden
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); tick(); } });
tick();
</script></body></html>
"""
