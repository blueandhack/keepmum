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
                .onAppear { store.start(); engine.start(); DashboardWindow.opened() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1200, height: 780)

        Window("Skills for local turns", id: SkillsWindow.id) {
            SkillsEditor(engine: engine)
                .frame(minWidth: 760, minHeight: 640)
                .onAppear { engine.start() }
        }
        .defaultSize(width: 980, height: 760)
    }
}

struct MenuLabel: View {
    var store: StatsStore
    var engine: Engine

    var body: some View {
        let unique = store.snapshot.vaultSize ?? 0
        HStack(spacing: 3) {
            let live = store.connection == .live || store.connection == .sample
            let attention = store.toolsNeedReview || engine.aroundClient != nil || store.scriptWarning != nil
                || !ReviewCenter.shared.pending.isEmpty
            Image(nsImage: !live ? Brand.menuBarIdle : attention ? Brand.menuBarAttention : Brand.menuBarActive)
            if unique > 0 { Text(unique.compact).monospacedDigit() }
        }
        .task { store.start(); engine.start(); Updates.shared.start(); ReviewCenter.shared.start { store.port } }
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
    @State private var height: CGFloat = 0

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
            if engine.isBundled {
                ServiceRow(engine: engine)
                if let error = engine.error {
                    Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ProtectionRow(engine: engine) {
                engine.settingsRequested = .setup
                DashboardWindow.show(openWindow)
            }
            if !ReviewCenter.shared.pending.isEmpty {
                let waiting = ReviewCenter.shared.pending.count
                HStack(spacing: Space.s) {
                    Image(systemName: "hand.raised.square").foregroundStyle(Theme.accent)
                    Text(waiting == 1 ? "A hand-back waits for you" : "\(waiting) hand-backs wait for you")
                        .font(.km(.callout)).foregroundStyle(Theme.text)
                    Spacer(minLength: Space.s)
                    PillButton(title: "Review") { ReviewCenter.shared.reopen() }
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            if let around = engine.aroundClient {
                // The gateway's own app pointed the agent back at itself (switching a model, say).
                HStack(spacing: Space.s) {
                    Image(systemName: "exclamationmark.shield").foregroundStyle(Theme.blocked)
                    Text("\(around.name) skips keepmum").font(.km(.callout)).foregroundStyle(Theme.text)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .help("It goes to \(hostPort(around.url)) directly, unscanned: that gateway's app most likely set it back")
                    Spacer(minLength: Space.s)
                    PillButton(title: "Protect") { Task { await engine.setProtection(true, client: around.id) } }
                        .disabled(!engine.isBundled || engine.busy || !engine.serviceRunning)
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            if let warning = store.scriptWarning {
                // A Codex script handed values keepmum could not see to a network tool (ScriptWarnings).
                HStack(spacing: Space.s) {
                    Image(systemName: "curlybraces").foregroundStyle(Theme.warning)
                    // The server alone ("notes"): the tool is on the Logs page.
                    let to = (warning.categories ?? []).first?.replacingOccurrences(of: "computed values sent to ", with: "")
                        .components(separatedBy: " › ").first ?? "a network tool"
                    Text("Unchecked values to \(to)")
                        .font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                        .help("A Codex script passed values it worked out, such as a command's output, to a network tool. keepmum could not check them.")
                    Spacer(minLength: Space.s)
                    PillButton(title: "Logs") {
                        store.markScriptWarningsSeen()
                        engine.pageRequested = .logs
                        DashboardWindow.show(openWindow)
                    }
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            if let review = store.toolServers.first(where: { $0.paused }) ?? store.toolServers.first(where: { $0.new }) {
                let others = store.toolServers.filter { $0.new || $0.paused }.count - 1
                HStack(spacing: Space.s) {
                    Image(systemName: review.paused ? "exclamationmark.shield" : "puzzlepiece.extension")
                        .foregroundStyle(review.paused ? Theme.warning : Theme.accent)
                    Text(review.paused ? "\(review.label): tools changed" : "New tool: \(review.label)" + (others > 0 ? " and \(others) more" : ""))
                        .font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1)
                    Spacer(minLength: Space.s)
                    PillButton(title: "Review") {
                        engine.settingsRequested = .tools
                        DashboardWindow.show(openWindow)
                    }
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            if let added = engine.status?.extensions?.new, let first = added.first {
                // Another app added a plugin, hook or MCP server to a coding agent (Settings › Extensions).
                HStack(spacing: Space.s) {
                    Image(systemName: "puzzlepiece.extension").foregroundStyle(Theme.warning)
                    Text(added.count == 1 ? "\(first.agent) changed: \(first.name)" : "\(added.count) added to your agents")
                        .font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: Space.s)
                    PillButton(title: "Review") {
                        engine.settingsRequested = .extensions
                        DashboardWindow.show(openWindow)
                    }
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            if let version = Updates.shared.available {
                HStack(spacing: Space.s) {
                    Image(systemName: "arrow.down.circle").foregroundStyle(Theme.accent)
                    Text("keepmum \(version) is available").font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1)
                    Spacer(minLength: Space.s)
                    PillButton(title: "Install…") { Updates.shared.checkNow() }
                }
                .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            }
            // With the bundled engine, the service row above says what to do; this is for when it
            // runs and still cannot be reached, or for the command line.
            if case .offline(let reason) = store.connection, !engine.isBundled || engine.serviceRunning {
                Text("\(reason.sentence).\(engine.isBundled ? "" : " Run keepmum claude to start.")")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: Space.s) {
                PillButton(title: "Open dashboard", icon: "macwindow", prominent: true) {
                    DashboardWindow.show(openWindow)
                }
                Spacer()
                PillButton(title: "Quit") { NSApp.terminate(nil) }
            }
        }
        .padding(Space.l)
        .frame(width: 320)
        // Its full height, always: the menu bar window is sized from this, and without it a row
        // that appears while the panel is open is squeezed (text cut to one line) instead.
        .fixedSize(horizontal: false, vertical: true)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
        .background { if !isSnapshot { FitMenuBarWindow(height: height) } }
        .task { await engine.refresh() }
        // Opaque: the menu bar window's material shows what is behind it, and the theme's colors,
        // unlike the system's, do not adapt to it (gray labels vanish over a light window).
        .background(Theme.canvas)
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

/// Keeps the menu bar window as tall as the panel, its top under the menu bar. SwiftUI grows the
/// window when a row appears but never shrinks it when the row goes, and centers the panel in what
/// is left over: the panel then opened a long way below the menu bar, with an empty frame under it.
private struct FitMenuBarWindow: NSViewRepresentable {
    var height: CGFloat

    func makeNSView(context: Context) -> Fitter { Fitter() }

    func updateNSView(_ view: Fitter, context: Context) {
        view.height = height
        DispatchQueue.main.async { view.fit() }  // the view is in its window once this update has run
    }

    /// Fits its window when the panel's height changes, and again whenever the window opens or is
    /// resized, should SwiftUI put its old size back.
    final class Fitter: NSView {
        var height: CGFloat = 0
        private var observers: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            observers.forEach(NotificationCenter.default.removeObserver)
            observers = []
            guard let window else { return }
            for name in [NSWindow.didResizeNotification, NSWindow.didBecomeKeyNotification] {
                observers.append(NotificationCenter.default.addObserver(forName: name, object: window, queue: .main) { [weak self] _ in
                    MainActor.assumeIsolated { self?.fit() }
                })
            }
            fit()
        }

        func fit() {
            guard height > 0, let window else { return }
            let frame = window.frame
            let fitted = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: frame.width, height: height)).height
            guard abs(frame.height - fitted) > 0.5 else { return }
            window.setFrame(NSRect(x: frame.minX, y: frame.maxY - fitted, width: frame.width, height: fitted), display: true)
        }
    }
}

/// The dashboard window, and keepmum's place in the Dock. keepmum lives in the menu bar; while the
/// dashboard is open it is also an ordinary app, in the Dock and in Cmd-Tab, so the window can be
/// found again behind other apps (unless Settings › General › Display turns that off).
@MainActor
enum DashboardWindow {
    static let dockKey = "showInDock"
    static var showsInDock: Bool { UserDefaults.standard.object(forKey: dockKey) as? Bool ?? true }
    private static var isOpen = false
    private static var watching = false

    /// Open the dashboard, or bring it back, in front of the app in use. NSApp.activate() alone
    /// is a request the frontmost app may refuse; ordering the window front is not.
    static func show(_ openWindow: OpenWindowAction) {
        // Called from the menu bar panel: it closes, as a menu does once you pick something.
        let panel = NSApp.keyWindow.flatMap { $0.identifier?.rawValue.hasPrefix("dashboard") == true ? nil : $0 }
        openWindow(id: "dashboard")
        DispatchQueue.main.async {  // the window exists once openWindow's update has run
            panel?.close()
            opened()
            NSApp.activate()
            if let window {
                window.makeKeyAndOrderFront(nil)
                window.orderFrontRegardless()
            }
        }
    }

    /// The dashboard is on screen: keepmum joins the Dock. SwiftUI calls the window's onAppear only
    /// the first time it opens, not when it is opened again after a close, so `show` and AppKit's
    /// didBecomeKey say so too; otherwise keepmum stayed a menu bar app with its dashboard open,
    /// with no Dock icon, and its window fell behind the app in use whenever the menu panel closed.
    static func opened() {
        isOpen = true
        applyDockPolicy()
        guard !watching else { return }
        watching = true
        // SwiftUI does not reliably call onDisappear when the window closes; AppKit always says so.
        // Both observers last as long as the app (`watching`): there is nothing to remove them for.
        // swiftlint:disable:next discarded_notification_center_observer - kept for the app's life
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { note in
            let window = note.object as? NSWindow
            MainActor.assumeIsolated {
                guard isDashboard(window) else { return }
                isOpen = false
                applyDockPolicy()
            }
        }
        // swiftlint:disable:next discarded_notification_center_observer - kept for the app's life
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { note in
            let window = note.object as? NSWindow
            MainActor.assumeIsolated {
                if isDashboard(window) && !isOpen { opened() }
            }
        }
    }

    private static func isDashboard(_ window: NSWindow?) -> Bool {
        window?.identifier?.rawValue.hasPrefix("dashboard") == true
    }

    /// In the Dock while the dashboard is open and the setting allows it; otherwise menu bar only.
    static func applyDockPolicy() {
        let policy: NSApplication.ActivationPolicy = isOpen && showsInDock ? .regular : .accessory
        guard NSApp.activationPolicy() != policy else { return }
        NSApp.setActivationPolicy(policy)
        // A menu bar app that joins the Dock can get a blank tile (the Dock's icon cache for the
        // bundle path, stale after a rebuild): hand it the icon from the bundle itself.
        if policy == .regular, let icon = Bundle.main.image(forResource: "Keepmum") {
            NSApp.applicationIconImage = icon
        }
        // Changing the policy can send the app to the back: keep the dashboard where it was.
        if isOpen, let window {
            NSApp.activate()
            window.makeKeyAndOrderFront(nil)
        }
    }

    private static var window: NSWindow? {
        NSApp.windows.first { isDashboard($0) }
    }
}

/// Renders the app to PNGs and exits, to check the design without a screen.
///   `Keepmum --snapshot dir`         every page in light and dark, with preview data
///   `Keepmum --snapshot dir --live`  the overview from the running keepmum's stats feed
///   `Keepmum --icon dir`             the app icon at every size iconutil needs
///   `Keepmum --dmg-background dir`   the DMG window's background, at 1x and 2x (make-dmg.sh)
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
        if let i = args.firstIndex(of: "--dmg-background"), i + 1 < args.count {
            _ = NSApplication.shared
            for scale in [1, 2] {
                render(DMGBackground(), to: "\(args[i + 1])/background\(scale == 2 ? "@2x" : "").png", dark: false, scale: CGFloat(scale))
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
        store.injectTools(ToolServer.samples)
        store.pulses = (0..<9).map { k in
            let kinds: [Pulse.Kind] = [.out("masked"), .out("clean"), .back(restored: true, result: "masked"), .out("blocked"),
                                       .back(restored: false, result: "clean")]
            let client = ["claude-code", "codex", "pi"][k % 3]
            // A reply sets off once its request has landed, so its born is that much earlier.
            let reply = if case .back = kinds[k % kinds.count] { 2.0 } else { 0.0 }
            return Pulse(kind: kinds[k % kinds.count], client: client, provider: client == "codex" ? "openai" : "anthropic",
                         born: now.addingTimeInterval(-Double(k) * 0.12 - reply))
        }
        // Requests out at a provider, waiting on its reply.
        store.injectOpen([("masked", "claude-code", 9.0), ("clean", "claude-code", 6.0), ("clean", "codex", 4.0)].map { kind, client, age in
            Pulse(kind: .out(kind), client: client, provider: client == "codex" ? "openai" : "anthropic", born: now.addingTimeInterval(-age))
        })
        for page in Page.allCases {
            for dark in [false, true] {
                render(window(store, page), to: "\(dir)/\(page.rawValue)-\(dark ? "dark" : "light").png", dark: dark)
            }
        }
        for dark in [false, true] {
            render(MenuPanel(store: store, engine: .preview()), to: "\(dir)/menu-\(dark ? "dark" : "light").png", dark: dark)
            // Each Settings pane in full (the window scrolls a long one, a snapshot cannot), and on a
            // wide window, where a pane with two groups of sections puts them side by side.
            for pane in SettingsPane.allCases {
                render(Dashboard(store: store, engine: .preview(), page: .settings, settingsPane: pane).frame(width: 1200, height: 1240),
                       to: "\(dir)/settings-\(pane.rawValue)-\(dark ? "dark" : "light").png", dark: dark)
                render(Dashboard(store: store, engine: .preview(), page: .settings, settingsPane: pane).frame(width: 1900, height: 980),
                       to: "\(dir)/settings-wide-\(pane.rawValue)-\(dark ? "dark" : "light").png", dark: dark)
            }
        }
        // The Skills window, on the first preview skill.
        for dark in [false, true] {
            render(SkillsEditor(engine: .preview(), initial: 1).frame(width: 980, height: 760),
                   to: "\(dir)/skills-\(dark ? "dark" : "light").png", dark: dark)
        }
        // Every built-in rule, as the Detection page's sheet shows them.
        let rulesEngine = Engine.preview()
        rulesEngine.previewRules()
        render(AllRulesSheet(engine: rulesEngine).background(Theme.canvas), to: "\(dir)/rules-sheet-light.png", dark: false)
        // A Logs row's details, as its popover shows them.
        for dark in [false, true] {
            render(FindingDetail(finding: Finding.samples[0]).frame(width: 440).background(Theme.canvas),
                   to: "\(dir)/logs-detail-\(dark ? "dark" : "light").png", dark: dark)
        }
        // A Checks row's details: one per outcome in the preview data.
        for (i, hit) in store.snapshot.guardHits.enumerated() {
            render(CheckDetail(hit: hit, texts: hit.seq.flatMap { SampleFeed.sampleCheckTexts[$0] }).frame(width: 540).background(Theme.canvas), to: "\(dir)/logs-check-\(i)-light.png", dark: false)
        }
        // The background service just started, waiting for it to answer.
        render(Dashboard(store: store, engine: .preview(starting: true), page: .settings).frame(width: 1200, height: 780),
               to: "\(dir)/settings-starting-light.png", dark: false)
        render(MenuPanel(store: store, engine: .preview(starting: true)), to: "\(dir)/menu-starting-light.png", dark: false)
        // A gateway's app pointed Claude Code back at itself, around keepmum.
        render(MenuPanel(store: store, engine: .preview(around: true)), to: "\(dir)/menu-around-light.png", dark: false)
        render(Dashboard(store: store, engine: .preview(around: true), page: .settings).frame(width: 1200, height: 1100),
               to: "\(dir)/settings-around-light.png", dark: false)
        // A hand-back waiting for review: the panel, and the menu with its row.
        ReviewCenter.shared.inject([.sample])
        for dark in [false, true] {
            render(ReviewCard(center: .shared, review: .sample, more: 1).frame(width: 560).background(Theme.canvas),
                   to: "\(dir)/review-\(dark ? "dark" : "light").png", dark: dark)
        }
        render(MenuPanel(store: store, engine: .preview()), to: "\(dir)/menu-review-light.png", dark: false)
        render(ReviewCard(center: .shared, review: .sampleAnswer).frame(width: 560).background(Theme.canvas),
               to: "\(dir)/review-answer-light.png", dark: false)
        ReviewCenter.shared.inject([], mode: "off")
        // The menu bar icon, plain and with the dot that points at Settings › Tools.
        for (name, image) in [("menubar", Brand.menuBarActive), ("menubar-attention", Brand.menuBarAttention)] {
            if let tiff = image.tiffRepresentation, let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
                try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
            }
        }
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
            FileHandle.standardError.write(Data("snapshot failed: \(path)\n".utf8))
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
