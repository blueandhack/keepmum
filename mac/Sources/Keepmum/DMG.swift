import SwiftUI

/// The DMG window's background (`Keepmum --dmg-background dir`, for mac/make-dmg.sh): a line that
/// says what to do, and an arrow from where Finder shows the app to where it shows Applications.
/// Finder draws the two icons and their names on top, at `DMGBackground.app` and `.applications`
/// (mac/dmg-settings.py places them there). One light design: Finder shows the same picture in
/// either appearance. Finder may add a tab bar, a path bar and a status bar (they follow the
/// user's own Finder settings, whatever the DMG asks), up to about 125 points of a 500-point
/// window: what matters is in the top 360 points, and the rest is only the gradient.
struct DMGBackground: View {
    /// The window's content, in points.
    static let size = CGSize(width: 640, height: 480)
    /// Where Finder centers each icon, in points from the top left.
    static let app = CGPoint(x: 170, y: 190)
    static let applications = CGPoint(x: 470, y: 190)
    static let iconSize: CGFloat = 112

    private let coral = Color(nsColor: NSColor(hex: 0xE4572E))
    private let ink = Color(nsColor: NSColor(hex: 0x1D1D1F))
    private let quiet = Color(nsColor: NSColor(hex: 0x6E6E73))

    var body: some View {
        ZStack(alignment: .topLeading) {
            LinearGradient(colors: [Color(nsColor: NSColor(hex: 0xFFFBF8)), Color(nsColor: NSColor(hex: 0xF6EEE8))],
                           startPoint: .top, endPoint: .bottom)
            // A soft well under each icon, so the icons sit on something.
            ForEach([Self.app, Self.applications], id: \.x) { p in
                RoundedRectangle(cornerRadius: 30, style: .continuous)
                    .fill(Color.white.opacity(0.75))
                    .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous).strokeBorder(coral.opacity(0.10), lineWidth: 1))
                    .shadow(color: Color(nsColor: NSColor(hex: 0x8A4A30)).opacity(0.08), radius: 14, y: 6)
                    .frame(width: 168, height: 178)
                    .position(x: p.x, y: p.y + 14)
            }
            Arrow()
                .stroke(coral.opacity(0.85), style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round, dash: [1, 9]))
                .frame(width: 92, height: 34)
                .position(x: (Self.app.x + Self.applications.x) / 2, y: Self.app.y - 6)
            ArrowHead()
                .fill(coral.opacity(0.85))
                .frame(width: 12, height: 14)
                .position(x: (Self.app.x + Self.applications.x) / 2 + 49, y: Self.app.y - 6)
            VStack(spacing: 6) {
                HStack(spacing: 8) {
                    KeepmumMark(color: coral, weight: 11).frame(width: 22, height: 22)
                    Text("keepmum").font(.system(size: 15, weight: .semibold, design: .rounded)).foregroundStyle(ink)
                }
                Text("Drag keepmum to Applications").font(.system(size: 22, weight: .semibold, design: .rounded))
                    .foregroundStyle(ink)
            }
            .frame(width: Self.size.width)
            .position(x: Self.size.width / 2, y: 58)
            Text("Then open it from Applications: it lives in the menu bar.")
                .font(.system(size: 12)).foregroundStyle(quiet)
                .frame(width: Self.size.width)
                .position(x: Self.size.width / 2, y: 335)
        }
        .frame(width: Self.size.width, height: Self.size.height)
    }

    /// A gentle arc from left to right.
    private struct Arrow: Shape {
        func path(in r: CGRect) -> Path {
            var p = Path()
            p.move(to: CGPoint(x: r.minX, y: r.midY))
            p.addQuadCurve(to: CGPoint(x: r.maxX - 4, y: r.midY), control: CGPoint(x: r.midX, y: r.minY - 6))
            return p
        }
    }

    private struct ArrowHead: Shape {
        func path(in r: CGRect) -> Path {
            var p = Path()
            p.move(to: CGPoint(x: r.minX, y: r.minY))
            p.addLine(to: CGPoint(x: r.maxX, y: r.midY))
            p.addLine(to: CGPoint(x: r.minX, y: r.maxY))
            p.closeSubpath()
            return p
        }
    }
}
