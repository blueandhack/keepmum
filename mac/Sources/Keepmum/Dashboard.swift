import SwiftUI

enum Page: String, CaseIterable, Identifiable {
    case overview, activity, logs, performance, map, detection, audit, settings
    var id: String { rawValue }
    var title: String { self == .map ? "Protection map" : rawValue.capitalized }
    var icon: String {
        switch self {
        case .overview: "square.grid.2x2"
        case .activity: "list.bullet.rectangle"
        case .logs: "text.magnifyingglass"
        case .performance: "speedometer"
        case .map: "point.topleft.down.to.point.bottomright.curvepath"
        case .detection: "line.3.horizontal.decrease.circle"
        case .audit: "checkmark.seal"
        case .settings: "gearshape"
        }
    }

    /// The sidebar's groups: what keepmum saw, and what it does about it.
    static let groups: [(String, [Page])] = [("Monitor", [.overview, .activity, .logs, .performance]),
                                             ("Protection", [.map, .detection, .audit])]
}

struct Dashboard: View {
    @Bindable var store: StatsStore
    @Bindable var engine: Engine
    @AppStorage("appearance") private var appearance: Appearance = .system
    @State private var page: Page
    @State private var settingsPane: SettingsPane
    /// ⌘K's command palette, over the page.
    @State private var paletteOpen: Bool
    private let paletteQuery: String

    /// `page` and `settingsPane`: where it opens (the snapshot renderer opens each in turn), and
    /// `palette`, a query in the open command palette.
    init(store: StatsStore, engine: Engine, page: Page = .overview, settingsPane: SettingsPane = .setup, palette: String? = nil) {
        self.store = store
        self.engine = engine
        _page = State(initialValue: page)
        _settingsPane = State(initialValue: settingsPane)
        _paletteOpen = State(initialValue: palette != nil)
        paletteQuery = palette ?? ""
    }

    var body: some View {
        HStack(spacing: 0) {
            Sidebar(store: store, page: $page, needsYou: Attention.items(store: store, engine: engine).count) {
                paletteOpen = true
            }
            .frame(width: 208)
            Hairline(vertical: true)
            VStack(spacing: Space.l) {
                Notices(store: store, engine: engine, page: $page)
                Group {
                    switch page {
                    case .overview: OverviewPage(store: store, engine: engine)
                    case .activity: ActivityPage(store: store, engine: engine)
                    case .logs: LogsPage(store: store, engine: engine)
                    case .performance: PerformancePage(store: store)
                    case .detection: DetectionPage(store: store, engine: engine)
                    case .map: ProtectionMapPage(store: store, engine: engine)
                    case .audit: AuditPage(engine: engine)
                    case .settings: SettingsPage(store: store, engine: engine, appearance: $appearance, pane: $settingsPane)
                    }
                }
                // Both bounds set, so the frame keeps its offered height and a taller page runs off
                // the bottom rather than pushing the notices off the top.
                .frame(maxWidth: .infinity, minHeight: 0, maxHeight: .infinity, alignment: .topLeading)
                .clipped()
            }
            .padding(Space.xl)
        }
        .overlay(alignment: .top) {
            if paletteOpen {
                ZStack(alignment: .top) {
                    Color.black.opacity(0.18).ignoresSafeArea().onTapGesture { paletteOpen = false }
                    CommandPalette(store: store, engine: engine, appearance: $appearance,
                                   close: { paletteOpen = false }, initialQuery: paletteQuery)
                        .padding(.top, 96)
                }
                .transition(.opacity)
            }
        }
        .animation(Motion.quick, value: paletteOpen)
        .background { shortcuts }
        .followsPalette()
        // Up under the hidden title bar, so the sidebar's material reaches the top of the window.
        .ignoresSafeArea(.container, edges: .top)
        .background(Theme.canvas)
        .preferredColorScheme(appearance.scheme)
        .onChange(of: engine.settingsRequested, initial: true) { _, requested in
            if let requested { page = .settings; settingsPane = requested; engine.settingsRequested = nil }
        }
        .onChange(of: engine.pageRequested, initial: true) { _, requested in
            if let requested { page = requested; engine.pageRequested = nil }
        }
        .onChange(of: engine.status?.port, initial: true) { _, p in
            if let p { store.configuredPort = p }
        }
    }

    /// ⌘K for the command palette, ⌘1 to ⌘8 for the pages in the sidebar's order: buttons no one
    /// sees, there for their keyboard shortcuts.
    private var shortcuts: some View {
        ZStack {
            Button("Search") { paletteOpen.toggle() }.keyboardShortcut("k")
            ForEach(Array(Page.shortcutOrder.enumerated()), id: \.element) { i, p in
                Button(p.title) { paletteOpen = false; page = p }.keyboardShortcut(KeyEquivalent(Character("\(i + 1)")))
            }
            Button("Settings") { paletteOpen = false; page = .settings }.keyboardShortcut(",")
        }
        .opacity(0).frame(width: 0, height: 0).accessibilityHidden(true)
    }
}

// MARK: - Sidebar

struct Sidebar: View {
    var store: StatsStore
    @Binding var page: Page
    /// How many things the Overview lists as needing you: a count beside it.
    var needsYou = 0
    var search: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            HStack(spacing: Space.s + Space.xxs) {
                KeepmumBadge(size: 28)
                VStack(alignment: .leading, spacing: 0) {
                    Text("keepmum").font(.km(.headline)).foregroundStyle(Theme.text)
                    Text("privacy proxy").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                }
            }
            .padding(.horizontal, Space.s).padding(.top, 44).padding(.bottom, Space.m)
            SearchButton(action: search).padding(.bottom, Space.xs)
            ForEach(Page.groups, id: \.0) { title, pages in
                Text(title).eyebrow().padding(.horizontal, Space.s).padding(.top, Space.m).padding(.bottom, Space.xs)
                ForEach(pages) { SidebarItem(page: $0, selection: $page, badge: $0 == .overview && needsYou > 0 ? needsYou : nil) }
            }
            Spacer()
            SidebarItem(page: .settings, selection: $page)
            Hairline().padding(.vertical, Space.s)
            VStack(alignment: .leading, spacing: Space.xs) {
                StatusDot(connection: store.connection, observing: store.snapshot.observe == true)
                Text(sessionLine).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, Space.s).padding(.bottom, Space.xs)
        }
        .padding(.horizontal, Space.s + Space.xxs).padding(.bottom, Space.m)
        .frame(maxHeight: .infinity)
        .background(SidebarBackground())
    }

    private var sessionLine: String {
        let s = store.snapshot
        switch store.connection {
        case .offline(let reason): return reason.sentence
        case .connecting: return "Looking for keepmum…"
        default:
            return "\(s.modeLabel) · PII \(s.pii == true ? "on" : "off")\nUp \(durationText(s.uptime))"
        }
    }
}

/// Looks like a search field; opens the command palette.
private struct SearchButton: View {
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.s) {
                Image(systemName: "magnifyingglass").font(.km(.callout)).frame(width: 18)
                Text("Search").font(.km(.body))
                Spacer(minLength: 0)
                Text("⌘K").font(.km(.caption).weight(.medium))
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, Space.s).padding(.vertical, Space.xs + 1)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(hovering ? Theme.fillStrong : Theme.fill))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("Find any page, setting or action (⌘K)")
        .accessibilityLabel("Search pages, settings and actions")
    }
}

private struct SidebarItem: View {
    var page: Page
    @Binding var selection: Page
    var badge: Int? = nil
    @State private var hovering = false

    var body: some View {
        let on = selection == page
        Button { selection = page } label: {
            HStack(spacing: Space.s) {
                Image(systemName: page.icon).font(.km(.body)).frame(width: 18)
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
                Text(page.title).font(.km(.body).weight(on ? .medium : .regular)).foregroundStyle(Theme.text)
                Spacer(minLength: 0)
                if let badge {
                    Text("\(badge)").font(.km(.caption).weight(.semibold)).foregroundStyle(Theme.onAccent)
                        .padding(.horizontal, Space.xs + 1).frame(minWidth: 18, minHeight: 16)
                        .background(Capsule().fill(Theme.accentFill))
                        .help(badge == 1 ? "One thing needs you" : "\(badge) things need you")
                }
            }
            .padding(.horizontal, Space.s).padding(.vertical, Space.xs + 1)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                .fill(on ? Theme.fillStrong : hovering ? Theme.fill : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(Page.shortcutOrder.firstIndex(of: page).map { "\(page.title) (⌘\($0 + 1))" } ?? page.title)
        .accessibilityLabel(page.title + (badge.map { ", \($0) need you" } ?? ""))
    }
}

// MARK: - Notices

/// What the dashboard must say before anything else, on every page: values are leaving unmasked
/// (observe mode), keepmum is not running, or the numbers are synthetic.
private struct Notices: View {
    var store: StatsStore
    var engine: Engine
    @Binding var page: Page

    var body: some View {
        // The Overview's status header says both of these itself, with the same button.
        let onOverview = page == .overview
        let observing = store.snapshot.observe == true && !isOffline && !onOverview
        if observing || (isOffline && !onOverview) || store.sampleMode {
            VStack(spacing: Space.s) {
                if observing {
                    Banner(tone: .danger, icon: "eye.trianglebadge.exclamationmark", title: "Observe only.",
                           message: "Requests leave this Mac unmasked; keepmum only counts what it would mask.") {
                        PillButton(title: "Mask again", icon: "eye.slash", onSolid: true) { Task { await engine.setObserve(false) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                }
                if case .offline(let reason) = store.connection, !onOverview {
                    Banner(tone: .warning, icon: "bolt.horizontal.circle", title: "Not connected.",
                           message: "\(reason.sentence). Start the background service in Settings, or run keepmum claude.") {
                        if page != .settings { PillButton(title: "Open Settings") { engine.settingsRequested = .setup } }
                    }
                }
                if store.sampleMode {
                    Banner(tone: .info, icon: "sparkles", title: "Preview data.",
                           message: "Synthetic numbers that show every page; nothing here happened on this Mac.") {
                        PillButton(title: "Exit preview") { store.sampleMode = false }
                    }
                }
            }
        }
    }

    private var isOffline: Bool {
        if case .offline = store.connection { return true }
        return false
    }
}

// MARK: - Pages

struct OverviewPage: View {
    var store: StatsStore
    /// Nil in the tour, whose cursor is placed for the page's plain title.
    var engine: Engine? = nil

    var body: some View {
        let s = store.snapshot
        VStack(spacing: Space.l) {
            // Where protection stands comes first; the numbers and the flow say what it did.
            if let engine {
                StatusHeader(store: store, engine: engine) { $0.perform(store: store, engine: engine) }
            } else {
                PageHeader(title: "Overview", subtitle: "What left this Mac, and what stayed")
            }
            HStack(spacing: Space.m) {
                Metric(title: "Unique values masked", value: (s.vaultSize ?? 0).compact, icon: "lock.fill",
                       footnote: engine == nil ? "\(s.maskedTotal.compact) masks applied, incl. resends" : "\(s.maskedTotal.compact) masks with resends", compact: engine != nil)
                Metric(title: "Requests scanned", value: s.funnel.scanned.compact, icon: "doc.text.magnifyingglass",
                       footnote: s.funnel.withNew.map { engine == nil ? "\($0.compact) brought new sensitive values" : "\($0.compact) with new values" }
                           ?? "\(percent(s.funnel.withFindings, of: s.funnel.scanned)) carried sensitive data", compact: engine != nil)
                Metric(title: "Blocked before leaving", value: s.blocked.compact, icon: "hand.raised.fill",
                       footnote: engine == nil ? "fail-closed: never sent raw" : "never sent raw", tone: s.blocked > 0 ? Theme.blocked : nil, compact: engine != nil)
                Metric(title: "Scan time (p95)", value: milliseconds(s.scanMs.p95), icon: "timer",
                       footnote: "budget \(Int(LatencyChart.budget)) ms",
                       tone: (s.scanMs.p95 ?? 0) > LatencyChart.budget ? Theme.warning : nil, compact: engine != nil)
            }
            Card(title: "Live flow", subtitle: "Requests out on top, replies back below; band width is the number of requests") {
                Segmented(options: [(3600.0, "Last hour"), (86400.0, "24 hours"), (604800.0, "7 days")],
                          selection: Binding(get: { store.flowWindow }, set: { store.flowWindow = $0 }))
            } content: {
                FlowBandsView(flows: store.flows, pulses: store.pulses, open: Array(store.open.values),
                              live: store.connection == .live || store.connection == .sample)
                    .task { await store.followLive() }
                    .frame(minHeight: 320, maxHeight: .infinity)
            }
        }
    }
}

struct PerformancePage: View {
    var store: StatsStore

    var body: some View {
        let s = store.snapshot
        let over = s.recent.compactMap(\.scanMs).filter { $0 > LatencyChart.budget }.count
        VStack(spacing: Space.l) {
            PageHeader(title: "Performance", subtitle: "Time keepmum adds before a request leaves")
            HStack(spacing: Space.m) {
                Metric(title: "Median (p50)", value: milliseconds(s.scanMs.p50), icon: "timer", footnote: "half of scans are faster")
                Metric(title: "p95", value: milliseconds(s.scanMs.p95), icon: "gauge.with.dots.needle.67percent",
                       footnote: "budget \(Int(LatencyChart.budget)) ms", tone: (s.scanMs.p95 ?? 0) > LatencyChart.budget ? Theme.warning : nil)
                Metric(title: "Scans measured", value: s.scanMs.n.compact, icon: "number", footnote: "only new text is scanned")
                Metric(title: "Over budget", value: over.compact, icon: "exclamationmark.triangle", footnote: "of recent requests",
                       tone: over > 0 ? Theme.warning : nil)
            }
            Card(title: "Scan time", subtitle: "Recent requests") { LatencyChart(events: s.recent) }
            Card(title: "Distribution", subtitle: "Requests per 25 ms bucket") { LatencyHistogram(events: s.recent) }
                .frame(height: 210)
        }
    }
}

/// Settings as one column of groups, as in System Settings: setup and the agents first, then
/// what gets masked, exclusions, display, and the connection.

/// `mcp__claude_ai_Gmail__search_threads` -> "Gmail".
func toolLabel(_ tool: String) -> String {
    guard tool.hasPrefix("mcp__") else { return tool }
    let server = tool.dropFirst(5).components(separatedBy: "__").first ?? ""
    let name = server.hasPrefix("claude_ai_") ? String(server.dropFirst(10)) : server
    return name.replacingOccurrences(of: "_", with: " ")
}

/// A check's hit in an Activity row: "Injection · WebFetch", "Harm · Jailbreak", "Command · a download run as a script".
func checkLabel(_ c: Snapshot.Event.Check) -> String {
    switch c.check {
    case "harm": "Harm · \((c.categories ?? []).first ?? "flagged")"
    case "commands": "Command · \((c.categories ?? []).first ?? "flagged")"
    case "script": "Script · \((c.categories ?? []).first ?? "flagged")"
    default: "Injection · \(toolLabel(c.source ?? "a tool"))"
    }
}

func checkHelp(_ c: Snapshot.Event.Check) -> String {
    if c.check == "script" {
        return "A Codex script in the reply hands values it works out itself, such as a command's output, to a network tool: "
            + "\((c.categories ?? []).joined(separator: ", ")). keepmum could not check them; the agent got the script with a note after it (warn)."
    }
    if c.check == "commands" {
        let what = "The commands check flagged a \(c.source ?? "shell") command in the reply: \((c.categories ?? []).joined(separator: ", "))."
        let outcome = c.action == "blocked" ? "The agent got a command that says why and fails, in its place."
                                            : "The agent got it as the model wrote it (warn)."
        return "\(what) \(outcome) keepmum keeps the rule, never the command."
    }
    let what = c.check == "harm"
        ? "The harm check flagged what was typed" + ((c.categories ?? []).isEmpty ? "" : " (\((c.categories ?? []).joined(separator: ", ")))")
        : "The injection check flagged what \(c.source.map(toolLabel) ?? "a tool") returned as a likely prompt injection"
    let outcome = c.action == "blocked" ? "The request was not sent." : "The request was sent; the check ran after it (warn)."
    return "\(what), score \(String(format: "%.2f", c.score)). \(outcome) keepmum keeps the score, never the text."
}
