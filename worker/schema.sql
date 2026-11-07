-- One row per day, kind and app version: how many times. Nothing about who.
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

-- GitHub's running download count of each release's DMG, as last seen that day (every six hours).
-- It includes the downloads above that redirect to GitHub.
CREATE TABLE IF NOT EXISTS releases (
  day TEXT NOT NULL,
  version TEXT NOT NULL,
  n INTEGER NOT NULL,
  PRIMARY KEY (day, version)
);
