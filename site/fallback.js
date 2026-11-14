// The 404 page (404.html): GitHub Pages serves it for a path the site does not have. The Worker
// (worker/) answers /download and /update/... on keepmum.app; with the Worker gone or skipping
// them (over its daily limit, fail open), they reach this page, which sends them to GitHub
// Releases, as the Worker would have.
(function () {
  var releases = "https://github.com/blueandhack/keepmum/releases";
  var path = location.pathname.replace(/\/+$/, "");
  var m = path.match(/^\/update\/(v[0-9A-Za-z.+-]{1,32})\/(Keepmum-[0-9A-Za-z.+-]{1,32}\.dmg)$/);
  var to = path === "/download" ? releases + "/latest" : m ? releases + "/download/" + m[1] + "/" + m[2] : null;
  if (to) {
    location.replace(to);
    var note = document.getElementById("moving");
    if (note) note.hidden = false;
  }
})();
