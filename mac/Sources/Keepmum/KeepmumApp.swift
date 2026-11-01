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
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let s = store.snapshot
        let observing = s.observe == true && store.connection == .live
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(spacing: Space.s) {
                KeepmumBadge(size: 22)
                Text("keepmum").font(.km(.headline)).foregroundStyle(Theme.text)
                Spacer()
                StatusDot(connection: store.connection, observing: observing)
            }
            if observing {
                HStack(spacing: Space.s) {
                    Image(systemName: "eye.trianglebadge.exclamationmark")
                    Text("Sending unmasked").font(.km(.callout).weight(.medium)).fixedSize()
                    Spacer(minLength: Space.s)
                    PillButton(title: "Mask again", onSolid: true) { Task { await engine.setObserve(false) } }
                        .disabled(!engine.isBundled || engine.busy)
                }
                .foregroundStyle(Theme.onAccent)
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.blockedFill))
            }
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text((s.vaultSize ?? 0).compact).font(.km(.display)).monospacedDigit()
                    .foregroundStyle(Theme.text).contentTransition(.numericText())
                Text("unique values kept on this Mac").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            }
            HStack(spacing: 0) {
                stat("Requests", s.contentRequests.compact)
                stat("Sensitive", s.funnel.withFindings.compact)
                stat("Blocked", s.blocked.compact, color: s.blocked > 0 ? Theme.blocked : Theme.text)
                stat("p95", milliseconds(s.scanMs.p95))
            }
            .padding(.vertical, Space.s + Space.xxs)
            .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            ProtectionRow(engine: engine) {
                engine.settingsRequested = true
                openWindow(id: "dashboard")
                NSApp.activate()
            }
            if case .offline(let reason) = store.connection {
                Text("\(reason.sentence). \(engine.isBundled ? "Start the background service in Settings." : "Run keepmum claude to start.")")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
            HStack(spacing: Space.s) {
                PillButton(title: "Open dashboard", icon: "macwindow", prominent: true) {
                    openWindow(id: "dashboard")
                    NSApp.activate()
                }
                Spacer()
                PillButton(title: "Quit") { NSApp.terminate(nil) }
            }
        }
        .padding(Space.l)
        .frame(width: 320)
        .task { await engine.refresh() }
        // The menu bar window brings its own material; snapshots need a surface to draw on.
        .background(isSnapshot ? Theme.canvas : .clear)
        .preferredColorScheme(appearance.scheme)
    }

    private func stat(_ title: String, _ value: String, color: Color = Theme.text) -> some View {
        VStack(spacing: Space.xxs) {
            Text(value).font(.km(.headline)).monospacedDigit().foregroundStyle(color)
            Text(title).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
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
            // Settings in full: the window scrolls it, a snapshot cannot.
            render(Dashboard(store: store, engine: .preview(), page: .settings).frame(width: 1200, height: 1640),
                   to: "\(dir)/settings-full-\(dark ? "dark" : "light").png", dark: dark)
        }
        // The background service just started, waiting for it to answer.
        render(Dashboard(store: store, engine: .preview(starting: true), page: .settings).frame(width: 1200, height: 780),
               to: "\(dir)/settings-starting-light.png", dark: false)
        render(MenuPanel(store: store, engine: .preview(starting: true)), to: "\(dir)/menu-starting-light.png", dark: false)
        // Observe mode, live: the one state where values leave this Mac unmasked.
        store.snapshot.observe = true
        store.connection = .live
        for dark in [false, true] {
            render(window(store, .overview), to: "\(dir)/observe-\(dark ? "dark" : "light").png", dark: dark)
            render(MenuPanel(store: store, engine: .preview()), to: "\(dir)/menu-observe-\(dark ? "dark" : "light").png", dark: dark)
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
