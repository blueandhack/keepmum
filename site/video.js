// The flow video plays by itself, muted (it has no sound), once half of it is on screen, and pauses
// when it scrolls away. Nothing loads before then (preload="none"). It stays still for a visitor who
// asked for reduced motion or to save data, and once they pause it or it ends, it is theirs: it never
// starts again on its own.
(function () {
  var video = document.querySelector("#video video");
  if (!video || !("IntersectionObserver" in window)) return;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var saveData = navigator.connection && navigator.connection.saveData;
  if (reduce || saveData) return;

  video.muted = true;
  var auto = true;      // still ours to play and pause
  var pausing = false;  // the pause in progress is ours, not the visitor's

  var observer = new IntersectionObserver(function (entries) {
    if (!auto) return;
    if (entries[0].isIntersecting) {
      var played = video.play();
      if (played && played.catch) played.catch(function () {});
    } else if (!video.paused) {
      pausing = true;
      video.pause();
    }
  }, { threshold: 0.5 });
  observer.observe(video);

  function handOver() {
    auto = false;
    observer.disconnect();
  }
  video.addEventListener("pause", function () {
    if (pausing) pausing = false;
    else handOver();
  });
  video.addEventListener("ended", handOver);
})();
