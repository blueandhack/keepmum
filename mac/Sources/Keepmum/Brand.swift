import AppKit
import IOKit.ps
import SwiftUI

/// Logos. The Claude and Anthropic logos (mac/Brand, from Anthropic's press kit; see
/// Brand/NOTICE.md) identify the services keepmum works with. build-app.sh copies them into the
/// app; when running from the build folder they are found in mac/Brand.
enum Brand {
    static func logo(_ name: String) -> NSImage? {
        if let url = Bundle.main.url(forResource: name, withExtension: "png") { return NSImage(contentsOf: url) }
        var dir = Bundle.main.bundleURL
        for _ in 0..<5 {
            dir = dir.deletingLastPathComponent()
            let candidate = dir.appending(path: "Brand/\(name).png")
            if FileManager.default.fileExists(atPath: candidate.path) { return NSImage(contentsOf: candidate) }
        }
        return nil
    }

    /// A laptop when this Mac has a battery, otherwise a Mac mini.
    static let thisMacSymbol: String = {
        let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue()
        let list = info.flatMap { IOPSCopyPowerSourcesList($0)?.takeRetainedValue() as? [CFTypeRef] } ?? []
        return list.isEmpty ? "macmini" : "laptopcomputer"
    }()

    /// keepmum's own color, coral (#E4572E), as the icon's top-to-bottom gradient.
    static let iconGradient = LinearGradient(colors: [Color(nsColor: NSColor(hex: 0xEC6843)), Color(nsColor: NSColor(hex: 0xD9481F))],
                                             startPoint: .top, endPoint: .bottom)
}

/// The keepmum mark: a sealed m. The m of "mum" stands on a line that closes it, like a shut
/// mouth. Drawn in a 100x100 box at any size, in one color, so it works as an app icon, a
/// template image in the menu bar, and a small logo. `weight` is the stroke in box units: small
/// renders (the menu bar) want it heavier.
struct KeepmumMark: View {
    var color: Color = .white
    var weight: CGFloat = 10

    var body: some View {
        GeometryReader { geo in
            let u = min(geo.size.width, geo.size.height) / 100
            SealedM()
                .stroke(color, style: StrokeStyle(lineWidth: weight * u, lineCap: .round, lineJoin: .round))
                .frame(width: 100 * u, height: 100 * u)
                .frame(width: geo.size.width, height: geo.size.height)
        }
    }

    /// Two arches on three legs, and the line under them, as one stroked path.
    struct SealedM: Shape {
        func path(in r: CGRect) -> Path {
            let u = min(r.width, r.height) / 100
            func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: r.minX + x * u, y: r.minY + y * u) }
            var path = Path()
            for (left, right) in [(21.6, 50.0), (50.0, 78.4)] {
                let mid = (left + right) / 2
                path.move(to: p(left, 69.8))
                path.addLine(to: p(left, 46.3))
                path.addCurve(to: p(mid, 30.9), control1: p(left, 37), control2: p(left + 6.2, 30.9))
                path.addCurve(to: p(right, 46.3), control1: p(right - 6.2, 30.9), control2: p(right, 37))
                path.addLine(to: p(right, 69.8))
            }
            path.move(to: p(12.9, 69.8))
            path.addLine(to: p(87.1, 69.8))
            return path
        }
    }
}

/// The mark on the brand squircle: the app icon's look, at small sizes. The UI accent is the same
/// coral (Theme.accent, a shade darker for legible text); blocked is crimson, well apart from it.
struct KeepmumBadge: View {
    var size: CGFloat

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
            .fill(Brand.iconGradient)
            .overlay(KeepmumMark(color: .white, weight: size < 32 ? 12 : 10).padding(size * 0.15))
            .frame(width: size, height: size)
    }
}

extension Brand {
    /// The mark as a template image for the menu bar: macOS tints it for light and dark.
    /// Rendered once per variant; the menu bar label updates every second.
    @MainActor static let menuBarActive = renderMenuBarImage(dimmed: false)
    @MainActor static let menuBarIdle = renderMenuBarImage(dimmed: true)

    @MainActor private static func renderMenuBarImage(dimmed: Bool) -> NSImage {
        let renderer = ImageRenderer(content: KeepmumMark(color: .black.opacity(dimmed ? 0.45 : 1), weight: 13).frame(width: 18, height: 18))
        renderer.scale = 2
        let image = renderer.nsImage ?? NSImage(systemSymbolName: "text.alignleft", accessibilityDescription: nil)!
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = true
        return image
    }
}
