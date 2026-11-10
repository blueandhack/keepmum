// The /stats page: one HTML document with the counts in it, drawn in the browser (client below).
// No libraries and no requests of its own.

export function dashboard(data, who) {
  const json = (v) => JSON.stringify(v).replace(/</g, "\\u003c");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>keepmum counts</title>
<style>${CSS}</style>
</head>
<body>
<main class="wrap">
  <header class="top">
    <div class="brand"><span class="mark" aria-hidden="true">m</span><div><h1>keepmum counts</h1>
      <p class="sub">Downloads, installs in use and upgrades. Counts only: nothing about who.</p></div></div>
    <div class="who"><span id="who"></span><span id="generated"></span></div>
  </header>

  <div class="filters" role="group" aria-label="Time range">
    <button type="button" data-range="30">30 days</button>
    <button type="button" data-range="90">90 days</button>
    <button type="button" data-range="all">All</button>
    <span class="utc" id="tz">Days are UTC</span>
  </div>

  <section class="tiles" id="tiles" aria-label="Summary"></section>

  <section class="card wide">
    <div class="card-head"><h2>Installs in use</h2><p>Update checks per day, by app version. Each copy with automatic checks on checks once a day.</p></div>
    <div class="legend" id="lg-installs"></div>
    <div class="chart" id="ch-installs"></div>
  </section>

  <div class="grid2">
    <section class="card">
      <div class="card-head"><h2>Downloads</h2><p>New copies: from the website's button, and straight from GitHub (estimated from GitHub's totals).</p></div>
      <div class="legend" id="lg-downloads"></div>
      <div class="chart" id="ch-downloads"></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>Upgrades</h2><p>Updates the app downloaded, by the version it updated to.</p></div>
      <div class="legend" id="lg-updates"></div>
      <div class="chart" id="ch-updates"></div>
    </section>
  </div>

  <section class="card">
    <div class="card-head"><h2>Releases</h2><p>Each release's DMG: all of GitHub's downloads of it, the upgrades to it, and the copies on it yesterday.</p></div>
    <div class="table-wrap"><table id="releases"></table></div>
  </section>

  <details class="card">
    <summary>Every day, as numbers</summary>
    <div class="table-wrap"><table id="daily"></table></div>
  </details>

  <section class="notes">
    <h2>How these are counted</h2>
    <ul>
      <li><b>Installs in use</b> are update checks: keepmum.app/appcast.xml, once a day per copy with automatic checks on. Copies with checks off, and builds from 0.1.0a4 and earlier (which read the feed on GitHub), are not counted. Nothing is counted when the app opens.</li>
      <li><b>Downloads</b> from the website are keepmum.app/download. <b>From GitHub</b> is the growth in GitHub's own download counts, noted every six hours, less the website's downloads and the upgrades (both redirect to GitHub); it starts the day after the first note.</li>
      <li><b>Upgrades</b> are keepmum.app/update/…, where the update feed sends the app for a new version's DMG, from 0.1.0a5 on.</li>
      <li>Each count is a day, a kind, a version and a number. No IP address or user agent is kept.</li>
    </ul>
  </section>
</main>
<div class="tip" id="tip" role="status" hidden></div>
<script>(${client.toString()})(${json(data)}, ${json(who)});</script>
</body>
</html>`;
}

// Layout: one column of cards; summary tiles first, the daily-installs chart full width, then
// downloads and upgrades side by side, then the release table. Colors: the Mac app's surfaces and
// text; series from a colorblind-checked palette (blue, orange, aqua), older versions in gray.
const CSS = `
:root {
  color-scheme: light;
  --canvas: #ffffff; --surface: #f4f4f5; --raised: #ffffff; --sep: #e3e3e6;
  --text: #1d1d1f; --text-2: #6e6e73; --text-3: #8e8e93;
  --accent: #c93f17; --accent-ink: #ffffff;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --older: #7c7c82;
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: dark) { :root {
  color-scheme: dark;
  --canvas: #1e1e1e; --surface: #2a2a2c; --raised: #3a3a3c; --sep: #3a3a3c;
  --text: #f5f5f7; --text-2: #98989d; --text-3: #7c7c80;
  --accent: #ff7a4d; --accent-ink: #1e1e1e;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --older: #6c6c70;
} }
* { box-sizing: border-box; }
body { margin: 0; background: var(--canvas); color: var(--text); font: 14px/1.5 var(--font); }
.wrap { max-width: 1120px; margin: 0 auto; padding: 28px max(16px, env(safe-area-inset-left)) 56px; display: flex; flex-direction: column; gap: 18px; }
h1 { font-size: 20px; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 14px; margin: 0; }
p { margin: 0; }
.top { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: flex-end; justify-content: space-between; }
.brand { display: flex; gap: 12px; align-items: center; }
.mark { width: 36px; height: 36px; border-radius: 9px; background: var(--accent); color: var(--accent-ink); display: grid; place-items: center; font-weight: 700; font-size: 20px; }
.sub, .who { color: var(--text-2); font-size: 12.5px; }
.who { display: flex; flex-direction: column; align-items: flex-end; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filters button { appearance: none; border: 0; background: var(--surface); color: var(--text); font: 500 12.5px var(--font); padding: 6px 12px; border-radius: 999px; cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--text); color: var(--canvas); }
.filters button:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.utc { margin-left: auto; color: var(--text-3); font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr)); gap: 12px; }
.tile { background: var(--surface); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.tile .label { font-size: 12px; font-weight: 600; color: var(--text-2); letter-spacing: 0.02em; }
.tile .value { font-size: 30px; font-weight: 650; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; line-height: 1.15; }
.tile .caption { font-size: 12px; color: var(--text-2); }
.tile svg { margin-top: 4px; }
.card { background: var(--surface); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card-head p { color: var(--text-2); font-size: 12.5px; margin-top: 2px; }
.grid2 .card-head { flex: 1; } /* side-by-side charts share a baseline, however their text wraps */
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr)); gap: 18px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--text-2); min-height: 18px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.chart { position: relative; height: 220px; }
.chart svg { display: block; overflow: visible; }
.empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--text-3); font-size: 13px; text-align: center; padding: 0 24px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--sep); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-2); font-weight: 600; font-size: 12px; }
tr:last-child td { border-bottom: 0; }
summary { cursor: pointer; font-weight: 600; }
.notes { color: var(--text-2); font-size: 12.5px; }
.notes ul { margin: 6px 0 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; max-width: 90ch; }
.notes b { color: var(--text); font-weight: 600; }
.tip { position: fixed; pointer-events: none; z-index: 5; background: var(--raised); color: var(--text); border: 1px solid var(--sep); border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.14); min-width: 150px; }
.tip .d { font-weight: 600; margin-bottom: 4px; }
.tip .local { color: var(--text-3); margin: -4px 0 4px; }
.tip .row { display: flex; gap: 10px; justify-content: space-between; font-variant-numeric: tabular-nums; }
.tip .row span:first-child { color: var(--text-2); }
.tip i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }
.tip .total { border-top: 1px solid var(--sep); margin-top: 4px; padding-top: 4px; font-weight: 600; }
`;

// --- In the browser -----------------------------------------------------------------------------

function client(DATA, WHO) {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmt = (n) => (n == null ? "—" : n.toLocaleString("en-US"));
  const sum = (a) => a.reduce((s, x) => s + (x || 0), 0);
  const DAY = 86400000;
  const iso = (t) => new Date(t).toISOString().slice(0, 10);
  const todayISO = iso(Date.now());
  const yesterdayISO = iso(Date.now() - DAY);
  const label = (d, long) => new Date(d + "T00:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC", ...(long ? { weekday: "short" } : {}) });
  // The counts are kept per UTC day; the page says when each one starts and ends in the browser's
  // own time zone.
  const at = (t, opts) => new Date(t).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", ...opts });
  const isUTC = (t) => new Date(t).getTimezoneOffset() === 0;
  const zone = (t) => new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(new Date(t)).find((p) => p.type === "timeZoneName")?.value || "";
  function span(d) {
    const s = Date.parse(d + "T00:00:00Z"), e = s + DAY;
    return isUTC(s) && isUTC(e) ? "" : `${at(s, { weekday: "short" })} – ${at(e, { weekday: "short" })} ${zone(e)}`;
  }

  // keepmum's versions in release order: 0.1.0a4 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 (as build-app.sh numbers them).
  function vkey(v) {
    const m = /^(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?$/.exec(v);
    if (!m) return -1;
    const pre = m[4] ? { a: 0, b: 30, rc: 60 }[m[4]] + Number(m[5]) : 99;
    return Number(m[1]) * 1e6 + Number(m[2]) * 1e4 + Number(m[3] || 0) * 100 + pre;
  }

  // --- the numbers, by day ---
  const counts = DATA.counts || [], releases = DATA.releases || [];
  const by = (kind) => {
    const m = new Map();
    for (const r of counts) if (r.kind === kind) {
      const d = m.get(r.day) || new Map();
      d.set(r.version, (d.get(r.version) || 0) + r.n);
      m.set(r.day, d);
    }
    return m;
  };
  const checks = by("check"), updates = by("update"), site = by("download");
  const siteOn = (d) => (site.get(d) ? sum([...site.get(d).values()]) : 0);
  const updatesOn = (d) => (updates.get(d) ? sum([...updates.get(d).values()]) : 0);
  const checksOn = (d) => (checks.get(d) ? sum([...checks.get(d).values()]) : 0);

  // GitHub's totals, noted per day: each day's growth over the day noted before it.
  const ghDays = [...new Set(releases.map((r) => r.day))].sort();
  const ghTotal = new Map(), latestPerVersion = new Map();
  for (const d of ghDays) {
    for (const r of releases) if (r.day === d) latestPerVersion.set(r.version, r.n);
    ghTotal.set(d, sum([...latestPerVersion.values()]));
  }
  const ghGrowth = new Map();
  ghDays.forEach((d, i) => { if (i > 0) ghGrowth.set(d, ghTotal.get(d) - ghTotal.get(ghDays[i - 1])); });
  const directOn = (d) => (ghGrowth.has(d) ? Math.max(0, ghGrowth.get(d) - siteOn(d) - updatesOn(d)) : null);

  // Versions get their color once, from all the data, so a range change never repaints them:
  // the three newest have a hue each, the rest are "Older".
  const allVersions = [...new Set(counts.filter((r) => r.kind !== "download").map((r) => r.version))]
    .sort((a, b) => vkey(b) - vkey(a));
  const named = allVersions.filter((v) => vkey(v) >= 0).slice(0, 3);
  const slot = ["--s1", "--s2", "--s3"];
  const newest = named[0];
  function versionSeries(map, days) {
    const series = named.map((v, i) => ({ name: v, color: css(slot[i]), values: days.map((d) => map.get(d)?.get(v) || 0) }));
    const older = days.map((d) => {
      const m = map.get(d);
      return m ? sum([...m.entries()].filter(([v]) => !named.includes(v)).map(([, n]) => n)) : 0;
    });
    if (sum(older) > 0) series.push({ name: "Older", color: css("--older"), values: older });
    return series.filter((s) => sum(s.values) > 0);
  }

  // --- range ---
  const firstDay = [...counts.map((r) => r.day), ...ghDays].sort()[0] || todayISO;
  function daysFor(range) {
    const start = range === "all" ? Date.parse(firstDay + "T00:00:00Z") : Date.parse(todayISO + "T00:00:00Z") - (Number(range) - 1) * DAY;
    const out = [];
    for (let t = start; t <= Date.parse(todayISO + "T00:00:00Z"); t += DAY) out.push(iso(t));
    return out.length ? out : [todayISO];
  }

  // --- drawing ---
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  };
  function niceMax(v) {
    if (v <= 4) return 4;
    const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  const tip = document.getElementById("tip");
  function showTip(evt, day, series, i, extra) {
    const rows = series.filter((s) => s.values[i] != null).map((s) =>
      `<div class="row"><span><i style="background:${s.color}"></i>${s.name}</span><span>${fmt(s.values[i])}</span></div>`).join("");
    const measured = series.some((s) => s.values[i] != null);
    const total = measured && series.length > 1 ? `<div class="row total"><span>Total</span><span>${fmt(sum(series.map((s) => s.values[i])))}</span></div>` : "";
    const local = span(day);
    tip.innerHTML = `<div class="d">${label(day, true)}${day === todayISO ? " (so far)" : ""}</div>${local ? `<div class="local">${local}</div>` : ""}${rows || ""}${total}${extra || ""}`;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + 14, y = evt.clientY + 14;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // A version chart always names its versions, even one: its title does not.
  function legend(id, series, always) {
    const box = document.getElementById(id);
    box.innerHTML = series.length > 1 || (always && series.length) ? series.map((s) => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("") : "";
  }

  /** A stacked bar or area chart over days, with a hover tooltip per day. */
  function chart(id, days, series, kind, empty, extraFor) {
    const box = document.getElementById(id);
    box.innerHTML = "";
    const W = box.clientWidth, H = box.clientHeight;
    const m = { l: 40, r: 8, t: 8, b: 24 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const totals = days.map((_, i) => sum(series.map((s) => s.values[i])));
    if (!series.length || totals.every((t) => !t)) {
      const e = document.createElement("div");
      e.className = "empty"; e.textContent = empty;
      box.appendChild(e);
    }
    const max = niceMax(Math.max(0, ...totals));
    const svg = el("svg", { width: W, height: H, role: "img", "aria-label": box.previousElementSibling?.previousElementSibling?.querySelector("h2")?.textContent || "" }, box);
    const y = (v) => m.t + ph - (v / max) * ph;
    const band = pw / days.length;
    const x = (i) => m.l + band * i + band / 2;
    // recessive grid and axis labels
    const steps = max % 4 === 0 ? 4 : max % 5 === 0 ? 5 : 2; // whole-number ticks: counts have no halves
    for (let k = 0; k <= steps; k++) {
      const v = (max / steps) * k;
      el("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: css("--sep"), "stroke-width": 1 }, svg);
      const t = el("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end", fill: css("--text-3"), "font-size": 11 }, svg);
      t.textContent = Number.isInteger(v) ? fmt(v) : v.toFixed(1);
    }
    const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(pw / 70))));
    days.forEach((d, i) => {
      if (i % every && i !== days.length - 1) return;
      if (i !== days.length - 1 && days.length - 1 - i < every / 2) return; // keep clear of the last label
      const t = el("text", { x: x(i), y: H - 6, "text-anchor": "middle", fill: css("--text-3"), "font-size": 11 }, svg);
      t.textContent = label(d);
    });
    const hover = el("rect", { x: 0, y: m.t, width: 0, height: ph, fill: css("--text"), opacity: 0 }, svg);
    const surface = css("--surface");
    if (kind === "bar" || days.length < 3) {
      const bw = Math.max(2, Math.min(22, band * 0.68));
      days.forEach((_, i) => {
        let base = 0;
        const segs = series.map((s) => ({ s, v: s.values[i] || 0 })).filter((g) => g.v > 0);
        segs.forEach((g, j) => {
          const y0 = y(base), y1 = y(base + g.v);
          base += g.v;
          const top = j === segs.length - 1;
          const h = Math.max(1, y0 - y1 - (j > 0 ? 2 : 0)); // a 2px gap between stacked segments
          const r = top ? Math.min(4, bw / 2, h) : 0;
          const x0 = x(i) - bw / 2, yt = y0 - (j > 0 ? 2 : 0) - h;
          el("path", { d: `M${x0},${yt + h} V${yt + r} Q${x0},${yt} ${x0 + r},${yt} H${x0 + bw - r} Q${x0 + bw},${yt} ${x0 + bw},${yt + r} V${yt + h} Z`, fill: g.s.color }, svg);
        });
      });
    } else {
      // stacked areas, bottom series first; a 2px surface line separates the layers
      let lower = days.map(() => 0);
      const layers = series.map((s) => {
        const upper = lower.map((b, i) => b + (s.values[i] || 0));
        const l = { s, lower, upper };
        lower = upper;
        return l;
      });
      for (const l of layers) {
        const top = l.upper.map((v, i) => `${x(i)},${y(v)}`).join(" L");
        const bottom = l.lower.map((v, i) => `${x(i)},${y(v)}`).reverse().join(" L");
        el("path", { d: `M${top} L${bottom} Z`, fill: l.s.color, opacity: 0.9 }, svg);
      }
      for (const l of layers) {
        el("path", { d: "M" + l.upper.map((v, i) => `${x(i)},${y(v)}`).join(" L"), fill: "none", stroke: surface, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
      }
    }
    const cross = el("line", { x1: 0, x2: 0, y1: m.t, y2: m.t + ph, stroke: css("--text-2"), "stroke-width": 1, opacity: 0 }, svg);
    const hit = el("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (evt) => {
      const bx = svg.getBoundingClientRect().left;
      const i = Math.min(days.length - 1, Math.max(0, Math.floor((evt.clientX - bx - m.l) / band)));
      if (kind === "bar" || days.length < 3) {
        hover.setAttribute("x", m.l + band * i); hover.setAttribute("width", band); hover.setAttribute("opacity", 0.06);
      } else {
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 0.6);
      }
      showTip(evt, days[i], series.length ? series : [{ name: "Count", color: css("--older"), values: days.map(() => 0) }], i, extraFor && extraFor(days[i]));
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("opacity", 0); cross.setAttribute("opacity", 0); hideTip(); });
  }

  function sparkline(values, color) {
    const W = 200, H = 28, max = Math.max(1, ...values);
    const pts = values.map((v, i) => `${(i / Math.max(1, values.length - 1)) * W},${H - 2 - (v / max) * (H - 4)}`).join(" ");
    return `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
  }

  // --- the page ---
  function render(range) {
    for (const b of document.querySelectorAll("[data-range]")) b.setAttribute("aria-pressed", String(b.dataset.range === range));
    const days = daysFor(range);
    const full = days.filter((d) => d !== todayISO);

    const installs = versionSeries(checks, days);
    const yesterday = checksOn(yesterdayISO);
    const siteN = sum(days.map(siteOn)), directN = sum(days.map((d) => directOn(d) || 0)), updN = sum(days.map(updatesOn));
    const onNewest = newest && yesterday ? (checks.get(yesterdayISO)?.get(newest) || 0) / yesterday : null;
    const tiles = [
      { label: "Installs in use", value: yesterday ? fmt(yesterday) : "—",
        caption: yesterday ? `Update checks on ${label(yesterdayISO)} · ${fmt(checksOn(todayISO))} today so far` : "Counted from 0.1.0a5 on",
        spark: full.length > 1 ? sparkline(full.map(checksOn), css("--s1")) : "" },
      { label: "Downloads", value: fmt(siteN + directN), caption: `${fmt(siteN)} from the website · ${fmt(directN)} from GitHub` },
      { label: "Upgrades", value: fmt(updN), caption: newest ? `${fmt(sum(days.map((d) => updates.get(d)?.get(newest) || 0)))} to ${newest}, the newest` : "Counted from 0.1.0a5 on" },
      { label: "On the newest version", value: onNewest == null ? "—" : Math.round(onNewest * 100) + "%",
        caption: newest ? `${newest}, of yesterday's installs in use` : "No version seen yet" },
    ];
    document.getElementById("tiles").innerHTML = tiles.map((t) =>
      `<div class="tile"><span class="label">${t.label}</span><span class="value">${t.value}</span><span class="caption">${t.caption}</span>${t.spark || ""}</div>`).join("");

    legend("lg-installs", installs, true);
    chart("ch-installs", days, installs, "area", "No update checks yet. Builds from 0.1.0a5 on check keepmum.app.");

    const downloads = [
      { name: "Website", color: css("--s1"), values: days.map(siteOn) },
      { name: "GitHub", color: css("--s2"), values: days.map(directOn) },
    ];
    legend("lg-downloads", downloads);
    chart("ch-downloads", days, downloads, "bar", "No downloads in this range yet.",
      (d) => (ghGrowth.has(d) ? "" : `<div class="row"><span>GitHub</span><span>not noted yet</span></div>`));

    const ups = versionSeries(updates, days);
    legend("lg-updates", ups, true);
    chart("ch-updates", days, ups, "bar", "No upgrades yet. Releases from 0.1.0a5 on are counted.");

    // releases, newest first
    const vs = [...new Set([...latestPerVersion.keys(), ...allVersions.filter((v) => vkey(v) >= 0)])].sort((a, b) => vkey(b) - vkey(a));
    const upTotal = (v) => sum(counts.filter((r) => r.kind === "update" && r.version === v).map((r) => r.n));
    document.getElementById("releases").innerHTML =
      `<thead><tr><th>Version</th><th>GitHub downloads</th><th>Upgrades to it</th><th>Installs on it yesterday</th></tr></thead><tbody>` +
      (vs.length ? vs.map((v) => `<tr><td>${v}</td><td>${fmt(latestPerVersion.get(v) ?? null)}</td><td>${fmt(upTotal(v))}</td><td>${fmt(checks.get(yesterdayISO)?.get(v) || 0)}</td></tr>`).join("")
        : `<tr><td colspan="4">No releases noted yet: GitHub's counts are noted every six hours.</td></tr>`) + "</tbody>";

    document.getElementById("daily").innerHTML =
      `<thead><tr><th>Day</th><th>Installs in use</th><th>Website downloads</th><th>GitHub downloads</th><th>Upgrades</th></tr></thead><tbody>` +
      [...days].reverse().map((d) => `<tr><td>${label(d, true)}${d === todayISO ? " (so far)" : ""}</td><td>${fmt(checksOn(d))}</td><td>${fmt(siteOn(d))}</td><td>${fmt(directOn(d))}</td><td>${fmt(updatesOn(d))}</td></tr>`).join("") + "</tbody>";
  }

  document.getElementById("who").textContent = WHO;
  document.getElementById("generated").textContent = "As of " + at(DATA.generated, { year: "numeric", month: "short", day: "numeric", timeZoneName: "short" });
  const tz = document.getElementById("tz"), dayStart = Date.parse(todayISO + "T00:00:00Z");
  if (!isUTC(dayStart)) tz.textContent = `Days are UTC: they start at ${at(dayStart, { timeZoneName: "short" })}, your time`;
  try { tz.title = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch {}
  let range = "30";
  try { range = localStorage.getItem("range") || "30"; } catch {}
  for (const b of document.querySelectorAll("[data-range]")) {
    b.addEventListener("click", () => { range = b.dataset.range; try { localStorage.setItem("range", range); } catch {} render(range); });
  }
  render(range);
  let t;
  addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => render(range), 120); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => render(range));
}
