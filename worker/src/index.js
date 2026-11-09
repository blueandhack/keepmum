// keepmum.app's counted paths. Counts only: no IP address, user agent or anything else about a
// request is stored.
//
//   /appcast.xml   the Mac app's update feed, mac/appcast.xml on main byte for byte (the app checks
//                  its EdDSA signature); counted as a check, per app version (?v=)
//   /download      the website's download buttons: redirects to the newest DMG; counted
//   /update/v<version>/Keepmum-<version>.dmg
//                  what the feed points updates at: redirects to that DMG on GitHub; counted as an
//                  update to <version>
//   /stats         the dashboard (dashboard.js), only behind Cloudflare Access
//   /video/<file>  the flow video and the like, from the keepmum-media R2 bucket; not counted
//
// Every six hours (cron), GitHub's download count for each release's DMG is noted, so downloads
// straight from GitHub show up too.
//
// Nothing is counted for a Mac with "Count this Mac" off (the app sends Keepmum-Count: off) or a
// browser that asks not to be tracked (Global Privacy Control, Do Not Track): keepmum.app/privacy.

import { dashboard } from "./dashboard.js";

const REPO = "blueandhack/keepmum";
const FEED = `https://raw.githubusercontent.com/${REPO}/main/mac/appcast.xml`;
const RELEASES = `https://github.com/${REPO}/releases`;
const VERSION = "[0-9A-Za-z.+-]{1,32}";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const counted = request.method === "GET" && !optedOut(request);
    if (url.pathname === "/appcast.xml") {
      const feed = await getFeed();
      if (!feed) return new Response("The update feed is unavailable; try again later.\n", { status: 502 });
      if (counted) ctx.waitUntil(count(env, "check", appVersion(url)));
      return new Response(request.method === "HEAD" ? null : feed, {
        headers: { "content-type": "application/xml; charset=utf-8", "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/download") {
      const feed = await getFeed();
      const dmg = feed && newestDMG(new TextDecoder().decode(feed));
      if (!dmg) return Response.redirect(RELEASES, 302);
      if (counted) ctx.waitUntil(count(env, "download", ""));
      return Response.redirect(dmg, 302);
    }
    const update = url.pathname.match(new RegExp(`^/update/v(${VERSION})/(Keepmum-(${VERSION})\\.dmg)$`));
    if (update && update[1] === update[3]) {
      if (counted) ctx.waitUntil(count(env, "update", update[1]));
      return Response.redirect(`${RELEASES}/download/v${update[1]}/${update[2]}`, 302);
    }
    const file = url.pathname.match(/^\/video\/([A-Za-z0-9._-]{1,100}\.(?:mp4|webm|vtt))$/);
    if (file && (request.method === "GET" || request.method === "HEAD")) return media(request, env, file[1]);
    if (url.pathname === "/stats" || url.pathname === "/stats.json") {
      const who = await accessUser(request, env);
      if (!who) return denied();
      const data = await statsData(env, ctx);
      if (url.pathname === "/stats.json") return Response.json(data, { headers: { "cache-control": "no-store" } });
      return new Response(dashboard(data, who), {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
          "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
          "x-robots-tag": "noindex",
        },
      });
    }
    return fetch(request); // not ours: the site, as it would be without this Worker
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(snapshotGitHub(env));
  },
};

/**
 * A file from the media bucket, with byte ranges (a browser seeks a video with them). Names carry a
 * hash of the content (keepmum-flow-<hash>.mp4), so a file never changes and is cached for a year.
 */
async function media(request, env, key) {
  let object;
  try {
    object = request.method === "HEAD" ? await env.MEDIA.head(key)
      : await env.MEDIA.get(key, { range: request.headers, onlyIf: request.headers });
  } catch {
    return new Response(null, { status: 416 }); // a range past the end
  }
  if (!object) return new Response("Not found\n", { status: 404 });
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("accept-ranges", "bytes");
  headers.set("cache-control", "public, max-age=31536000, immutable");
  headers.set("x-content-type-options", "nosniff");
  if (request.method === "HEAD") {
    headers.set("content-length", String(object.size));
    return new Response(null, { headers });
  }
  if (!object.body) return new Response(null, { status: 304, headers }); // If-None-Match matched
  // R2 answers a range that starts past the end with the whole file; HTTP says 416.
  const start = request.headers.get("range")?.match(/^bytes=(\d+)-/);
  if (start && Number(start[1]) >= object.size) {
    return new Response(null, { status: 416, headers: { "content-range": `bytes */${object.size}` } });
  }
  const range = request.headers.has("range") ? object.range : undefined;
  if (range) {
    const offset = "suffix" in range ? object.size - range.suffix : (range.offset ?? 0);
    const length = "suffix" in range ? range.suffix : (range.length ?? object.size - offset);
    headers.set("content-range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set("content-length", String(length));
    return new Response(object.body, { status: 206, headers });
  }
  headers.set("content-length", String(object.size));
  return new Response(object.body, { headers });
}

/** The request asks not to be counted: the app's own opt-out, or the browser's. */
function optedOut(request) {
  const h = request.headers;
  return h.get("keepmum-count") === "off" || h.get("sec-gpc") === "1" || h.get("dnt") === "1";
}

/** The feed's bytes, cached at Cloudflare for five minutes; null if GitHub does not answer. */
async function getFeed() {
  try {
    const r = await fetch(FEED, { cf: { cacheTtl: 300, cacheEverything: true } });
    return r.ok ? await r.arrayBuffer() : null;
  } catch {
    return null;
  }
}

/** The version the app put in its feed URL (`?v=0.1.0a5`, from build-app.sh); "unknown" without one. */
function appVersion(url) {
  const v = url.searchParams.get("v");
  if (v === null) return "unknown";
  return new RegExp(`^${VERSION}$`).test(v) ? v : "other";
}

/** The GitHub DMG of the release with the highest build number in the feed, as Sparkle would choose. */
function newestDMG(xml) {
  let best = null;
  for (const item of xml.split("<item>").slice(1)) {
    const build = Number(item.match(/<sparkle:version>(\d+)<\/sparkle:version>/)?.[1]);
    let dmg = item.match(/<enclosure[^>]*\burl="([^"]+\.dmg)"/)?.[1];
    // A feed entry that points at /update/ (and so would count an update) maps to GitHub directly.
    dmg = dmg?.replace(/^https:\/\/keepmum\.app\/update\//, `${RELEASES}/download/`);
    if (!dmg || !Number.isFinite(build) || !dmg.startsWith(RELEASES + "/download/")) continue;
    if (!best || build > best.build) best = { build, dmg };
  }
  return best?.dmg ?? null;
}

const today = () => new Date().toISOString().slice(0, 10);

/** One more for today. A counting failure never fails the request. */
async function count(env, kind, version) {
  try {
    await env.DB.prepare(
      "INSERT INTO counts (day, kind, version, n) VALUES (?1, ?2, ?3, 1) " +
        "ON CONFLICT (day, kind, version) DO UPDATE SET n = n + 1",
    ).bind(today(), kind, version).run();
  } catch (e) {
    console.error("count failed", e);
  }
}

/** GitHub's running download count of each release's DMG, noted for today (the latest one wins). */
async function snapshotGitHub(env) {
  const headers = { "user-agent": "keepmum-counts", accept: "application/vnd.github+json" };
  if (env.GITHUB_TOKEN) headers.authorization = `Bearer ${env.GITHUB_TOKEN}`;
  const r = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=100`, { headers });
  if (!r.ok) {
    console.error("GitHub releases", r.status, await r.text());
    return;
  }
  const day = today();
  const rows = [];
  for (const release of await r.json()) {
    for (const asset of release.assets ?? []) {
      if (asset.name.endsWith(".dmg")) rows.push([release.tag_name.replace(/^v/, ""), asset.download_count]);
    }
  }
  if (!rows.length) return;
  await env.DB.batch(rows.map(([version, n]) =>
    env.DB.prepare(
      "INSERT INTO releases (day, version, n) VALUES (?1, ?2, ?3) ON CONFLICT (day, version) DO UPDATE SET n = excluded.n",
    ).bind(day, version, n)));
}

async function statsData(env, ctx) {
  const [counts, releases] = await Promise.all([
    env.DB.prepare("SELECT day, kind, version, n FROM counts ORDER BY day").all(),
    env.DB.prepare("SELECT day, version, n FROM releases ORDER BY day").all(),
  ]);
  // The first look of a day takes a GitHub snapshot if the cron has not yet.
  if (!releases.results.some((r) => r.day === today())) ctx.waitUntil(snapshotGitHub(env));
  return { counts: counts.results, releases: releases.results, generated: new Date().toISOString() };
}

// --- Cloudflare Access ------------------------------------------------------------------------
// Access signs in whoever opens /stats and passes a signed token (Cf-Access-Jwt-Assertion). The
// page trusts only a token this Worker verifies itself against the team's keys, for this
// application (ACCESS_AUD): a request that did not come through Access gets nothing.

async function accessUser(request, env) {
  if (env.STATS_OPEN === "local") return "local preview"; // wrangler dev only (.dev.vars), never deployed
  const team = env.ACCESS_TEAM_DOMAIN, aud = env.ACCESS_AUD;
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!team || !aud || !token) return null;
  try {
    const [h, p, s] = token.split(".");
    const header = JSON.parse(text(h)), claims = JSON.parse(text(p));
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (header.alg !== "RS256" || !auds.includes(aud) || claims.iss !== `https://${team}`) return null;
    if (!(claims.exp * 1000 > Date.now())) return null;
    const certs = await (await fetch(`https://${team}/cdn-cgi/access/certs`, { cf: { cacheTtl: 3600 } })).json();
    const jwk = certs.keys?.find((k) => k.kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, bytes(s), new TextEncoder().encode(`${h}.${p}`));
    return ok ? claims.email || "signed in" : null;
  } catch {
    return null;
  }
}

function bytes(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((b64url.length + 3) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}
const text = (b64url) => new TextDecoder().decode(bytes(b64url));

function denied() {
  return new Response(
    "keepmum stats are behind Cloudflare Access, and this request did not come through it.\n" +
      "Set up: worker/README.md, \"Dashboard\".\n",
    { status: 403, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } },
  );
}
