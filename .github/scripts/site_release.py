"""site_release.py: put the newest release in mac/appcast.xml (version, build time) on keepmum.app.

    python3 .github/scripts/site_release.py [site/index.html ...]

Fills each <span data-release="version"> and <time data-release="date"> in the pages, under the
download buttons. The newest release is the feed item with the highest sparkle:version, the one
keepmum.app/download redirects to (worker/src/index.js); its pubDate is when release.sh built it.
The Site workflow runs this before publishing, so a pushed feed updates the site.
"""

import re
import sys
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def newest(feed: Path) -> tuple[str, str, str]:
    items = list(ET.parse(feed).getroot().iter("item"))
    if not items:
        sys.exit(f"{feed}: no releases")
    item = max(items, key=lambda i: int(i.findtext(f"{SPARKLE}version") or 0))
    version = item.findtext(f"{SPARKLE}shortVersionString")
    when = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
    # The text without JavaScript; release.js rewrites it in the visitor's time zone.
    text = f"{when:%b} {when.day}, {when.year}, {when:%H:%M} UTC"
    return version, when.strftime("%Y-%m-%dT%H:%M:%SZ"), text


def main() -> None:
    version, iso, text = newest(ROOT / "mac/appcast.xml")
    pages = [Path(p) for p in sys.argv[1:]] or [ROOT / "site/index.html"]
    for page in pages:
        html = page.read_text()
        html, nv = re.subn(r'(<span data-release="version">)[^<]*(</span>)', rf"\g<1>{version}\g<2>", html)
        html, nd = re.subn(r'<time data-release="date" datetime="[^"]*">[^<]*</time>',
                           f'<time data-release="date" datetime="{iso}">{text}</time>', html)
        if not nv or nv != nd:
            sys.exit(f"{page}: expected matching data-release version and date, found {nv} and {nd}")
        page.write_text(html)
        print(f"{page}: {version}, {text} ({nv} places)")


if __name__ == "__main__":
    main()
