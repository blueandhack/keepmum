# keepmum.app counts

A Cloudflare Worker on two paths of keepmum.app. The rest of the site is GitHub Pages and does not go through it.

| Path | What it does | Counted as |
|---|---|---|
| `/appcast.xml` | The Mac app's update feed: `mac/appcast.xml` on `main`, byte for byte (the app checks its signature) | `check`, per app version (`?v=`, set by `mac/build-app.sh`) |
| `/download` | Redirects to the newest DMG in that feed. The website's download buttons link here. | `download` |

Each row in the `counts` table is a day, a kind, a version and a number. No IP address, user agent or anything else about a request is stored. An installed app checks once a day if automatic checks are on, so checks per day are roughly the number of installs in use. Apps built before the feed moved here (0.1.0a4 and earlier) still read GitHub directly and are not counted.

Free on Cloudflare's Workers Free plan up to 100,000 requests a day.

## Set up (once)

```sh
cd worker
npx wrangler login
npx wrangler d1 create keepmum-counts          # put the database_id it prints in wrangler.toml
npx wrangler d1 execute keepmum-counts --remote --file schema.sql
npx wrangler deploy
```

Deploy before the site links to `/download`: until the Worker answers there, GitHub Pages returns 404.

## Read the counts

```sh
cd worker
npx wrangler d1 execute keepmum-counts --remote --command \
  "SELECT day, kind, version, n FROM counts ORDER BY day DESC, kind, version"
```

## Try it locally

```sh
npx wrangler d1 execute keepmum-counts --local --file schema.sql
npx wrangler dev --local    # http://127.0.0.1:8787/appcast.xml?v=0.1.0a5 and /download
```
