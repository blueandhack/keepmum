import AppKit
import SwiftUI

@main
struct KeepmumApp: App {
    @State private var store = StatsStore()
    @State private var engine = Engine()

    init() {
        Snapshotter.runIfRequested()
    }

    var body: some Scene {
        MenuBarExtra {
            MenuPanel(store: store, engine: engine)
        } label: {
            MenuLabel(store: store, engine: engine)
        }
        .menuBarExtraStyle(.window)

        Window("keepmum", id: "dashboard") {
            Dashboard(store: store, engine: engine)
                .frame(minWidth: 1100, minHeight: 720)
                .onAppear { store.start(); engine.start() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1200, height: 780)
    }
}

struct MenuLabel: View {
    var store: StatsStore
    var engine: Engine

    var body: some View {
        let unique = store.snapshot.vaultSize ?? 0
        HStack(spacing: 3) {
            Image(nsImage: store.connection == .live || store.connection == .sample ? Brand.menuBarActive : Brand.menuBarIdle)
            if unique > 0 { Text(unique.compact).monospacedDigit() }
        }
        .task { store.start(); engine.start() }
        .onChange(of: engine.status?.port, initial: true) { _, p in
            if let p { store.configuredPort = p }
        }
    }
}

struct MenuPanel: View {
    @Bindable var store: StatsStore
    var engine: Engine
    @AppStorage("appearance") private var appearance: Appearance = .system
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let s = store.snapshot
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 8) {
                KeepmumBadge(size: 24)
                Text("keepmum").font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.text)
                Spacer()
                StatusDot(connection: store.connection)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text((s.vaultSize ?? 0).compact).font(.system(size: 32, weight: .semibold, design: .rounded)).monospacedDigit()
                    .foregroundStyle(Theme.text).contentTransition(.numericText())
                Text("unique values kept on this Mac").font(.system(size: 12)).foregroundStyle(Theme.secondary)
            }
            HStack(spacing: 0) {
                stat("Requests", s.contentRequests.compact)
                stat("Sensitive", s.funnel.withFindings.compact)
                stat("Blocked", s.blocked.compact, color: s.blocked > 0 ? Theme.danger : Theme.text)
                stat("p95", s.scanMs.p95.map { String(format: "%.0f ms", $0) } ?? "—")
            }
            .padding(.vertical, 12)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.faint))
            ProtectionRow(engine: engine) {
                engine.settingsRequested = true
                openWindow(id: "dashboard")
                NSApp.activate()
            }
            if case .offline(let reason) = store.connection {
                Text("\(reason). \(engine.isBundled ? "Start the background service in Setup." : "Run keepmum claude to start.")")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.secondary)
            }
            HStack(spacing: 8) {
                PillButton(title: "Open dashboard", icon: "macwindow", prominent: true) {
                    openWindow(id: "dashboard")
                    NSApp.activate()
                }
                PillButton(title: store.sampleMode ? "Live" : "Preview", icon: store.sampleMode ? "dot.radiowaves.left.and.right" : "sparkles") {
                    store.sampleMode.toggle()
                }
                Spacer()
                PillButton(title: "Quit") { NSApp.terminate(nil) }
            }
        }
        .padding(16)
        .frame(width: 330)
        .task { await engine.refresh() }
        .background(Theme.background)
        .preferredColorScheme(appearance.scheme)
    }

    private func stat(_ title: String, _ value: String, color: Color = Theme.text) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.system(size: 14, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(color)
            Text(title).font(.system(size: 10.5)).foregroundStyle(Theme.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

/// Renders the app to PNGs and exits, to check the design without a screen.
///   `Keepmum --snapshot dir`         every page in light and dark, with preview data
///   `Keepmum --snapshot dir --live`  the overview from the running keepmum's stats feed
///   `Keepmum --icon dir`             the app icon at every size iconutil needs
@MainActor
enum Snapshotter {
    static func runIfRequested() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--icon"), i + 1 < args.count {
            _ = NSApplication.shared
            for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                               ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
                render(AppIcon(weight: px <= 32 ? 14 : px <= 64 ? 12 : 10).frame(width: 1024, height: 1024).scaleEffect(CGFloat(px) / 1024).frame(width: CGFloat(px), height: CGFloat(px)),
                       to: "\(args[i + 1])/icon_\(name).png", dark: false, scale: 1)
            }
            exit(0)
        }
        guard let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count else { return }
        _ = NSApplication.shared
        let dir = args[i + 1]
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let store = StatsStore()
        if args.contains("--live") {
            let done = DispatchSemaphore(value: 0)
            Task { @MainActor in
                await store.refresh()
                done.signal()
            }
            while done.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            for dark in [false, true] {
                render(window(store, .overview), to: "\(dir)/live-overview-\(dark ? "dark" : "light").png", dark: dark)
            }
            exit(store.connection == .live ? 0 : 1)
        }
        store.sampleMode = true
        var feed = SampleFeed()
        store.snapshot = feed.next()
        store.connection = .sample
        let now = Date()
        store.injectFlows(SampleFeed.flows(window: 3600))
        store.pulses = (0..<9).map { k in
            let kinds: [Pulse.Kind] = [.out("masked"), .out("clean"), .back(restored: true), .out("blocked"), .back(restored: false)]
            let client = ["claude-code", "codex", "pi"][k % 3]
            return Pulse(kind: kinds[k % kinds.count], client: client, provider: client == "codex" ? "openai" : "anthropic",
                         born: now.addingTimeInterval(-Double(k) * 0.23))
        }
        for page in Page.allCases {
            for dark in [false, true] {
                render(window(store, page), to: "\(dir)/\(page.rawValue)-\(dark ? "dark" : "light").png", dark: dark)
            }
        }
        for dark in [false, true] {
            render(MenuPanel(store: store, engine: .preview()), to: "\(dir)/menu-\(dark ? "dark" : "light").png", dark: dark)
        }
        exit(0)
    }

    /// The dashboard at its default window size: what fits without scrolling is what you see.
    private static func window(_ store: StatsStore, _ page: Page) -> some View {
        Dashboard(store: store, engine: .preview(), page: page).frame(width: 1200, height: 780)
    }

    private static func render(_ view: some View, to path: String, dark: Bool, scale: CGFloat = 2) {
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let renderer = ImageRenderer(content: view.environment(\.colorScheme, dark ? .dark : .light).environment(\.isSnapshot, true))
        renderer.scale = scale
        guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
              let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("snapshot failed: \(path)\n".data(using: .utf8)!)
            exit(1)
        }
        try? png.write(to: URL(fileURLWithPath: path))
    }
}

/// The app icon: the keepmum mark (a sealed m) on a coral squircle.
struct AppIcon: View {
    /// The mark's stroke: heavier for the small sizes, so the m stays open at 16 px.
    var weight: CGFloat = 10

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 230, style: .continuous)
                .fill(Brand.iconGradient)
                .padding(100)
            RoundedRectangle(cornerRadius: 230, style: .continuous)
                .strokeBorder(Color.white.opacity(0.12), lineWidth: 4)
                .padding(100)
            KeepmumMark(color: .white, weight: weight)
                .frame(width: 600, height: 600)
                .shadow(color: .black.opacity(0.18), radius: 12, y: 8)
        }
        .frame(width: 1024, height: 1024)
    }
}
