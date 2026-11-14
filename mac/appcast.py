"""appcast.py: add a release to mac/appcast.xml, the feed the app's updater reads (Updater.swift).

    python3 mac/appcast.py --version 0.1.0a2 --build 10002 --dmg dist/Keepmum-0.1.0a2.dmg \
        --signature <sign_update's EdDSA signature> [--notes notes.md] [--url URL]

    python3 mac/appcast.py --github-copy     # only (re)write appcast-github.xml from appcast.xml

The newest release comes first; one with the same build number is replaced. The feed is written
without a signature: sign it afterwards (`sign_update mac/appcast.xml`), as release.sh does, since
the app requires a signed feed (SURequireSignedFeed). Standard library only.

It also writes appcast-github.xml: the same feed with each download straight from GitHub Releases,
not through keepmum.app/update/ (the Worker). The website publishes it, signed, as
keepmum.app/appcast.xml, which installed apps reach when the Worker is gone or skips them
(worker/README.md, "If the Worker goes away"); release.sh signs it with the feed.
"""

from __future__ import annotations

import argparse
import copy
import re
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REPO = "https://github.com/blueandhack/keepmum"
FEED = Path(__file__).resolve().parent / "appcast.xml"
GITHUB_COPY = "appcast-github.xml"
_THROUGH_WORKER = re.compile(r"^https://keepmum\.app/update/(v[0-9A-Za-z.+-]{1,32})/([^/]+\.dmg)$")
MINIMUM_SYSTEM = "14.0"

ET.register_namespace("sparkle", SPARKLE)
S = lambda tag: f"{{{SPARKLE}}}{tag}"


def load(feed: Path) -> tuple[ET.ElementTree, ET.Element]:
    if feed.exists():
        tree = ET.parse(feed)  # comments, among them an earlier signature, are dropped
        return tree, tree.getroot().find("channel")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "keepmum"
    ET.SubElement(channel, "link").text = REPO
    ET.SubElement(channel, "description").text = "Releases of the keepmum Mac app"
    ET.SubElement(channel, "language").text = "en"
    return ET.ElementTree(rss), channel


def item(version: str, build: str, url: str, length: int, signature: str, notes: str | None) -> ET.Element:
    it = ET.Element("item")
    ET.SubElement(it, "title").text = f"keepmum {version}"
    ET.SubElement(it, "pubDate").text = formatdate(usegmt=True)
    ET.SubElement(it, S("version")).text = build
    ET.SubElement(it, S("shortVersionString")).text = version
    ET.SubElement(it, S("minimumSystemVersion")).text = MINIMUM_SYSTEM
    ET.SubElement(it, S("fullReleaseNotesLink")).text = f"{REPO}/releases/tag/v{version}"
    if notes:
        ET.SubElement(it, "description", {S("format"): "markdown"}).text = notes
    ET.SubElement(it, "enclosure", {"url": url, "length": str(length), "type": "application/octet-stream",
                                    S("edSignature"): signature})
    return it


def github_url(url: str) -> str:
    """A keepmum.app/update/ download as the GitHub Releases asset it redirects to; others as they are."""
    m = _THROUGH_WORKER.match(url)
    return f"{REPO}/releases/download/{m.group(1)}/{m.group(2)}" if m else url


def write(tree: ET.ElementTree, path: Path) -> None:
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    path.write_text(path.read_text() + "\n")


def write_github_copy(feed: Path) -> Path:
    """appcast-github.xml beside feed: the feed with its downloads straight from GitHub, unsigned."""
    tree = copy.deepcopy(ET.parse(feed))  # the feed's signature, a comment, is dropped
    for enclosure in tree.getroot().iter("enclosure"):
        enclosure.set("url", github_url(enclosure.get("url", "")))
    out = feed.with_name(GITHUB_COPY)
    write(tree, out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--github-copy", action="store_true", help="only write appcast-github.xml from the feed")
    ap.add_argument("--version", help="keepmum's version (0.1.0a2)")
    ap.add_argument("--build", help="the app's CFBundleVersion (build-app.sh)")
    ap.add_argument("--dmg", type=Path)
    ap.add_argument("--signature", help="the DMG's EdDSA signature (sign_update -p)")
    ap.add_argument("--notes", type=Path, help="release notes, in Markdown")
    ap.add_argument("--url", help="where the DMG is downloaded from (default: keepmum.app/update/..., which counts the update and redirects to the GitHub release's asset; worker/)")
    ap.add_argument("--feed", type=Path, default=FEED)
    a = ap.parse_args()
    if a.github_copy:
        print(f"{write_github_copy(a.feed)}: downloads from GitHub Releases, unsigned")
        return
    if not (a.version and a.build and a.dmg and a.signature):
        ap.error("--version, --build, --dmg and --signature are required to add a release")
    url = a.url or f"https://keepmum.app/update/v{a.version}/{a.dmg.name}"
    tree, channel = load(a.feed)
    for old in channel.findall("item"):
        if old.findtext(S("version")) == a.build:
            channel.remove(old)
    first = next((i for i, child in enumerate(channel) if child.tag == "item"), len(channel))
    channel.insert(first, item(a.version, a.build, url, a.dmg.stat().st_size, a.signature,
                               a.notes.read_text().strip() if a.notes else None))
    write(tree, a.feed)
    print(f"{a.feed}: keepmum {a.version} (build {a.build}) at {url}")
    print(f"{write_github_copy(a.feed)}: the same, its downloads from GitHub Releases")


if __name__ == "__main__":
    main()
