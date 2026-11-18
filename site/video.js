// The videos play by themselves, muted (they have no sound), once half of one is on screen: the app
// tour, and the flow where flow.js does not draw it in the page instead, and pause when it scrolls
// away. Nothing loads before then (preload="none").
// They stay still for a visitor who asked for reduced motion or to save data, and once they pause
// one or it ends, it is theirs: it never starts again on its own.
(function () {
  if (!("IntersectionObserver" in window)) return;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var saveData = navigator.connection && navigator.connection.saveData;
  if (reduce || saveData) return;

  var videos = document.querySelectorAll("figure.video video");
  for (var i = 0; i < videos.length; i++) autoplay(videos[i]);

  function autoplay(video) {
    video.muted = true;
    var pausing = false;  // the pause in progress is ours, not the visitor's
    var settle = null;    // a video scrolled past, not stopped at, never starts loading

    var observer = new IntersectionObserver(function (entries) {
      clearTimeout(settle);
      if (entries[0].isIntersecting) {
        settle = setTimeout(function () {
          var played = video.play();
          if (played && played.catch) played.catch(function () {});
        }, 500);
      } else if (!video.paused) {
        pausing = true;
        video.pause();
      }
    }, { threshold: 0.5 });
    observer.observe(video);

    function handOver() {
      clearTimeout(settle);
      observer.disconnect();
    }
    video.addEventListener("pause", function () {
      if (pausing) pausing = false;
      else handOver();
    });
    video.addEventListener("ended", handOver);
  }
})();
