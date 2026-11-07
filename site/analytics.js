// Cloudflare Web Analytics (no cookies, no fingerprinting), loaded only for a browser that has not
// asked to opt out: Global Privacy Control or Do Not Track keeps it off (keepmum.app/privacy).
(function () {
  var dnt = navigator.doNotTrack === "1" || window.doNotTrack === "1";
  if (navigator.globalPrivacyControl === true || dnt) return;
  var s = document.createElement("script");
  s.defer = true;
  s.src = "https://static.cloudflareinsights.com/beacon.min.js";
  s.setAttribute("data-cf-beacon", '{"token": "3fa83345f68e452f8798221aaad356da"}');
  document.head.appendChild(s);
})();
