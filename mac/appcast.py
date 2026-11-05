"""appcast.py: add a release to mac/appcast.xml, the feed the app's updater reads (Updater.swift).

    python3 mac/appcast.py --version 0.1.0a2 --build 10002 --dmg dist/Keepmum-0.1.0a2.dmg \
        --signature <sign_update's EdDSA signature> [--notes notes.md] [--url URL]

The newest release comes first; one with the same build number is replaced. The feed is written
without a signature: sign it afterwards (`sign_update mac/appcast.xml`), as release.sh does, since
the app requires a signed feed (SURequireSignedFeed). Standard library only.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from email.utils import formatdate
from pathlib import Path

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REPO = "https://github.com/blueandhack/keepmum"
FEED = Path(__file__).resolve().parent / "appcast.xml"
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="keepmum's version (0.1.0a2)")
    ap.add_argument("--build", required=True, help="the app's CFBundleVersion (build-app.sh)")
    ap.add_argument("--dmg", type=Path, required=True)
    ap.add_argument("--signature", required=True, help="the DMG's EdDSA signature (sign_update -p)")
    ap.add_argument("--notes", type=Path, help="release notes, in Markdown")
    ap.add_argument("--url", help="where the DMG is downloaded from (default: the GitHub release's asset)")
    ap.add_argument("--feed", type=Path, default=FEED)
    a = ap.parse_args()
    url = a.url or f"{REPO}/releases/download/v{a.version}/{a.dmg.name}"
    tree, channel = load(a.feed)
    for old in channel.findall("item"):
        if old.findtext(S("version")) == a.build:
            channel.remove(old)
    first = next((i for i, child in enumerate(channel) if child.tag == "item"), len(channel))
    channel.insert(first, item(a.version, a.build, url, a.dmg.stat().st_size, a.signature,
                               a.notes.read_text().strip() if a.notes else None))
    ET.indent(tree, space="  ")
    tree.write(a.feed, encoding="utf-8", xml_declaration=True)
    a.feed.write_text(a.feed.read_text() + "\n")
    print(f"{a.feed}: keepmum {a.version} (build {a.build}) at {url}")


if __name__ == "__main__":
    main()
