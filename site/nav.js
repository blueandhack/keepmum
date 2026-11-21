// keepmum.app's section bar and the install command's Copy button.
//
// The bar (#bar) slides in once the hero's own nav has scrolled out of view, and marks the link of
// the section in the middle of the screen (a link's data-for lists the section ids it stands for).
// Without this script the bar stays hidden and the hero's nav is the page's nav.
(function () {
  "use strict";
  const bar = document.getElementById("bar");
  const top = document.querySelector(".hero .top");
  if (bar && top && "IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => bar.classList.toggle("show", !e.isIntersecting)).observe(top);

    const links = [...bar.querySelectorAll("[data-for]")];
    const owner = new Map();
    for (const a of links) for (const id of a.dataset.for.split(" ")) owner.set(id, a);
    const sections = [...owner.keys()].map((id) => document.getElementById(id)).filter(Boolean);
    const visible = new Set();
    const mark = () => {
      // The first section, in page order, that crosses the middle of the screen.
      const s = sections.find((x) => visible.has(x));
      const current = s ? owner.get(s.id) : null;
      for (const a of links) {
        if (a === current) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      }
      // On a phone the links scroll sideways: keep the marked one in view.
      const strip = current && current.parentElement;
      if (strip && strip.tagName === "NAV" && strip.scrollWidth > strip.clientWidth) {
        const l = current.offsetLeft - strip.offsetLeft, r = l + current.offsetWidth;
        if (l < strip.scrollLeft || r > strip.scrollLeft + strip.clientWidth) strip.scrollTo({ left: l - 8 });
      }
    };
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) e.isIntersecting ? visible.add(e.target) : visible.delete(e.target);
      mark();
    }, { rootMargin: "-50% 0px -50% 0px" });
    sections.forEach((s) => io.observe(s));
  }

  // Copy, on the command-line install steps.
  const pre = document.getElementById("cli-steps");
  if (pre && navigator.clipboard) {
    const box = document.createElement("div");
    box.className = "code";
    pre.replaceWith(box);
    box.appendChild(pre);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy the install commands");
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText.trim() + "\n");
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select and copy";
      }
      setTimeout(() => { button.textContent = "Copy"; }, 2000);
    });
    box.appendChild(button);
  }
})();
