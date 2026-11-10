// The build time under the download buttons, in the visitor's own time zone. Without this script
// it reads in UTC, as .github/scripts/site_release.py wrote it.
(function () {
  var times = document.querySelectorAll('time[data-release="date"]');
  for (var i = 0; i < times.length; i++) {
    var t = new Date(times[i].getAttribute("datetime"));
    if (isNaN(t)) continue;
    try {
      times[i].textContent = t.toLocaleString("en-US", {
        year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
      });
    } catch (e) {}
  }
})();
