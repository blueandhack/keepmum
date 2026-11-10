// The Worker against an in-memory D1 (node:sqlite, with schema.sql), with GitHub and the feed faked.
// Every count is checked as rows: what a request adds, and that opted-out, bot and stray requests add none.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { beforeEach, test } from "node:test";

const here = new URL("..", import.meta.url);
const SCHEMA = readFileSync(new URL("schema.sql", here), "utf8");
// The update feed as it was at 0.1.0a6, kept here so a new release does not change what these expect.
const FEED = readFileSync(new URL("test/appcast.xml", here), "utf8");
const SPARKLE = "Keepmum/0.1.0a6 Sparkle/2.10.1";
const SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15";

/** D1's API over node:sqlite: prepare().bind().run()/all(), and batch(). */
function d1() {
  const db = new DatabaseSync(":memory:");
  db.exec(SCHEMA);
  const statement = (sql, args = []) => ({
    bind: (...a) => statement(sql, a),
    run: async () => ({ meta: { changes: db.prepare(sql).run(...args).changes } }),
    all: async () => ({ results: db.prepare(sql).all(...args).map((r) => ({ ...r })) }),
  });
  return {
    raw: db,
    prepare: (sql) => statement(sql),
    batch: async (stmts) => Promise.all(stmts.map((s) => s.run())),
  };
}

let env, worker, github, feed;
beforeEach(async () => {
  env = { DB: d1(), STATS_OPEN: "local" };
  github = 0;
  feed = FEED;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes("raw.githubusercontent.com")) return new Response(feed);
    if (u.startsWith("https://api.github.com/")) {
      github++;
      return Response.json([{ tag_name: "v0.1.0a6", assets: [{ name: "Keepmum-0.1.0a6.dmg", download_count: 42 }] },
        { tag_name: "v0.1.0a5", assets: [{ name: "Keepmum-0.1.0a5.dmg", download_count: 17 }, { name: "x.sha256", download_count: 3 }] }]);
    }
    return new Response("site", { status: 200 });
  };
  // A fresh module each test: the Worker remembers the versions it has noted, per isolate.
  worker = (await import(`../src/index.js?t=${Math.random()}`)).default;
});

async function get(path, headers = {}) {
  const waits = [];
  const res = await worker.fetch(new Request(`https://keepmum.app${path}`, { headers }), env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  return res;
}
const rows = (kind) => env.DB.raw.prepare("SELECT kind, version, n FROM counts WHERE kind LIKE ? ORDER BY kind, version").all(kind ?? "%")
  .map((r) => `${r.kind} ${r.version} ${r.n}`);

// --- Update checks -----------------------------------------------------------------------------

test("a check is counted under the app's version, with the firsts it says", async () => {
  const res = await get("/appcast.xml?v=0.1.0a7&first=week,new", { "user-agent": SPARKLE });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), FEED);
  assert.deepEqual(rows(), ["check 0.1.0a7 1", "new 0.1.0a7 1", "weekly 0.1.0a7 1"]);
});

test("the day's first check counts the Mac's macOS version and chip, once", async () => {
  await get("/appcast.xml?v=0.1.0a7&first=day,month&os=15&chip=arm64", { "user-agent": SPARKLE });
  await get("/appcast.xml?v=0.1.0a7&os=15&chip=arm64", { "user-agent": SPARKLE }); // a later check that day
  assert.deepEqual(rows(), ["check 0.1.0a7 2", "chip arm64 1", "daily 0.1.0a7 1", "macos 15 1", "monthly 0.1.0a7 1"]);
});

test("a macOS version or chip that is not one is left out", async () => {
  await get("/appcast.xml?v=0.1.0a7&first=day&os=fifteen&chip=m4", { "user-agent": SPARKLE });
  await get("/appcast.xml?v=0.1.0a7&first=day&os=150&chip=x86_64", { "user-agent": SPARKLE });
  assert.deepEqual(rows("macos"), []);
  assert.deepEqual(rows("chip"), ["chip x86_64 1"]);
});

test("a Mac with Count this Mac off, a bot, or a browser counts nothing, and still gets the feed", async () => {
  for (const headers of [{ "user-agent": SPARKLE, "keepmum-count": "off" }, { "user-agent": "curl/8.7.1" }, { "user-agent": SAFARI }, {}]) {
    const res = await get("/appcast.xml?v=0.1.0a7&first=day&os=15&chip=arm64", headers);
    assert.equal(res.status, 200);
  }
  assert.deepEqual(rows(), []);
});

test("an odd or missing app version is counted as other or unknown, not as written", async () => {
  await get("/appcast.xml?v=<script>", { "user-agent": SPARKLE });
  await get("/appcast.xml", { "user-agent": SPARKLE });
  assert.deepEqual(rows(), ["check other 1", "check unknown 1"]);
});

// --- Updates -----------------------------------------------------------------------------------

test("an update is counted with the path from the version Sparkle names", async () => {
  const res = await get("/update/v0.1.0a7/Keepmum-0.1.0a7.dmg", { "user-agent": SPARKLE });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("location"), "https://github.com/blueandhack/keepmum/releases/download/v0.1.0a7/Keepmum-0.1.0a7.dmg");
  assert.deepEqual(rows(), ["path 0.1.0a6>0.1.0a7 1", "update 0.1.0a7 1"]);
});

test("an update fetched without Sparkle counts no path, and a mismatched file name is not ours", async () => {
  await get("/update/v0.1.0a7/Keepmum-0.1.0a7.dmg", { "user-agent": SAFARI });
  const odd = await get("/update/v0.1.0a7/Keepmum-0.1.0a6.dmg", { "user-agent": SPARKLE });
  assert.equal(await odd.text(), "site");
  assert.deepEqual(rows(), ["update 0.1.0a7 1"]);
});

// --- Downloads ---------------------------------------------------------------------------------

test("a download goes to the newest DMG and is counted with where it came from", async () => {
  const res = await get("/download?via=news.ycombinator.com", { "user-agent": SAFARI });
  assert.equal(res.headers.get("location"), "https://github.com/blueandhack/keepmum/releases/download/v0.1.0a6/Keepmum-0.1.0a6.dmg");
  await get("/download?via=www.GitHub.com", { "user-agent": SAFARI });
  await get("/download?via=direct", { "user-agent": SAFARI });
  await get("/download?via=javascript:alert(1)", { "user-agent": SAFARI });
  assert.deepEqual(rows(), ["download  4", "via direct 1", "via github.com 1", "via news.ycombinator.com 1", "via other 1"]);
});

test("without ?via= a download is counted by its Referer's domain, never its path", async () => {
  await get("/download", { "user-agent": SAFARI, referer: "https://lobste.rs/s/abc123/keepmum?secret=1" });
  await get("/download", { "user-agent": SAFARI });
  assert.deepEqual(rows("via"), ["via direct 1", "via lobste.rs 1"]);
  assert.ok(!JSON.stringify(env.DB.raw.prepare("SELECT * FROM counts").all()).includes("abc123"));
});

test("a browser that asks not to be tracked downloads without being counted", async () => {
  for (const h of [{ "sec-gpc": "1" }, { dnt: "1" }]) {
    const res = await get("/download?via=github.com", { "user-agent": SAFARI, ...h });
    assert.equal(res.status, 302);
  }
  assert.deepEqual(rows(), []);
});

// --- New releases --------------------------------------------------------------------------------

test("a version the feed lists for the first time is noted, with GitHub's counts at once", async () => {
  await get("/appcast.xml", { "user-agent": "curl/8.7.1" }); // even a request that is not counted
  const versions = env.DB.raw.prepare("SELECT version, build, published FROM versions ORDER BY build").all();
  assert.deepEqual(versions.map((v) => v.version), ["0.1.0a2", "0.1.0a3", "0.1.0a4", "0.1.0a5", "0.1.0a6"]);
  assert.equal(versions.at(-1).build, 10006);
  assert.equal(versions.at(-1).published, "2026-11-09T09:14:26.000Z");
  assert.equal(github, 1);
  assert.deepEqual(env.DB.raw.prepare("SELECT version, n FROM releases ORDER BY version").all().map((r) => `${r.version} ${r.n}`),
    ["0.1.0a5 17", "0.1.0a6 42"]);
  await get("/appcast.xml", { "user-agent": SPARKLE });
  await get("/download", { "user-agent": SAFARI });
  assert.equal(github, 1); // known now: no more snapshots, no database reads for it
});

test("a release added to the feed later is noted then", async () => {
  await get("/appcast.xml", { "user-agent": SPARKLE });
  feed = FEED.replace("<item>", `<item>
      <title>keepmum 0.1.0a7</title>
      <pubDate>Tue, 10 Nov 2026 12:00:00 GMT</pubDate>
      <sparkle:version>10007</sparkle:version>
      <sparkle:shortVersionString>0.1.0a7</sparkle:shortVersionString>
      <enclosure url="https://keepmum.app/update/v0.1.0a7/Keepmum-0.1.0a7.dmg" length="1" type="application/octet-stream"/>
    </item>
    <item>`);
  await get("/appcast.xml", { "user-agent": SPARKLE });
  assert.equal(github, 2);
  assert.equal(env.DB.raw.prepare("SELECT published FROM versions WHERE version = '0.1.0a7'").get().published, "2026-11-10T12:00:00.000Z");
  const res = await get("/download", { "user-agent": SAFARI });
  assert.match(res.headers.get("location"), /v0\.1\.0a7\/Keepmum-0\.1\.0a7\.dmg$/);
});

test("the dashboard's data has the counts, GitHub's notes and the releases", async () => {
  await get("/appcast.xml?v=0.1.0a6&first=day&os=26&chip=arm64", { "user-agent": SPARKLE });
  const data = await (await get("/stats.json")).json();
  assert.deepEqual(data.versions.map((v) => v.version).at(-1), "0.1.0a6");
  assert.ok(data.counts.some((r) => r.kind === "macos" && r.version === "26"));
  assert.ok(data.releases.length >= 2 && data.generated);
  const page = await (await get("/stats")).text();
  assert.match(page, /keepmum counts/);
});

test("the dashboard refuses a request that did not come through Access", async () => {
  delete env.STATS_OPEN;
  const res = await get("/stats.json");
  assert.equal(res.status, 403);
});
