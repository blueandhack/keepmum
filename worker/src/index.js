// keepmum.app/appcast.xml and keepmum.app/download: the Mac app's update feed and the DMG link,
// each counted per day; update checks also per app version. Counts only: no IP address, user
// agent or anything else about a request is stored.
//
// The feed is mac/appcast.xml on GitHub, passed through byte for byte: the app checks its EdDSA
// signature (SURequireSignedFeed), so it must not change on the way.

const FEED = "https://raw.githubusercontent.com/blueandhack/keepmum/main/mac/appcast.xml";
const RELEASES = "https://github.com/blueandhack/keepmum/releases";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/appcast.xml") {
      const feed = await getFeed();
      if (!feed) return new Response("The update feed is unavailable; try again later.\n", { status: 502 });
      if (request.method === "GET") ctx.waitUntil(count(env, "check", appVersion(url)));
      return new Response(request.method === "HEAD" ? null : feed, {
        headers: { "content-type": "application/xml; charset=utf-8", "cache-control": "no-store" },
      });
    }
    if (url.pathname === "/download") {
      const feed = await getFeed();
      const dmg = feed && newestDMG(new TextDecoder().decode(feed));
      if (!dmg) return Response.redirect(RELEASES, 302);
      if (request.method === "GET") ctx.waitUntil(count(env, "download", ""));
      return Response.redirect(dmg, 302);
    }
    return fetch(request); // not ours: the site, as it would be without this Worker
  },
};

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
  return /^[0-9A-Za-z.+-]{1,32}$/.test(v) ? v : "other";
}

/** The DMG of the release with the highest build number in the feed, as Sparkle would choose. */
function newestDMG(xml) {
  let best = null;
  for (const item of xml.split("<item>").slice(1)) {
    const build = Number(item.match(/<sparkle:version>(\d+)<\/sparkle:version>/)?.[1]);
    const dmg = item.match(/<enclosure[^>]*\burl="([^"]+\.dmg)"/)?.[1];
    if (!dmg || !Number.isFinite(build) || !dmg.startsWith(RELEASES + "/download/")) continue;
    if (!best || build > best.build) best = { build, dmg };
  }
  return best?.dmg ?? null;
}

/** One more for today. A counting failure never fails the request. */
async function count(env, kind, version) {
  try {
    const day = new Date().toISOString().slice(0, 10);
    await env.DB.prepare(
      "INSERT INTO counts (day, kind, version, n) VALUES (?1, ?2, ?3, 1) " +
        "ON CONFLICT (day, kind, version) DO UPDATE SET n = n + 1",
    ).bind(day, kind, version).run();
  } catch (e) {
    console.error("count failed", e);
  }
}
