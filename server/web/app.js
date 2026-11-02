const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// обработчики onclick="…" запрещены политикой безопасности, поэтому ссылки открывают панели через data-open
document.addEventListener("click", e => { const b = e.target.closest("[data-open]"); if (b) openDrawer(b.dataset.open); });

const fmt = {
  pct: v => v == null ? "—" : (v >= 10 ? v.toFixed(0) : v.toFixed(1)),
  bytes(v, d = 1) {
    if (v == null) return "—";
    const u = ["Б","КБ","МБ","ГБ","ТБ","ПБ"]; let i = 0;
    while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i ? v.toFixed(v >= 100 ? 0 : 1).replace(/\.0$/, "") : Math.round(v)) + " " + u[i];
  },
  rate: v => v == null ? "—" : fmt.bytes(v) + "/с",
  bits(v) {
    if (v == null) return "—";
    v *= 8; const u = ["бит/с","Кбит/с","Мбит/с","Гбит/с"]; let i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return v.toFixed(v >= 100 || i == 0 ? 0 : 1) + " " + u[i];
  },
  dur(s) {
    if (s == null) return "—";
    s = Math.floor(s);
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d} д ${h} ч` : h ? `${h} ч ${m} мин` : m ? `${m} мин` : `${s} с`;
  },
  short(s) {
    s = Math.max(0, Math.floor(s));
    return s >= 86400 ? Math.floor(s / 86400) + " д" : s >= 3600 ? Math.floor(s / 3600) + " ч" : s >= 60 ? Math.floor(s / 60) + " мин" : s + " с";
  },
  ago: s => s < 60 ? "только что" : fmt.dur(s) + " назад",
  time(ts, withDate) {
    const d = new Date(ts * 1000);
    const t = d.toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit"});
    return withDate ? d.toLocaleDateString("ru-RU", {day:"2-digit", month:"2-digit"}) + " " + t : t;
  },
};
const level = (v, w, c) => v >= c ? "crit" : v >= w ? "warn" : "";
const bar = (pct, w = 80, c = 92) => `<div class="bar"><i class="${level(pct, w, c)}" style="width:${Math.min(100, Math.max(1.5, pct))}%"></i></div>`;
const lvColor = lv => css(lv == "crit" ? "--crit" : lv == "warn" ? "--warn" : "--c1");

/* --------------------------------------------------------------- api */
async function getJSON(url) {
  const r = await fetch(url);
  if (r.status == 401) { location.href = "/login"; throw new Error("login"); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json();
}
async function api(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Luxmon": "1"}, body: JSON.stringify(body)});
  if (r.status == 401) { location.href = "/login"; return null; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}

/* --------------------------------------------------------------- sparkline */
// масштаб по данным: при CPU 5% линия не должна прилипать ко дну
function spark(points, color, floor = 10) {
  if (!points || points.length < 2) return `<svg class="spark"></svg>`;
  const W = 120, H = 26, t0 = points[0][0], t1 = points.at(-1)[0];
  const max = Math.max(floor, ...points.map(p => p[1])) * 1.15;
  const xy = points.map(([t, v]) => [(t - t0) / (t1 - t0 || 1) * W, H - 1.5 - v / max * (H - 3)]);
  const d = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
  const id = "g" + Math.random().toString(36).slice(2, 8);
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".25"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d}L${W} ${H}L0 ${H}Z" fill="url(#${id})"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

/* --------------------------------------------------------------- big chart */
// Все графики открытой панели связаны: курсор показывает одно и то же время на всех,
// выделение мышью приближает период, полосы отмечают алерты и обслуживание.
let CHARTS = [], ZOOM = null;
const ANN_COLOR = {crit: "--crit", warn: "--warn", maint: "--info"};

function lineChart(el, series, opt = {}) {
  const W = el.clientWidth || 440, H = 170, L = 52, R = 8, T = 8, B = 20;
  const all = series.flatMap(s => s.points);
  if (!all.length) { el.innerHTML = `<div class="empty" style="height:${H}px;display:grid;place-items:center">нет данных</div>`; return; }
  const t0 = opt.start ?? Math.min(...all.map(p => p[0])), t1 = opt.end ?? (Math.max(...all.map(p => p[0])) || t0 + 1);
  let max = opt.max ?? Math.max(opt.minMax || 0, Math.max(...all.map(p => p[1])) * 1.15);
  if (!max || max <= 0) max = 1;
  const x = t => L + (t - t0) / (t1 - t0 || 1) * (W - L - R);
  const y = v => T + (1 - Math.min(v, max) / max) * (H - T - B);
  const yf = opt.fmt || (v => v.toFixed(0));
  let g = `<g class="grid">`;
  for (let i = 0; i <= 4; i++) g += `<line x1="${L}" x2="${W - R}" y1="${y(max * i / 4)}" y2="${y(max * i / 4)}"/>`;
  g += `</g><g class="axis">`;
  for (let i = 0; i <= 4; i += 2) g += `<text x="${L - 6}" y="${y(max * i / 4) + 3}" text-anchor="end">${yf(max * i / 4)}</text>`;
  const span = t1 - t0;
  for (let i = 0; i <= 4; i++) {
    const t = t0 + span * i / 4;
    const lbl = span > 20 * 86400 ? new Date(t * 1000).toLocaleDateString("ru-RU", {day: "2-digit", month: "2-digit", ...(span > 200 * 86400 ? {year: "2-digit"} : {})}) : fmt.time(t, span > 86400 * 1.5);
    g += `<text x="${x(t)}" y="${H - 4}" text-anchor="${i == 0 ? "start" : i == 4 ? "end" : "middle"}">${lbl}</text>`;
  }
  g += `</g>`;
  let ann = "";
  for (const a of opt.ann || []) {
    if (a.kind == "note") {
      if (a.start < t0 || a.start > t1) continue;
      ann += `<g class="note"><line x1="${x(a.start)}" x2="${x(a.start)}" y1="${T}" y2="${H - B}" stroke="${css("--c2")}" stroke-width="1.5" stroke-dasharray="2 2"/><circle cx="${x(a.start)}" cy="${T + 3}" r="4" fill="${css("--c2")}"/><title>${esc(fmt.time(a.start, true) + " · " + a.msg + (a.by ? " — " + a.by : ""))}</title></g>`;
      continue;
    }
    const a0 = Math.max(a.start, t0), a1 = Math.min(a.end ?? Date.now() / 1000, t1);
    if (a1 <= a0) continue;
    ann += `<rect x="${x(a0)}" y="${T}" width="${Math.max(2, x(a1) - x(a0))}" height="${H - T - B}" fill="${css(ANN_COLOR[a.kind] || "--warn")}" opacity="${a.kind == "maint" ? .1 : .14}"><title>${esc(a.msg)}</title></rect>`;
  }
  let paths = "";
  for (const s of series) {
    if (!s.points.length) continue;
    // разрыв линии, если между точками пропуск больше 3 шагов
    let d = "", prev = null;
    for (const [t, v] of s.points) {
      d += (prev == null || t - prev > (s.step || 60) * 3 ? "M" : "L") + x(t).toFixed(1) + " " + y(v).toFixed(1);
      prev = t;
    }
    if (s.fill && !s.dash) {
      const gid = "f" + Math.random().toString(36).slice(2, 8);
      paths += `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".28"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient></defs><path d="${d}L${x(s.points.at(-1)[0])} ${y(0)}L${x(s.points[0][0])} ${y(0)}Z" fill="url(#${gid})"/>`;
    }
    paths += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.dash ? 1.4 : 1.8}" ${s.dash ? `stroke-dasharray="4 3" opacity=".6"` : ""} stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${ann}${g}${paths}<line class="cur" y1="${T}" y2="${H - B}" stroke="${css("--faint")}" stroke-dasharray="3 3" style="display:none"/><rect class="zoom" y="${T}" height="${H - T - B}" style="display:none"/><rect class="hit" x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent"/></svg>`;
  const svg = el.querySelector("svg");
  const dots = series.filter(s => s.points.length).map(s => {
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("r", 3.5); c.setAttribute("fill", s.color); c.setAttribute("stroke", css("--surface")); c.setAttribute("stroke-width", 1.5); c.style.display = "none"; c.style.pointerEvents = "none";
    svg.insertBefore(c, svg.querySelector(".hit"));
    return {c, s};
  });
  const ch = {svg, series, yf, x, y, t0, t1, dots, cur: svg.querySelector(".cur"), zoom: svg.querySelector(".zoom")};
  CHARTS.push(ch);
  const tAt = e => { const r = svg.getBoundingClientRect(); return t0 + ((e.clientX - r.left) * W / r.width - L) / (W - L - R) * (t1 - t0); };
  let drag = null;
  svg.addEventListener("mousemove", e => {
    const t = tAt(e);
    hoverAll(t, ch, e);
    if (drag != null) {
      const a = Math.max(t0, Math.min(drag, t)), b = Math.min(t1, Math.max(drag, t));
      ch.zoom.setAttribute("x", x(a)); ch.zoom.setAttribute("width", Math.max(0, x(b) - x(a))); ch.zoom.style.display = "";
    }
  });
  // на телефоне значение показывается по касанию, листание страницы не мешает
  let touchEnd;
  const touch = e => { clearTimeout(touchEnd); const p = {clientX: e.touches[0].clientX, clientY: e.touches[0].clientY}; hoverAll(tAt(p), ch, p); };
  svg.addEventListener("touchstart", touch, {passive: true});
  svg.addEventListener("touchmove", touch, {passive: true});
  svg.addEventListener("touchend", () => { touchEnd = setTimeout(() => hoverAll(null), 1800); });
  svg.addEventListener("mousedown", e => { drag = tAt(e); e.preventDefault(); });
  svg.addEventListener("mouseup", e => {
    if (drag == null) return;
    const a = Math.max(t0, Math.min(drag, tAt(e))), b = Math.min(t1, Math.max(drag, tAt(e)));
    drag = null; ch.zoom.style.display = "none";
    if (x(b) - x(a) > 8 && b - a > 120) { ZOOM = [Math.floor(a), Math.ceil(b)]; setHash(); drawOpen(); }
  });
  svg.addEventListener("mouseleave", () => { drag = null; ch.zoom.style.display = "none"; hoverAll(null); });
}

function hoverAll(t, src, e) {
  const tip = $("#tip");
  const near = (pts, at) => pts.reduce((b, p) => Math.abs(p[0] - at) < Math.abs(b[0] - at) ? p : b, pts[0]);
  if (t == null) { CHARTS.forEach(c => { c.cur.style.display = "none"; c.dots.forEach(x => x.c.style.display = "none"); }); tip.style.display = "none"; return; }
  for (const c of CHARTS) {
    if (!c.svg.isConnected || t < c.t0 || t > c.t1) { c.cur.style.display = "none"; c.dots.forEach(x => x.c.style.display = "none"); continue; }
    c.cur.setAttribute("x1", c.x(t)); c.cur.setAttribute("x2", c.x(t)); c.cur.style.display = "";
    c.dots.forEach(({c: dot, s}) => { const p = near(s.points, t); dot.setAttribute("cx", c.x(p[0])); dot.setAttribute("cy", c.y(p[1])); dot.style.display = ""; });
  }
  let rows = "";
  for (const s of src.series) {
    if (!s.points.length) continue;
    let best = s.points[0];
    for (const p of s.points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    rows += `<div><span><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:6px"></i>${esc(s.name)}</span><b class="num">${src.yf(best[1])}</b></div>`;
  }
  tip.innerHTML = `<div class="t">${fmt.time(t, true)}</div>${rows}`;
  tip.style.display = "block";
  tip.style.left = Math.max(8, Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8)) + "px";
  tip.style.top = Math.max(8, Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8)) + "px";
}

/* --------------------------------------------------------------- render */
let DATA = null, OPEN = null, RANGE = 3600, GFILTER = "all";

const diskMax = h => Math.max(0, ...(h.disks || []).map(d => 100 * d.used / d.size));
function hostState(h) {
  if (h.age > 90) return "off";
  const m = h.metrics || {};
  return level(Math.max(m.cpu || 0, m.mem || 0, diskMax(h)), 85, 95) || "ok";
}
const ic = n => `<svg class="ic"><use href="#i-${n}"/></svg>`;
const FLAME = `<svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true"><path d="M17 2c1 6 8 9 8 17a9 9 0 0 1-18 0c0-4 2-7 4-9 .3 2.300 1 3.700 2.300 4.500C13 10 14 6 17 2z"/></svg>`;
const plural = (n, a, b, c) => n % 10 == 1 && n % 100 != 11 ? a : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? b : c;
const panelHead = (title, extra = "", meta = "") => `<div class="ph"><h3>${title}</h3>${extra}${meta ? `<span class="meta">${meta}</span>` : ""}</div>`;
const scrollTo = id => document.getElementById(id)?.scrollIntoView({behavior: "smooth", block: "start"});
const fcText = f => f.days != null ? `заполнится через ~${Math.round(f.days)} дн` : f.per_day <= 0.01 ? "рост не заметен" : "заполнится нескоро";

const KPI_PREV = {};
function renderKpis(d) {
  const c = d.cluster, pv = c.pve, ceph = c.ceph, pg = c.patroni, bk = c.backups;
  const checks = Object.entries(c).filter(([k]) => k.startsWith("checks:")).flatMap(([, v]) => v.items || []);
  const hostsUp = d.hosts.filter(h => h.age <= 90).length;
  const guests = (pv?.guests || []).filter(g => !g.template);
  const running = guests.filter(g => g.status == "running").length;
  const leader = pg?.members?.find(m => m.role == "leader");
  const maxLag = Math.max(0, ...(pg?.members || []).map(m => typeof m.lag == "number" ? m.lag : 0));
  const pgOk = pg && !pg.error && leader && pg.members.every(m => ["running", "streaming"].includes(m.state));
  const chkBad = checks.filter(x => !x.ok).length;
  const now = d.now, bkAges = (bk?.guests || []).map(g => now - g.last);
  const bkStale = (bk?.guests || []).filter(g => g.stale).length;
  const bkLast = bk?.guests?.length ? Math.min(...bkAges) : null;
  const cephUsed = ceph ? 100 * ceph.bytes.used / ceph.bytes.total : 0;
  const tiles = [
    ["hosts-p", "Хосты", hostsUp == d.hosts.length ? "" : "crit", `${hostsUp}<small>/${d.hosts.length}</small>`, "в сети"],
    ["map-p", "VM и контейнеры", "", `${running}<small>/${guests.length}</small>`, "запущено"],
    ["ceph-p", "Ceph", !ceph ? "off" : ceph.health == "HEALTH_OK" ? "" : ceph.health == "HEALTH_ERR" ? "crit" : "warn",
      ceph ? esc(ceph.health.replace("HEALTH_", "")) : "—", ceph ? `занято ${cephUsed.toFixed(0)}% · ${ceph.osd.up}/${ceph.osd.total} OSD` : "нет данных"],
    ["patroni-p", "PostgreSQL", !pg ? "off" : pgOk ? "" : "crit", leader ? esc(leader.name) : "нет лидера", leader ? `лидер · лаг ${fmt.bytes(maxLag)}` : esc(pg?.error || "")],
    ["checks-p", "Сервисы", !checks.length ? "off" : chkBad ? "crit" : "", checks.length ? `${checks.length - chkBad}<small>/${checks.length}</small>` : "—", chkBad ? `${chkBad} не отвечает` : "отвечают"],
    ["backups-p", "Бэкапы", !bk ? "off" : bkStale ? "warn" : "", bkLast != null ? fmt.short(bkLast) + " <small>назад</small>" : "—", bk ? (bkStale ? `${bkStale} устарели` : `все ${bkAges.length} свежие`) : "нет данных"],
  ];
  $("#kpis").innerHTML = tiles.map(([id, l, lv, v, s]) => `<div class="kpi ${lv} ${KPI_PREV[id] != null && KPI_PREV[id] != v ? "chg" : ""}" data-to="${id}">
      <div class="l"><span class="dot ${lv}"></span>${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");
  tiles.forEach(([id, , , v]) => KPI_PREV[id] = v);
  document.querySelectorAll(".kpi").forEach(k => k.onclick = () => scrollTo(k.dataset.to));
}

function updText(u) {
  if (!u) return "";
  const tip = u.count ? `Ждут обновления: ${u.packages.join(", ")}${u.count > u.packages.length ? " …" : ""}` : "";
  return (u.count ? ` · <span class="upd" title="${esc(tip)}">${ic("update")} ${u.count}${u.security ? ` (${u.security} безоп.)` : ""}</span>` : "")
    + (u.reboot ? ` · <span class="upd boot" title="${esc(u.reason)}">нужна перезагрузка</span>` : "");
}

function renderHosts(d) {
  $("#hosts-meta").textContent = "нажми на строку, чтобы открыть графики";
  if (!d.hosts.length) { $("#hosts").innerHTML = `<div class="empty">Агенты ещё не прислали данные</div>`; return; }
  const fc = Object.fromEntries((d.forecast || []).filter(f => f.host[0] != "_").map(f => [f.host + ":" + f.target, f]));
  const head = `<div class="hhead"><span>Хост</span><span>CPU · 1 ч</span><span>Память · 1 ч</span><span>Диск</span><span>Сеть</span><span>Load</span></div>`;
  const row = h => {
    const m = h.metrics || {}, st = hostState(h), i = h.info || {};
    const dm = diskMax(h), worst = (h.disks || []).reduce((a, x) => !a || x.used / x.size > a.used / a.size ? x : a, null);
    const cpuLv = level(m.cpu || 0, 85, 95), memLv = level(m.mem || 0, 85, 95);
    const silenced = d.silences.some(x => x.scope == "all" || x.scope == "host:" + h.name);
    const tip = (h.disks || []).map(x => {
      const f = fc[h.name + ":" + x.mount];
      return `${x.mount}: ${(100 * x.used / x.size).toFixed(0)}% из ${fmt.bytes(x.size)}${f ? " · " + fcText(f) : ""}`;
    }).join("\n");
    return `<div class="hrow ${st == "off" ? "down" : ""}" data-host="${esc(h.name)}">
      <div class="hname"><span class="dot ${st == "ok" ? "" : st}"></span><div style="min-width:0">
        <b>${esc(h.name)}</b>${silenced ? " " + ic("mute") : ""}
        <div class="sub">${st == "off" ? "нет связи " + fmt.ago(h.age) : `${i.cpus ?? "?"} vCPU · ${fmt.bytes(m.mem_total)} · up ${fmt.short(i.uptime || 0)}`}${updText(d.cluster["updates:" + h.name])}</div></div></div>
      <div><div class="hl">CPU</div><div class="mc"><span class="v">${fmt.pct(m.cpu)}<small>%</small></span>${spark(h.spark.cpu, lvColor(cpuLv))}</div></div>
      <div><div class="hl">Память</div><div class="mc"><span class="v">${fmt.pct(m.mem)}<small>%</small></span>${spark(h.spark.mem, memLv ? lvColor(memLv) : css("--c2"), 20)}</div></div>
      <div class="dk" title="${esc(tip)}">
        <div class="hl">Диск</div><div class="v"><span>${(h.disks || []).length > 1 ? "макс." : esc(worst?.mount || "")}</span><span class="num">${dm.toFixed(0)}%</span></div>${bar(dm, 85, 95)}</div>
      <div class="two" ${m.net_util != null ? `title="канал ${m.link_mbit} Мбит/с, занято ${m.net_util.toFixed(0)}%"` : ""}><div class="hl">Сеть</div>↓ <b>${fmt.bits(m.net_rx)}</b><br>↑ <b>${fmt.bits(m.net_tx)}</b>${m.net_util != null ? bar(m.net_util, 70, 90) : ""}</div>
      <div class="two"><div class="hl">Load</div><b>${m.load1 != null ? m.load1.toFixed(2) : "—"}</b>${m.temp != null ? `<br>${m.temp.toFixed(0)} °C` : m.iowait != null ? `<br>io ${m.iowait.toFixed(1)}%` : ""}</div>
    </div>`;
  };
  // проблемные всегда сверху, дальше физические узлы и гости
  const nodes = new Set((d.cluster.pve?.nodes || []).map(n => n.name));
  const bad = h => hostState(h) != "ok", weight = {off: 0, crit: 1, warn: 2};
  const byName = (a, b) => a.name.localeCompare(b.name);
  const group = (title, list, cls = "") => list.length ? `<div class="hgroup ${cls}">${title}</div>` + list.map(row).join("") : "";
  $("#hosts").innerHTML = head
    + group("Требуют внимания", d.hosts.filter(bad).sort((a, b) => weight[hostState(a)] - weight[hostState(b)]), "warn")
    + group("Узлы", d.hosts.filter(h => !bad(h) && nodes.has(h.name)).sort(byName))
    + group("Гости", d.hosts.filter(h => !bad(h) && !nodes.has(h.name)).sort(byName));
  document.querySelectorAll(".hrow").forEach(el => el.onclick = () => openDrawer(el.dataset.host));
}

/* Кольцо стартует с прошлого значения и плавно доходит до нового */
const RING_LEN = 2 * Math.PI * 22, RINGS = {};
const ring = (id, v, label, w, c) => `<div class="ring ${level(v, w, c)}" title="${label} ${v.toFixed(0)}%"><svg viewBox="0 0 56 56"><circle class="bg" cx="28" cy="28" r="22"/><circle class="fg" data-ring="${id}" data-v="${v}" cx="28" cy="28" r="22" stroke-dasharray="${RING_LEN * (RINGS[id] ?? 0) / 100} ${RING_LEN}"/><text x="28" y="28">${v.toFixed(0)}</text></svg>${label}</div>`;
function growRings(el) {
  const fg = [...el.querySelectorAll(".fg")];
  el.getBoundingClientRect();
  requestAnimationFrame(() => fg.forEach(c => {
    c.setAttribute("stroke-dasharray", `${RING_LEN * Math.min(100, +c.dataset.v) / 100} ${RING_LEN}`);
    RINGS[c.dataset.ring] = +c.dataset.v;
  }));
}
const GUEST_NODE = {}, MOVED = {};
function trackMoves(guests, now) {
  guests.forEach(g => {
    if (GUEST_NODE[g.vmid] && GUEST_NODE[g.vmid] != g.node) MOVED[g.vmid] = {from: GUEST_NODE[g.vmid], ts: now};
    GUEST_NODE[g.vmid] = g.node;
    if (MOVED[g.vmid] && now - MOVED[g.vmid].ts > 600) delete MOVED[g.vmid];
  });
}
/* Роль PostgreSQL на плитке гостя: адрес участника Patroni ищем среди адресов агентов */
function pgRoles(d) {
  const out = {};
  (d.cluster.patroni?.members || []).forEach(m => {
    const h = d.hosts.find(x => (x.info?.ips || []).includes(m.host));
    if (h) out[h.name] = m.role == "leader" ? "leader" : "replica";
  });
  return out;
}
function renderMap(pv) {
  const el = $("#map-p");
  if (!pv) { el.innerHTML = panelHead("Схема кластера") + `<div class="empty">нет данных Proxmox</div>`; return; }
  const nodes = [...pv.nodes].sort((a, b) => a.name.localeCompare(b.name));
  const roles = pgRoles(DATA), c = DATA.cluster, ceph = c.ceph, pg = c.patroni;
  trackMoves(pv.guests, DATA.now);
  const leader = pg?.members?.find(m => m.role == "leader");
  const pgLag = Math.max(0, ...(pg?.members || []).map(m => typeof m.lag == "number" ? m.lag : 0));
  const pgOk = pg && !pg.error && leader && pg.members.every(m => ["running", "streaming"].includes(m.state));
  const cephLv = !ceph ? "off" : ceph.health == "HEALTH_OK" ? "" : ceph.health == "HEALTH_ERR" ? "crit" : "warn";
  const buses = `<div class="buses">
    <div class="bus ${cephLv}" data-to="ceph-p"><span class="dot ${cephLv}"></span><b>Ceph</b><span>${ceph ? `${esc(ceph.health.replace("HEALTH_", ""))} · ${ceph.osd.up}/${ceph.osd.total} OSD · занято ${(100 * ceph.bytes.used / ceph.bytes.total).toFixed(0)}%` : "нет данных"}</span></div>
    <div class="bus ${!pg ? "off" : pgOk ? "" : "crit"}" data-to="patroni-p"><span class="dot ${!pg ? "off" : pgOk ? "" : "crit"}"></span><b>PostgreSQL</b><span>${leader ? `лидер ${esc(leader.name)} · лаг ${fmt.bytes(pgLag)}` : "нет лидера"}</span></div>
  </div>`;
  el.innerHTML = panelHead("Схема кластера", "", "цвет полосы — нагрузка CPU гостя") + `<div class="map">${nodes.map(n => {
    const gs = pv.guests.filter(g => g.node == n.name && !g.template);
    const up = n.status == "online", cpu = 100 * (n.cpu || 0), mem = n.maxmem ? 100 * n.mem / n.maxmem : 0;
    const t = DATA.hosts.find(h => h.name == n.name)?.metrics?.temp;
    return `<div class="node ${up ? "" : "down"}"><div class="node-h"><span class="dot ${up ? "live" : "crit"}"></span><b>${esc(n.name)}</b>${t != null ? `<span class="num" style="font-size:12px;margin-left:4px;white-space:nowrap;color:${t >= 85 ? "var(--crit)" : t >= 70 ? "var(--warn)" : "var(--muted)"}" title="температура CPU">${ic("temp")}${t.toFixed(0)} °C</span>` : ""}<span class="faint num" style="margin-left:auto;font-size:11.5px;white-space:nowrap" title="гостей запущено">${gs.filter(g => g.status == "running").length} из ${gs.length}</span></div>
      ${up ? `<div class="node-rings">${ring(n.name + ":cpu", cpu, "CPU", 80, 95)}${ring(n.name + ":mem", mem, "RAM", 85, 95)}</div>` : `<div class="empty" style="padding:14px 0;color:var(--crit)">узел недоступен</div>`}
      <div class="gtiles">${gs.map(g => {
        const run = g.status == "running", cp = 100 * (g.cpu || 0), mv = MOVED[g.vmid], role = roles[g.name];
        return `<div class="gt ${run ? level(cp, 70, 90) : "off"} ${mv ? "moved" : ""}" data-vm="${esc(g.vmid)}" title="${esc(`${esc(g.vmid)} ${g.name} · ${g.status}${run ? ` · CPU ${cp.toFixed(0)}% · RAM ${fmt.bytes(g.mem)} / ${fmt.bytes(g.maxmem)}` : ""}${mv ? ` · переехал с ${mv.from}` : ""}`)}">
          <b>${esc(g.name)}</b><span>${mv ? `↪ с ${esc(mv.from)}` : `${role ? `<i class="role ${role}">${role == "leader" ? "лидер" : "реплика"}</i>` : `${esc(g.vmid)} · ${g.type == "lxc" ? "CT" : "VM"}`}${run ? ` · ${cp.toFixed(0)}%` : ""}`}</span></div>`;
      }).join("") || `<span class="faint" style="font-size:12px">пусто</span>`}</div></div>`;
  }).join("")}</div>${buses}`;
  el.querySelectorAll(".gt").forEach(t => t.onclick = () => openDrawer("@vm:" + t.dataset.vm));
  el.querySelectorAll(".bus").forEach(b => b.onclick = () => scrollTo(b.dataset.to));
  growRings(el);
}

function renderGuests(pv) {
  const el = $("#guests-p");
  if (!pv) { el.innerHTML = panelHead("Виртуальные машины и контейнеры") + `<div class="empty">нет данных Proxmox</div>`; return; }
  const nodes = pv.nodes.map(n => n.name).sort();
  const opts = [["all", "все"], ...nodes.map(n => [n, n]), ["stopped", "остановленные"]];
  let guests = pv.guests.filter(g => !g.template);
  if (GFILTER == "stopped") guests = guests.filter(g => g.status != "running");
  else if (GFILTER != "all") guests = guests.filter(g => g.node == GFILTER);
  const rows = guests.map(g => {
    const run = g.status == "running", mem = g.maxmem ? 100 * g.mem / g.maxmem : 0, cpu = 100 * (g.cpu || 0);
    return `<tr class="click ${run ? "" : "off"}" data-vm="${esc(g.vmid)}">
      <td class="faint">${esc(g.vmid)}</td>
      <td><b style="font-weight:600">${esc(g.name)}</b> <span class="tag">${g.type == "lxc" ? "CT" : "VM"}</span></td>
      <td class="muted">${esc(g.node)}</td>
      <td><span class="dot ${run ? "" : "off"}" style="width:7px;height:7px;margin-right:6px"></span>${run ? "работает" : esc(g.status)}</td>
      <td>${run ? `<div class="minibar">${bar(cpu, 80, 95)}<span class="muted">${cpu.toFixed(0)}%</span></div>` : ""}</td>
      <td>${run ? `<div class="minibar">${bar(mem, 85, 95)}<span class="muted">${fmt.bytes(g.mem)} / ${fmt.bytes(g.maxmem)}</span></div>` : ""}</td>
      <td class="muted">${run && g.net_rx != null ? `↓ ${fmt.bits(g.net_rx)} ↑ ${fmt.bits(g.net_tx)}` : ""}</td>
      <td class="r muted">${run ? fmt.dur(g.uptime) : ""}</td></tr>`;
  }).join("");
  const offline = pv.nodes.filter(n => n.status != "online").map(n => `<span class="badge crit">${esc(n.name)} offline</span>`).join("");
  el.innerHTML = panelHead("Виртуальные машины и контейнеры", offline +
      `<div class="filters">${opts.map(([v, l]) => `<button data-f="${esc(v)}" class="${v == GFILTER ? "on" : ""}">${l}</button>`).join("")}</div>`) +
    `<div class="tbl"><table><tr><th>ID</th><th>Имя</th><th>Узел</th><th>Статус</th><th>CPU</th><th>Память</th><th>Сеть</th><th class="r">Аптайм</th></tr>
      ${rows || `<tr><td colspan="8" class="faint">ничего не найдено</td></tr>`}</table></div>`;
  el.querySelectorAll(".filters button").forEach(b => b.onclick = () => { GFILTER = b.dataset.f; renderGuests(DATA.cluster.pve); });
  el.querySelectorAll("tr[data-vm]").forEach(r => r.onclick = () => openDrawer("@vm:" + r.dataset.vm));
}

function renderChecks(cl) {
  const el = $("#checks-p");
  const all = Object.entries(cl).filter(([k]) => k.startsWith("checks:"))
    .flatMap(([k, v]) => (v.items || []).map(c => ({...c, src: k.slice(7), age: v._age})));
  if (!all.length) { el.innerHTML = panelHead("Сервисы") + `<div class="empty">Проверки не настроены: добавь их в настройках</div>`; return; }
  all.sort((a, b) => a.ok - b.ok || a.name.localeCompare(b.name));
  const bad = all.filter(c => !c.ok).length;
  el.innerHTML = panelHead("Сервисы", `<span class="badge ${bad ? "crit" : ""}">${bad ? bad + " не отвечает" : "все отвечают"}</span><button class="link" data-open="@checks">время ответа →</button>`,
      `с ${esc([...new Set(all.map(c => c.src))].join(", "))} · ${fmt.ago(all[0].age)}`) +
    `<div class="pb"><div class="checks">${all.map(c => {
      const cd = c.cert_days, certLv = cd == null ? "" : cd < 3 ? "crit" : cd < 30 ? "warn" : "";
      const certTip = cd == null ? "" : ` · сертификат ещё ${cd > 3650 ? "больше 10 лет" : Math.floor(cd) + " дн"}`;
      return `<div class="chk ${c.ok ? "" : "bad"}" title="${esc(c.type.toUpperCase() + " · " + c.target + (c.code ? " · " + c.code : "") + certTip)}">
      <span class="dot ${c.ok ? "" : "crit"}"></span><span class="n">${esc(c.name)}</span>
      <span class="ms num">${certLv ? `<span style="color:var(--${certLv})">🔒 ${Math.max(0, Math.floor(cd))} дн</span> · ` : ""}${c.ok ? (c.ms != null ? (c.ms < 10 ? c.ms.toFixed(1) : Math.round(c.ms)) + " мс" : "ok") : esc(c.err || "ошибка")}</span></div>`;
    }).join("")}</div></div>`;
}

function renderCeph(c, forecast) {
  const el = $("#ceph-p");
  if (!c) { el.innerHTML = panelHead("Ceph") + `<div class="empty">нет данных</div>`; return; }
  const hl = c.health == "HEALTH_OK" ? "" : c.health == "HEALTH_ERR" ? "crit" : "warn";
  const used = c.bytes.used / c.bytes.total * 100;
  const lat = c.osd_lat ? Object.entries(c.osd_lat).sort((a, b) => b[1] - a[1])[0] : null;
  const pgsBad = c.pgs.states.filter(s => s.state_name != "active+clean");
  const fc = (forecast || []).find(f => f.host == "_ceph");
  el.innerHTML = panelHead("Ceph", `<span class="badge ${hl}">${esc(c.health)}</span><button class="link" data-open="@ceph">графики →</button>`, fmt.ago(c._age)) +
    `<div class="pb">
      ${c.checks.map(x => `<div class="banner ${x.severity == "HEALTH_ERR" ? "crit" : "warn"}" style="margin-bottom:10px;font-size:12.5px">${esc(x.msg)}</div>`).join("")}
      <div class="stats">
        <div class="stat"><div class="l">OSD</div><div class="v num">${c.osd.up}/${c.osd.total} <span class="faint" style="font-size:12px;font-weight:500">up</span></div><div class="s">${lat ? `макс. задержка ${lat[1]} мс` : `${c.osd.in} in`}</div></div>
        <div class="stat"><div class="l">PG</div><div class="v num">${c.pgs.total}</div><div class="s">${pgsBad.length ? esc(pgsBad.map(s => s.count + " " + s.state_name).join(", ")) : "все active+clean"}</div></div>
        <div class="stat"><div class="l">Чтение</div><div class="v num">${fmt.rate(c.io.rd)}</div><div class="s">${c.io.rd_ops} оп/с</div></div>
        <div class="stat"><div class="l">Запись</div><div class="v num">${fmt.rate(c.io.wr)}</div><div class="s">${c.io.wr_ops} оп/с</div></div>
      </div>
      <div class="pool" style="font-weight:600"><span>Всего</span>${bar(used, 75, 85)}<span class="r num">${used.toFixed(0)}% · ${fmt.bytes(c.bytes.total)}</span></div>
      ${c.pools.map(p => `<div class="pool"><span class="muted">${esc(p.name)}</span>${bar(p.pct, 75, 85)}<span class="r num">${fmt.bytes(p.stored)}</span></div>`).join("")}
      <div class="muted" style="font-size:12px;margin-top:8px">${fc ? `Прогноз: ${fcText(fc)} <span class="faint">(+${fc.per_day.toFixed(2)}% в сутки, по данным за ${fc.span_h} ч)</span>` : `<span class="faint">Прогноз заполнения появится, когда накопится 12 ч истории</span>`}</div>
    </div>`;
}

function renderPatroni(p, cl) {
  const el = $("#patroni-p");
  if (!p) { el.innerHTML = panelHead("PostgreSQL") + `<div class="empty">нет данных</div>`; return; }
  const ok = !p.error && p.members?.some(m => m.role == "leader") && p.members.every(m => ["running", "streaming"].includes(m.state));
  const members = (p.members || []).map(m => {
    const good = ["running", "streaming"].includes(m.state);
    return `<div class="li"><span class="dot ${good ? "" : "crit"}"></span><b style="font-weight:600">${esc(m.name)}</b>
      ${m.role == "leader" ? `<span class="badge">лидер</span>` : `<span class="badge mute">${esc(m.role)}</span>`}
      <span class="faint" style="font-size:12px">${esc(m.host)}</span>
      <span class="end">${good ? "" : `<span class="badge crit">${esc(m.state)}</span> `}${m.role == "leader" ? "TL " + (m.timeline ?? "—") : "лаг " + (m.lag == null ? "—" : fmt.bytes(m.lag))}</span></div>`;
  }).join("");
  const dbs = Object.entries(cl).filter(([k]) => k.startsWith("postgres:")).sort().map(([k, s]) => {
    const h = k.slice(9);
    if (s.error) return `<div class="li"><span class="dot warn"></span>${esc(h)}<span class="end" style="color:var(--warn)">${esc(s.error)}</span></div>`;
    const cp = s.max_conn ? 100 * s.conn_total / s.max_conn : 0;
    return `<div class="li"><span class="dot ${level(cp, 70, 90)}"></span><span>${esc(h)} <span class="faint" style="font-size:12px">${s.in_recovery ? "реплика" : "основная"}</span></span>
      <span class="end num">${s.conn_total}/${s.max_conn} подкл.${s.in_recovery ? "" : ` · ${s.tps != null ? s.tps.toFixed(1) : "—"} TPS`} · кэш ${s.cache_hit != null ? s.cache_hit.toFixed(1) + "%" : "—"} · ${fmt.bytes(s.db_size)}</span></div>`;
  }).join("");
  const etcd = Object.entries(p.etcd || {}).map(([ep, s]) => `<div class="li"><span class="dot ${s.ok ? "" : "crit"}"></span><span>${esc(ep.split(":")[0])}</span>
      <span class="end num">${s.ok ? `ответ ${s.ms} мс · fsync ${s.fsync_ms != null ? `<b style="color:${s.fsync_ms > 100 ? "var(--warn)" : "var(--text)"};font-weight:500">${s.fsync_ms.toFixed(1)} мс</b>` : "—"}` : `<span style="color:var(--crit)">${esc(s.err || "fail")}</span>`}</span></div>`).join("");
  el.innerHTML = panelHead("PostgreSQL", `<span class="badge ${ok ? "" : "crit"}">${ok ? "OK" : "проблема"}</span><button class="link" data-open="@patroni">графики →</button>`, esc(p.scope || "")) +
    (p.error ? `<div class="banner crit" style="margin:12px 16px 0">${esc(p.error)}</div>` : "") +
    `<div class="list" style="padding:4px 0">${members}</div>` +
    (dbs ? `<div class="sub-h">Базы</div><div class="list">${dbs}</div>` : "") +
    `<div class="sub-h">etcd</div><div class="list" style="padding-bottom:4px">${etcd}</div>`;
}

function renderBackups(b, pv) {
  const el = $("#backups-p");
  if (!b) { el.innerHTML = panelHead("Бэкапы") + `<div class="empty">нет данных</div>`; return; }
  const now = Date.now() / 1000;
  const names = Object.fromEntries((pv?.guests || []).map(g => [g.vmid, g.name]));
  const rows = [...b.guests].sort((a, c) => a.last - c.last).map(g => {
    const age = now - g.last, lv = g.stale ? "crit" : g.work_age > 26 * 3600 ? "warn" : "";
    return `<div class="li"><span class="faint num" style="width:28px">${esc(g.vmid)}</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(names[g.vmid] || g.name)}</span>
      <span class="end num"><span style="color:${lv ? `var(--${lv})` : "var(--text)"}">${fmt.short(age)} назад</span> · ${fmt.bytes(g.size)}${g.verified == "ok" ? " · ✓" : g.verified ? ` · <span style="color:var(--crit)">${esc(g.verified)}</span>` : ""}</span></div>`;
  }).join("");
  const tasks = b.tasks.slice(0, 4).map(t => `<div class="ev"><time class="num">${fmt.time(t.starttime, true).split(" ")[0]}</time>
      <span class="dot ${t.status == "OK" ? "" : t.status ? "crit" : "warn"}"></span>
      <span>${esc(t.node)} · ${t.status == "OK" ? "успешно" : t.status ? `<span style="color:var(--crit)">${esc(t.status)}</span>` : "идёт…"} <span class="faint">${t.endtime ? "· " + fmt.dur(t.endtime - t.starttime) : ""}</span></span></div>`).join("");
  const pbs = (DATA.cluster.pbs?.stores || []).map(st => {
    const pct = 100 * st.used / st.size, gc = st.gc || {};
    const job = (label, t, sched) => `<div class="li"><span class="dot ${!t ? "off" : t.status == "OK" ? "" : "crit"}"></span><span>${label}</span>
      <span class="end num muted">${t ? `${t.status == "OK" ? "успешно" : `<span style="color:var(--crit)">${esc(t.status)}</span>`} · ${fmt.ago(now - t.end)}` : "не было"}${sched ? ` · <span class="faint">${esc(sched)}</span>` : ""}</span></div>`;
    return `<div class="sub-h">PBS · ${esc(st.name)}</div>
      <div class="pb" style="padding-top:4px;padding-bottom:8px"><div class="dk"><div class="v"><span>занято ${fmt.bytes(st.used)} из ${fmt.bytes(st.size)}</span><span class="num">${pct.toFixed(0)}%</span></div>${bar(pct, 85, 95)}</div>
      ${gc.index_bytes && gc.disk_bytes ? `<div class="faint" style="font-size:11.5px;margin-top:6px">дедупликация ×${(gc.index_bytes / gc.disk_bytes).toFixed(1)}: ${fmt.bytes(gc.index_bytes)} копий занимают ${fmt.bytes(gc.disk_bytes)}</div>` : ""}</div>
      <div class="list">${job("Очистка (GC)", gc.last ? {status: gc.state, end: gc.last} : null, gc.schedule)}${job("Прореживание", st.prune)}${job("Проверка (verify)", st.verify, st.verify_schedule)}</div>`;
  }).join("");
  el.innerHTML = panelHead("Бэкапы", `<span class="badge mute">${esc(b.storage)}</span>`, fmt.ago(b._age)) +
    `<div class="list" style="padding:4px 0">${rows || `<div class="empty">нет копий</div>`}</div>
     <div class="sub-h">Задания vzdump</div><div style="padding-bottom:6px">${tasks || `<div class="empty">нет</div>`}</div>${pbs}`;
}

const SMART_LABELS = {realloc: "переназначено", pending: "ждут переназн.", uncorrectable: "нечитаемых", reported_uncorrect: "неисправл.", crc: "CRC"};
function smartRows(list, withHost) {
  return list.map(d => {
    if (d.error) return `<tr>${withHost ? `<td>${esc(d.host)}</td>` : ""}<td>${esc(d.dev)}</td><td colspan="6" class="faint">${esc(d.error)}</td></tr>`;
    const bad = d.passed === false || d.pending || d.uncorrectable, wear = d.wear;
    const errs = Object.entries(SMART_LABELS).filter(([k]) => d[k]).map(([k, l]) => `${l} ${d[k]}`).join(", ");
    return `<tr>${withHost ? `<td><b style="font-weight:600">${esc(d.host)}</b></td>` : ""}<td class="muted">${esc(d.dev)}</td>
      <td title="${esc("серийный " + (d.serial || "?"))}">${esc(d.model)} <span class="tag">${d.ssd ? "SSD" : "HDD"}</span> <span class="faint">${d.size ? fmt.bytes(d.size, 0) : ""}</span></td>
      <td class="num" style="color:${d.temp > 55 ? "var(--warn)" : ""}">${d.temp != null ? d.temp + " °C" : "—"}</td>
      <td class="num muted">${d.hours != null ? (d.hours / 24 / 365).toFixed(1) + " г" : "—"}</td>
      <td>${wear != null ? `<span class="wear">${bar(wear, 70, 90)}<span class="num muted">${wear}%</span></span>` : `<span class="faint" title="Износ от записи бывает только у SSD">${d.ssd ? "не сообщает" : "не бывает у HDD"}</span>`}</td>
      <td class="muted" style="white-space:normal" title="${esc(Object.entries(SMART_LABELS).filter(([k]) => d[k] != null).map(([k, l]) => `${l}: ${d[k]}`).join("\n"))}">${errs ? `<span style="color:${d.pending || d.uncorrectable || d.realloc ? "var(--warn)" : "var(--muted)"}">${esc(errs)}</span>` : `<span style="color:var(--ok)">все по нулям</span>`}</td>
      <td class="r"><span class="badge ${d.passed === false ? "crit" : bad ? "warn" : ""}">${d.passed === false ? "отказ" : bad ? "внимание" : "исправен"}</span></td></tr>`;
  }).join("");
}
const smartHead = withHost => `<tr>${withHost ? "<th>Узел</th>" : ""}<th>Диск</th><th>Модель</th><th>Темп.</th><th>Наработка</th><th>Износ</th><th>Счётчики ошибок</th><th class="r">SMART</th></tr>`;

function renderDisks(cl) {
  const el = $("#disks-p");
  const src = Object.entries(cl).filter(([k]) => k.startsWith("smart:"));
  if (!src.length) { el.innerHTML = panelHead("Диски узлов") + `<div class="empty">нет данных SMART (модуль smart агента)</div>`; return; }
  const all = src.sort(([a], [b]) => a.localeCompare(b)).flatMap(([k, v]) => (v.disks || []).map(d => ({...d, host: k.slice(6)})));
  const warn = all.filter(d => d.passed === false || d.pending || d.uncorrectable).length;
  el.innerHTML = panelHead("Диски узлов", `<span class="badge ${warn ? "warn" : ""}">${warn ? warn + " требуют внимания" : `все ${all.length} исправны`}</span>`,
      `SMART · ${fmt.ago(Math.min(...src.map(([, v]) => v._age)))}`) +
    `<div class="tbl"><table>${smartHead(true)}${smartRows(all, true)}</table></div>`;
}

function logList(top, now, withHost) {
  return top.map(e => `<div class="ev"><time class="num">${fmt.time(e.last, now - e.last > 86400)}</time><span class="dot warn"></span>
    <span>${withHost ? `<b style="font-weight:600">${esc(e.host)}</b> · ` : ""}<span class="muted">${esc(e.ident)}</span>${e.n > 1 ? ` <span class="faint">×${e.n}</span>` : ""}<div class="logmsg">${esc(e.msg)}</div></span></div>`).join("");
}
function renderLogs(cl, now) {
  const el = $("#logs-p");
  const src = Object.entries(cl).filter(([k]) => k.startsWith("logs:"));
  if (!src.length) { el.innerHTML = panelHead("Ошибки в журналах") + `<div class="empty">нет данных (модуль logs агента)</div>`; return; }
  const per = src.map(([k, v]) => [k.slice(5), v.hour || 0]).sort((a, b) => b[1] - a[1]);
  const total = per.reduce((a, [, n]) => a + n, 0);
  const top = src.flatMap(([k, v]) => (v.top || []).map(e => ({...e, host: k.slice(5)}))).sort((a, b) => b.n - a.n || b.last - a.last).slice(0, 6);
  el.innerHTML = panelHead("Ошибки в журналах", total ? `<span class="badge warn">${total} за час</span>` : `<span class="badge">нет</span>`, "уровень err и выше · 1 ч") +
    (total ? `<div class="pb" style="padding-bottom:6px;display:flex;flex-wrap:wrap;gap:6px">${per.filter(([, n]) => n).map(([h, n]) => `<span class="badge mute" style="cursor:pointer" data-host="${esc(h)}">${esc(h)} ${n}</span>`).join("")}</div>
      <div style="padding-bottom:6px">${logList(top, now, true)}</div>` : `<div class="empty">За последний час ошибок нет ни на одном хосте</div>`);
  el.querySelectorAll("[data-host]").forEach(b => b.onclick = () => openDrawer(b.dataset.host));
}

// подряд идущие одинаковые короткие сессии (скрипты, миграции между узлами) — одной строкой ×N
function sshGroups(list) {
  const groups = [];
  for (const l of list) {
    const g = groups[groups.length - 1], short = x => x.fail || !x.tracked || (x.end && x.end - x.ts < 60);
    if (g && g.fail == l.fail && g.user == l.user && g.host == l.host && g.ip == l.ip && g.key == l.key && short(g) && short(l) && g.first - l.ts < 600) { g.n++; g.first = l.ts; }
    else groups.push({...l, n: 1, first: l.ts});
  }
  return groups;
}
const SSH_HEAD = `<div class="sx sxh"><div>Кто → куда</div><div>Откуда</div><div>Чем вошёл</div><div>Когда</div></div>`;
function sshRow(l, now) {
  const old = now - l.ts > 86400, t = x => fmt.time(x, old);
  // интервал «вход – выход»; если всё в одну минуту — одно время
  const span = (a, b) => b && t(a) != fmt.time(b, old) ? `${t(a)} – ${fmt.time(b, false)}` : t(a);
  const dur = x => x < 60 ? Math.max(1, Math.round(x)) + " с" : fmt.dur(x);
  const when = l.fail ? `<div class="m num">${span(l.first, l.n > 1 ? l.ts : 0)}</div><div class="s">не пустили</div>`
    : `<div class="m num">${span(l.first, l.end || (l.n > 1 ? l.ts : 0))}</div>
       <div class="s">${!l.tracked ? "выход не записан" : l.end ? (l.n > 1 ? `сессии по ${dur(l.end - l.ts)}` : `сессия ${dur(l.end - l.ts)}`) : `<span style="color:var(--ok)">● сейчас в системе ${fmt.dur(now - l.ts)}</span>`}</div>`;
  return `<div class="sx ${l.fail ? "fail" : ""}">
    <div><div class="m"><span class="dot ${l.fail ? "crit" : l.known ? "" : "warn"}" style="margin-right:7px"></span><b style="font-weight:600">${esc(l.user)}</b> → ${esc(l.host)}${l.n > 1 ? `<span class="cnt num">×${l.n}</span>` : ""}</div>
      <div class="s num" style="padding-left:15px">${esc(l.host_ip || "")}</div></div>
    <div><div class="m" style="${l.known ? "" : "color:var(--warn)"}">${esc(l.src || l.ip)}</div><div class="s num">${l.src ? esc(l.ip) : l.known ? "известный адрес" : "<span style='color:var(--warn)'>незнакомый адрес</span>"}</div></div>
    <div><div class="m">${esc(l.method || "—")}</div><div class="s" title="${esc(l.key || "")}">${esc(l.key || "")}</div></div>
    <div>${when}</div></div>`;
}
function failLine(f, now) {
  return `<div class="sfail"><span class="dot crit"></span><span><b>${f.n}</b> неудачн${f.n == 1 ? "ая попытка" : f.n < 5 ? "ые попытки" : "ых попыток"} с <b class="num">${esc(f.ip)}</b> на ${esc(f.host)}
    <span class="muted">· ${esc(f.users.slice(0, 5).join(", "))}${f.users.length > 5 ? "…" : ""}</span></span><span class="muted num" style="margin-left:auto;white-space:nowrap">${fmt.ago(now - f.last)}</span></div>`;
}
function renderSSH(logins, now) {
  const el = $("#ssh-p"), fails = DATA.ssh_fails || [], top = el.querySelector(".sbody")?.scrollTop || 0;
  const unknown = logins.filter(l => !l.known && now - l.ts < 86400).length, active = logins.filter(l => l.tracked && !l.end).length;
  const nf = fails.reduce((a, f) => a + f.n, 0);
  el.innerHTML = `<div class="ph"><h3>Входы по SSH</h3>${active ? `<span class="badge">${active} сейчас в системе</span>` : ""}${unknown ? `<span class="badge warn">${unknown} с незнакомых адресов</span>` : ""}${nf ? `<span class="badge crit">${nf} неудачных за сутки</span>` : ""}
    <button class="link" data-open="@ssh">вся история →</button><span class="meta">за 7 дней${logins.length >= 150 ? ", последние 150" : ""}</span></div>` +
    `<div class="sbody">${fails.slice(0, 3).map(f => failLine(f, now)).join("")}` +
    (logins.length ? `<div style="padding-bottom:4px">${SSH_HEAD}${sshGroups(logins).map(l => sshRow(l, now)).join("")}</div>`
      : `<div class="empty">Входов пока не было. Отслеживаются с момента запуска агентов.</div>`) + `</div>`;
  el.querySelector(".sbody").scrollTop = top;  // обновление раз в несколько секунд не сбрасывает прокрутку
}
let SDAYS = 7;
async function drawSSH() {
  $("#drawer-in").innerHTML = `<div class="drawer-h"><h3>Входы по SSH</h3><span class="spacer"></span>
    <div class="ranges">${[[1, "сутки"], [7, "7 дней"], [30, "30 дней"]].map(([d, l]) => `<button data-d="${d}" class="${d == SDAYS ? "on" : ""}">${l}</button>`).join("")}</div><button id="close">✕</button></div>
    <div id="sshh" class="empty">загрузка…</div>`;
  $("#close").onclick = closeDrawer;
  document.querySelectorAll(".ranges button").forEach(b => b.onclick = () => { SDAYS = +b.dataset.d; drawSSH(); });
  const r = await getJSON(`api/ssh?days=${SDAYS}`);
  if (OPEN != "@ssh") return;
  const now = Date.now() / 1000, ok = r.sessions.filter(x => !x.fail);
  const by = {};
  for (const x of ok) { const k = `${x.user} с ${x.src || x.ip}`; by[k] = (by[k] || 0) + 1; }
  $("#sshh").className = "";
  $("#sshh").innerHTML = `
    <div class="info-grid">
      <div class="stat"><div class="l">Входов</div><div class="v num">${ok.length}</div></div>
      <div class="stat"><div class="l">С незнакомых адресов</div><div class="v num" style="${ok.some(x => !x.known) ? "color:var(--warn)" : ""}">${ok.filter(x => !x.known).length}</div></div>
      <div class="stat"><div class="l">Неудачных попыток</div><div class="v num" style="${r.fails.length ? "color:var(--crit)" : ""}">${r.fails.reduce((a, f) => a + f.n, 0)}</div></div>
      <div class="stat"><div class="l">Чаще всего</div><div class="v" style="font-size:13px">${Object.entries(by).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, n]) => `${esc(k)} <span class="muted">×${n}</span>`).join("<br>") || "—"}</div></div>
    </div>
    ${r.fails.length ? `<div class="panel" style="margin-bottom:12px">${panelHead("Неудачные попытки")}${r.fails.map(f => failLine(f, now)).join("")}</div>` : ""}
    <div class="panel">${panelHead("Все входы и попытки", "", `за ${SDAYS == 1 ? "сутки" : SDAYS + " дн"}`)}
      ${r.sessions.length ? SSH_HEAD + sshGroups(r.sessions).map(l => sshRow(l, now)).join("") : `<div class="empty">За этот период входов не было</div>`}</div>`;
}

function setState(state, n) {
  document.body.dataset.state = state;
  const color = {ok: "%233ecf8e", warn: "%23f5b83d", crit: "%23f2555a"}[state];
  const svg = FLAME.replace('fill="currentColor"', `fill="${color}" xmlns="http://www.w3.org/2000/svg"`).replace(' aria-hidden="true"', "").replace(/"/g, "'");
  document.querySelector("link[rel=icon]").href = "data:image/svg+xml," + svg;
  document.title = n ? `(${n}) Omega Luxmon` : "Omega Luxmon";
}

/* Главный статус: коротко, что сейчас с кластером */
function renderHero(d, live, crit, warn) {
  const crits = live.filter(a => a.level == "crit");
  const muted = d.alerts.length - live.length;
  const title = crit == 1 ? esc(crits[0].msg) : crit ? `${crit} ${plural(crit, "критичная проблема", "критичные проблемы", "критичных проблем")}`
    : warn ? `${warn} ${plural(warn, "предупреждение", "предупреждения", "предупреждений")}` : "Всё работает";
  // «спокойно» считаем от последней тревоги или её решения в ленте событий
  const calm = d.events.find(e => e.level == "warn" || e.level == "crit" || e.msg.startsWith("Решено"));
  const oldest = live.length ? Math.min(...live.map(a => a.since)) : null;
  const sub = [live.length ? `началось ${fmt.ago(d.now - oldest)}` : calm ? `спокойно уже ${fmt.dur(d.now - calm.ts)}` : "тревог в недавних событиях нет",
    muted && `${ic("mute")} заглушено ${muted}`].filter(Boolean).join(" · ");
  const last = d.events[0];
  $("#hero").innerHTML = `<div class="hero">${FLAME}<div class="hero-t"><div class="hero-h">${title}</div><div class="hero-s">${sub}</div></div>
    <div class="hero-r"><div class="clock num">${new Date(d.now * 1000).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"})}</div>${last ? `<div class="ev-last"><div class="ttl">последнее событие · ${fmt.time(last.ts, d.now - last.ts > 86400)}</div>${esc(last.msg)}</div>` : ""}</div>${dayStrip(d)}</div>`;
}

/* Сутки по 15 минут: худшее состояние за отрезок; серым — когда сам Luxmon не получал данные */
function dayStrip(d) {
  if (!d.day) return "";
  const step = 900, t0 = d.now - 96 * step;
  const hit = (a, b, s) => a < s + step && (b ?? d.now) > s;
  let cells = "", n = d.day.alerts.filter(a => (a.end ?? d.now) > t0).length;
  for (let i = 0; i < 96; i++) {
    const s = t0 + i * step, on = d.day.alerts.filter(a => hit(a.start, a.end, s));
    const lv = on.some(a => a.level == "crit") ? "crit" : on.length ? "warn" : d.day.gaps.some(g => hit(g[0], g[1], s)) ? "off" : "";
    const tip = `${fmt.time(s)}–${fmt.time(s + step)}: ` + (on.length ? on.slice(0, 4).map(a => a.msg).join("; ") + (on.length > 4 ? ` и ещё ${on.length - 4}` : "") : lv == "off" ? "нет данных" : "без тревог");
    cells += `<i class="${lv}" title="${esc(tip)}"></i>`;
  }
  return `<div class="day"><div class="day-b">${cells}</div><div class="day-l"><span>24 часа назад</span><span>${n ? `${n} ${plural(n, "тревога", "тревоги", "тревог")} за сутки` : "за сутки без тревог"}</span><span>сейчас</span></div></div>`;
}

/* Значок события по его тексту */
function evKind(msg) {
  const m = msg.toLowerCase();
  return /бэкап|pbs/.test(m) ? "backup" : /ssh|вход/.test(m) ? "login" : /ceph|osd|smart|диск|sda|sdb/.test(m) ? "disk"
    : /сет|линк|интерфейс|etcd/.test(m) ? "net" : /перезагруз|перезапуст|запуст|останов|недоступ|узел|хост|cpu|памят|iowait/.test(m) ? "node" : "alert";
}
/* Одинаковые события подряд сворачиваются: десять «Решено: бэкап …» — одна строка */
const EV_OPEN = new Set();
function renderEvents(d, seen) {
  const line = e => `<div class="ev ${seen != null && e.ts > seen ? "fresh" : ""}"><time class="num">${fmt.time(e.ts, d.now - e.ts > 86400)}</time><span class="ev-i ${e.level}">${ic(evKind(e.msg))}</span><span>${esc(e.msg)}</span></div>`;
  const runs = [];
  for (const e of d.events) {
    const k = evKind(e.msg) + e.level + e.msg.startsWith("Решено"), r = runs.at(-1);
    if (r && r.k == k && r.list.at(-1).ts - e.ts < 900) r.list.push(e);
    else runs.push({k, list: [e]});
  }
  $("#events").innerHTML = runs.map(({list}) => {
    if (list.length < 3) return list.map(line).join("");
    const e = list[0], n = list.length, solved = e.msg.startsWith("Решено"), key = String(e.ts);
    const title = evKind(e.msg) == "backup" ? (solved ? `Бэкапы: закрыто предупреждений — ${n}` : `Бэкапы устарели: ${n}`) : `${e.msg} и ещё ${n - 1}`;
    return `<details class="evg" data-k="${key}" ${EV_OPEN.has(key) ? "open" : ""}><summary class="ev ${seen != null && e.ts > seen ? "fresh" : ""}"><time class="num">${fmt.time(e.ts, d.now - e.ts > 86400)}</time><span class="ev-i ${e.level}">${ic(evKind(e.msg))}</span><span><b>${esc(title)}</b><small>показать</small></span></summary>${list.map(line).join("")}</details>`;
  }).join("") || `<div class="empty">пока тихо</div>`;
  document.querySelectorAll("#events .evg").forEach(g => g.ontoggle = () => { g.open ? EV_OPEN.add(g.dataset.k) : EV_OPEN.delete(g.dataset.k); balance(); });
}

/* Три и больше однотипных алерта сворачиваются в одну строку; раскрытые группы помним между обновлениями */
const GROUP_TITLES = {bk: n => `Бэкапы устарели: ${n} ${plural(n, "гость", "гостя", "гостей")}`,
  chk: n => `Не отвечают сервисы: ${n}`, disk: n => `Проблемы с дисками: ${n}`, smart: n => `Предупреждения SMART: ${n}`};
const GROUPS_OPEN = new Set();
function alertBanners(d) {
  const line = a => `<div class="banner ${a.level} ${a.muted ? "muted" : ""}"><span class="dot ${a.level}"></span>${a.muted ? ic("mute") + " " : ""}${esc(a.msg)}<span class="since">${fmt.ago(d.now - a.since)}</span></div>`;
  const groups = new Map();
  // единственная критичная проблема уже написана в главном статусе
  const crits = d.alerts.filter(a => a.level == "crit" && !a.muted);
  d.alerts.filter(a => !(crits.length == 1 && a == crits[0])).forEach(a => {
    const k = `${a.key.split(":")[0]}|${a.level}|${a.muted ? 1 : 0}`;
    groups.set(k, [...(groups.get(k) || []), a]);
  });
  return [...groups].map(([k, list]) => {
    if (list.length < 3) return list.map(line).join("");
    const a = list[0], kind = k.split("|")[0];
    const title = GROUP_TITLES[kind]?.(list.length) || `${a.msg} и ещё ${list.length - 1}`;
    const since = Math.min(...list.map(x => x.since));
    return `<details class="bgroup" data-g="${esc(k)}" ${GROUPS_OPEN.has(k) ? "open" : ""}><summary class="banner ${a.level} ${a.muted ? "muted" : ""}"><span class="dot ${a.level}"></span>${a.muted ? ic("mute") + " " : ""}${esc(title)}<span class="since">${fmt.ago(d.now - since)}<span class="chev">▾</span></span></summary><div class="gbody">${list.map(line).join("")}</div></details>`;
  }).join("");
}

let LAST_EVENT = null;
function render(d) {
  DATA = d;
  const live = d.alerts.filter(a => !a.muted);
  const crit = live.filter(a => a.level == "crit").length, warn = live.length - crit;
  const sum = $("#summary");
  sum.className = "status " + (crit ? "crit" : warn ? "warn" : "");
  sum.innerHTML = live.length
    ? `<span class="dot ${crit ? "crit" : "warn"}"></span>${[crit && crit + " критич.", warn && warn + " предупр."].filter(Boolean).join(" · ")}`
    : `<span class="dot live"></span>Всё в порядке`;
  setState(crit ? "crit" : warn ? "warn" : "ok", live.length);
  renderHero(d, live, crit, warn);
  $("#updated").textContent = "обновлено " + new Date(d.now * 1000).toLocaleTimeString("ru-RU");
  document.body.classList.toggle("viewer", d.role == "viewer");
  $("#userbox").innerHTML = d.auth ? `<button id="logout" title="Выйти"><span class="u">${esc(d.user)}${d.role == "viewer" ? ` <span class="faint">· просмотр</span>` : ""}</span>${ic("logout")}</button>` : `<button id="logout" style="color:var(--warn)" title="Задай пароль командой passwd на сервере">без пароля</button>`;
  $("#logout").onclick = async () => { if (!DATA.auth) return alert("Вход без пароля. Задай пароль на сервере:\n/opt/luxmon/luxmon-server.py passwd admin"); await api("api/logout", {}); location.href = "/login"; };
  $("#banners").innerHTML =
    d.silences.map(x => `<div class="banner info">${ic("mute")}<span><b>${esc(x.label)}</b> — обслуживание ещё ${fmt.dur(x.until - d.now)}${x.reason ? ` <span class="muted" style="font-weight:400">· ${esc(x.reason)}</span>` : ""}</span><button class="admin-only" style="margin-left:auto" data-sid="${esc(x.id)}">Снять</button></div>`).join("") +
    alertBanners(d);
  document.querySelectorAll("#banners .bgroup").forEach(g => g.ontoggle = () => { g.open ? GROUPS_OPEN.add(g.dataset.g) : GROUPS_OPEN.delete(g.dataset.g); });
  document.querySelectorAll("#banners button[data-sid]").forEach(b => b.onclick = () => api("api/silence/delete", {id: b.dataset.sid}).then(refresh));
  renderKpis(d);
  renderHosts(d);
  renderMap(d.cluster.pve);
  renderGuests(d.cluster.pve);
  renderChecks(d.cluster);
  renderCeph(d.cluster.ceph, d.forecast);
  renderPatroni(d.cluster.patroni, d.cluster);
  renderBackups(d.cluster.backups, d.cluster.pve);
  renderSSH(d.logins || [], d.now);
  renderDisks(d.cluster);
  renderLogs(d.cluster, d.now);
  const seen = LAST_EVENT;
  LAST_EVENT = d.events[0]?.ts ?? LAST_EVENT;
  renderEvents(d, seen);
  balance();
}

/* Выравнивание колонок: ошибки журналов, бэкапы и входы по SSH переезжают в левую колонку, если правая заметно длиннее */
function balance() {
  const L = $("#col-l"), R = $("#col-r"), movable = [$("#logs-p"), $("#backups-p"), $("#ssh-p")], ssh = $("#ssh-p");
  ssh.style.height = "";
  movable.forEach(p => R.append(p));
  if (getComputedStyle(L.parentNode).gridTemplateColumns.split(" ").length < 2) return;
  const gap = 16;
  for (const p of [...movable].reverse()) {
    if (p.offsetHeight + gap >= R.offsetHeight - L.offsetHeight) break;
    L.insertBefore(p, movable.find(m => m != p && m.parentNode == L && movable.indexOf(m) > movable.indexOf(p)) || null);
  }
  // входы по SSH не тянут колонку вниз: заканчиваются вровень с соседней, остальное — прокруткой внутри
  const col = ssh.parentNode, other = col == L ? R : L;
  if (col.lastElementChild != ssh) return;
  const room = other.getBoundingClientRect().bottom - ssh.getBoundingClientRect().top;
  if (ssh.offsetHeight > room) ssh.style.height = Math.max(260, room) + "px";
}

/* --------------------------------------------------------------- drawers */
const RANGES = [[3600, "1ч"], [6 * 3600, "6ч"], [86400, "24ч"], [7 * 86400, "7д"], [30 * 86400, "30д"], [90 * 86400, "90д"], [365 * 86400, "1г"]];
// сравнение: тот же период сутки или неделю назад — пунктиром поверх
const CMPS = [[0, "без сравнения"], [86400, "со вчера"], [7 * 86400, "с прошлой неделей"]];
let CMP = 0;
const cmpLabel = () => CMP == 86400 ? "вчера" : "неделю назад";
const rangeBtns = () => `${ZOOM ? `<button id="unzoom" class="sm" title="Вернуть выбранный период">${ic("undo")} ${fmt.time(ZOOM[0], true)} – ${fmt.time(ZOOM[1], true)}</button>` : ""}
  <select id="cmp" title="Сравнить с тем же временем раньше" style="padding:4px 8px;font-size:12px">${CMPS.map(([v, l]) => `<option value="${v}" ${v == CMP ? "selected" : ""}>${l}</option>`).join("")}</select>
  <div class="ranges" title="выдели участок мышью, чтобы приблизить">${RANGES.map(([r, l]) => `<button data-r="${r}" class="${r == RANGE && !ZOOM ? "on" : ""}">${l}</button>`).join("")}</div>
  <button id="addnote" class="sm admin-only" title="Отметить событие на графиках: «обновил ядро», «перенёс VM»">+ Заметка</button>
  <button id="share" class="sm" title="Скопировать ссылку на этот график и период">Ссылка</button><button id="close">✕</button>
  <div id="note-f" style="display:none;flex-basis:100%;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="note-t" maxlength="300" placeholder="Что произошло, например «обновил ядро»" style="flex:1;min-width:220px">
    <input id="note-ts" type="datetime-local" title="Когда">
    <label class="muted" style="font-size:12px;white-space:nowrap"><input type="checkbox" id="note-all"> на всех графиках</label>
    <button class="on" id="note-ok">Сохранить</button></div>`;
function bindDrawer() {
  $("#close").onclick = closeDrawer;
  if ($("#unzoom")) $("#unzoom").onclick = () => { ZOOM = null; setHash(); drawOpen(); };
  document.querySelectorAll(".ranges button").forEach(b => b.onclick = () => { RANGE = +b.dataset.r; ZOOM = null; setHash(); drawOpen(); });
  if ($("#addnote")) $("#addnote").onclick = () => {
    const f = $("#note-f"), d = new Date((ZOOM ? (ZOOM[0] + ZOOM[1]) / 2 : Date.now() / 1000) * 1000);
    f.style.display = f.style.display == "none" ? "flex" : "none";
    // datetime-local ждёт местное время без пояса
    $("#note-ts").value = new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    $("#note-t").focus();
  };
  if ($("#note-ok")) $("#note-ok").onclick = async () => {
    const text = $("#note-t").value.trim();
    if (!text) return $("#note-t").focus();
    try { await api("api/notes", {scope: $("#note-all").checked ? "all" : noteScope(), text, ts: new Date($("#note-ts").value).getTime() / 1000 || null}); drawOpen(); }
    catch (e) { alert("Ошибка: " + e.message); }
  };
  if ($("#cmp")) $("#cmp").onchange = () => { CMP = +$("#cmp").value; setHash(); drawOpen(); };
  if ($("#share")) $("#share").onclick = () => {
    // в ссылке — точное время, чтобы у получателя открылся тот же отрезок, а не «последние сутки» на момент открытия
    const {start, end} = timeSpan(), url = new URL(location.href);
    url.hash = hashFor(OPEN, [Math.floor(start), Math.ceil(end)]);
    navigator.clipboard?.writeText(url.href).then(() => { $("#share").textContent = "Скопировано"; }, () => prompt("Ссылка:", url.href));
  };
}
function hashFor(key, zoom) {
  const p = new URLSearchParams();
  if (zoom) p.set("t", zoom.join("-")); else if (RANGE != 3600) p.set("r", RANGE);
  if (CMP) p.set("c", CMP);
  return "#/" + encodeURIComponent(key.replace(/^@/, "")) + (String(p) ? "?" + p : "");
}
const noteScope = () => OPEN.startsWith("@vm:") ? OPEN.slice(1) : OPEN[0] == "@" ? OPEN.slice(1) : "host:" + OPEN;
/* Заметки за показанный период — под графиками, с удалением */
function noteList(ann) {
  const notes = (ann || []).filter(a => a.kind == "note");
  if (!notes.length) return;
  const el = document.createElement("div");
  el.className = "panel"; el.style.marginTop = "12px";
  el.innerHTML = panelHead("Заметки за период") + notes.map(n => `<div class="li"><span class="dot" style="background:var(--c2)"></span>
    <span class="num muted" style="white-space:nowrap">${fmt.time(n.start, true)}</span><span>${esc(n.msg)}${n.all ? ` <span class="faint">· на всех графиках</span>` : ""}</span>
    <span class="end">${esc(n.by || "")} <button class="link admin-only" data-note="${n.id}">удалить</button></span></div>`).join("");
  $("#drawer-in").append(el);
  el.querySelectorAll("[data-note]").forEach(b => b.onclick = () => confirm("Удалить заметку?") && api("api/notes/delete", {id: +b.dataset.note}).then(drawOpen));
}
const setHash = () => OPEN && history.replaceState(null, "", hashFor(OPEN, ZOOM));
/* История метрик; при сравнении — ещё и тот же период раньше, сдвинутый к текущему (в .prev) */
async function histQ(params) {
  const r = await getJSON(`api/history?${params}&${rangeQS()}`);
  if (!CMP) return r;
  const {start, end} = timeSpan();
  const p = await getJSON(`api/history?${params}&start=${Math.floor(start - CMP)}&end=${Math.ceil(end - CMP)}`.replace(/&scope=[^&]*/, ""));
  for (const [k, v] of Object.entries(p)) if (r[k] && v.points) r[k].prev = v.points.map(([t, x]) => [t + CMP, x]);
  return r;
}
function openDrawer(key, zoom = null) {
  OPEN = key; ZOOM = zoom;
  setHash();
  $("#overlay").classList.add("open"); $("#drawer").classList.add("open");
  $("#drawer").scrollTop = 0;
  drawOpen();
}
function closeDrawer() {
  NEWTOKEN = null;
  if (OPEN) history.replaceState(null, "", location.pathname + location.search);
  OPEN = null; ZOOM = null;
  $("#overlay").classList.remove("open"); $("#drawer").classList.remove("open");
}
function drawOpen() {
  if (!OPEN) return;
  if (OPEN == "@settings") return drawSettings();
  if (OPEN == "@history") return drawHistory();
  if (OPEN == "@ssh") return drawSSH();
  if (OPEN == "@agents") return drawAgents();
  if (OPEN.startsWith("@vm:")) return drawVM(+OPEN.slice(4));
  return OPEN[0] == "@" ? drawCluster(OPEN.slice(1)) : drawHost();
}
const rangeQS = () => ZOOM ? `start=${ZOOM[0]}&end=${ZOOM[1]}` : `range=${RANGE}`;
const timeSpan = () => ZOOM ? {start: ZOOM[0], end: ZOOM[1]} : {start: Date.now() / 1000 - RANGE, end: Date.now() / 1000};
const chartBox = (id, title, wide) => `<div class="panel chart" ${wide ? 'style="grid-column:1/-1"' : ""}><div class="chart-h"><h4>${esc(title)}</h4><div class="legend" id="lg-${id}"></div></div><div id="ch-${id}"></div></div>`;
function drawSeries(id, series, opt) {
  series = series.filter(s => s.points.length);
  series = series.concat(series.filter(s => s.prev?.length).map(s => ({name: `${s.name} · ${cmpLabel()}`, color: s.color, points: s.prev, step: s.step, dash: true})));
  $("#lg-" + id).innerHTML = series.filter(s => !s.dash).map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")
    + (series.some(s => s.dash) ? `<span><i style="background:repeating-linear-gradient(90deg,var(--muted) 0 4px,transparent 4px 7px)"></i>${cmpLabel()}</span>` : "");
  lineChart($("#ch-" + id), series, {...timeSpan(), ...opt});
}
const PAL = () => [1, 2, 3, 4, 5, 6, 7, 8].map(i => css("--c" + i));

async function drawHost() {
  const name = OPEN, h = DATA.hosts.find(x => x.name == name);
  if (!h) return;
  const i = h.info || {}, m = h.metrics || {};
  const disks = (h.disks || []).map(d => "disk:" + d.mount);
  const st = hostState(h);
  const fc = (DATA.forecast || []).filter(f => f.host == name);
  $("#drawer-in").innerHTML = `
    <div class="drawer-h"><span class="dot ${st == "ok" ? "" : st}"></span><h3>${esc(name)}</h3>
      <span class="muted">${esc(i.os || "")} · ${esc(i.kernel || "")}</span><span class="spacer"></span>${rangeBtns()}</div>
    <div class="info-grid">
      <div class="stat"><div class="l">Аптайм</div><div class="v num">${fmt.dur(i.uptime)}</div></div>
      <div class="stat"><div class="l">CPU</div><div class="v num">${i.cpus ?? "—"} ядер</div></div>
      <div class="stat"><div class="l">Память</div><div class="v num">${fmt.bytes(m.mem_used)} / ${fmt.bytes(m.mem_total)}</div></div>
      <div class="stat"><div class="l">Прогноз диска</div><div class="v num" style="font-size:13px">${fc.length ? fc.map(f => `${esc(f.target)}: ${fcText(f)}`).join("<br>") : "мало истории"}</div></div>
    </div>
    <div class="charts">${[["cpu", "CPU"], ["mem", "Память"], ["net", "Сеть"], ["io", "Диск I/O"], ["load", "Load average"], ...(m.temp != null ? [["temp", "Температура CPU"]] : []), ["disk", "Заполнение дисков"],
        ...(m.net_util != null ? [["netu", `Загрузка канала ${m.link_mbit} Мбит/с`]] : []), ...(m.log_err != null ? [["logs", "Ошибки в журнале, в минуту"]] : []),
        ...(m.disk_lat != null ? [["lat", "Задержка диска, мс на операцию"]] : []),
        ...(m.agent_cpu != null ? [["agent", "Агент Luxmon: CPU и диск"]] : [])]
        .map(([id, t]) => chartBox(id, t)).join("")}</div>${hostExtras(name)}`;
  bindDrawer();
  bindProcs(name);
  CHARTS = [];
  const metrics = ["cpu", "iowait", "mem", "swap", "net_rx", "net_tx", "disk_r", "disk_w", "load1", "temp", "net_util", "log_err", "agent_cpu", "agent_io", "disk_lat", "mem_psi", ...disks];
  const r = await histQ(`host=${encodeURIComponent(name)}&scope=${encodeURIComponent("host:" + name)}&metrics=${encodeURIComponent(metrics.join(","))}`);
  if (OPEN != name) return;
  const S = (key, label, color, fill) => ({name: label, color, fill, points: r[key]?.points || [], step: r[key]?.step, prev: r[key]?.prev});
  const C = PAL(), ann = r._annotations;
  const pct = {max: 100, fmt: v => v.toFixed(0) + "%", ann};
  drawSeries("cpu", [S("cpu", "CPU", C[0], true), S("iowait", "iowait", C[4])], pct);
  drawSeries("mem", [S("mem", "RAM", C[1], true), S("swap", "swap", C[3]), ...(m.mem_psi != null ? [S("mem_psi", "ждут память", C[4])] : [])], pct);
  drawSeries("net", [S("net_rx", "вход", C[2], true), S("net_tx", "выход", C[0])], {fmt: fmt.bits, ann});
  drawSeries("io", [S("disk_r", "чтение", C[0], true), S("disk_w", "запись", C[3])], {fmt: fmt.rate, ann});
  drawSeries("load", [S("load1", "1 мин", C[4], true)], {fmt: v => v.toFixed(1), ann});
  if (m.temp != null) drawSeries("temp", [S("temp", "°C", C[3], true)], {fmt: v => v.toFixed(0) + " °C", minMax: 50, ann});
  drawSeries("disk", disks.map((d, k) => S(d, d.slice(5), C[k % 3])), pct);
  if (m.net_util != null) drawSeries("netu", [S("net_util", "занято канала", C[2], true)], pct);
  if (m.log_err != null) drawSeries("logs", [S("log_err", "ошибок в минуту", C[3], true)], {fmt: v => v.toFixed(1), minMax: 1, ann});
  if (m.disk_lat != null) drawSeries("lat", [S("disk_lat", "самый медленный диск", C[3], true)], {fmt: v => v.toFixed(0) + " мс", minMax: 10, ann});
  if (m.agent_cpu != null) drawSeries("agent", [S("agent_cpu", "CPU, %", C[0], true)], {fmt: v => v.toFixed(1) + "%", minMax: 2, ann});
  noteList(ann);
}
let PSORT = "cpu";
function procTable(name) {
  const pr = DATA.cluster["procs:" + name];
  if (!pr) return "";
  const key = {cpu: r => r.cpu, mem: r => r.rss, io: r => (r.rd || 0) + (r.wr || 0)}[PSORT];
  const rows = [...pr.top].sort((a, b) => key(b) - key(a)).slice(0, 10);
  const tabs = [["cpu", "CPU"], ["mem", "память"], ["io", "диск"]].map(([k, l]) => `<button data-ps="${k}" class="${k == PSORT ? "on" : ""}">${l}</button>`).join("");
  return panelHead("Процессы", `<div class="filters">${tabs}</div>`, `всего ${pr.count} · за ${pr.window} с · ${fmt.ago(pr._age)}`) +
    `<div class="tbl"><table class="ptable"><tr><th>PID</th><th>Процесс</th><th class="r">CPU</th><th class="r">Память</th><th class="r">Чтение</th><th class="r">Запись</th><th>Команда</th></tr>${rows.map(r => `<tr>
      <td class="faint num">${esc(r.pid)}</td><td><b style="font-weight:600">${esc(r.name)}</b></td>
      <td class="r num" style="color:${r.cpu > 50 ? "var(--warn)" : ""}">${r.cpu.toFixed(1)}%</td><td class="r num">${fmt.bytes(r.rss)}</td>
      <td class="r num muted">${r.rd != null ? fmt.rate(r.rd) : "—"}</td><td class="r num muted">${r.wr != null ? fmt.rate(r.wr) : "—"}</td>
      <td class="cmd" title="${esc(r.cmd)}">${esc(r.cmd)}</td></tr>`).join("")}</table></div>`;
}
function bindProcs(name) {
  document.querySelectorAll("[data-ps]").forEach(b => b.onclick = () => { PSORT = b.dataset.ps; $("#procs-box").innerHTML = procTable(name); bindProcs(name); });
}
function hostExtras(name) {
  const sm = DATA.cluster["smart:" + name], lg = DATA.cluster["logs:" + name], up = DATA.cluster["updates:" + name];
  const h = DATA.hosts.find(x => x.name == name), m = h.metrics || {}, ck = h.clock || {};
  const skew = Math.max(Math.abs(ck.skew_ms || 0), Math.abs(ck.offset_ms || 0));
  let out = `<div class="info-grid" style="margin-top:12px">
    <div class="stat"><div class="l">Агент Luxmon</div><div class="v num" style="font-size:13px">CPU ${m.agent_cpu != null ? m.agent_cpu.toFixed(1) + "%" : "—"} · ${fmt.bytes(m.agent_mem)}<br><span class="muted">диск ${fmt.rate(m.agent_io || 0)}</span></div></div>
    <div class="stat"><div class="l">Время</div><div class="v num" style="font-size:13px">${ck.sync === false ? `<span style="color:var(--warn)">не синхронизировано</span>` : ck.sync ? "синхронизировано" : "—"}<br><span class="muted">${ck.offset_ms != null ? `chrony ${ck.offset_ms} мс` : ""}${ck.skew_ms != null ? `${ck.offset_ms != null ? " · " : ""}<span title="разница с часами сервера Luxmon, включая время в пути">к Luxmon ${ck.skew_ms > 0 ? "+" : ""}${ck.skew_ms} мс</span>` : ""}</span></div></div>
    <div class="stat"><div class="l">Обновления</div><div class="v num" style="font-size:13px">${up ? (up.count ? `${up.count} пакетов${up.security ? `, безопасности ${up.security}` : ""}` : "всё обновлено") : "—"}<br><span class="muted" style="${up?.reboot ? "color:var(--warn)" : ""}">${up ? (up.reboot ? "нужна перезагрузка" : "перезагрузка не нужна") : ""}</span></div></div>
    <div class="stat"><div class="l">Процессов</div><div class="v num">${DATA.cluster["procs:" + name]?.count ?? "—"}</div></div>
    ${h.ifaces?.length ? `<div class="stat"><div class="l">Сетевые линки</div><div class="v num" style="font-size:13px">${h.ifaces.map(x => `${esc(x.name)} ${x.up ? (x.speed ? x.speed + " Мбит/с" : "поднят") : `<span style="color:var(--crit)">нет линка</span>`}${x.err_min ? ` <span style="color:var(--warn)">${x.err_min} ош/мин</span>` : ""}`).join("<br>")}</div></div>` : ""}
  </div>`;
  if (DATA.cluster["procs:" + name]) out += `<div class="panel" id="procs-box" style="margin-top:12px">${procTable(name)}</div>`;
  if (sm?.disks?.length) out += `<div class="panel" style="margin-top:12px">${panelHead("Диски (SMART)", "", fmt.ago(sm._age))}<div class="tbl"><table>${smartHead(false)}${smartRows(sm.disks, false)}</table></div></div>`;
  if (lg) out += `<div class="panel" style="margin-top:12px">${panelHead("Частые ошибки в журнале", lg.hour ? `<span class="badge warn">${lg.hour} за час</span>` : "", "последний час")}${lg.top?.length ? `<div style="padding-bottom:6px">${logList(lg.top, DATA.now, false)}</div>` : `<div class="empty">ошибок нет</div>`}</div>`;
  return out;
}

async function drawVM(vmid) {
  const key = OPEN, g = DATA.cluster.pve?.guests.find(x => x.vmid == vmid);
  if (!g) return;
  const run = g.status == "running";
  $("#drawer-in").innerHTML = `
    <div class="drawer-h"><span class="dot ${run ? "" : "off"}"></span><h3>${esc(g.name)}</h3>
      <span class="muted">${g.type == "lxc" ? "контейнер" : "VM"} ${esc(g.vmid)} · ${esc(g.node)} · ${run ? "работает " + fmt.dur(g.uptime) : esc(g.status)}</span><span class="spacer"></span>${rangeBtns()}</div>
    <div class="info-grid">
      <div class="stat"><div class="l">CPU</div><div class="v num">${run ? (100 * g.cpu).toFixed(0) + "%" : "—"} <span class="faint" style="font-size:12px">из ${g.maxcpu} vCPU</span></div></div>
      <div class="stat"><div class="l">Память</div><div class="v num">${run ? fmt.bytes(g.mem) : "—"} / ${fmt.bytes(g.maxmem)}</div></div>
      <div class="stat"><div class="l">Диск</div><div class="v num">${g.maxdisk ? (g.disk ? fmt.bytes(g.disk) + " / " : "") + fmt.bytes(g.maxdisk) : "—"}</div></div>
      <div class="stat"><div class="l">Сеть сейчас</div><div class="v num" style="font-size:13px">${run && g.net_rx != null ? `↓ ${fmt.bits(g.net_rx)}<br>↑ ${fmt.bits(g.net_tx)}` : "—"}</div></div>
    </div>
    <div class="charts">${chartBox("cpu", "CPU (от выделенных ядер)")}${chartBox("mem", "Память")}${chartBox("net", "Сеть")}${chartBox("io", "Диск I/O")}${chartBox("lat", "Задержка диска в Ceph", true)}</div>`;
  bindDrawer();
  CHARTS = [];
  const r = await histQ(`host=_vm${vmid}&scope=vm:${vmid}&metrics=cpu,mem,net_rx,net_tx,disk_r,disk_w,rd_lat,wr_lat`);
  if (OPEN != key) return;
  const S = (k, label, color, fill) => ({name: label, color, fill, points: r[k]?.points || [], step: r[k]?.step, prev: r[k]?.prev});
  const C = PAL(), ann = r._annotations;
  drawSeries("cpu", [S("cpu", "CPU", C[0], true)], {max: 100, fmt: v => v.toFixed(0) + "%", ann});
  drawSeries("mem", [S("mem", "память", C[1], true)], {max: 100, fmt: v => v.toFixed(0) + "%", ann});
  drawSeries("net", [S("net_rx", "вход", C[2], true), S("net_tx", "выход", C[0])], {fmt: fmt.bits, ann});
  drawSeries("io", [S("disk_r", "чтение", C[0], true), S("disk_w", "запись", C[3])], {fmt: fmt.rate, ann});
  drawSeries("lat", [S("wr_lat", "запись", C[3], true), S("rd_lat", "чтение", C[0])], {fmt: v => v.toFixed(1) + " мс", minMax: 5, ann});
  noteList(ann);
}

const CLUSTER = {
  ceph: {title: "Ceph", groups: ["_ceph"], charts: [
    {id: "osd", title: "Задержка записи OSD (commit)", pick: m => m.startsWith("osd."), fmt: v => v.toFixed(0) + " мс", wide: true, minMax: 10},
    {id: "io", title: "Пропускная способность", pick: m => m == "io_rd" || m == "io_wr", label: m => m == "io_rd" ? "чтение" : "запись", fmt: v => fmt.rate(v)},
    {id: "ops", title: "Операции в секунду", pick: m => m == "ops_rd" || m == "ops_wr", label: m => m == "ops_rd" ? "чтение" : "запись", fmt: v => v.toFixed(0)},
    {id: "used", title: "Заполнение", pick: m => m == "used_pct", label: () => "занято", fmt: v => v.toFixed(1) + "%", wide: true},
  ]},
  patroni: {title: "PostgreSQL и etcd", groups: ["_etcd"], pg: true, charts: [
    {id: "fsync", title: "etcd: fsync WAL (среднее за интервал)", pick: m => m.startsWith("fsync:"), label: m => m.slice(6), fmt: v => v.toFixed(1) + " мс"},
    {id: "commit", title: "etcd: commit backend", pick: m => m.startsWith("commit:"), label: m => m.slice(7), fmt: v => v.toFixed(1) + " мс"},
    {id: "rtt", title: "etcd: время ответа /health", pick: m => m.startsWith("rtt:"), label: m => m.slice(4), fmt: v => v.toFixed(1) + " мс"},
    {id: "lag", title: "Лаг репликации", pick: m => m.startsWith("lag:"), label: m => m.slice(4), fmt: v => fmt.bytes(v), minMax: 1048576},
    {id: "conn", title: "PostgreSQL: подключения", pg: "conn_total", fmt: v => v.toFixed(0), minMax: 10},
    {id: "tps", title: "PostgreSQL: транзакций в секунду", pg: "tps", fmt: v => v.toFixed(1), minMax: 5},
    {id: "hit", title: "PostgreSQL: попадание в кэш", pg: "cache_hit", fmt: v => v.toFixed(0) + "%", max: 100},
    {id: "size", title: "PostgreSQL: размер баз", pg: "db_size", fmt: v => fmt.bytes(v)},
  ]},
  checks: {title: "Время ответа сервисов", groups: ["_checks"], charts: [
    {id: "chk", title: "Время ответа", pick: () => true, fmt: v => v.toFixed(0) + " мс", wide: true},
  ]},
};
async function drawCluster(kind) {
  const cfg = CLUSTER[kind], key = OPEN;
  const pgHosts = cfg.pg ? Object.keys(DATA.cluster).filter(k => k.startsWith("postgres:")).map(k => k.slice(9)).sort() : [];
  const charts = cfg.charts.filter(c => !c.pg || pgHosts.length);
  $("#drawer-in").innerHTML = `
    <div class="drawer-h"><h3>${esc(cfg.title)}</h3><span class="spacer"></span>${rangeBtns()}</div>
    ${kind == "checks" ? `<div class="panel" id="uptime-box" style="margin-bottom:12px"><div class="empty">загрузка…</div></div>` : ""}
    <div class="charts">${charts.map(c => chartBox(c.id, c.title, c.wide)).join("")}</div>${kind == "ceph" ? rbdTable() : ""}`;
  bindDrawer();
  if (kind == "checks") drawUptime();
  document.querySelectorAll("#drawer-in tr[data-vm]").forEach(r => { if (r.dataset.vm) r.onclick = () => openDrawer("@vm:" + r.dataset.vm); });
  CHARTS = [];
  const names = await getJSON(`api/metrics?host=${cfg.groups[0]}`);
  const r = names.length ? await histQ(`host=${cfg.groups[0]}&scope=${kind}&metrics=${encodeURIComponent(names.join(","))}`) : {};
  const pgData = {};
  for (const h of pgHosts) pgData[h] = await histQ(`host=${encodeURIComponent("_pg_" + h)}&metrics=conn_total,tps,cache_hit,db_size`);
  if (OPEN != key) return;
  const C = PAL();
  for (const c of charts) {
    let series;
    if (c.pg) series = pgHosts.map((h, i) => ({name: h, color: C[i % C.length], points: pgData[h][c.pg]?.points || [], step: pgData[h][c.pg]?.step, prev: pgData[h][c.pg]?.prev}));
    else {
      const ms = names.filter(c.pick);
      series = ms.map((m, i) => ({name: c.label ? c.label(m) : m, color: C[i % C.length], points: r[m]?.points || [], step: r[m]?.step, prev: r[m]?.prev, fill: ms.length == 1}));
    }
    drawSeries(c.id, series, {fmt: c.fmt, ann: r._annotations, max: c.max, minMax: c.minMax});
  }
  noteList(r._annotations);
}

function rbdTable() {
  const imgs = DATA.cluster.ceph?.images;
  if (!imgs?.length) return "";
  const names = Object.fromEntries((DATA.cluster.pve?.guests || []).map(g => [g.vmid, g.name]));
  const rows = [...imgs].sort((a, b) => (b.rd + b.wr) - (a.rd + a.wr));
  const lat = v => `<span style="color:${v > 100 ? "var(--crit)" : v > 30 ? "var(--warn)" : ""}">${v.toFixed(1)} мс</span>`;
  return `<div class="panel" style="margin-top:12px">${panelHead("Нагрузка на Ceph по дискам VM", "", "за последние ~30 с")}
    <div class="tbl"><table><tr><th>Диск</th><th>Гость</th><th class="r">Чтение</th><th class="r">Запись</th><th class="r">Операций/с</th><th class="r">Задержка чтения</th><th class="r">Задержка записи</th></tr>
    ${rows.map(i => `<tr class="${i.vmid != null ? "click" : ""}" data-vm="${esc(i.vmid)}"><td class="muted">${esc(i.pool)}/${esc(i.image)}</td><td><b style="font-weight:600">${esc(names[i.vmid] || "")}</b> <span class="faint">${esc(i.vmid)}</span></td>
      <td class="r num">${fmt.rate(i.rd)}</td><td class="r num">${fmt.rate(i.wr)}</td><td class="r num muted">${(i.rd_ops + i.wr_ops).toFixed(0)}</td>
      <td class="r num">${i.rd_ops ? lat(i.rd_lat) : "—"}</td><td class="r num">${i.wr_ops ? lat(i.wr_lat) : "—"}</td></tr>`).join("")}</table></div></div>`;
}
async function drawUptime() {
  const u = await getJSON("api/uptime?days=30");
  if (OPEN != "@checks") return;
  const names = Object.keys(u).sort();
  const lv = p => p == null ? "none" : p >= 99.9 ? "" : p >= 99 ? "warn" : "crit";
  $("#uptime-box").innerHTML = panelHead("Доступность за 30 дней", "", "каждая полоска — сутки") + (names.length ? names.map(n => `<div class="urow">
    <b style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n)}</b>
    <div class="ubars">${u[n].days.map(([d, p]) => `<i class="${lv(p)}" title="${esc(new Date(d * 1000).toLocaleDateString("ru-RU") + ": " + (p == null ? "нет данных" : p.toFixed(2) + "%"))}"></i>`).join("")}</div>
    <span class="r num" style="text-align:right;color:${u[n].total < 99 ? "var(--crit)" : u[n].total < 99.9 ? "var(--warn)" : ""}">${u[n].total.toFixed(2)}%</span></div>`).join("")
    : `<div class="empty">Данные о доступности копятся с момента обновления агента — первые полоски появятся через несколько минут</div>`);
}

/* --------------------------------------------------------------- agents */
let NEWTOKEN = null;  // {host, token}: показываем один раз, сервер хранит только хеш
async function drawAgents() {
  $("#drawer-in").innerHTML = `<div class="drawer-h"><h3>Агенты и токены</h3><span class="spacer"></span><button id="close">✕</button></div><div id="ag" class="empty">загрузка…</div>`;
  $("#close").onclick = closeDrawer;
  const r = await getJSON("api/agents");
  if (OPEN != "@agents") return;
  const now = Date.now() / 1000, admin = DATA.role != "viewer";
  const shared = r.agents.filter(a => a.auth == "shared").length;
  const tok = a => a.auth == "host" ? `<span class="badge">свой</span> <span class="faint">с ${fmt.time(a.token_since, true)}</span>`
    : a.token_since ? `<span class="badge warn">выдан, агент ещё не прислал с ним данные</span>`
    : a.auth == "shared" ? `<span class="badge warn">общий</span>` : `<span class="faint">—</span>`;
  const row = a => `<tr><td><span class="dot ${a.last == null ? "off" : now - a.last > 90 ? "crit" : ""}" style="margin-right:8px"></span><b style="font-weight:600">${esc(a.name)}</b><div class="faint" style="font-size:11.5px;padding-left:16px">${esc((a.ips || []).join(", "))}</div></td>
    <td class="muted">${a.last == null ? "не было" : fmt.ago(now - a.last)}</td><td>${tok(a)}</td>
    <td class="num">${esc(a.version || "старый")}</td><td class="muted" style="white-space:normal;font-size:12px">${esc((a.modules || []).join(", "))}</td>
    <td class="r admin-only" style="white-space:nowrap"><button data-tok="${esc(a.name)}" title="Выдать новый токен: прежний перестанет действовать">Новый токен</button>
      ${a.token_since ? `<button data-rev="${esc(a.name)}">Отозвать</button>` : ""}<button data-fgt="${esc(a.name)}" title="Хост выведен из работы: убрать из списка, история графиков останется">Убрать</button></td></tr>`;
  $("#ag").className = "";
  $("#ag").innerHTML = `
    ${NEWTOKEN ? `<div class="banner info" style="flex-wrap:wrap;margin-bottom:12px"><span>Токен для <b>${esc(NEWTOKEN.host)}</b> — показывается один раз. Впишите его в <code>"token"</code> в <code>/etc/luxmon/agent.json</code> на этом хосте и перезапустите агента: <code>systemctl restart luxmon-agent</code></span>
      <input id="newtok" readonly value="${esc(NEWTOKEN.token)}" style="flex:1;min-width:260px;font-family:ui-monospace,monospace"><button id="copytok">Скопировать</button></div>` : ""}
    ${shared ? `<div class="banner warn" style="margin-bottom:12px">${shared} ${plural(shared, "хост присылает", "хоста присылают", "хостов присылают")} данные с общим токеном: им можно выдать себя за другой хост. Выдайте каждому свой.</div>` : ""}
    ${r.shared_token && !shared ? `<div class="banner info" style="margin-bottom:12px">У всех хостов свои токены. Общий токен <code>token</code> в server.json больше не нужен — уберите его и перезапустите сервер.</div>` : ""}
    <div class="panel"><div class="tbl"><table><tr><th>Хост</th><th>Данные</th><th>Токен</th><th>Агент</th><th>Модули</th><th class="admin-only"></th></tr>${r.agents.map(row).join("")}</table></div>
      <div class="pb admin-only" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-top:1px solid var(--line)"><span class="muted">Новый хост:</span><input id="newhost" placeholder="имя, как в agent.json" maxlength="64"><button id="addhost">Выдать токен</button></div></div>`;
  const issue = async host => {
    try { NEWTOKEN = {host, token: (await api("api/agents/token", {host})).token}; drawAgents(); }
    catch (e) { alert("Ошибка: " + e.message); }
  };
  const act = async (url, host, q) => { if (!confirm(q)) return; try { await api(url, {host}); NEWTOKEN = null; drawAgents(); refresh(); } catch (e) { alert("Ошибка: " + e.message); } };
  if (admin) {
    document.querySelectorAll("[data-tok]").forEach(b => b.onclick = () => confirm(`Выдать ${b.dataset.tok} новый токен? Прежний перестанет действовать сразу, до замены в agent.json данные с хоста приходить не будут.`) && issue(b.dataset.tok));
    document.querySelectorAll("[data-rev]").forEach(b => b.onclick = () => act("api/agents/revoke", b.dataset.rev, `Отозвать токен ${b.dataset.rev}? Хост перестанет присылать данные.`));
    document.querySelectorAll("[data-fgt]").forEach(b => b.onclick = () => act("api/agents/forget", b.dataset.fgt, `Убрать ${b.dataset.fgt} из списка и отозвать токен? История графиков останется.`));
    $("#addhost").onclick = () => $("#newhost").value.trim() && issue($("#newhost").value.trim());
  }
  if ($("#copytok")) $("#copytok").onclick = () => { $("#newtok").select(); navigator.clipboard?.writeText(NEWTOKEN.token).catch(() => document.execCommand("copy")); $("#copytok").textContent = "Скопировано"; };
}

/* --------------------------------------------------------------- alert history */
let HDAYS = 7;
async function drawHistory() {
  $("#drawer-in").innerHTML = `<div class="drawer-h"><h3>История алертов</h3><span class="spacer"></span>
    <div class="ranges">${[[1, "сутки"], [7, "7 дней"], [30, "30 дней"]].map(([d, l]) => `<button data-d="${d}" class="${d == HDAYS ? "on" : ""}">${l}</button>`).join("")}</div><button id="close">✕</button></div>
    <div id="hist" class="empty">загрузка…</div>`;
  $("#close").onclick = closeDrawer;
  document.querySelectorAll(".ranges button").forEach(b => b.onclick = () => { HDAYS = +b.dataset.d; drawHistory(); });
  const h = await getJSON(`api/alerts/history?days=${HDAYS}`);
  if (OPEN != "@history") return;
  const now = Date.now() / 1000;
  $("#hist").className = "";
  $("#hist").innerHTML = !h.summary.length ? `<div class="panel empty">За этот период алертов не было</div>` : `
    <div class="panel" style="margin-bottom:12px">${panelHead("Чаще всего", "", `${h.summary.length} разных за ${HDAYS == 1 ? "сутки" : HDAYS + " дн"}`)}
      <div class="tbl"><table><tr><th>Алерт</th><th class="r">Раз</th><th class="r">Всего длилось</th><th class="r">Последний</th></tr>
      ${h.summary.map(a => `<tr><td><span class="dot ${a.level}" style="margin-right:8px"></span>${esc(a.msg)}${a.active ? ` <span class="badge ${a.level}">сейчас</span>` : ""}</td>
        <td class="r">${a.count}</td><td class="r">${fmt.dur(a.total)}</td><td class="r muted">${fmt.ago(now - a.last)}</td></tr>`).join("")}</table></div></div>
    <div class="panel">${panelHead("Все срабатывания")}
      <div class="tbl"><table><tr><th>Начало</th><th>Длилось</th><th>Алерт</th></tr>
      ${h.items.map(a => `<tr><td class="muted">${fmt.time(a.start, true)}</td><td>${a.end ? fmt.dur(a.end - a.start) : `<span class="badge ${a.level}">идёт ${fmt.dur(now - a.start)}</span>`}</td>
        <td style="white-space:normal"><span class="dot ${a.level}" style="margin-right:8px"></span>${a.muted ? ic("mute") + " " : ""}${esc(a.msg)}</td></tr>`).join("")}</table></div></div>`;
}

/* --------------------------------------------------------------- settings */
let SET = null;
async function drawSettings() {
  $("#drawer-in").innerHTML = `<div class="drawer-h"><h3>Настройки</h3><span class="spacer"></span><button id="close">✕</button></div><div id="set" class="empty">загрузка…</div>`;
  $("#close").onclick = closeDrawer;
  SET = await getJSON("api/settings");
  if (OPEN != "@settings") return;
  renderSettings();
}
function checkRow(c, i) {
  const target = c.type == "http" ? c.url || "" : c.host || "";
  return `<tr data-i="${i}">
    <td><input data-k="name" value="${esc(c.name)}" placeholder="Имя"></td>
    <td style="width:90px"><select data-k="type">${["http", "tcp", "ping"].map(t => `<option ${t == c.type ? "selected" : ""}>${t}</option>`).join("")}</select></td>
    <td><input data-k="target" value="${esc(target)}" placeholder="${c.type == "http" ? "https://адрес/путь" : "адрес"}"></td>
    <td style="width:150px">${c.type == "tcp" ? `<input data-k="port" value="${esc(c.port ?? "")}" placeholder="порт">` : c.type == "http" ? `<label class="muted" style="font-size:12px;white-space:nowrap"><input type="checkbox" data-k="insecure" ${c.insecure ? "checked" : ""}> без TLS-проверки</label>` : ""}</td>
    <td>${c.type == "http" ? `<input data-k="expect_text" value="${esc(c.expect_text || "")}" placeholder="текст в ответе (необяз.)">` : ""}</td>
    <td style="width:36px"><button data-del="${i}" title="Удалить">✕</button></td></tr>`;
}
function readChecks() {
  return [...document.querySelectorAll("#checks-t tr[data-i]")].map(tr => {
    const v = k => tr.querySelector(`[data-k="${k}"]`);
    const c = {name: v("name").value.trim(), type: v("type").value};
    const target = v("target").value.trim();
    if (c.type == "http") { c.url = target; if (v("insecure")?.checked) c.insecure = true; if (v("expect_text")?.value.trim()) c.expect_text = v("expect_text").value.trim(); }
    else c.host = target;
    if (c.type == "tcp") c.port = +(v("port")?.value || 0);
    return c;
  });
}
function renderSettings(msg, isErr) {
  const s = SET;
  $("#set").className = "";
  $("#set").innerHTML = `
    <div class="sect" style="margin-top:0">Пороги алертов</div>
    <div class="form-grid">${s.rules.map(r => `<div class="field"><label for="r-${r.key}">${esc(r.label)}</label>
      <div class="unit"><input id="r-${r.key}" data-rule="${r.key}" type="number" step="any" min="${r.min}" max="${r.max}" value="${r.value}" title="по умолчанию ${r.default}"><span>${esc(r.unit)}</span></div></div>`).join("")}</div>

    <div class="sect">Проверки сервисов</div>
    <div class="field" style="max-width:320px"><label>Откуда проверять</label><select id="runner">${s.settings.checks_runner ? "" : `<option value="" selected>первый хост, приславший данные</option>`}${s.hosts.map(h => `<option ${h == s.settings.checks_runner ? "selected" : ""}>${esc(h)}</option>`).join("")}</select></div>
    <div class="panel tbl"><table class="ctable" id="checks-t"><tr><th>Имя</th><th>Тип</th><th>Адрес</th><th></th><th>Ожидаемый текст</th><th></th></tr>${s.checks.map(checkRow).join("")}</table>
      <div style="padding:8px 10px"><button id="add-check">+ Добавить проверку</button></div></div>

    <div class="sect">Бэкапы</div>
    <label class="muted" style="font-size:13px;display:flex;gap:8px;align-items:center"><input type="checkbox" id="bk-weekdays" ${s.settings.backup_weekdays_only ? "checked" : ""}> Бэкапы делаются только по будням: субботу и воскресенье не считать в возраст бэкапа</label>

    <div class="sect">Утренняя сводка в Telegram</div>
    <div class="field" style="max-width:320px"><label for="sumtime">Время отправки (ЧЧ:ММ, пусто — не отправлять)</label><input id="sumtime" value="${esc(s.settings.summary_time)}" placeholder="09:00"></div>
    <label class="muted" style="font-size:13px;display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="weekly" ${s.settings.weekly_report ? "checked" : ""}> По понедельникам в это же время присылать итоги недели (команда /week)</label>

    <div class="sect">Известные адреса для SSH</div>
    <div class="muted" style="font-size:12.5px;margin-bottom:8px">Вход по SSH с адреса не из этого списка сразу приходит в Telegram. По одному адресу или подсети (например 192.168.3.0/24) на строку. После адреса можно написать имя — оно будет видно во входах: <span class="num">192.168.3.123 мой ПК</span>.</div>
    <textarea id="known">${esc(s.settings.ssh_known.join("\n"))}</textarea>

    <div class="sect">Команды в Telegram</div>
    <div class="muted" style="font-size:12.5px">${s.bot ? "✅ Бот команд подключён: /status, /alerts, /mute, /unmute, /summary." : "Не настроены. Нужен отдельный бот (основной может опрашивать другая программа, тогда команды до Luxmon не дойдут): создай его у @BotFather и добавь <code>LUXMON_BOT_TOKEN=…</code> в /etc/luxmon/telegram.env на сервере Luxmon."}</div>

    <div class="row-end" style="margin-top:24px;position:sticky;bottom:0;background:var(--bg);padding:12px 0">
      <span class="${isErr ? "err-box" : "ok-box"}">${esc(msg || "")}</span>
      <button id="set-cancel">Закрыть</button><button class="on" id="set-save">Сохранить</button></div>`;
  $("#set-cancel").onclick = closeDrawer;
  $("#add-check").onclick = () => { s.checks = readChecks(); s.checks.push({name: "", type: "http", url: ""}); renderSettings(); };
  document.querySelectorAll("#checks-t [data-del]").forEach(b => b.onclick = () => { s.checks = readChecks(); s.checks.splice(+b.dataset.del, 1); renderSettings(); });
  document.querySelectorAll('#checks-t [data-k="type"]').forEach(sel => sel.onchange = () => { s.checks = readChecks(); renderSettings(); });
  $("#set-save").onclick = async () => {
    const body = {
      rules: Object.fromEntries([...document.querySelectorAll("[data-rule]")].map(i => [i.dataset.rule, +i.value])),
      checks: readChecks(), checks_runner: $("#runner").value,
      summary_time: $("#sumtime").value.trim(), weekly_report: $("#weekly").checked, backup_weekdays_only: $("#bk-weekdays").checked, ssh_known: $("#known").value.split("\n").map(x => x.trim()).filter(Boolean),
    };
    try { SET = await api("api/settings", body); renderSettings("Сохранено"); refresh(); }
    catch (e) { s.checks = body.checks; renderSettings(e.message, true); }
  };
}

/* --------------------------------------------------------------- maintenance */
const DURS = [[30, "30 мин"], [60, "1 ч"], [120, "2 ч"], [240, "4 ч"], [1440, "сутки"]];
let DUR = 60;
function openMaint() {
  if (!DATA) return;
  const opts = [["all", "Весь кластер"], ...DATA.hosts.map(h => ["host:" + h.name, "Хост " + h.name]),
                ["ceph", "Ceph"], ["patroni", "PostgreSQL и etcd"], ["checks", "Проверки сервисов"], ["backups", "Бэкапы"], ["ssh", "Входы по SSH"]];
  $("#m-scope").innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const drawDur = () => {
    $("#m-dur").innerHTML = DURS.map(([m, l]) => `<button data-m="${m}" class="${m == DUR ? "on" : ""}">${l}</button>`).join("");
    document.querySelectorAll("#m-dur button").forEach(b => b.onclick = () => { DUR = +b.dataset.m; drawDur(); });
  };
  drawDur();
  $("#m-reason").value = "";
  $("#modal").classList.add("open"); $("#overlay").classList.add("open");
}
function closeMaint() { $("#modal").classList.remove("open"); if (!OPEN) $("#overlay").classList.remove("open"); }
$("#maint").onclick = openMaint;
$("#settings").onclick = () => openDrawer("@settings");
$("#m-cancel").onclick = closeMaint;
$("#m-ok").onclick = async () => {
  try { await api("api/silence", {scope: $("#m-scope").value, minutes: DUR, reason: $("#m-reason").value.trim()}); }
  catch (e) { alert("Ошибка: " + e.message); }
  closeMaint(); refresh();
};
$("#overlay").onclick = () => { closePal(); closeMaint(); closeDrawer(); };
addEventListener("keydown", e => {
  if (e.key != "Escape") return;
  if ($("#pal").classList.contains("open")) return closePal();  // поиск закрывается первым, панель под ним остаётся
  closeMaint(); closeDrawer();
});

/* --------------------------------------------------------------- поиск (Ctrl+K) */
function palItems() {
  const d = DATA, admin = d.role != "viewer", out = [];
  const nodes = new Set((d.cluster.pve?.nodes || []).map(n => n.name));
  for (const h of d.hosts) out.push({t: h.name, s: `${nodes.has(h.name) ? "узел" : "хост"} · ${h.info?.os || ""} · ${(h.info?.ips || []).slice(0, 2).join(", ")}`, k: "графики cpu память диск сеть процессы журнал", kind: "хост", go: () => openDrawer(h.name)});
  for (const g of (d.cluster.pve?.guests || []).filter(g => !g.template))
    out.push({t: g.name, s: `${g.type == "lxc" ? "CT" : "VM"} ${g.vmid} на ${g.node} · ${g.status}`, k: `${g.vmid} vm ct гость`, kind: g.type == "lxc" ? "CT" : "VM", go: () => openDrawer("@vm:" + g.vmid)});
  const sect = (t, s, k, go, kind = "раздел") => out.push({t, s, k, kind, go});
  sect("Ceph", "графики OSD, пулы, нагрузка по дискам VM", "хранилище osd", () => openDrawer("@ceph"));
  sect("PostgreSQL и etcd", "Patroni, лаг реплик, подключения", "patroni база бд etcd", () => openDrawer("@patroni"));
  sect("Сервисы", "время ответа и доступность за 30 дней", "проверки checks uptime", () => openDrawer("@checks"));
  sect("История алертов", "что срабатывало и как долго", "алерты тревоги history", () => openDrawer("@history"));
  sect("Входы по SSH", "кто, откуда, неудачные попытки", "ssh логины вход", () => openDrawer("@ssh"));
  sect("Агенты и токены", "версии агентов, свои токены хостов", "agents токен", () => openDrawer("@agents"));
  sect("Бэкапы", "копии гостей, задания, PBS", "backup pbs vzdump", () => { closeDrawer(); scrollTo("backups-p"); });
  sect("Диски узлов", "SMART, износ SSD", "smart диски ssd hdd", () => { closeDrawer(); scrollTo("disks-p"); });
  sect("Схема кластера", "узлы и гости на них", "карта map ha", () => { closeDrawer(); scrollTo("map-p"); });
  if (admin) {
    sect("Настройки", "пороги алертов, проверки, сводка", "settings пороги", () => openDrawer("@settings"));
    sect("Режим обслуживания", "заглушить алерты на время работ", "mute тишина обслуживание", openMaint, "действие");
  }
  sect("Режим для экрана на стене", "крупно и без лишнего", "wall стена экран тв", () => setWall(!document.body.classList.contains("wall")), "действие");
  sect("Сменить тему", "тёмная, светлая, как в системе", "theme тема", () => $("#theme").click(), "действие");
  return out;
}
let PAL_SEL = 0, PAL_LIST = [];
function palRender() {
  const words = $("#pal-q").value.toLowerCase().split(/\s+/).filter(Boolean);
  // ищем и по русскому тексту, и по переводу
  PAL_LIST = palItems().filter(x => { const h = `${x.t} ${x.s} ${x.k} ${x.kind}`; return words.every(w => `${h} ${tr(h)}`.toLowerCase().includes(w)); })
    .sort((a, b) => words.length ? (b.t.toLowerCase().startsWith(words[0]) - a.t.toLowerCase().startsWith(words[0])) : 0).slice(0, 40);
  PAL_SEL = Math.min(PAL_SEL, Math.max(0, PAL_LIST.length - 1));
  $("#pal-l").innerHTML = PAL_LIST.map((x, i) => `<div class="pi ${i == PAL_SEL ? "sel" : ""}" data-i="${i}"><b>${esc(x.t)}</b><span>${esc(x.s)}</span><i>${esc(x.kind)}</i></div>`).join("")
    || `<div class="empty">ничего не найдено</div>`;
  $("#pal-l .sel")?.scrollIntoView({block: "nearest"});
  document.querySelectorAll("#pal-l .pi").forEach(el => el.onclick = () => palGo(+el.dataset.i));
}
function palGo(i) { const x = PAL_LIST[i]; closePal(); x?.go(); }
function openPal() {
  if (!DATA) return;
  $("#pal-q").value = ""; PAL_SEL = 0;
  $("#pal").classList.add("open"); $("#overlay").classList.add("open");
  palRender(); $("#pal-q").focus();
}
function closePal() { $("#pal").classList.remove("open"); if (!OPEN && !$("#modal").classList.contains("open")) $("#overlay").classList.remove("open"); }
$("#find").onclick = openPal;
$("#lang").textContent = LANG == "en" ? "RU" : "EN";
$("#lang").onclick = () => setLang(LANG == "en" ? "ru" : "en");
$("#pal-q").oninput = () => { PAL_SEL = 0; palRender(); };
$("#pal-q").onkeydown = e => {
  if (e.key == "ArrowDown" || e.key == "ArrowUp") { PAL_SEL = (PAL_SEL + (e.key == "ArrowDown" ? 1 : -1) + PAL_LIST.length) % Math.max(1, PAL_LIST.length); palRender(); e.preventDefault(); }
  else if (e.key == "Enter") palGo(PAL_SEL);
};
addEventListener("keydown", e => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() == "k" || e.key == "/" && !typing) { e.preventDefault(); $("#pal").classList.contains("open") ? closePal() : openPal(); }
});

/* --------------------------------------------------------------- wall mode, theme, loop */
const qs = new URLSearchParams(location.search);
function setWall(on) {
  document.body.classList.toggle("wall", on);
  try { localStorage.setItem("luxmon-wall", on ? "1" : ""); } catch (e) {}
  if (on && document.documentElement.requestFullscreen && !document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
  if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
}
let wallOn = qs.get("wall") == "1";
try { wallOn = wallOn || localStorage.getItem("luxmon-wall") == "1"; } catch (e) {}
document.body.classList.toggle("wall", wallOn);
$("#wall").onclick = () => setWall(!document.body.classList.contains("wall"));

const THEMES = ["auto", "dark", "light"];
function applyTheme(t) {
  if (t == "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  $("#theme").title = "Тема: " + {auto: "как в системе", dark: "тёмная", light: "светлая"}[t];
}
let theme = qs.get("theme");
if (!THEMES.includes(theme)) { try { theme = localStorage.getItem("luxmon-theme") || "auto"; } catch (e) { theme = "auto"; } }
applyTheme(theme);
$("#theme").onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
  try { localStorage.setItem("luxmon-theme", theme); } catch (e) {}
  applyTheme(theme);
  if (DATA) { render(DATA); if (OPEN && OPEN != "@settings") drawOpen(); }
};

// ссылка вида #/pve1, #/vm:105, #/ceph, #/settings открывает нужную панель;
// ?r=86400 — период, ?t=начало-конец — точный отрезок, ?c=86400 — сравнение
function openFromHash() {
  const [path, q] = location.hash.replace(/^#\/?/, "").split("?");
  const k = decodeURIComponent(path), p = new URLSearchParams(q || "");
  if (!k || !DATA) return;
  if (+p.get("r")) RANGE = +p.get("r");
  if (CMPS.some(([v]) => v == +p.get("c"))) CMP = +p.get("c");
  const t = (p.get("t") || "").split("-").map(Number), zoom = t.length == 2 && t[0] < t[1] ? t : null;
  const cluster = ["ceph", "patroni", "checks", "settings", "history", "ssh", "agents"].includes(k) || k.startsWith("vm:");
  if (cluster || DATA.hosts.some(h => h.name == k)) openDrawer(cluster ? "@" + k : k, zoom);
}
let tick = 0;
async function refresh() {
  try {
    const first = !DATA;
    render(await getJSON("api/overview"));
    if (first) setTimeout(() => document.body.classList.remove("entering"), 500);
    if (first) openFromHash();
    // графики в открытой панели обновляются реже, настройки — только вручную
    // пока пишут заметку, панель не перерисовываем: набранный текст пропал бы
    if (OPEN && !ZOOM && !["@settings", "@history", "@ssh", "@agents"].includes(OPEN) && $("#note-f")?.style.display != "flex" && ++tick % 3 == 0) drawOpen();
  } catch (e) {
    if (e.message == "login") return;
    $("#summary").className = "status crit";
    $("#summary").innerHTML = `<span class="dot crit"></span>сервер недоступен`;
    setState("crit", 0);
    $("#hero").innerHTML = `<div class="hero">${FLAME}<div class="hero-t"><div class="hero-h">Сервер Luxmon недоступен</div><div class="hero-s">${DATA ? `данные на экране устарели, последние — в ${fmt.time(DATA.now)}` : "данных пока нет"}</div></div></div>`;
  }
}
refresh();
setInterval(refresh, 10000);
let rs; addEventListener("resize", () => { clearTimeout(rs); rs = setTimeout(() => { if (DATA) balance(); OPEN && OPEN != "@settings" && drawOpen(); }, 200); });
