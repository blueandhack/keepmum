# keepmum.app counts

A Cloudflare Worker on a few paths of keepmum.app. The rest of the site is GitHub Pages and does not go through it.

| Path | What it does | Counted as |
|---|---|---|
| `/appcast.xml` | The Mac app's update feed: `mac/appcast.xml` on `main`, byte for byte (the app checks its signature) | `check`, per app version (`?v=`, set by `mac/build-app.sh`) |
| `/download` | Redirects to the newest DMG in that feed. The website's download buttons link here. | `download` |
| `/update/v<version>/Keepmum-<version>.dmg` | Redirects to that DMG on GitHub. The feed points updates here (`mac/appcast.py`), from 0.1.0a5 on. | `update`, per version updated to |
| `/stats` | The dashboard, behind Cloudflare Access (below). `/stats.json` is its data. | |
| `/video/<file>` | The flow video, from the `keepmum-media` R2 bucket, with byte ranges for seeking (below). | not counted |

Every six hours a cron notes GitHub's download count of each release's DMG (`releases` table), so downloads straight from GitHub show up too.

Each row in `counts` is an hour (UTC), a kind, a version and a number; the dashboard adds the hours up into days in the viewer's time zone. Rows from before hours were kept are whole UTC days. No IP address, user agent or anything else about a request is stored. Nothing is counted when the app opens: "installs in use" is the once-a-day update check of each copy with automatic checks on. Apps built before the feed moved here (0.1.0a4 and earlier) read GitHub directly and are not counted, though their upgrades to 0.1.0a5 and later are.

Free on Cloudflare's Workers Free plan up to 100,000 requests a day.

## Set up (once)

```sh
cd worker
npx wrangler login
npx wrangler d1 create keepmum-counts          # put the database_id it prints in wrangler.toml
npx wrangler d1 execute keepmum-counts --remote --file schema.sql
npx wrangler deploy
```

`schema.sql` only creates what is missing, so run it again after it gains a table.

## Dashboard

`keepmum.app/stats` shows installs in use, downloads, upgrades and each release. Cloudflare Access signs you in (an emailed code); the Worker checks Access's signed token itself, so with Access off, or a request that skipped it, the page refuses everyone.

1. Cloudflare dashboard › **Zero Trust**. The first time, pick a team name (your team domain is `<team>.cloudflareaccess.com`) and the Free plan.
2. **Access › Applications › Add an application › Self-hosted.** Name: `keepmum stats`. Add two public hostnames: domain `keepmum.app`, path `stats`; and domain `keepmum.app`, path `stats.json`.
3. Policy: **Allow**, Include **Emails**: your address. The login method can stay **One-time PIN**.
4. Save, then copy the application's **Application Audience (AUD) Tag** (its Overview).
5. In `wrangler.toml`, set `ACCESS_TEAM_DOMAIN = "<team>.cloudflareaccess.com"` and `ACCESS_AUD = "<the tag>"`, then `npx wrangler deploy`.

Optional: GitHub's API allows 60 unauthenticated requests an hour per address, shared on Cloudflare's network. If the cron's log shows 403s, give it a token with read access to public repositories: `npx wrangler secret put GITHUB_TOKEN`.

## Video

The website's flow video (`docs/video/flow.swift`) is too big for git, so it lives in an R2 bucket and the Worker serves it at `keepmum.app/video/`. Its name carries a hash of its content, so a new render gets a new name and each one can be cached for a year.

```sh
npx wrangler r2 bucket create keepmum-media                     # once
swift docs/video/flow.swift docs/video/keepmum-flow.mp4         # from the repository root
H=$(shasum -a 256 docs/video/keepmum-flow.mp4 | cut -c1-10)
cd worker && npx wrangler r2 object put "keepmum-media/keepmum-flow-$H.mp4" --remote \
  --file ../docs/video/keepmum-flow.mp4 --content-type video/mp4
```

Then point `site/index.html` at `/video/keepmum-flow-$H.mp4`, and refresh the poster (`site/img/flow-poster.webp`, a still from `swift docs/video/flow.swift --frames`). R2's free tier covers 10 GB stored and charges nothing for what is downloaded.

## Read the counts without the dashboard

```sh
cd worker
npx wrangler d1 execute keepmum-counts --remote --command \
  "SELECT day, kind, version, n FROM counts ORDER BY day DESC, kind, version"
```

Or the D1 console in the Cloudflare dashboard (Storage & Databases › D1 › keepmum-counts).

## Try it locally

```sh
npx wrangler d1 execute keepmum-counts --local --file schema.sql
echo STATS_OPEN=local > .dev.vars     # /stats without Access, in wrangler dev only (git-ignored)
npx wrangler dev --local              # http://127.0.0.1:8787/stats, /appcast.xml?v=0.1.0a5, /download
```
