# keepmum.app counts

A Cloudflare Worker on a few paths of keepmum.app. The rest of the site is GitHub Pages and does not go through it.

| Path | What it does | Counted as |
|---|---|---|
| `/appcast.xml` | The Mac app's update feed: `mac/appcast.xml` on `main`, byte for byte (the app checks its signature) | `check`, per app version (`?v=`, set by `mac/build-app.sh`) |
| `/download` | Redirects to the newest DMG in that feed. The website's download buttons link here. | `download` |
| `/update/v<version>/Keepmum-<version>.dmg` | Redirects to that DMG on GitHub. The feed points updates here (`mac/appcast.py`), from 0.1.0a5 on. | `update`, per version updated to |
| `/stats` | The dashboard, behind Cloudflare Access (below). `/stats.json` is its data. | |

Every six hours a cron notes GitHub's download count of each release's DMG (`releases` table), so downloads straight from GitHub show up too.

Each row in `counts` is a day, a kind, a version and a number. No IP address, user agent or anything else about a request is stored. Nothing is counted when the app opens: "installs in use" is the once-a-day update check of each copy with automatic checks on. Apps built before the feed moved here (0.1.0a4 and earlier) read GitHub directly and are not counted, though their upgrades to 0.1.0a5 and later are.

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
