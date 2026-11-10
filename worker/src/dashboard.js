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
<script>try { var t = localStorage.getItem("theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}</script>
<style>${CSS}</style>
</head>
<body>
<main class="wrap">
  <header class="top">
    <div class="brand">
      <span class="mark" aria-hidden="true"><svg viewBox="0 0 100 100" width="22" height="22"><path d="M21.6 69.8 L21.6 46.3 C21.6 37 27.8 30.9 35.8 30.9 C43.8 30.9 50 37 50 46.3 L50 69.8 M50 69.8 L50 46.3 C50 37 56.2 30.9 64.2 30.9 C72.2 30.9 78.4 37 78.4 46.3 L78.4 69.8 M12.9 69.8 L87.1 69.8" fill="none" stroke="currentColor" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
      <div><h1>keepmum counts</h1><p class="sub">Active Macs, releases, downloads and upgrades. Counts only: nothing about who.</p></div>
    </div>
    <div class="meta">
      <div class="seg" role="group" aria-label="Appearance">
        <button type="button" data-theme-set="auto" title="Match the system">Auto</button>
        <button type="button" data-theme-set="light" title="Light" aria-label="Light"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>
        <button type="button" data-theme-set="dark" title="Dark" aria-label="Dark"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z"/></svg></button>
      </div>
      <div class="who"><span id="who"></span><span id="generated"></span></div>
    </div>
  </header>

  <div class="filters">
    <div class="seg" role="group" aria-label="Time range">
      <button type="button" data-range="30">30 days</button>
      <button type="button" data-range="90">90 days</button>
      <button type="button" data-range="all">All</button>
    </div>
    <span class="tz" id="tz">Days in your time zone</span>
  </div>

  <section class="kpis" id="kpis" aria-label="Active Macs"></section>
  <section class="kpis minor" id="minor" aria-label="Downloads and upgrades"></section>

  <section class="card">
    <div class="card-head">
      <div><h2>Active Macs per day</h2><p>Each counted copy of keepmum once a day, by app version. Dashed lines mark releases.</p></div>
      <div class="legend" id="lg-active"></div>
    </div>
    <div class="chart tall" id="ch-active"></div>
  </section>

  <section class="card">
    <div class="card-head">
      <div><h2>Version adoption</h2><p>Each version's share of the day's active Macs: how fast a release takes over.</p></div>
      <div class="legend" id="lg-adoption"></div>
    </div>
    <div class="chart" id="ch-adoption"></div>
  </section>

  <div class="grid2">
    <section class="card">
      <div class="card-head"><div><h2>Active Macs per week</h2><p id="weekly-note">Each counted copy once a week. Weeks start Monday, UTC.</p></div></div>
      <div class="chart" id="ch-weekly"></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Versions in use</h2><p id="versions-note">Yesterday's active Macs, by app version.</p></div></div>
      <div class="versions" id="versions"></div>
    </section>
    <section class="card">
      <div class="card-head">
        <div><h2>Downloads</h2><p>From the website's button, and straight from GitHub (estimated from its totals).</p></div>
        <div class="legend" id="lg-downloads"></div>
      </div>
      <div class="chart" id="ch-downloads"></div>
    </section>
    <section class="card">
      <div class="card-head">
        <div><h2>New installs and upgrades</h2><p>A Mac's first check ever, and the updates the app downloaded.</p></div>
        <div class="legend" id="lg-installs"></div>
      </div>
      <div class="chart" id="ch-installs"></div>
    </section>
  </div>

  <div class="grid3">
    <section class="card">
      <div class="card-head"><div><h2>Upgrade paths</h2><p>Updates in this range, by the version they came from.</p></div></div>
      <div class="bars" id="paths"></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Where downloads come from</h2><p>Website downloads in this range, by the site that sent the visitor.</p></div></div>
      <div class="bars" id="via"></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Systems</h2><p>Active Macs in this range by macOS version and chip, each Mac once a day.</p></div></div>
      <div class="systems" id="systems"></div>
    </section>
  </div>

  <section class="card">
    <div class="card-head"><div><h2>Releases</h2><p>Newest first: when each shipped, GitHub's downloads of its DMG, the upgrades to it, and its share of yesterday's active Macs.</p></div></div>
    <div class="releases" id="releases"></div>
  </section>

  <details class="card">
    <summary>Every day, as numbers</summary>
    <div class="table-wrap"><table id="daily"></table></div>
  </details>

  <section class="notes">
    <h2>How these are counted</h2>
    <ul>
      <li><b>Active Macs</b> come from update checks: keepmum.app/appcast.xml, which each copy with automatic checks on fetches once a day. From 0.1.0a7 on, a check says whether it is the Mac's first of the UTC day, week or month, so each Mac counts once however often it checks (Check now, Install…). Older versions are counted by their checks, which can count a Mac twice on a day it checks more than once. Copies with checks off or Count this Mac off, and 0.1.0a4 and earlier, are not counted. Nothing is counted when the app opens.</li>
      <li><b>New installs</b> are a Mac's first check ever, from 0.1.0a7 on.</li>
      <li><b>Downloads</b> from the website are keepmum.app/download. <b>From GitHub</b> is the growth in GitHub's own download counts, noted every six hours, less the website's downloads and the upgrades (both redirect to GitHub); it starts the day after the first note.</li>
      <li><b>Upgrades</b> are keepmum.app/update/…, where the update feed sends the app for a new version's DMG, from 0.1.0a5 on. <b>Upgrade paths</b> take the version it came from out of the app's update library's user agent, which is read and not kept.</li>
      <li><b>Where downloads come from</b> is the domain of the page that sent the visitor to keepmum.app, which the site adds to its Download buttons (never the page or anything after the domain); <i>keepmum.app</i> means they came from another page of the site, <i>direct</i> that there was none.</li>
      <li><b>Systems</b> come from the day's first update check, which from 0.1.0a7 on also says the Mac's macOS major version and chip.</li>
      <li><b>Releases</b> are noted when the update feed first lists them, with the date the feed gives; GitHub's counts are noted at that moment too.</li>
      <li>Bots, link previews and scripts are not counted; the user agent is read to tell, never kept. A check counts only from the app itself.</li>
      <li>Each count is an hour (UTC), a kind, a version and a number, added up here into days in your time zone. Counts from before hours were kept are whole UTC days. No IP address, user agent or identifier is kept.</li>
    </ul>
  </section>
</main>
<div class="tip" id="tip" role="status" hidden></div>
<script>(${client.toString()})(${json(data)}, ${json(who)});</script>
</body>
</html>`;
}

// Layout: a header with the appearance switch; the active-Mac numbers as large tiles, downloads and
// upgrades as smaller ones; the daily chart full width; then four cards two by two; tables; notes.
// Colors are tokens on :root, light by default, dark from the system or the switch. Series come
// from a colorblind-checked palette (blue, orange, aqua), stepped for each mode; older versions gray.
const DARK = `
  color-scheme: dark;
  --canvas: #0f0f11; --glow: rgba(255, 122, 77, 0.10); --card: #19191c; --card-2: #212125; --border: #2a2a2f; --sep: #2c2c31;
  --text: #f4f4f6; --text-2: #a1a1a8; --text-3: #75757d;
  --accent: #ff7a4d; --accent-2: #ffb08f; --accent-ink: #160a05;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --older: #6c6c73;
  --up: #4cc38a; --up-bg: rgba(76, 195, 138, 0.14); --down: #ff7b72; --down-bg: rgba(255, 123, 114, 0.14);
  --shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset, 0 8px 24px rgba(0, 0, 0, 0.35);
`;
const CSS = `
:root {
  color-scheme: light;
  --canvas: #f5f5f7; --glow: rgba(228, 87, 46, 0.10); --card: #ffffff; --card-2: #f3f3f5; --border: #e6e6ea; --sep: #ececf0;
  --text: #17171a; --text-2: #5f5f68; --text-3: #8b8b94;
  --accent: #c93f17; --accent-2: #e4572e; --accent-ink: #ffffff;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --older: #6e6e76; /* validated: CVD ΔE ≥ 9.2, normal ≥ 18 against its neighbour */
  --up: #12804a; --up-bg: rgba(18, 128, 74, 0.10); --down: #c4322b; --down-bg: rgba(196, 50, 43, 0.09);
  --shadow: 0 1px 2px rgba(20, 20, 30, 0.04), 0 6px 20px rgba(20, 20, 30, 0.05);
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif;
  --display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { ${DARK} } }
:root[data-theme="dark"] { ${DARK} }
* { box-sizing: border-box; }
html { background: var(--canvas); }
body { margin: 0; color: var(--text); font: 14px/1.5 var(--font); -webkit-font-smoothing: antialiased;
  background: radial-gradient(900px 420px at 0% -8%, var(--glow), transparent 70%), radial-gradient(700px 360px at 100% -12%, var(--glow), transparent 65%), var(--canvas);
  background-repeat: no-repeat; min-height: 100vh; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px max(16px, env(safe-area-inset-left)) 64px; display: flex; flex-direction: column; gap: 20px; }
h1 { font: 700 22px/1.15 var(--display); margin: 0; letter-spacing: -0.02em; }
h2 { font-size: 14.5px; font-weight: 650; margin: 0; letter-spacing: -0.005em; }
p { margin: 0; }
button { font: inherit; color: inherit; }

.top { display: flex; flex-wrap: wrap; gap: 16px 24px; align-items: center; justify-content: space-between; }
.brand { display: flex; gap: 14px; align-items: center; min-width: 0; }
.mark { width: 44px; height: 44px; flex: none; border-radius: 12px; display: grid; place-items: center; color: var(--accent-ink);
  background: linear-gradient(145deg, var(--accent-2), var(--accent)); box-shadow: 0 6px 18px var(--glow), inset 0 1px 0 rgba(255, 255, 255, 0.25); }
.sub { color: var(--text-2); font-size: 13px; margin-top: 3px; }
.meta { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.who { display: flex; flex-direction: column; align-items: flex-end; color: var(--text-2); font-size: 12.5px; line-height: 1.4; }
@media (max-width: 640px) { .who { align-items: flex-start; } }

.seg { display: inline-flex; padding: 3px; gap: 2px; border-radius: 999px; background: var(--card-2); border: 1px solid var(--border); }
.seg button { appearance: none; border: 0; background: transparent; color: var(--text-2); font-size: 12.5px; font-weight: 550; padding: 5px 12px; border-radius: 999px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; min-height: 28px; transition: background 0.15s, color 0.15s; }
.seg button:hover { color: var(--text); }
.seg button[aria-pressed="true"] { background: var(--card); color: var(--text); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12); }
.seg button:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; justify-content: space-between; }
.tz { color: var(--text-3); font-size: 12px; }

.card, .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 14px; }
.kpi { padding: 16px 18px 14px; display: flex; flex-direction: column; gap: 6px; min-width: 0; position: relative; overflow: hidden; }
.kpi.hero { background: linear-gradient(160deg, color-mix(in srgb, var(--accent) 9%, var(--card)), var(--card) 60%); }
.kpi .label { font-size: 12px; font-weight: 600; color: var(--text-2); display: flex; align-items: center; gap: 8px; }
.kpi .row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.kpi .value { font: 700 34px/1.1 var(--display); letter-spacing: -0.03em; font-variant-numeric: tabular-nums; }
.kpi .caption { font-size: 12px; color: var(--text-2); }
.kpi svg.spark { display: block; margin-top: 2px; }
.minor .kpi { padding: 12px 16px; flex-direction: row; align-items: center; justify-content: space-between; gap: 12px; }
.minor .kpi .value { font-size: 22px; }
.minor .kpi .text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.delta { font-size: 11.5px; font-weight: 650; padding: 2px 7px; border-radius: 999px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.delta.up { color: var(--up); background: var(--up-bg); }
.delta.down { color: var(--down); background: var(--down-bg); }
.delta.flat { color: var(--text-2); background: var(--card-2); }

.card { padding: 18px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.card-head { display: flex; flex-wrap: wrap; gap: 8px 20px; justify-content: space-between; align-items: flex-start; }
.card-head p { color: var(--text-2); font-size: 12.5px; margin-top: 3px; max-width: 62ch; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr)); gap: 20px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--text-2); }
.legend i, .tip i, .versions i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.chart { position: relative; height: 220px; }
.chart.tall { height: 280px; }
.chart svg { display: block; overflow: visible; }
.reveal { animation: reveal 0.9s cubic-bezier(0.2, 0.7, 0.2, 1) both; }
@keyframes reveal { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }
@media (prefers-reduced-motion: reduce) { .reveal { animation: none; } .seg button { transition: none; } }
.empty { position: absolute; inset: 0 0 24px 40px; display: grid; place-items: center; color: var(--text-3); font-size: 13px; text-align: center; padding: 0 24px; }

.versions { display: flex; flex-direction: column; gap: 14px; justify-content: center; flex: 1; }
.versions .bar { display: flex; gap: 2px; height: 28px; border-radius: 8px; overflow: hidden; }
.versions .bar span { display: block; min-width: 3px; transition: filter 0.15s; }
.versions .bar span:hover { filter: brightness(1.12); }
.versions ul { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 16px; }
.versions li { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; padding: 6px 0; border-bottom: 1px solid var(--sep); }
.versions li b { font-variant-numeric: tabular-nums; font-weight: 600; }
.versions li small { color: var(--text-3); font-size: 12px; margin-left: 6px; }
.versions .none, .bars .none, .systems .none, .releases .none { color: var(--text-3); text-align: center; font-size: 13px; padding: 32px 0; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 20px; }

/* A ranked list with a bar each: one series, so one color; the label and number carry the meaning. */
.bars { display: flex; flex-direction: column; gap: 10px; }
.bars .item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; align-items: baseline; font-size: 13px; }
.bars .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bars .name .arrow { color: var(--text-3); margin: 0 5px; }
.bars .n { font-variant-numeric: tabular-nums; font-weight: 600; }
.bars .n small { color: var(--text-3); font-weight: 500; font-size: 12px; margin-left: 6px; }
.bars .track { grid-column: 1 / -1; height: 8px; border-radius: 4px; background: var(--card-2); overflow: hidden; }
.bars .fill { height: 100%; border-radius: 4px; background: var(--s1); transform-origin: left; }
.bars .fill.muted { background: var(--older); }
.bars .item:hover .fill { filter: brightness(1.1); }
.grow { transform-origin: left; animation: grow 0.8s cubic-bezier(0.2, 0.7, 0.2, 1) both; }
@keyframes grow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@media (prefers-reduced-motion: reduce) { .grow { animation: none; } }

.systems { display: flex; flex-direction: column; gap: 18px; }
.systems h3 { font-size: 12px; font-weight: 600; color: var(--text-2); margin: 0 0 8px; }
.split { display: flex; gap: 2px; height: 28px; border-radius: 8px; overflow: hidden; }
.split span { display: block; min-width: 3px; }
.split span:hover { filter: brightness(1.12); }
.split + .key { display: flex; justify-content: space-between; gap: 10px; margin-top: 6px; font-size: 12.5px; color: var(--text-2); }
.split + .key b { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 600; }

/* Releases: one row each, newest first, with its share of yesterday's active Macs. */
.releases { display: flex; flex-direction: column; }
.rel { display: grid; grid-template-columns: minmax(150px, 1.2fr) repeat(3, minmax(90px, 0.7fr)) minmax(160px, 1.4fr); gap: 6px 18px; align-items: center; padding: 12px 4px; border-bottom: 1px solid var(--sep); font-size: 13px; }
.rel:last-child { border-bottom: 0; }
.rel .v { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.rel .v b { font-size: 14px; font-weight: 650; display: flex; align-items: center; gap: 8px; }
.rel .v b i { width: 10px; height: 10px; border-radius: 3px; flex: none; }
.rel .v span, .rel .m span { color: var(--text-3); font-size: 12px; }
.rel .m { display: flex; flex-direction: column; gap: 1px; font-variant-numeric: tabular-nums; }
.rel .m b { font-weight: 600; font-size: 14px; }
.rel .share { display: flex; align-items: center; gap: 10px; }
.rel .share .track { flex: 1; height: 8px; border-radius: 4px; background: var(--card-2); overflow: hidden; }
.rel .share .fill { height: 100%; border-radius: 4px; }
.rel .share em { font-style: normal; font-variant-numeric: tabular-nums; font-weight: 600; min-width: 3.2em; text-align: right; }
.badge { font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; padding: 2px 7px; border-radius: 999px; color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
@media (max-width: 760px) { .rel { grid-template-columns: 1fr 1fr 1fr; } .rel .v, .rel .share { grid-column: 1 / -1; } }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; }
th, td { text-align: right; padding: 8px 10px; border-bottom: 1px solid var(--sep); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-3); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em; }
tbody tr:hover td { background: var(--card-2); }
tr:last-child td { border-bottom: 0; }
summary { cursor: pointer; font-weight: 650; }
.notes { color: var(--text-2); font-size: 12.5px; padding: 0 4px; }
.notes ul { margin: 8px 0 0; padding-left: 18px; display: flex; flex-direction: column; gap: 5px; max-width: 95ch; }
.notes b { color: var(--text); font-weight: 600; }

.tip { position: fixed; pointer-events: none; z-index: 5; background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 10px; padding: 9px 11px; font-size: 12px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18); min-width: 160px; }
.tip .d { font-weight: 650; margin-bottom: 5px; }
.tip .r { display: flex; gap: 12px; justify-content: space-between; font-variant-numeric: tabular-nums; }
.tip .r span:first-child { color: var(--text-2); }
.tip .total { border-top: 1px solid var(--sep); margin-top: 5px; padding-top: 5px; font-weight: 650; }
`;

// --- In the browser -----------------------------------------------------------------------------

function client(DATA, WHO) {
  const root = document.documentElement;
  const css = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  const fmt = (n) => (n == null ? "—" : n.toLocaleString("en-US"));
  const sum = (a) => a.reduce((s, x) => s + (x || 0), 0);
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Days are the viewer's own: YYYY-MM-DD in the browser's time zone.
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (dt) => `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
  const dateOf = (d) => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd); };
  const addDays = (d, k) => { const dt = dateOf(d); dt.setDate(dt.getDate() + k); return ymd(dt); };
  const todayISO = ymd(new Date());
  const yesterdayISO = addDays(todayISO, -1);
  const label = (d, long) => dateOf(d).toLocaleDateString("en-US", { month: "short", day: "numeric", ...(long ? { weekday: "short" } : {}) });
  const at = (t, opts) => new Date(t).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", ...opts });
  const zone = (t) => new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(new Date(t)).find((p) => p.type === "timeZoneName")?.value || "";
  // The Worker keeps each count under its UTC hour (2026-11-09T14), which lands on the viewer's day.
  // Rows from before it kept hours hold a whole UTC day (2026-11-09) and keep their date.
  const hourly = (k) => k.length > 10;
  const timeOf = (k) => Date.parse(hourly(k) ? k + ":00:00Z" : k + "T00:00:00Z");
  const localDay = (k) => (hourly(k) ? ymd(new Date(timeOf(k))) : k);
  // Weeks and months are UTC, as the app marks them (Updater.swift): weeks from Monday, keyed by it.
  const DAY = 86400000;
  const weekOf = (t) => {
    const d = new Date(t), back = (d.getUTCDay() + 6) % 7;
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - back)).toISOString().slice(0, 10);
  };
  const monthOf = (t) => new Date(t).toISOString().slice(0, 7);
  const now = Date.now(), nowUTC = new Date(now);
  const thisWeek = weekOf(now), lastWeek = weekOf(now - 7 * DAY), weekBefore = weekOf(now - 14 * DAY);
  const thisMonth = monthOf(now), lastMonth = monthOf(Date.UTC(nowUTC.getUTCFullYear(), nowUTC.getUTCMonth() - 1, 15));
  const weekLabel = (w, long) => `${long ? "Week of " : ""}${dateOf(w).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
  const monthName = (m) => new Date(m + "-15T00:00:00Z").toLocaleDateString("en-US", { month: "long", timeZone: "UTC" });

  // keepmum's versions in release order: 0.1.0a4 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 (as build-app.sh numbers them).
  function vkey(v) {
    const m = /^(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?$/.exec(v);
    if (!m) return -1;
    const pre = m[4] ? { a: 0, b: 30, rc: 60 }[m[4]] + Number(m[5]) : 99;
    return Number(m[1]) * 1e6 + Number(m[2]) * 1e4 + Number(m[3] || 0) * 100 + pre;
  }

  // --- the numbers ---
  const raw = DATA.counts || [];
  const counts = raw.map((r) => ({ ...r, day: localDay(r.day) }));
  const add = (m, d, v, n) => { const x = m.get(d) || new Map(); x.set(v, (x.get(v) || 0) + n); m.set(d, x); };
  const by = (keep) => { const m = new Map(); for (const r of counts) if (keep(r)) add(m, r.day, r.version, r.n); return m; };
  const total = (m) => (d) => (m.get(d) ? sum([...m.get(d).values()]) : 0);
  // Versions that say which checks are a Mac's first of the day count once a day by that; older
  // ones by their checks.
  const marking = new Set(raw.filter((r) => r.kind === "daily").map((r) => r.version));
  const active = by((r) => (marking.has(r.version) ? r.kind === "daily" : r.kind === "check"));
  const checks = by((r) => r.kind === "check"), updates = by((r) => r.kind === "update"), site = by((r) => r.kind === "download"), fresh = by((r) => r.kind === "new");
  const activeOn = total(active), checksOn = total(checks), siteOn = total(site), updatesOn = total(updates), newOn = total(fresh);
  const per = (kind, keyOf) => {
    const m = new Map();
    for (const r of raw) if (r.kind === kind && hourly(r.day)) { const k = keyOf(timeOf(r.day)); m.set(k, (m.get(k) || 0) + r.n); }
    return m;
  };
  const weekly = per("weekly", weekOf), monthly = per("monthly", monthOf);
  const markingSince = [...marking].filter((v) => vkey(v) >= 0).sort((a, b) => vkey(a) - vkey(b))[0];

  // GitHub's totals, noted every six hours: a day's total is its last note, and its growth is over
  // the last note of the day noted before it.
  const notes = (DATA.releases || []).map((r) => ({ ...r, t: timeOf(r.day), day: localDay(r.day) })).sort((a, b) => a.t - b.t);
  const ghTotal = new Map(), latestPerVersion = new Map();
  for (const r of notes) {
    latestPerVersion.set(r.version, r.n);
    ghTotal.set(r.day, sum([...latestPerVersion.values()]));
  }
  const ghDays = [...ghTotal.keys()].sort();
  const ghGrowth = new Map();
  ghDays.forEach((d, i) => { if (i > 0) ghGrowth.set(d, ghTotal.get(d) - ghTotal.get(ghDays[i - 1])); });
  const directOn = (d) => (ghGrowth.has(d) ? Math.max(0, ghGrowth.get(d) - siteOn(d) - updatesOn(d)) : null);

  // Versions get their color once, from all the data, so a range change never repaints them:
  // the three newest have a hue each, the rest are "Older".
  // Only these kinds carry an app version: the others hold a domain, a path, a macOS version or a chip.
  const VERSIONED = new Set(["check", "daily", "weekly", "monthly", "new", "update"]);
  const releasesSeen = (DATA.versions || []).map((r) => r.version);
  const allVersions = [...new Set([...raw.filter((r) => VERSIONED.has(r.kind)).map((r) => r.version), ...releasesSeen])].sort((a, b) => vkey(b) - vkey(a));
  const named = allVersions.filter((v) => vkey(v) >= 0).slice(0, 3);
  const slot = ["--s1", "--s2", "--s3"];
  const newest = named[0];
  const colorOf = (v) => (named.includes(v) ? css(slot[named.indexOf(v)]) : css("--older"));
  function versionSeries(map, days) {
    const series = named.map((v) => ({ name: v, color: colorOf(v), values: days.map((d) => map.get(d)?.get(v) || 0) }));
    const older = days.map((d) => { const m = map.get(d); return m ? sum([...m.entries()].filter(([v]) => !named.includes(v)).map(([, n]) => n)) : 0; });
    if (sum(older) > 0) series.push({ name: "Older", color: css("--older"), values: older });
    return series.filter((s) => sum(s.values) > 0).reverse(); // oldest at the bottom of a stack
  }

  // --- range ---
  const firstDay = [...counts.map((r) => r.day), ...ghDays].sort()[0] || todayISO;
  function daysFor(range) {
    let d = range === "all" ? (firstDay < todayISO ? firstDay : todayISO) : addDays(todayISO, 1 - Number(range));
    const out = [];
    for (; d <= todayISO; d = addDays(d, 1)) out.push(d);
    return out;
  }
  function weeksFor(days) {
    const out = [];
    for (let w = weekOf(Date.parse(days[0] + "T12:00:00Z")); w <= thisWeek; w = new Date(Date.parse(w + "T00:00:00Z") + 7 * DAY).toISOString().slice(0, 10)) out.push(w);
    return out;
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
  function placeTip(evt) {
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + 14, y = evt.clientY + 14;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function showTip(evt, title, series, i, extra) {
    const known = series.filter((s) => s.values[i] != null);
    const shown = known.some((s) => s.values[i]) ? known.filter((s) => s.values[i]) : known;
    const f = series.format || fmt;
    const rows = shown.slice().reverse().map((s) => `<div class="r"><span><i style="background:${s.color}"></i>${s.name}</span><span>${f(s.values[i])}</span></div>`).join("");
    const totalRow = shown.length > 1 && !series.noTotal ? `<div class="r total"><span>Total</span><span>${f(sum(shown.map((s) => s.values[i])))}</span></div>` : "";
    tip.innerHTML = `<div class="d">${title}</div>${rows}${totalRow}${extra || ""}`;
    placeTip(evt);
  }
  const hideTip = () => { tip.hidden = true; };

  function legend(id, series, always) {
    const box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = series.length > 1 || (always && series.length) ? series.slice().reverse().map((s) => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("") : "";
  }

  /**
   * A ranked list with a bar each (bars, below a card's head): the biggest `top` and the rest as
   * "Other", each with its number and share of the total. One series, one color.
   */
  function barList(id, entries, o) {
    const box = document.getElementById(id);
    const all = [...entries].filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    const total = sum(all.map(([, n]) => n));
    if (!total) { box.innerHTML = `<div class="none">${o.empty}</div>`; return; }
    const top = all.slice(0, o.top || 7);
    const rest = sum(all.slice(o.top || 7).map(([, n]) => n));
    const items = [...top.map(([k, n]) => ({ k, n })), ...(rest ? [{ k: null, n: rest }] : [])];
    const max = Math.max(...items.map((x) => x.n));
    box.innerHTML = items.map((x, i) => `<div class="item" data-i="${i}"><span class="name">${x.k === null ? "Other" : o.name(x.k)}</span>` +
      `<span class="n">${fmt(x.n)}<small>${Math.round((x.n / total) * 100)}%</small></span>` +
      `<div class="track"><div class="fill${x.k === null ? " muted" : ""}${o.animate && !reducedMotion ? " grow" : ""}" style="width:${Math.max(1.5, (x.n / max) * 100)}%"></div></div></div>`).join("");
    for (const row of box.querySelectorAll(".item")) {
      const x = items[Number(row.dataset.i)];
      row.addEventListener("mousemove", (evt) => {
        tip.innerHTML = `<div class="d">${x.k === null ? `Other (${all.length - top.length})` : o.name(x.k)}</div>` +
          `<div class="r"><span>${o.unit}</span><span>${fmt(x.n)}</span></div><div class="r"><span>Share</span><span>${((x.n / total) * 100).toFixed(1)}%</span></div>`;
        placeTip(evt);
      });
      row.addEventListener("mouseleave", hideTip);
    }
  }

  /** Counts of one kind in a set of days, by version (a domain, a path, a macOS version, a chip). */
  const kindIn = (kind, days) => {
    const keep = new Set(days), m = new Map();
    for (const r of counts) if (r.kind === kind && keep.has(r.day)) m.set(r.version, (m.get(r.version) || 0) + r.n);
    return m;
  };

  // Releases: the day each shipped (the feed's date, in the viewer's time zone), one marker per day.
  const shipped = new Map((DATA.versions || []).map((r) => [r.version, r.published ? ymd(new Date(r.published)) : localDay(r.seen)]));
  const markers = [...shipped.entries()].reduce((byDay, [v, d]) => byDay.set(d, [...(byDay.get(d) || []), v]), new Map());
  const releaseMarkers = [...markers.entries()].map(([d, vs]) => {
    const sorted = vs.sort((a, b) => vkey(b) - vkey(a));
    return { x: d, label: sorted.join(", "), short: sorted[0] + (sorted.length > 1 ? ` +${sorted.length - 1}` : "") };
  });
  const ago = (d) => {
    const n = Math.round((dateOf(todayISO) - dateOf(d)) / DAY);
    return n <= 0 ? "today" : n === 1 ? "yesterday" : n < 60 ? `${n} days ago` : `${Math.round(n / 30)} months ago`;
  };
  const MACOS = { 14: "Sonoma", 15: "Sequoia", 26: "Tahoe" };
  const macosName = (v) => `macOS ${v}${MACOS[v] ? ` <span style="color:var(--text-3)">${MACOS[v]}</span>` : ""}`;

  /**
   * A chart over days or weeks: stacked areas or bars (o.kind), stacked or side by side (o.grouped),
   * with a crosshair or band and a tooltip per x. A partial last x (today, this week) is drawn lighter.
   */
  function chart(id, xs, series, o) {
    const box = document.getElementById(id);
    box.innerHTML = "";
    const W = box.clientWidth, H = box.clientHeight;
    const m = { l: 40, r: 8, t: (o.markers || []).some((k) => xs.includes(k.x)) ? 24 : 10, b: 26 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    const totals = xs.map((_, i) => (o.grouped ? Math.max(0, ...series.map((s) => s.values[i] || 0)) : sum(series.map((s) => s.values[i]))));
    if (!series.length || totals.every((t) => !t)) {
      const e = document.createElement("div");
      e.className = "empty"; e.textContent = o.empty;
      box.appendChild(e);
    }
    const max = o.max || niceMax(Math.max(0, ...totals));
    const svg = el("svg", { width: W, height: H, role: "img", "aria-label": o.title || "" }, box);
    const y = (v) => m.t + ph - (v / max) * ph;
    const band = pw / xs.length;
    const x = (i) => m.l + band * i + band / 2;
    const steps = max % 4 === 0 ? 4 : max % 5 === 0 ? 5 : 2; // whole-number ticks: counts have no halves
    for (let k = 0; k <= steps; k++) {
      const v = (max / steps) * k;
      el("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: css("--sep"), "stroke-width": 1, "stroke-dasharray": k ? "2 4" : "" }, svg);
      const t = el("text", { x: m.l - 10, y: y(v) + 4, "text-anchor": "end", fill: css("--text-3"), "font-size": 11 }, svg);
      t.textContent = (Number.isInteger(v) ? fmt(v) : v.toFixed(1)) + (o.unitSuffix || "");
    }
    // Releases: a dashed line on the day each shipped, over the data, its version above the plot;
    // versions shipped on the same day share one line (o.markers has one per day), and a label that
    // would run into the one before it is left off.
    const drawMarkers = () => {
      let lastLabel = -Infinity;
      for (const k of (o.markers || []).filter((k) => xs.includes(k.x))) {
        const mx = x(xs.indexOf(k.x));
        el("line", { x1: mx, x2: mx, y1: m.t - 4, y2: m.t + ph, stroke: css("--text-2"), "stroke-width": 1, "stroke-dasharray": "2 3", opacity: 0.8 }, svg);
        const anchor = mx > W - m.r - 70 ? "end" : "start";
        if ((anchor === "start" ? mx : mx - 70) - lastLabel < 8) continue;
        const t = el("text", { x: anchor === "start" ? mx + 4 : mx - 4, y: m.t - 10, "text-anchor": anchor, fill: css("--text-2"), "font-size": 11, "font-weight": 600 }, svg);
        t.textContent = k.short || k.label;
        lastLabel = (anchor === "start" ? mx : mx - 4) + t.getComputedTextLength() + 4;
      }
    };
    const every = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(pw / 72))));
    xs.forEach((d, i) => {
      if (i % every && i !== xs.length - 1) return;
      if (i !== xs.length - 1 && xs.length - 1 - i < every / 2) return; // keep clear of the last label
      const t = el("text", { x: x(i), y: H - 6, "text-anchor": "middle", fill: css("--text-3"), "font-size": 11 }, svg);
      t.textContent = o.label(d);
    });
    const hover = el("rect", { x: 0, y: m.t, width: 0, height: ph, fill: css("--text"), opacity: 0, rx: 6 }, svg);
    const g = el("g", { class: o.animate && !reducedMotion ? "reveal" : "" }, svg);
    const partial = (i) => o.partial && o.partial(xs[i]);
    const asBars = o.kind === "bar" || xs.length < 3;
    if (asBars) {
      const groups = o.grouped ? series.length : 1;
      const bw = Math.max(2, Math.min(o.grouped ? 12 : 24, (band * 0.7) / groups));
      xs.forEach((_, i) => {
        let base = 0;
        const segs = series.map((s, si) => ({ s, si, v: s.values[i] || 0 })).filter((q) => q.v > 0);
        segs.forEach((q, j) => {
          const x0 = o.grouped ? x(i) - (groups * bw + (groups - 1) * 2) / 2 + q.si * (bw + 2) : x(i) - bw / 2;
          const lo = o.grouped ? 0 : base, hi = lo + q.v;
          if (!o.grouped) base = hi;
          const top = o.grouped || j === segs.length - 1;
          const gap = !o.grouped && j > 0 ? 2 : 0; // 2px of surface between stacked segments
          const y0 = y(lo) - gap, h = Math.max(1, y0 - y(hi)), yt = y0 - h;
          const r = top ? Math.min(4, bw / 2, h) : 0;
          el("path", { d: `M${x0},${yt + h} V${yt + r} Q${x0},${yt} ${x0 + r},${yt} H${x0 + bw - r} Q${x0 + bw},${yt} ${x0 + bw},${yt + r} V${yt + h} Z`, fill: q.s.color, opacity: partial(i) ? 0.45 : 1 }, g);
        });
      });
    } else {
      // stacked areas, the oldest version at the bottom; a 2px line of card separates the layers
      const surface = css("--card");
      let lower = xs.map(() => 0);
      const layers = series.map((s) => { const upper = lower.map((b, i) => b + (s.values[i] || 0)); const l = { s, lower, upper }; lower = upper; return l; });
      const defs = el("defs", {}, svg);
      layers.forEach((l, li) => {
        const gid = `${id}-g${li}`;
        const grad = el("linearGradient", { id: gid, x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
        el("stop", { offset: "0%", "stop-color": l.s.color, "stop-opacity": layers.length === 1 ? 0.5 : 0.95 }, grad);
        el("stop", { offset: "100%", "stop-color": l.s.color, "stop-opacity": layers.length === 1 ? 0.04 : 0.75 }, grad);
        const top = l.upper.map((v, i) => `${x(i)},${y(v)}`).join(" L");
        const bottom = l.lower.map((v, i) => `${x(i)},${y(v)}`).reverse().join(" L");
        el("path", { d: `M${top} L${bottom} Z`, fill: `url(#${gid})` }, g);
      });
      for (const l of layers) {
        const line = "M" + l.upper.map((v, i) => `${x(i)},${y(v)}`).join(" L");
        el("path", { d: line, fill: "none", stroke: layers.length === 1 ? l.s.color : surface, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, g);
      }
      // today is not over: its stretch is washed out, so its dip does not read as a drop
      if (partial(xs.length - 1)) el("rect", { x: x(xs.length - 2), y: m.t - 2, width: x(xs.length - 1) - x(xs.length - 2) + 2, height: ph + 2, fill: surface, opacity: 0.55 }, g);
    }
    drawMarkers();
    const cross = el("line", { x1: 0, x2: 0, y1: m.t, y2: m.t + ph, stroke: css("--text-2"), "stroke-width": 1, "stroke-dasharray": "3 3", opacity: 0 }, svg);
    const dot = el("circle", { r: 4.5, fill: series[series.length - 1]?.color || "transparent", stroke: css("--card"), "stroke-width": 2, opacity: 0 }, svg);
    const hit = el("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (evt) => {
      const bx = svg.getBoundingClientRect().left;
      const i = Math.min(xs.length - 1, Math.max(0, Math.floor((evt.clientX - bx - m.l) / band)));
      if (asBars) {
        hover.setAttribute("x", m.l + band * i + 1); hover.setAttribute("width", Math.max(0, band - 2)); hover.setAttribute("opacity", 0.05);
      } else {
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 0.7);
        dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(totals[i])); dot.setAttribute("opacity", 1);
      }
      const shown = series.length ? series : [{ name: o.unit || "Count", color: css("--older"), values: xs.map(() => 0) }];
      shown.noTotal = o.grouped || o.noTotal;
      shown.format = o.format;
      const released = (o.markers || []).filter((k) => k.x === xs[i]).map((k) => k.label).join(", ");
      const extra = (o.extraFor ? o.extraFor(xs[i]) : "") + (released ? `<div class="r total"><span>Released</span><span>${released}</span></div>` : "");
      showTip(evt, o.label(xs[i], true) + (partial(i) ? " (so far)" : ""), shown, i, extra);
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("opacity", 0); cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); hideTip(); });
  }

  function sparkline(values, color) {
    const W = 240, H = 34, max = Math.max(1, ...values), n = Math.max(1, values.length - 1);
    const pts = values.map((v, i) => [(i / n) * W, H - 3 - (v / max) * (H - 8)]);
    const line = pts.map((p) => p.join(",")).join(" L");
    const id = "sp" + Math.random().toString(36).slice(2, 8);
    return `<svg class="spark" width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity="0.28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
      <path d="M${line} L${W},${H} L0,${H} Z" fill="url(#${id})"/>
      <path d="M${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  }
  function delta(now, before, what) {
    if (!now || !before) return "";
    const p = Math.round(((now - before) / before) * 100);
    const cls = p > 0 ? "up" : p < 0 ? "down" : "flat";
    return `<span class="delta ${cls}" title="Against ${what}">${p > 0 ? "▲" : p < 0 ? "▼" : "■"} ${Math.abs(p)}%</span>`;
  }

  /** Numbers in the tiles count up from zero on the first draw. */
  function countUp(animate) {
    for (const v of document.querySelectorAll("[data-to]")) {
      const to = Number(v.dataset.to), suffix = v.dataset.suffix || "";
      if (!animate || reducedMotion || !to) { v.textContent = fmt(to) + suffix; continue; }
      const t0 = performance.now(), dur = 800;
      const step = (t) => {
        const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
        v.textContent = fmt(Math.round(to * e)) + suffix;
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
  }
  const num = (n, suffix) => (n == null ? "—" : `<span data-to="${n}" data-suffix="${suffix || ""}">0</span>`);

  // --- the page ---
  function render(range, first) {
    for (const b of document.querySelectorAll("[data-range]")) b.setAttribute("aria-pressed", String(b.dataset.range === range));
    const days = daysFor(range);
    const full = days.filter((d) => d !== todayISO);
    const isToday = (d) => d === todayISO;

    // tiles: active Macs
    const yesterday = activeOn(yesterdayISO), dayBefore = activeOn(addDays(todayISO, -2));
    const since = markingSince ? `Counted from ${markingSince} on` : "Counted from 0.1.0a7 on";
    const wkLast = weekly.get(lastWeek), wkNow = weekly.get(thisWeek);
    const moLast = monthly.get(lastMonth), moNow = monthly.get(thisMonth);
    const newN = sum(days.map(newOn));
    const kpis = [
      { hero: true, label: "Daily active Macs", value: yesterday || null, delta: delta(yesterday, dayBefore, label(addDays(todayISO, -2))),
        caption: yesterday ? `${label(yesterdayISO, true)} · ${fmt(activeOn(todayISO))} today so far` : "No checks counted yet",
        spark: full.length > 1 ? sparkline(full.map(activeOn), css("--s1")) : "" },
      { label: "Weekly active Macs", value: wkLast ?? wkNow ?? null, delta: wkLast ? delta(wkLast, weekly.get(weekBefore), weekLabel(weekBefore, true).toLowerCase()) : "",
        caption: wkLast != null ? `${weekLabel(lastWeek, true)} · ${fmt(wkNow || 0)} this week so far` : wkNow != null ? "This week so far" : since,
        spark: weekly.size > 1 ? sparkline(weeksFor(days).filter((w) => w !== thisWeek).map((w) => weekly.get(w) || 0), css("--s1")) : "" },
      { label: "Monthly active Macs", value: moLast ?? moNow ?? null, delta: "",
        caption: moLast != null ? `${monthName(lastMonth)} · ${fmt(moNow || 0)} in ${monthName(thisMonth)} so far` : moNow != null ? `${monthName(thisMonth)} so far` : since },
      { label: "New installs", value: marking.size ? newN : null, delta: "",
        caption: marking.size ? `In this range · ${fmt(newOn(todayISO))} today` : since,
        spark: marking.size && full.length > 1 ? sparkline(full.map(newOn), css("--s3")) : "" },
    ];
    document.getElementById("kpis").innerHTML = kpis.map((k) =>
      `<div class="kpi${k.hero ? " hero" : ""}"><span class="label">${k.label}</span><div class="row"><span class="value">${num(k.value)}</span>${k.delta}</div><span class="caption">${k.caption}</span>${k.spark || ""}</div>`).join("");

    // tiles: downloads and upgrades
    const siteN = sum(days.map(siteOn)), directN = sum(days.map((d) => directOn(d) || 0)), updN = sum(days.map(updatesOn));
    const onNewest = newest && yesterday ? Math.round(((active.get(yesterdayISO)?.get(newest) || 0) / yesterday) * 100) : null;
    const minor = [
      { label: "Downloads", value: siteN + directN, caption: `${fmt(siteN)} website · ${fmt(directN)} GitHub` },
      { label: "Upgrades", value: updN, caption: newest ? `${fmt(sum(days.map((d) => updates.get(d)?.get(newest) || 0)))} to ${newest}` : "From 0.1.0a5 on" },
      { label: "On the newest version", value: onNewest, suffix: "%", caption: newest ? `${newest}, of yesterday's active Macs` : "No version seen yet" },
    ];
    const latest = [...shipped.keys()].sort((a, b) => vkey(b) - vkey(a))[0];
    if (latest) {
      const upLatest = sum(raw.filter((r) => r.kind === "update" && r.version === latest).map((r) => r.n));
      minor.unshift({ label: "Latest release", text: latest,
        caption: `Shipped ${label(shipped.get(latest))}, ${ago(shipped.get(latest))} · ${fmt(upLatest)} upgrade${upLatest === 1 ? "" : "s"} to it` });
    }
    document.getElementById("minor").innerHTML = minor.map((k) =>
      `<div class="kpi"><div class="text"><span class="label">${k.label}</span><span class="caption">${k.caption}</span></div><span class="value">${k.text ?? num(k.value, k.suffix)}</span></div>`).join("");
    countUp(first);

    // active Macs per day
    const act = versionSeries(active, days);
    legend("lg-active", act, true);
    chart("ch-active", days, act, { kind: "area", title: "Active Macs per day", label, partial: isToday, animate: first, unit: "Active Macs",
      markers: releaseMarkers, empty: "No update checks yet. Builds from 0.1.0a5 on check keepmum.app." });

    // each version's share of the day's active Macs, out of 100
    const share = act.map((s) => ({ ...s, values: s.values.map((v, i) => (activeOn(days[i]) ? Math.round((v / activeOn(days[i])) * 1000) / 10 : null)) }));
    legend("lg-adoption", share, true);
    chart("ch-adoption", days, share, { kind: "area", title: "Version adoption", label, partial: isToday, animate: first, unit: "Share",
      markers: releaseMarkers, noTotal: true, max: 100, unitSuffix: "%", format: (v) => (v == null ? "—" : `${v}%`),
      empty: "No active Macs counted yet." });

    // per week
    const weeks = weeksFor(days);
    chart("ch-weekly", weeks, weekly.size ? [{ name: "Active Macs", color: css("--s1"), values: weeks.map((w) => weekly.get(w) ?? null) }] : [],
      { kind: "bar", title: "Active Macs per week", label: weekLabel, partial: (w) => w === thisWeek, animate: first, unit: "Active Macs",
        empty: `No weekly counts yet. ${since}.` });
    document.getElementById("weekly-note").textContent = `Each counted Mac once a week${markingSince ? `, from ${markingSince} on` : ""}. Weeks start Monday, UTC.`;

    // versions in use yesterday
    const vy = active.get(yesterdayISO) || new Map(), vt = sum([...vy.values()]);
    const vlist = named.map((v) => ({ v, n: vy.get(v) || 0 }));
    const older = sum([...vy.entries()].filter(([v]) => !named.includes(v)).map(([, n]) => n));
    if (older) vlist.push({ v: "Older", n: older });
    const shown = vlist.filter((x) => x.n > 0);
    const vbox = document.getElementById("versions");
    vbox.innerHTML = vt
      ? `<div class="bar" role="img" aria-label="Versions in use yesterday">${shown.map((x) => `<span data-v="${x.v}" style="flex:${x.n};background:${x.v === "Older" ? css("--older") : colorOf(x.v)}"></span>`).join("")}</div>` +
        `<ul>${shown.map((x) => `<li><span><i style="background:${x.v === "Older" ? css("--older") : colorOf(x.v)}"></i>${x.v}</span><span><b>${fmt(x.n)}</b><small>${Math.round((x.n / vt) * 100)}%</small></span></li>`).join("")}</ul>`
      : `<div class="none">No active Macs counted yesterday.</div>`;
    for (const s of vbox.querySelectorAll(".bar span")) {
      const x = shown.find((q) => q.v === s.dataset.v);
      s.addEventListener("mousemove", (evt) => { tip.innerHTML = `<div class="d">${x.v}</div><div class="r"><span>Active Macs</span><span>${fmt(x.n)}</span></div><div class="r"><span>Share</span><span>${Math.round((x.n / vt) * 100)}%</span></div>`; placeTip(evt); });
      s.addEventListener("mouseleave", hideTip);
    }

    // downloads
    const downloads = [
      { name: "GitHub", color: css("--s2"), values: days.map(directOn) },
      { name: "Website", color: css("--s1"), values: days.map(siteOn) },
    ];
    legend("lg-downloads", downloads);
    chart("ch-downloads", days, downloads, { kind: "bar", title: "Downloads", label, partial: isToday, animate: first,
      empty: "No downloads in this range yet.",
      extraFor: (d) => (ghGrowth.has(d) ? "" : `<div class="r"><span>GitHub</span><span>not noted yet</span></div>`) });

    // new installs and upgrades, side by side
    const inst = [
      { name: "New installs", color: css("--s3"), values: days.map(newOn) },
      { name: "Upgrades", color: css("--s1"), values: days.map(updatesOn) },
    ];
    legend("lg-installs", inst.slice().reverse(), true);
    chart("ch-installs", days, inst, { kind: "bar", grouped: true, title: "New installs and upgrades", label, partial: isToday, animate: first,
      empty: "No new installs or upgrades in this range yet." });

    // where updates came from, where downloads came from, and the systems
    barList("paths", kindIn("path", days), { animate: first, unit: "Upgrades", top: 6,
      name: (k) => { const [a, b] = k.split(">"); return `${a}<span class="arrow">→</span>${b}`; },
      empty: "No upgrade paths in this range. The app names the version it updates from from 0.1.0a5 on." });
    barList("via", kindIn("via", days), { animate: first, unit: "Downloads", top: 7,
      name: (k) => (k === "direct" ? "Direct, or not said" : k === "keepmum.app" ? "keepmum.app <span style=\"color:var(--text-3)\">another page</span>" : k === "other" ? "Not a domain" : k),
      empty: "No website downloads with a source in this range yet." });
    const chips = kindIn("chip", days), arm = chips.get("arm64") || 0, intel = chips.get("x86_64") || 0;
    const sys = document.getElementById("systems");
    sys.innerHTML = `<div><h3>macOS</h3><div class="bars" id="macos"></div></div>` + (arm + intel
      ? `<div><h3>Chip</h3><div class="split" role="img" aria-label="Apple silicon ${fmt(arm)}, Intel ${fmt(intel)}">` +
        `${arm ? `<span style="flex:${arm};background:${css("--s1")}"></span>` : ""}${intel ? `<span style="flex:${intel};background:${css("--s2")}"></span>` : ""}</div>` +
        `<div class="key">${[["Apple silicon", arm, "--s1"], ["Intel", intel, "--s2"]].filter(([, n]) => n).map(([name, n, c]) =>
          `<span><i style="display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;background:${css(c)}"></i>${name} <b>${fmt(n)}</b> · ${Math.round((n / (arm + intel)) * 100)}%</span>`).join("")}</div></div>`
      : "");
    barList("macos", kindIn("macos", days), { animate: first, unit: "Mac-days", top: 5, name: macosName,
      empty: "No systems counted in this range. Macs say theirs from 0.1.0a7 on." });

    // releases, newest first
    const vs = [...new Set([...latestPerVersion.keys(), ...allVersions.filter((v) => vkey(v) >= 0)])].sort((a, b) => vkey(b) - vkey(a));
    const upTotal = (v) => sum(raw.filter((r) => r.kind === "update" && r.version === v).map((r) => r.n));
    const yt = activeOn(yesterdayISO);
    document.getElementById("releases").innerHTML = vs.length ? vs.map((v, i) => {
      const on = active.get(yesterdayISO)?.get(v) || 0, pct = yt ? (on / yt) * 100 : 0, d = shipped.get(v);
      return `<div class="rel"><div class="v"><b><i style="background:${colorOf(v)}"></i>${v}${i === 0 && d ? ' <span class="badge">Latest</span>' : ""}</b>` +
        `<span>${d ? `${label(d, true)} · ${ago(d)}` : "Date not noted"}</span></div>` +
        `<div class="m"><b>${fmt(latestPerVersion.get(v) ?? null)}</b><span>GitHub downloads</span></div>` +
        `<div class="m"><b>${fmt(upTotal(v))}</b><span>upgrades to it</span></div>` +
        `<div class="m"><b>${fmt(on)}</b><span>active yesterday</span></div>` +
        `<div class="share"><div class="track"><div class="fill${first && !reducedMotion ? " grow" : ""}" style="width:${pct}%;background:${colorOf(v)}"></div></div><em>${yt ? Math.round(pct) + "%" : "—"}</em></div></div>`;
    }).join("") : `<div class="none">No releases noted yet: the update feed is read when an app checks.</div>`;

    document.getElementById("daily").innerHTML =
      `<thead><tr><th>Day</th><th>Active Macs</th><th>New installs</th><th>Update checks</th><th>Website downloads</th><th>GitHub downloads</th><th>Upgrades</th></tr></thead><tbody>` +
      [...days].reverse().map((d) => `<tr><td>${label(d, true)}${isToday(d) ? " (so far)" : ""}</td><td>${fmt(activeOn(d))}</td><td>${fmt(newOn(d))}</td><td>${fmt(checksOn(d))}</td><td>${fmt(siteOn(d))}</td><td>${fmt(directOn(d))}</td><td>${fmt(updatesOn(d))}</td></tr>`).join("") + "</tbody>";
  }

  // --- header, switches ---
  document.getElementById("who").textContent = WHO;
  document.getElementById("generated").textContent = "As of " + at(DATA.generated, { year: "numeric", month: "short", day: "numeric", timeZoneName: "short" });
  const firstHourly = raw.filter((r) => hourly(r.day) && ["check", "download", "update"].includes(r.kind)).map((r) => localDay(r.day)).sort()[0];
  const tz = document.getElementById("tz");
  tz.textContent = `Days in your time zone, ${zone(now)}` +
    (!raw.some((r) => !hourly(r.day)) ? "" : firstHourly ? `; UTC days before ${label(firstHourly)}` : "; UTC days so far");
  try { tz.title = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch {}

  let range = "30";
  try { range = localStorage.getItem("range") || "30"; } catch {}
  for (const b of document.querySelectorAll("[data-range]")) {
    b.addEventListener("click", () => { range = b.dataset.range; try { localStorage.setItem("range", range); } catch {} render(range, true); });
  }
  function setTheme(t) {
    if (t === "light" || t === "dark") root.dataset.theme = t; else delete root.dataset.theme;
    for (const b of document.querySelectorAll("[data-theme-set]")) b.setAttribute("aria-pressed", String(b.dataset.themeSet === (t || "auto")));
  }
  let theme = "auto";
  try { theme = localStorage.getItem("theme") || "auto"; } catch {}
  setTheme(theme);
  for (const b of document.querySelectorAll("[data-theme-set]")) {
    b.addEventListener("click", () => {
      theme = b.dataset.themeSet;
      try { localStorage.setItem("theme", theme); } catch {}
      setTheme(theme);
      render(range, false);
    });
  }
  render(range, true);
  let t;
  addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => render(range, false), 120); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => render(range, false));
}
