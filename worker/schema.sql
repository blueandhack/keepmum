-- One row per day, kind and app version: how many times. Nothing about who.
--   kind: check (the app looked for an update) or download (keepmum.app/download)
--   version: the app's version for a check (unknown: a build without it in its feed URL); empty for a download
CREATE TABLE IF NOT EXISTS counts (
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  version TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, kind, version)
);
