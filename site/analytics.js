// Cloudflare Web Analytics (no cookies, no fingerprinting), loaded only for a browser that has not
// asked to opt out: Global Privacy Control or Do Not Track keeps it off (keepmum.app/privacy).
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

  var via = "direct";
  try {
    if (document.referrer) via = new URL(document.referrer).hostname.replace(/^www\./, "") || "direct";
  } catch (e) {}
  var links = document.querySelectorAll('a[href="/download"]');
  for (var i = 0; i < links.length; i++) links[i].setAttribute("href", "/download?via=" + encodeURIComponent(via));
})();
