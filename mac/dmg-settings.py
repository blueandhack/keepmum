# dmgbuild settings for keepmum's DMG (make-dmg.sh): the app, a link to /Applications, the window's
# background (Keepmum --dmg-background, DMG.swift) and the icons' places on it, and the app's icon
# as the volume's. dmgbuild writes Finder's .DS_Store itself, so no Finder scripting is needed.
# Defines: app (the .app), background (the image), icon (an .icns).
import os.path

app = defines["app"]  # noqa: F821 (dmgbuild provides defines)
appname = os.path.basename(app)

format = "UDZO"
filesystem = "APFS"
files = [app]
symlinks = {"Applications": "/Applications"}
# No hide_extensions: it writes Finder information onto the app bundle, which breaks the strict
# check of its code signature (codesign --verify --strict: "Finder information ... not allowed").
icon = defines["icon"]  # noqa: F821
background = defines["background"]  # noqa: F821

# DMGBackground.size, .app, .applications and .iconSize, in DMG.swift.
# The window's frame, bars included: Finder adds the user's own (tab, path and status bars).
window_rect = ((200, 120), (640, 500))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_icon_preview = False
icon_size = 112
text_size = 13
label_pos = "bottom"
arrange_by = None
icon_locations = {appname: (170, 190), "Applications": (470, 190)}
