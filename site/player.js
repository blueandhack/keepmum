// The player for keepmum.app's animations (flow.js and tour.js): a stage of a
// fixed size scaled to the column, a play button, a scrubber and the time, in place of a <video>.
//
// It plays by itself once half of it is on screen (after half a second there, so scrolling past
// does nothing) and pauses when it scrolls away; it stays still for a visitor who asked for reduced
// motion, and once they pause it or it ends, it is theirs. On a narrow screen the caption on
// screen shows below the stage, where it can be read; screen readers get every caption in order.
//
//   keepmumPlayer({ video, stage, width, height, duration, draw(t), caption(t) -> [text, opacity],
//                   captions: [text], name: "the tour", near(), ready() -> Promise })
//
// `near` runs once the stage comes close to the screen (to load what it shows), and autoplay waits
// for `ready`.
window.keepmumPlayer = function (o) {
  "use strict";
  const figure = o.video.closest("figure");
  const el = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
  const wrap = el("div", "flow");
  o.stage.classList.add("flow-stage");
  o.stage.setAttribute("aria-hidden", "true");
  wrap.appendChild(o.stage);
  const controls = el("div", "flow-controls");
  const button = el("button", "flow-play");
  button.type = "button";
  const scrub = el("input", "flow-scrub");
  scrub.type = "range";
  scrub.min = "0"; scrub.max = String(o.duration); scrub.step = "0.1"; scrub.value = "0";
  scrub.setAttribute("aria-label", "Position in " + o.name);
  const clock = el("span", "flow-time");
  controls.append(button, scrub, clock);
  const subtitle = el("p", "flow-sub");
  const transcript = el("ol", "flow-transcript");
  transcript.setAttribute("aria-label", "What " + o.name + " shows");
  for (const s of o.captions) transcript.appendChild(el("li")).textContent = s;

  const glyph = {
    play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor"/>',
    pause: '<path d="M8 5h3v14H8zM13 5h3v14h-3z" fill="currentColor"/>',
    replay: '<path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4.5h4.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
  };
  const fmt = (s) => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");

  let t = 0, playing = false, last = 0, raf = 0;
  function render() {
    o.draw(t);
    scrub.value = String(t);
    clock.textContent = fmt(t) + " / " + fmt(o.duration);
    const [text, on] = o.caption(t);
    if (text && subtitle.textContent !== text) subtitle.textContent = text;
    subtitle.style.opacity = text ? Math.min(1, on * 1.5) : 0;
  }
  function setButton() {
    const ended = !playing && t >= o.duration;
    button.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">'
      + glyph[playing ? "pause" : ended ? "replay" : "play"] + "</svg>";
    button.setAttribute("aria-label", (playing ? "Pause " : ended ? "Play again: " : "Play ") + o.name);
  }
  function frame(now) {
    t = Math.min(o.duration, t + Math.min(0.1, (now - last) / 1000));
    last = now;
    render();
    if (t >= o.duration) { pause(); handOver(); } else raf = requestAnimationFrame(frame);
  }
  function play() {
    if (playing) return;
    if (t >= o.duration) t = 0;
    playing = true;
    last = performance.now();
    raf = requestAnimationFrame(frame);
    setButton();
  }
  function pause() {
    playing = false;
    cancelAnimationFrame(raf);
    setButton();
  }

  let observer = null, settle = null;
  function handOver() {
    clearTimeout(settle);
    if (observer) observer.disconnect();
    observer = null;
  }
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if ("IntersectionObserver" in window) {
    const soon = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) { soon.disconnect(); if (o.near) o.near(); }
    }, { rootMargin: "800px 0px" });
    soon.observe(wrap);
    if (!reduce) {
      observer = new IntersectionObserver((entries) => {
        clearTimeout(settle);
        if (entries[0].isIntersecting) {
          settle = setTimeout(() => {
            Promise.resolve(o.ready && o.ready()).then(() => { if (observer) play(); });
          }, 500);
        } else pause();
      }, { threshold: 0.5 });
      observer.observe(wrap);
    }
  } else if (o.near) o.near();
  function toggle() {
    if (playing) { pause(); handOver(); } else { if (o.near) o.near(); play(); }
  }
  button.addEventListener("click", toggle);
  wrap.addEventListener("click", () => { if (!String(window.getSelection && window.getSelection())) toggle(); });
  scrub.addEventListener("input", () => {
    if (o.near) o.near();
    t = Math.min(o.duration, Math.max(0, Number(scrub.value)));
    render();
    if (!playing) setButton();
  });

  // The stage is drawn at its own size, scaled to the column.
  new ResizeObserver((entries) => {
    const width = entries[0].contentRect.width;
    o.stage.style.transform = "scale(" + width / o.width + ")";
    figure.classList.toggle("flow-narrow", width < 640);
  }).observe(wrap);

  o.video.replaceWith(wrap);
  wrap.after(controls, subtitle, transcript);
  render();
  setButton();
};
