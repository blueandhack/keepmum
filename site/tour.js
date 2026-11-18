// keepmum.app's app tour (#tour), drawn in the page: the app's own screens, as stills the app
// renders (mac/tour-stills.sh, into img/tour/), on a desktop drawn here, with the camera, the
// cursor, the clicks and the captions of the tour video. mac/Sources/Keepmum/Tour.swift is the
// source of both: its timing comes from there too (tour-data.js), key for key. What moves inside a
// screen in the video (the Overview's pulses, the map's sample, the review's clock) is a still
// here, or a few in turn. player.js plays it; without either script, the page's <video> stays.
(function () {
  "use strict";
  const T = window.keepmumTour;
  const figure = document.getElementById("tour");
  const video = figure && figure.querySelector("video");
  if (!T || !video || !window.keepmumPlayer || !("ResizeObserver" in window)) return;

  const [W, H] = T.frame, [DW, DH] = T.desk, base = W / DW;

  // MARK: Timing, as Tour.swift has it

  const clamp = (x) => Math.min(1, Math.max(0, x));
  const ease = (x) => { const c = clamp(x); return c * c * (3 - 2 * c); };
  /** 0 before a, 1 after b, eased between. */
  const p = (t, a, b) => ease((t - a) / (b - a));
  /** In over [a, a+f], out over [b-f, b]. */
  const shown = (t, a, b, f = 0.4) => Math.min(p(t, a, a + f), 1 - p(t, b - f, b));
  /** Where keyframed values ([t, v1, v2, …]) are at t: held at each key, eased from one to the next. */
  function track(keys, t) {
    let prev = keys[0];
    for (let i = 1; i < keys.length; i++) {
      const key = keys[i];
      if (t < key[0]) {
        const k = ease((t - prev[0]) / Math.max(key[0] - prev[0], 0.001));
        return prev.slice(1).map((v, j) => v + (key[j + 1] - v) * k);
      }
      prev = key;
    }
    return prev.slice(1);
  }

  // MARK: Building blocks

  function el(tag, parent, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }
  function place(e, x, y, w, h) {
    e.style.left = x + "px"; e.style.top = y + "px";
    if (w != null) e.style.width = w + "px";
    if (h != null) e.style.height = h + "px";
    return e;
  }
  const images = [];
  /** An image of the tour, loaded once the tour comes close to the screen. */
  function still(parent, name, w, h) {
    const img = el("img", parent, "tr-still");
    img.alt = "";
    img.decoding = "async";
    img.width = w; img.height = h;
    img.style.width = w + "px"; img.style.height = h + "px";
    img.dataset.src = "img/tour/" + name + ".webp";
    images.push(img);
    return img;
  }

  // MARK: The desktop, in its own points; the camera moves it

  const stage = el("div");
  const desk = place(el("div", stage, "tr-desk"), 0, 0, DW, DH);

  /** A window: the screens on rounded corners, a hairline, a shadow, and (the dashboard's) its three buttons. */
  function windowBox([x, y, w, h], buttons) {
    const box = place(el("div", desk, "tr-win"), x, y, w, h);
    const screens = el("div", box, "tr-screens");
    if (buttons) {
      const lights = el("div", box, "tr-lights");
      for (const c of ["#FF5F57", "#FEBC2E", "#28C840"]) el("span", lights).style.background = c;
    }
    el("div", box, "tr-hair");
    return { box, screens };
  }
  const app = windowBox(T.window, true);
  const turn = windowBox(T.review, false);
  // The map page scrolls inside the dashboard's content area.
  const [cx, cy, cw, ch] = T.content;
  const scrollMost = Math.max(...T.scroll.map((k) => k[1]));
  const contentBox = place(el("div", app.box, "tr-content"), cx, cy, cw, ch);
  const contentPage = el("div", contentBox, "tr-page");

  const kinds = { window: [], content: [], review: [] };
  let flowStill = null;
  const mapStills = [];
  for (const s of T.stills) {
    const img = s.kind === "window" ? still(app.screens, s.name, T.window[2], T.window[3])
      : s.kind === "content" ? still(contentPage, s.name, cw, ch + scrollMost)
      : still(turn.screens, s.name, T.review[2], T.review[3]);
    kinds[s.kind].push({ img, from: s.from, to: s.to });
    if (s.flow) flowStill = img;
    if (s.map) mapStills.push(img);
  }

  // MARK: The Overview's live flow: its pulses, drawn moving over a still of its bands

  // As the app draws them (FlowGeometry and FlowBands in mac/Sources/Keepmum/Pipeline.swift): the
  // bands are Béziers, a pulse crosses its route at one speed, eased at both ends, with a comet's
  // tail; each bar it passes flashes, and a ring spreads where it lands.
  const flow = T.flow;
  const flowCanvas = flow && flowStill ? place(el("canvas", app.box, "tr-flow"), ...flow.frame) : null;
  app.box.insertBefore(contentBox, app.box.querySelector(".tr-lights"));
  const SPEED = 520, TURN_EASING = 0.6, DENSITY = 2.5;
  const fEase = (u) => u - TURN_EASING * Math.sin(2 * Math.PI * u) / (2 * Math.PI);
  const fPace = (u) => (1 - TURN_EASING * Math.cos(2 * Math.PI * u)) / (1 + TURN_EASING);
  function uneased(x) {
    let lo = 0, hi = 1;
    for (let i = 0; i < 22; i++) { const m = (lo + hi) / 2; if (fEase(m) < x) lo = m; else hi = m; }
    return (lo + hi) / 2;
  }
  const travel = (length) => Math.min(Math.max(length / SPEED, 0.9), 1.8);
  let links = [], routes = [], waits = [];
  if (flowCanvas) {
    flowCanvas.width = Math.round(flow.size[0] * DENSITY);
    flowCanvas.height = Math.round(flow.size[1] * DENSITY);
    links = flow.links.map((l) => {
      const a = l.y0 + l.t / 2, b = l.y1 + l.t / 2, xm = (l.x0 + l.x1) / 2;
      const point = (f) => {
        const u = 1 - f;
        return [u * u * u * l.x0 + 3 * u * u * f * xm + 3 * u * f * f * xm + f * f * f * l.x1,
          u * u * u * a + 3 * u * u * f * a + 3 * u * f * f * b + f * f * f * b];
      };
      // Distance along the centre line at 32 even Bézier steps, to move at one speed on it.
      const lengths = [0];
      let total = 0, last = point(0);
      for (let k = 1; k <= 32; k++) {
        const q = point(k / 32);
        total += Math.hypot(q[0] - last[0], q[1] - last[1]);
        lengths.push(total);
        last = q;
      }
      const at = (d) => {
        if (d <= 0) return point(0);
        if (d >= total) return point(1);
        let lo = 0, hi = 32;
        while (hi - lo > 1) { const m = (lo + hi) >> 1; if (lengths[m] <= d) lo = m; else hi = m; }
        const span = lengths[hi] - lengths[lo];
        return point((lo + (span > 0 ? (d - lengths[lo]) / span : 0)) / 32);
      };
      return { ...l, point, at, length: total, from: flow.nodes[l.d + ":" + l.from], to: flow.nodes[l.d + ":" + l.to] };
    });
    const route = (ids) => {
      const ls = ids.map((i) => links[i]), starts = [];
      let d = 0;
      for (const l of ls) { starts.push(d); d += l.length; }
      return {
        links: ls, starts, length: d, duration: travel(d),
        passes: starts.map((s0) => uneased(s0 / d)),
        at(s) {
          s = Math.min(Math.max(s, 0), d);
          let i = 0;
          for (let k = 0; k < starts.length; k++) if (starts[k] <= s) i = k;
          return [ls[i].at(s - starts[i]), ls[i]];
        },
      };
    };
    routes = flow.pulses.map((p) => ({ ...route(p.links), start: p.start, stopped: p.stopped }));
    waits = flow.open.map((o) => ({ ...route(o.links), born: o.born, color: o.color })).sort((x, y) => x.born - y.born);
  }
  // MARK: The Protection map: requests drifting along its bands, and the sample along its route

  // As MapDiagram draws them (mac/Sources/Keepmum/ProtectionMap.swift): each path comes sampled at
  // even lengths; the drift and the dashes run on the clock, where it stood when the tour began.
  const map = T.map && T.map.frame ? T.map : null;
  const mapCanvas = map && mapStills.length ? place(el("canvas", contentPage, "tr-flow"), ...map.frame) : null;
  if (mapCanvas) {
    mapCanvas.width = Math.round(map.frame[2] * DENSITY);
    mapCanvas.height = Math.round(map.frame[3] * DENSITY);
  }
  /** The point a fraction u of the way along a path sampled at even lengths. */
  function along(pts, u) {
    const f = Math.min(Math.max(u, 0), 1) * (pts.length - 1), i = Math.min(Math.floor(f), pts.length - 2), k = f - i;
    return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k];
  }
  function mapDot(g, pts, u, color, size, ring) {
    if (u <= 0) return;
    const [x, y] = along(pts, u);
    if (ring) circle(g, x, y, size / 2 + 2, rgba(map.canvas, 1));
    circle(g, x, y, size / 2, color);
  }
  function drawMap(t) {
    const g = mapCanvas.getContext("2d");
    g.setTransform(DENSITY, 0, 0, DENSITY, 0, 0);
    g.clearRect(0, 0, map.frame[2], map.frame[3]);
    const clock = map.clock + t;
    // The cards are drawn over what moves, as in the app: it is drawn only around them.
    g.save();
    const around = new Path2D();
    around.rect(0, 0, map.frame[2], map.frame[3]);
    for (const [x, y, w, h] of map.cards) around.roundRect(x, y, w, h, map.radius);
    g.clip(around, "evenodd");
    drawMoving(g, t, clock);
    g.restore();
  }
  function drawMoving(g, t, clock) {
    map.ambient.forEach((pts, i) => {
      const u = ((clock + i * 1.37) % 5) / 5;
      mapDot(g, pts, u, rgba(map.ambientColor, 0.9 * Math.min(1, Math.min(u, 1 - u) * 12)), 6, false);
    });
    const run = map.run;
    if (!run || t < run.started) return;
    const elapsed = t - run.started;
    g.beginPath();
    run.route.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.setLineDash([2, 6]);
    g.lineDashOffset = -((clock * 26) % 8);
    g.lineCap = "round";
    g.lineJoin = "round";
    g.lineWidth = 2;
    g.strokeStyle = rgba(run.color, 0.85);
    g.stroke();
    g.setLineDash([]);
    for (const d of run.dots) {
      if (elapsed < d.delay) continue;
      const e = fEase(Math.min(1, (elapsed - d.delay) / d.duration));
      // Its color from a fraction of the way on.
      const c = d.colors.filter((k) => e >= k[0]).pop() || d.colors[0];
      mapDot(g, d.path, e, rgba(c.slice(1), 1), d.size, true);
    }
  }

  const rgba = (c, a) => "rgba(" + Math.round(c[0] * 255) + "," + Math.round(c[1] * 255) + "," + Math.round(c[2] * 255) + ","
    + Math.max(0, Math.min(1, c[3] * a)).toFixed(3) + ")";
  function circle(g, x, y, r, color) { g.beginPath(); g.arc(x, y, r, 0, 2 * Math.PI); g.fillStyle = color; g.fill(); }
  function drawFlow(t) {
    const g = flowCanvas.getContext("2d");
    g.setTransform(DENSITY, 0, 0, DENSITY, 0, 0);
    g.clearRect(0, 0, flow.size[0], flow.size[1]);
    // Requests out at a provider: a dot breathes at the provider's end of its band, the later ones queued behind it.
    const queued = new Map();
    for (const w of waits) {
      const last = w.links[w.links.length - 1];
      const age = t - w.born - w.duration;
      if (age < 0) continue;
      const i = queued.get(last) || 0;
      queued.set(last, i + 1);
      if (i >= 6) continue;
      const end = last.point(1), x = end[0] - 6 - i * 9, y = end[1];
      const breath = 0.5 - 0.5 * Math.cos(2 * Math.PI * age / 1.6), a = Math.min(1, age / 0.2);
      circle(g, x, y, 5 + 3 * breath, rgba(w.color, 0.22 * a));
      circle(g, x, y, 3, rgba(w.color, (0.55 + 0.45 * breath) * a));
    }
    for (const r of routes) {
      const elapsed = t - r.start, u = elapsed / r.duration;
      if (u < 0) continue;
      ticks(g, r, u);
      if (u >= 1) continue;
      const s = fEase(u) * r.length;
      const [p, link] = r.at(s);
      const alpha = Math.min(1, elapsed / 0.12) * Math.min(1, (1 - u) / 0.04);
      const size = u > 0.9 ? 0.55 + 0.45 * (1 - u) / 0.1 : 1;
      tail(g, r, s, u, alpha, size);
      circle(g, p[0], p[1], 8 * size, rgba(link.color, 0.18 * alpha));
      circle(g, p[0], p[1], 3.5 * size, rgba(link.color, alpha));
    }
  }
  /** The comet's tail: longer while the pulse is fast, in the color of each band it lies on. */
  function tail(g, r, s, u, alpha, size) {
    const length = (14 + 64 * fPace(u)) * size, steps = 12;
    let [last, on] = r.at(s);
    g.lineCap = "round";
    for (let j = 1; j <= steps; j++) {
      const d = s - length * j / steps;
      if (d < 0) break;
      const [p, link] = r.at(d);
      // A band can leave a bar higher or lower than the one before it arrives: the tail breaks there.
      if (link !== on) { last = link.point(1); on = link; }
      const q = 1 - (j - 1) / steps;
      g.beginPath();
      g.moveTo(last[0], last[1]);
      g.lineTo(p[0], p[1]);
      g.strokeStyle = rgba(link.color, alpha * Math.pow(q, 1.3) * 0.95);
      g.lineWidth = 0.6 + 5.4 * q * size;
      g.stroke();
      last = p;
    }
  }
  /** Each bar a pulse passes flashes; a ring spreads where it lands, red where a request was stopped. */
  function ticks(g, r, u) {
    for (let k = 1; k < r.links.length; k++) {
      const age = (u - r.passes[k]) * r.duration, bar = r.links[k].from;
      if (age < 0 || age >= 0.45 || !bar) continue;
      const q = age / 0.45, e = 2 + 5 * q, [x, y, w, h] = bar.rect;
      g.beginPath();
      g.roundRect(x - e, y - e, w + 2 * e, h + 2 * e, 3 + e);
      g.fillStyle = rgba(r.links[k].color, 0.28 * (1 - q));
      g.fill();
    }
    const age = (u - 1) * r.duration, last = r.links[r.links.length - 1];
    if (age < 0 || age >= 0.6 || !last.to) return;
    const q = age / 0.6, end = last.point(1), color = r.stopped ? flow.blocked : last.to.color;
    g.lineWidth = 1.6;
    g.strokeStyle = rgba(color, 0.75 * (1 - q));
    for (const radius of r.stopped ? [4 + 14 * Math.sqrt(q), 2 + 7 * Math.sqrt(q)] : [4 + 14 * Math.sqrt(q)]) {
      g.beginPath();
      g.arc(end[0], end[1], radius, 0, 2 * Math.PI);
      g.stroke();
    }
  }
  /** Each still fades in over the one before, which goes once it is covered; the last of a kind fades out at its end. */
  function stillsAt(list, t) {
    let any = 0;
    list.forEach((s, i) => {
      const last = i === list.length - 1;
      const o = p(t, s.from, s.from + 0.3) * (last ? 1 - p(t, s.to, s.to + 0.3) : t <= s.to + 0.3 ? 1 : 0);
      s.img.style.opacity = o;
      s.img.style.visibility = o > 0.001 ? "visible" : "hidden";
      any = Math.max(any, o);
    });
    return any;
  }

  const bar = el("div", desk, "tr-bar");
  bar.style.width = DW + "px"; bar.style.height = T.menuBar + "px";
  const barIdle = still(bar, "menubar", DW, T.menuBar);
  const barOpen = still(bar, "menubar-open", DW, T.menuBar);
  const menu = place(el("div", desk, "tr-menu"), T.panel[0], T.panel[1], T.menu[0], T.menu[1]);
  still(menu, "menu", T.menu[0], T.menu[1]);

  // The pointer: the arrow, pressed for a moment on a click, with a ring that spreads from it.
  const pointer = el("div", desk, "tr-pointer");
  const ringEl = el("div", pointer, "tr-ring");
  const arrow = el("div", pointer, "tr-arrow");
  arrow.innerHTML = '<svg width="13" height="20" viewBox="0 0 13 20" overflow="visible">'
    + '<path d="M0 0L0 16.5L4 12.8L6.8 19L9.4 17.9L6.7 11.9L12 11.9Z" fill="#000" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>';

  // MARK: The frame's own parts: captions, the preview-data note, the title and closing cards

  const caps = place(el("div", stage, "tr-caps"), 0, 0, W, H - 54);
  const captions = T.captions.map(([a, b, s]) => ({ a, b, s, e: el("span", caps, "tr-caption", s) }));
  const note = el("span", stage, "tr-note", "Preview data: made up, nothing real");
  function card(title, lines) {
    const c = place(el("div", stage, "tr-card"), 0, 0, W, H);
    const icon = still(c, "icon", 200, 200);
    icon.classList.remove("tr-still");
    el("div", c, "tr-card-title", title);
    for (const line of lines) el("div", c, line.includes(".app") ? "tr-card-link" : "tr-card-line", line);
    return c;
  }
  const opening = card("keepmum on your Mac", ["A tour of the app"]);
  const closing = card("Keep mum.", ["Free and open source, for macOS", "keepmum.app"]);

  // MARK: A moment of the tour

  function draw(t) {
    // The camera: how far in, and the desktop point at the frame's center, kept on the desktop.
    const [zoom, focusX, focusY] = track(T.camera, t);
    const z = base * zoom;
    const hw = W / 2 / z, hh = H / 2 / z;
    const fx = Math.min(Math.max(focusX, hw), DW - hw), fy = Math.min(Math.max(focusY, hh), DH - hh);
    desk.style.transform = "translate(" + (W / 2 - fx * z) + "px," + (H / 2 - fy * z) + "px) scale(" + z + ")";

    const appOn = p(t, T.windowIn[0], T.windowIn[1]);
    const turnOn = shown(t, ...T.reviewShown);
    const menuOn = shown(t, ...T.menuShown);
    for (const [w, on] of [[app.box, appOn], [turn.box, turnOn]]) {
      w.style.opacity = on;
      w.style.visibility = on > 0.001 ? "visible" : "hidden";
      w.style.transform = "scale(" + (0.97 + 0.03 * on) + ")";
    }
    stillsAt(kinds.window, t);
    if (flowCanvas) {
      const on = Number(flowStill.style.opacity) || 0;
      flowCanvas.style.opacity = on;
      flowCanvas.style.visibility = on > 0.001 ? "visible" : "hidden";
      if (on > 0.001) drawFlow(t);
    }
    stillsAt(kinds.review, t);
    const page = stillsAt(kinds.content, t);
    const mapOn = mapStills.reduce((m, img) => Math.max(m, Number(img.style.opacity) || 0), 0);
    contentBox.style.visibility = Math.max(page, mapOn) > 0.001 ? "visible" : "hidden";
    if (mapCanvas) {
      mapCanvas.style.opacity = mapOn;
      mapCanvas.style.visibility = mapOn > 0.001 ? "visible" : "hidden";
      if (mapOn > 0.001) drawMap(t);
    }
    contentPage.style.transform = "translateY(" + -track(T.scroll, t)[0] + "px)";

    barOpen.style.opacity = menuOn > 0 ? 1 : 0;
    barIdle.style.opacity = menuOn > 0 ? 0 : 1;
    menu.style.opacity = menuOn;
    menu.style.visibility = menuOn > 0.001 ? "visible" : "hidden";
    menu.style.transform = "translateY(" + -6 * (1 - menuOn) + "px)";

    const [px, py] = track(T.cursor, t);
    pointer.style.transform = "translate(" + px + "px," + py + "px)";
    const click = T.clicks.filter((c) => c <= t).pop();
    const since = click == null ? null : t - click;
    const k = since == null ? 1 : clamp(since / 0.45);
    const d = 10 + 34 * k;
    ringEl.style.width = ringEl.style.height = d + "px";
    ringEl.style.left = ringEl.style.top = -d / 2 + "px";
    ringEl.style.borderColor = "rgba(201,63,23," + (0.55 * (1 - k)).toFixed(3) + ")";
    arrow.style.transform = since != null && since < 0.16 ? "scale(0.88)" : "none";

    for (const c of captions) {
      const on = shown(t, c.a, c.b, 0.35);
      c.e.style.opacity = on;
      c.e.style.transform = "translateY(" + 8 * (1 - on) + "px)";
    }
    note.style.opacity = shown(t, ...T.previewShown);
    opening.style.opacity = 1 - p(t, ...T.titleOut);
    opening.style.visibility = t < T.titleOut[1] ? "visible" : "hidden";
    closing.style.opacity = p(t, ...T.closingIn);
    closing.style.visibility = t > T.closingIn[0] ? "visible" : "hidden";
  }

  let loading = null;
  function near() {
    if (loading) return;
    for (const img of images) img.src = img.dataset.src;
    // Autoplay waits for the screens, up to a few seconds.
    loading = Promise.race([
      Promise.all(images.map((img) => (img.decode ? img.decode() : Promise.resolve()).catch(() => {}))),
      new Promise((r) => setTimeout(r, 4000)),
    ]);
  }
  // The opening card's icon, at once: it is all the first second shows.
  for (const img of images) if (img.dataset.src.endsWith("/icon.webp")) img.src = img.dataset.src;

  stage.classList.add("tr-stage");
  window.keepmumPlayer({
    video, stage, width: W, height: H, duration: T.duration, name: "the tour",
    draw, near, ready: () => { near(); return loading; },
    captions: T.captions.map((c) => c[2]),
    caption: (t) => {
      let best = null, most = 0;
      for (const c of captions) { const on = shown(t, c.a, c.b, 0.35); if (on > most) { most = on; best = c.s; } }
      return [best, most];
    },
  });
})();
