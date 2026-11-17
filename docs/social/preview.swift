// The repository's social preview on GitHub (Settings › General › Social preview), 1280×640, in the
// style of keepmum.app's og.png: the site's coral and ink, its typeface, and the sealed m.
//
//     swift docs/social/preview.swift docs/social/preview.png

import SwiftUI

let W: CGFloat = 1280, H: CGFloat = 640
let coral = Color(red: 0xE4 / 255, green: 0x57 / 255, blue: 0x2E / 255)
let ink = Color(red: 0x1A / 255, green: 0x0F / 255, blue: 0x0B / 255)

let fonts: Void = {
    let dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appending(path: "../../site/fonts")
    CTFontManagerRegisterFontsForURL(dir.appending(path: "bricolage-grotesque.woff2") as CFURL, .process, nil)
}()

/// Bricolage is a variable font: the weight and optical size are set on its axes.
func sans(_ size: CGFloat, _ wght: CGFloat) -> Font {
    _ = fonts
    guard let base = NSFont(name: "BricolageGrotesque96ptExtraBold-Regular", size: size) else { fatalError("run from the repository") }
    let axes: [UInt32: CGFloat] = [0x7767_6874: wght, 0x6F70_737A: min(96, max(12, size))]  // 'wght', 'opsz'
    return Font(NSFont(descriptor: base.fontDescriptor.addingAttributes([.variation: axes]), size: size) ?? base)
}

/// The sealed m (KeepmumMark in the app), on a 100-unit square.
func mark(_ size: CGFloat) -> some View {
    let u = size / 100
    let m = Path { path in
        func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x * u, y: y * u) }
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
    }
    return m.stroke(.white, style: StrokeStyle(lineWidth: 10 * u, lineCap: .round, lineJoin: .round))
        .frame(width: size, height: size)
}

let preview = ZStack(alignment: .topLeading) {
    coral
    VStack(alignment: .leading, spacing: 0) {
        Text("keepmum").font(sans(34, 700)).foregroundStyle(ink.opacity(0.75))
        Text("Keep mum.").font(sans(132, 800)).foregroundStyle(ink).padding(.top, 6)
        Text("Secrets and personal data stay on your Mac\nwhile you use coding agents.")
            .font(sans(34, 600)).foregroundStyle(ink).lineSpacing(4).padding(.top, 14)
        Spacer(minLength: 0)
        Text("Claude Code · Codex · Gemini CLI · pi · opencode").font(sans(26, 500)).foregroundStyle(ink.opacity(0.75))
        Text("Free and open source · macOS · keepmum.app").font(sans(26, 500)).foregroundStyle(ink.opacity(0.75)).padding(.top, 8)
    }
    .padding(.leading, 88).padding(.vertical, 80)
    mark(320).offset(x: 890, y: 130)
}
.frame(width: W, height: H)

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "preview.png")
MainActor.assumeIsolated {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let renderer = ImageRenderer(content: preview.environment(\.colorScheme, .light))
    renderer.proposedSize = ProposedViewSize(width: W, height: H)
    renderer.scale = 1
    guard let image = renderer.cgImage,
          let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else { fatalError("no image") }
    try! png.write(to: out)
    print("wrote \(out.path)")
}
