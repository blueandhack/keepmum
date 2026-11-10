-- One row per UTC hour, kind and app version: how many times. Nothing about who.
--   day:  the UTC hour, 2026-11-09T14; older rows hold a whole UTC day, 2026-11-09
--   kind: check     the app looked for an update (version: the app's own; unknown: a build without it in its feed URL)
--         download  keepmum.app/download, the website's buttons (version: empty)
--         update    the app downloaded an update (version: the one it updated to)
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
