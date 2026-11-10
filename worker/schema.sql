-- One row per UTC hour, kind and app version: how many times. Nothing about who.
--   day:  the UTC hour, 2026-11-09T14; older rows hold a whole UTC day, 2026-11-09
--   kind: check     the app looked for an update (version: the app's own; unknown: a build without it in its feed URL)
--         daily     that check was the Mac's first counted one this UTC day (weekly: ISO week, monthly:
--         weekly    month; new: its first ever). The app says so (?first=); no identifier is kept or sent.
--         monthly
--         new
--         download  keepmum.app/download, the website's buttons (version: empty)
--         via       the same downloads by where they came from (version: a domain such as github.com,
--                   "keepmum.app", "direct" or "other"; never a path)
--         update    the app downloaded an update (version: the one it updated to)
--         path      the same updates by where they came from (version: from>to, 0.1.0a5>0.1.0a7,
--                   the "from" read from Sparkle's user agent)
--         macos     a Mac's macOS major version, with its first check of the UTC day (version: 15)
--         chip      its chip, the same way (version: arm64 or x86_64)
CREATE TABLE IF NOT EXISTS counts (
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  version TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, kind, version)
);

-- GitHub's running download count of each release's DMG, as seen at each note (every six hours).
--   day:  the UTC hour of the note, 2026-11-09T06; older rows hold a UTC day's last note
-- It includes the downloads above that redirect to GitHub.
CREATE TABLE IF NOT EXISTS releases (
  day TEXT NOT NULL,
  version TEXT NOT NULL,
  n INTEGER NOT NULL,
  PRIMARY KEY (day, version)
);

-- Each release the update feed has listed: its build number and publication date (the feed's
-- pubDate), and the UTC hour the Worker first saw it there, which is when it noted GitHub's counts.
CREATE TABLE IF NOT EXISTS versions (
  version TEXT PRIMARY KEY,
  build INTEGER NOT NULL,
  published TEXT,
  seen TEXT NOT NULL
);
