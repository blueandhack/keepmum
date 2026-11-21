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
            SkillsWindowRoot(engine: engine)
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
            let attention = !Attention.items(store: store, engine: engine).isEmpty
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
                .transition(Motion.notice)
            }
            // Status first: is every agent protected, and is the service running, on one surface.
            VStack(spacing: 0) {
                ProtectionRow(engine: engine, framed: false) {
                    engine.settingsRequested = .setup
                    DashboardWindow.show(openWindow)
                }
                if engine.isBundled {
                    Hairline().padding(.leading, Space.m)
                    ServiceRow(engine: engine, framed: false)
                }
            }
            .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            if engine.isBundled, let error = engine.error {
                Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(Motion.notice)
            }
            // Then what needs you, every kind in one list, each with its one action.
            let items = Attention.items(store: store, engine: engine)
            if !items.isEmpty {
                VStack(alignment: .leading, spacing: Space.xs) {
                    HStack(spacing: Space.xs) {
                        Text("Needs you").eyebrow()
                        Text("\(items.count)").font(.km(.label)).foregroundStyle(Theme.textSecondary)
                    }
                    .padding(.leading, Space.xs)
                    AttentionList(items: items, compact: true, limit: 4) { action in
                        action.perform(store: store, engine: engine)
                        if action.opensDashboard { DashboardWindow.show(openWindow) }
                    }
                    .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
                }
                .transition(Motion.notice)
            }
            // Then what keepmum did: the values it kept here, and the requests behind them.
            VStack(spacing: Space.s + Space.xxs) {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Text((s.vaultSize ?? 0).compact).font(.km(.display)).monospacedDigit()
                        .foregroundStyle(Theme.text).contentTransition(.numericText())
                    Text("unique values kept on this Mac").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, Space.m)
                Hairline()
                HStack(spacing: 0) {
                    stat("Requests", s.contentRequests.compact)
                    stat("Sensitive", s.funnel.withFindings.compact)
                    stat("Blocked", s.blocked.compact, color: s.blocked > 0 ? Theme.blocked : Theme.text)
                    stat("p95", milliseconds(s.scanMs.p95))
                }
            }
            .padding(.top, Space.s).padding(.bottom, Space.s + Space.xxs)
            .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
            // With the bundled engine, the service row above says what to do; this is for when it
            // runs and still cannot be reached, or for the command line.
            if case .offline(let reason) = store.connection, !engine.isBundled || engine.serviceRunning {
                Text("\(reason.sentence).\(engine.isBundled ? "" : " Run keepmum claude to start.")")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
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
        // A row that comes or goes slides in or out, and the rows under it move with it.
        .animation(Motion.settle, value: changes)
        .followsPalette()
        .frame(width: 320)
        // Its full height, always: the menu bar window is sized from this, and without it a row
        // that appears while the panel is open is squeezed (text cut to one line) instead.
        .fixedSize(horizontal: false, vertical: true)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
        // At the top of its window, which is a moment taller or shorter than the panel while it
        // follows: centered, the whole panel jumped by half the difference.
        .frame(maxHeight: .infinity, alignment: .top)
        .background { if !isSnapshot { FitMenuBarWindow(height: height) } }
        .task { await engine.refresh() }
        // Opaque: the menu bar window's material shows what is behind it, and the theme's colors,
        // unlike the system's, do not adapt to it (gray labels vanish over a light window).
        .background(Theme.canvas)
        .preferredColorScheme(appearance.scheme)
    }

    /// What makes a row come or go: the panel animates when it changes.
    private var changes: [String] {
        let offline: Bool = if case .offline = store.connection { true } else { false }
        return [store.snapshot.observe == true && !offline ? "observing" : "", offline ? "offline" : "",
                engine.error == nil ? "" : "error", engine.serviceRunning ? "running" : ""]
            + Attention.items(store: store, engine: engine).map(\.id)
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
        DispatchQueue.main.async { view.fit(animated: true) }  // the view is in its window once this update has run
    }

    /// Fits its window when the panel's height changes, and again whenever the window opens or is
    /// resized, should SwiftUI put its old size back.
    final class Fitter: NSView {
        var height: CGFloat = 0
        private var observers: [NSObjectProtocol] = []
        /// The height the window is animating to; its own resizes on the way are not SwiftUI's.
        private var target: CGFloat?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            observers.forEach(NotificationCenter.default.removeObserver)
            observers = []
            guard let window else { return }
            for name in [NSWindow.didResizeNotification, NSWindow.didBecomeKeyNotification] {
                observers.append(NotificationCenter.default.addObserver(forName: name, object: window, queue: .main) { [weak self] _ in
                    MainActor.assumeIsolated { self?.fit(animated: false) }
                })
            }
            fit(animated: false)
        }

        /// Animated while the panel is open, alongside its rows (Motion.settle); at once as it opens.
        func fit(animated: Bool) {
            guard height > 0, let window else { return }
            let frame = window.frame
            let fitted = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: frame.width, height: height)).height
            if let target, abs(target - fitted) < 0.5 { return }
            guard abs(frame.height - fitted) > 0.5 else { return }
            let next = NSRect(x: frame.minX, y: frame.maxY - fitted, width: frame.width, height: fitted)
            guard animated, window.isVisible, !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else {
                target = nil
                window.setFrame(next, display: true)
                return
            }
            target = fitted
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.3
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                window.animator().setFrame(next, display: true)
            } completionHandler: { [weak self] in
                MainActor.assumeIsolated {
                    guard let self, self.target == fitted else { return }
                    self.target = nil
                    self.fit(animated: false)  // should the panel have changed again on the way
                }
            }
        }
    }
}

/// The dashboard window, and keepmum's place in the Dock. keepmum lives in the menu bar; while the
/// dashboard is open it is also an ordinary app, in the Dock and in Cmd-Tab, so the window can be
/// found again behind other apps (unless Settings › Display turns that off).
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
///   `Keepmum --tour dir [t …]`        the app tour video's frames, or the moments given (Tour.swift)
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
        if let i = args.firstIndex(of: "--tour-stills"), i + 1 < args.count {
            _ = NSApplication.shared
            Tour.stills(dir: args[i + 1])
            exit(0)
        }
        if let i = args.firstIndex(of: "--tour"), i + 1 < args.count {
            _ = NSApplication.shared
            Tour.run(dir: args[i + 1], at: args.dropFirst(i + 2).compactMap(Double.init))
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
        // A local mode pane while no check answers on this Mac.
        render(Dashboard(store: store, engine: .preview(localOff: true), page: .settings, settingsPane: .localModels).frame(width: 1200, height: 1240),
               to: "\(dir)/settings-localModels-off-light.png", dark: false)
        // Splash's first download, not signed in to Hugging Face: the tip under its row.
        let downloadEngine = Engine.preview()
        downloadEngine.previewSplashDownload()
        render(Dashboard(store: store, engine: downloadEngine, page: .settings, settingsPane: .localModels).frame(width: 1200, height: 1240),
               to: "\(dir)/settings-localModels-download-light.png", dark: false)
        // Every built-in rule, as the Detection page's sheet shows them.
        let rulesEngine = Engine.preview()
        rulesEngine.previewRules()
        render(AllRulesSheet(engine: rulesEngine).background(Theme.canvas), to: "\(dir)/rules-sheet-light.png", dark: false)
        // A Logs row's details and an Activity row's, as they show beside the table.
        for dark in [false, true] {
            let f = Finding.samples[0]
            render(FindingDetail(finding: f, related: [Finding.samples[8]]).frame(width: 900).background(Theme.surface),
                   to: "\(dir)/logs-detail-\(dark ? "dark" : "light").png", dark: dark)
            if let e = store.snapshot.recent.last(where: { !($0.tools ?? []).isEmpty }) ?? store.snapshot.recent.last {
                render(ActivityDetail(event: e, findings: nil).frame(width: 900).background(Theme.surface),
                       to: "\(dir)/activity-detail-\(dark ? "dark" : "light").png", dark: dark)
            }
        }
        // Show values: a value's details with its value as it was (a made-up token, 40 characters like the sample's).
        var shown = Finding.samples[0]
        shown.value = "ghp_EXAMPLE" + String(repeating: "0", count: 28) + "Q"
        render(FindingDetail(finding: shown, related: [Finding.samples[8]]).environment(\.revealValues, true).frame(width: 900)
                .background(Theme.surface), to: "\(dir)/logs-detail-values-light.png", dark: false)
        // Logs and Activity with a row selected: its details beside the table on a wide window, in
        // its place on a narrow one.
        for (name, width, height) in [("wide", 1700.0, 1000.0), ("narrow", 900.0, 720.0)] {
            if let hit = store.snapshot.guardHits.max(by: { $0.ts < $1.ts }) {
                for layout in [LogsPage.Layout.together, .split] {
                    render(LogsPage(store: store, selectedHit: hit.id, layout: layout).padding(Space.xl).frame(width: width, height: height)
                            .background(Theme.canvas),
                           to: "\(dir)/logs-\(name)-\(layout.rawValue)-light.png", dark: false)
                }
            }
            if let e = store.snapshot.recent.last(where: { !($0.tools ?? []).isEmpty }) {
                render(ActivityPage(store: store, selected: e.id).padding(Space.xl).frame(width: width, height: height).background(Theme.canvas),
                       to: "\(dir)/activity-\(name)-light.png", dark: false)
            }
        }
        // A Checks row's details: one per outcome in the preview data.
        for (i, hit) in store.snapshot.guardHits.enumerated() {
            render(CheckDetail(hit: hit, texts: hit.seq.flatMap { SampleFeed.sampleCheckTexts[$0] }).frame(width: 540).background(Theme.canvas), to: "\(dir)/logs-check-\(i)-light.png", dark: false)
        }
        // The injection check left on with the model an earlier keepmum downloaded: paused until it updates.
        render(Dashboard(store: store, engine: .preview(outdated: true), page: .settings, settingsPane: .checks).frame(width: 1200, height: 1240),
               to: "\(dir)/settings-checks-outdated-light.png", dark: false)
        // The Protection map in full, on each part, and each sample halfway along its route.
        for dark in [false, true] {
            render(mapPage(store, .preview()), to: "\(dir)/map-full-\(dark ? "dark" : "light").png", dark: dark)
        }
        for part in MapPart.allCases where part != .injection {
            render(mapPage(store, .preview(), selected: part), to: "\(dir)/map-\(part.rawValue)-light.png", dark: false)
        }
        for sample in MapSample.allCases {
            render(mapPage(store, .preview(), sample: sample), to: "\(dir)/map-sample-\(sample.rawValue)-light.png", dark: false)
        }
        render(mapPage(store, .preview(localOff: true), sample: .injection), to: "\(dir)/map-warn-light.png", dark: false)
        render(mapPage(store, .preview(outdated: true)), to: "\(dir)/map-outdated-light.png", dark: false)
        // Only harm answers locally: the hand-back, for injection hits, is drawn faint.
        render(mapPage(store, .preview(harmOnlyLocal: true), sample: .harm), to: "\(dir)/map-harm-local-light.png", dark: false)
        // Every request refused for an error (a scan that failed): the provider column and its bands
        // stay drawn, and Masking says how many were not sent.
        store.injectFlows(Flows(clients: [.init(id: "codex", name: "Codex", sent: 60)],
                                out: [.init(client: "codex", provider: nil, kind: "blocked", n: 60)]))
        render(mapPage(store, .preview(), selected: .masking), to: "\(dir)/map-not-sent-light.png", dark: false)
        store.injectFlows(SampleFeed.flows(window: 3600))
        // A narrow window (the diagram scaled down) and a wide one (the settings beside the map).
        render(mapPage(store, .preview(), width: 720, height: 1500), to: "\(dir)/map-narrow-light.png", dark: false)
        for dark in [false, true] {
            render(Dashboard(store: store, engine: .preview(), page: .map).frame(width: 1900, height: 1100),
                   to: "\(dir)/map-wide-\(dark ? "dark" : "light").png", dark: dark)
        }
        // Extensions narrowed: hooks only, and a search.
        render(ExtensionsSection(engine: .preview(), initialKinds: [.hook]).padding(Space.xl).frame(width: 900).fixedSize(horizontal: false, vertical: true).background(Theme.canvas),
               to: "\(dir)/extensions-hooks-light.png", dark: false)
        render(ExtensionsSection(engine: .preview(), initialQuery: "postman").padding(Space.xl).frame(width: 900).fixedSize(horizontal: false, vertical: true).background(Theme.canvas),
               to: "\(dir)/extensions-search-light.png", dark: false)
        // The Overview's status header in its other states, and the command palette.
        render(Dashboard(store: store, engine: .preview(agentsOff: true)).frame(width: 1200, height: 780),
               to: "\(dir)/overview-setup-light.png", dark: false)
        render(Dashboard(store: store, engine: .preview(starting: true)).frame(width: 1200, height: 780),
               to: "\(dir)/overview-starting-light.png", dark: false)
        render(Dashboard(store: store, engine: .preview(around: true)).frame(width: 1200, height: 780),
               to: "\(dir)/overview-around-light.png", dark: false)
        for dark in [false, true] {
            render(Dashboard(store: store, engine: .preview(), palette: "").frame(width: 1200, height: 780),
                   to: "\(dir)/palette-\(dark ? "dark" : "light").png", dark: dark)
        }
        render(Dashboard(store: store, engine: .preview(), palette: "exce").frame(width: 1200, height: 780),
               to: "\(dir)/palette-search-light.png", dark: false)
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
        render(ReviewCard(center: .shared, review: .sampleTooLong).frame(width: 560).background(Theme.canvas),
               to: "\(dir)/review-too-long-light.png", dark: false)
        render(ReviewCard(center: .shared, review: .sampleTyped).frame(width: 560).background(Theme.canvas),
               to: "\(dir)/review-typed-light.png", dark: false)
        render(ReviewCard(center: .shared, review: .sampleTypedNothingLeft).frame(width: 560).background(Theme.canvas),
               to: "\(dir)/review-typed-nothing-left-light.png", dark: false)
        // The original back in the box: the caution, and Send the Original.
        for dark in [false, true] {
            render(ReviewCard(center: .shared, review: .sample, draft: PendingReview.sample.flagged).frame(width: 560).background(Theme.canvas),
                   to: "\(dir)/review-original-\(dark ? "dark" : "light").png", dark: dark)
        }
        // A whole turn under review, in its window.
        for dark in [false, true] {
            render(TurnReviewView(center: .shared, turn: .sample, more: 1).frame(width: 1040, height: 720).background(Theme.canvas),
                   to: "\(dir)/review-turn-\(dark ? "dark" : "light").png", dark: dark)
        }
        render(TurnReviewView(center: .shared, turn: .sample, choices: ["1": "original"]).frame(width: 1040, height: 720).background(Theme.canvas),
               to: "\(dir)/review-turn-original-light.png", dark: false)
        // Paused while the local model writes: the pause counted up, the most it can last.
        var paused = PendingTurn.sample
        paused.assisting = true
        paused.hardDeadline = paused.created + 60 + 120
        paused.agent = "claude-code"
        paused.agentSince = paused.created - 3
        paused.agentLimit = 360
        render(TurnReviewView(center: .shared, turn: paused, previewPause: 12).frame(width: 1040, height: 720).background(Theme.canvas),
               to: "\(dir)/review-turn-paused-light.png", dark: false)
        // For keepmum.app: Local turns at the size of the site's figure (1440x780), with both checks local.
        render(Dashboard(store: store, engine: .preview(bothLocal: true), page: .settings, settingsPane: .handback).frame(width: 1440, height: 780),
               to: "\(dir)/site-turns-light.png", dark: false)
        // For keepmum.app: the Protection map with both checks local, a jailbreak sample on its way.
        render(mapPage(store, .preview(bothLocal: true), sample: .jailbreak, width: 1440, height: 1080),
               to: "\(dir)/site-map-light.png", dark: false)
        // A demo review, and what it says once answered.
        render(TurnReviewView(center: .shared, turn: .demo()).frame(width: 1040, height: 720).background(Theme.canvas),
               to: "\(dir)/review-turn-demo-light.png", dark: false)
        render(DemoOutcomeView(center: .shared, outcome: DemoOutcome(window: true, title: "Sent to the cloud", lines: [
            "What you typed: its rewrite goes in its place; it begins \u{201C}[removed by keepmum] Then review the retry logic in sync.py\u{2026}\u{201D}.",
            "What WebFetch returned: its summary goes in its place.",
            "What exec returned: nothing of it goes; keepmum\u{2019}s notice takes its place.",
            "Then the turn goes to the cloud. This conversation\u{2019}s later turns resend these pieces as you answered them; typed or returned again, they are asked about again."]))
            .frame(width: 1040, height: 520).background(Theme.canvas), to: "\(dir)/review-demo-outcome-light.png", dark: false)
        // The instruction editor, on the harm rewrite's instruction.
        for dark in [false, true] {
            render(InstructionEditor(engine: .preview(), check: "harm", strategy: "rewrite"),
                   to: "\(dir)/instruction-editor-\(dark ? "dark" : "light").png", dark: dark)
        }
        // Local turns with the harm check in Local mode and the injection check off.
        render(Dashboard(store: store, engine: .preview(harmOnlyLocal: true), page: .settings, settingsPane: .handback).frame(width: 1200, height: 1500),
               to: "\(dir)/settings-handback-harm-local-light.png", dark: false)
        ReviewCenter.shared.inject([], mode: "off")
        // The menu bar icon, plain and with the dot that points at Tools.
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

    private static func mapPage(_ store: StatsStore, _ engine: Engine, selected: MapPart = .injection, sample: MapSample? = nil,
                                width: CGFloat = 992, height: CGFloat = 1500) -> some View {
        ProtectionMapPage(store: store, engine: engine, selected: selected, sample: sample)
            .padding(Space.xl).frame(width: width, height: height, alignment: .topLeading).background(Theme.canvas)
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
