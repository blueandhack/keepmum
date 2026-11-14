// Cloudflare Web Analytics (no cookies, no fingerprinting) and Google Analytics (G-WXVQPH2MGK, with
// Google signals and ad personalisation off), loaded only for a browser that has not asked to opt out:
// Global Privacy Control or Do Not Track keeps both off (keepmum.app/privacy).
// For the same browsers only, the Download buttons say which site sent the visitor here
// (/download?via=github.com): the domain of document.referrer, never the page; "direct" without one.
(function () {
  var dnt = navigator.doNotTrack === "1" || window.doNotTrack === "1";
  if (navigator.globalPrivacyControl === true || dnt) return;
  var s = document.createElement("script");
  s.defer = true;
  s.src = "https://static.cloudflareinsights.com/beacon.min.js";
  s.setAttribute("data-cf-beacon", '{"token": "3fa83345f68e452f8798221aaad356da"}');
  document.head.appendChild(s);

  var ga = document.createElement("script");
  ga.async = true;
  ga.src = "https://www.googletagmanager.com/gtag/js?id=G-WXVQPH2MGK";
  document.head.appendChild(ga);
  window.dataLayer = window.dataLayer || [];
  window.gtag = function () { window.dataLayer.push(arguments); };
  window.gtag("js", new Date());
  window.gtag("config", "G-WXVQPH2MGK", { allow_google_signals: false, allow_ad_personalization_signals: false });

  var via = "direct";
  try {
    if (document.referrer) via = new URL(document.referrer).hostname.replace(/^www\./, "") || "direct";
  } catch (e) {}
  var links = document.querySelectorAll('a[href="/download"]');
  for (var i = 0; i < links.length; i++) {
    links[i].setAttribute("href", "/download?via=" + encodeURIComponent(via));
    links[i].addEventListener("click", function () {
      window.gtag("event", "download_click", { via: via });
    });
  }
})();
