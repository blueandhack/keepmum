import SwiftUI

enum Page: String, CaseIterable, Identifiable {
    case overview, activity, logs, performance, detection, audit, settings
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var icon: String {
        switch self {
        case .overview: "square.grid.2x2"
        case .activity: "list.bullet.rectangle"
        case .logs: "text.magnifyingglass"
        case .performance: "speedometer"
        case .detection: "line.3.horizontal.decrease.circle"
        case .audit: "checkmark.seal"
        case .settings: "gearshape"
        }
    }

    /// The sidebar's groups: what keepmum saw, and what it does about it.
    static let groups: [(String, [Page])] = [("Monitor", [.overview, .activity, .logs, .performance]),
                                             ("Protection", [.detection, .audit])]
}

struct Dashboard: View {
    @Bindable var store: StatsStore
    @Bindable var engine: Engine
    @AppStorage("appearance") private var appearance: Appearance = .system
    @State var page: Page = .overview

    var body: some View {
        HStack(spacing: 0) {
            Sidebar(store: store, page: $page).frame(width: 208)
            Hairline(vertical: true)
            VStack(spacing: Space.l) {
                Notices(store: store, engine: engine, page: $page)
                Group {
                    switch page {
                    case .overview: OverviewPage(store: store)
                    case .activity: ActivityPage(store: store)
                    case .logs: LogsPage(store: store)
                    case .performance: PerformancePage(store: store)
                    case .detection: DetectionPage(store: store, engine: engine)
                    case .audit: AuditPage(engine: engine)
                    case .settings: SettingsPage(store: store, engine: engine, appearance: $appearance)
                    }
                }
                // Both bounds set, so the frame keeps its offered height and a taller page runs off
                // the bottom rather than pushing the notices off the top.
                .frame(maxWidth: .infinity, minHeight: 0, maxHeight: .infinity, alignment: .topLeading)
                .clipped()
            }
            .padding(Space.xl)
        }
        // Up under the hidden title bar, so the sidebar's material reaches the top of the window.
        .ignoresSafeArea(.container, edges: .top)
        .background(Theme.canvas)
        .preferredColorScheme(appearance.scheme)
        .onChange(of: engine.settingsRequested, initial: true) { _, requested in
            if requested { page = .settings; engine.settingsRequested = false }
        }
        .onChange(of: engine.status?.port, initial: true) { _, p in
            if let p { store.configuredPort = p }
        }
    }
}

// MARK: - Sidebar

struct Sidebar: View {
    var store: StatsStore
    @Binding var page: Page

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            HStack(spacing: Space.s + Space.xxs) {
                KeepmumBadge(size: 28)
                VStack(alignment: .leading, spacing: 0) {
                    Text("keepmum").font(.km(.headline)).foregroundStyle(Theme.text)
                    Text("privacy proxy").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                }
            }
            .padding(.horizontal, Space.s).padding(.top, 44).padding(.bottom, Space.l)
            ForEach(Page.groups, id: \.0) { title, pages in
                Text(title).eyebrow().padding(.horizontal, Space.s).padding(.top, Space.m).padding(.bottom, Space.xs)
                ForEach(pages) { SidebarItem(page: $0, selection: $page) }
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

private struct SidebarItem: View {
    var page: Page
    @Binding var selection: Page
    @State private var hovering = false

    var body: some View {
        let on = selection == page
        Button { selection = page } label: {
            HStack(spacing: Space.s) {
                Image(systemName: page.icon).font(.km(.body)).frame(width: 18)
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
                Text(page.title).font(.km(.body).weight(on ? .medium : .regular)).foregroundStyle(Theme.text)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Space.s).padding(.vertical, Space.xs + 1)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                .fill(on ? Theme.fillStrong : hovering ? Theme.fill : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
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
        let observing = store.snapshot.observe == true && !isOffline
        if observing || isOffline || store.sampleMode {
            VStack(spacing: Space.s) {
                if observing {
                    Banner(tone: .danger, icon: "eye.trianglebadge.exclamationmark", title: "Observe only.",
                           message: "Requests leave this Mac unmasked; keepmum only counts what it would mask.") {
                        PillButton(title: "Mask again", icon: "eye.slash", onSolid: true) { Task { await engine.setObserve(false) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                }
                if case .offline(let reason) = store.connection {
                    Banner(tone: .warning, icon: "bolt.horizontal.circle", title: "Not connected.",
                           message: "\(reason.sentence). Start the background service in Settings, or run keepmum claude.") {
                        if page != .settings { PillButton(title: "Open Settings") { page = .settings } }
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

    var body: some View {
        let s = store.snapshot
        VStack(spacing: Space.l) {
            PageHeader(title: "Overview", subtitle: "What left this Mac, and what stayed")
            HStack(spacing: Space.m) {
                Metric(title: "Unique values masked", value: (s.vaultSize ?? 0).compact, icon: "lock.fill",
                       footnote: "\(s.maskedTotal.compact) masks applied, incl. resends")
                Metric(title: "Requests scanned", value: s.funnel.scanned.compact, icon: "doc.text.magnifyingglass",
                       footnote: s.funnel.withNew.map { "\($0.compact) brought new sensitive values" }
                           ?? "\(percent(s.funnel.withFindings, of: s.funnel.scanned)) carried sensitive data")
                Metric(title: "Blocked before leaving", value: s.blocked.compact, icon: "hand.raised.fill",
                       footnote: "fail-closed: never sent raw", tone: s.blocked > 0 ? Theme.blocked : nil)
                Metric(title: "Scan time (p95)", value: milliseconds(s.scanMs.p95), icon: "timer",
                       footnote: "budget \(Int(LatencyChart.budget)) ms",
                       tone: (s.scanMs.p95 ?? 0) > LatencyChart.budget ? Theme.warning : nil)
            }
            Card(title: "Live flow", subtitle: "Requests out on top, replies back below; band width is the number of requests") {
                Segmented(options: [(3600.0, "Last hour"), (86400.0, "24 hours"), (604800.0, "7 days")],
                          selection: Binding(get: { store.flowWindow }, set: { store.flowWindow = $0 }))
            } content: {
                FlowBandsView(flows: store.flows, pulses: store.pulses, live: store.connection == .live || store.connection == .sample)
                    .frame(minHeight: 320, maxHeight: .infinity)
            }
        }
    }
}

struct ActivityPage: View {
    var store: StatsStore
    @State private var filter: Snapshot.Event.Kind? = nil
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let s = store.snapshot
        VStack(spacing: Space.l) {
            PageHeader(title: "Activity", subtitle: "Every request, newest first. Counts only: keepmum never shows a value.")
            Card(title: "Last hour", subtitle: "Masked values per minute; a red dot is a blocked request") {
                MaskedTimeline(minutes: s.timeline).frame(height: 110)
            }
            .fixedSize(horizontal: false, vertical: true)
            Card(title: "Requests", subtitle: subtitle) {
                Segmented(options: [(Optional<Snapshot.Event.Kind>.none, "All"), (.masked, "Masked"), (.clean, "Clean"),
                                    (.blocked, "Blocked"), (.observed, "Observed")], selection: $filter)
            } content: {
                let events = store.newerThanHistory(kind: filter) + store.history
                ActivityTable(events: events, rows: isSnapshot ? 11 : events.count, newestFirst: true,
                              footer: !store.canLoadOlder ? nil : { Task { await store.loadHistory(kind: filter, more: true) } })
            }
        }
        .task(id: "\(filter?.rawValue ?? ""):\(store.sampleMode)") { await store.loadHistory(kind: filter) }
    }

    private var subtitle: String {
        switch filter {
        case .masked: "Requests whose new text had something to mask"
        case .clean: "Nothing new to mask (earlier turns may still hold masked values)"
        case .blocked: "Never sent: scanning failed or the endpoint is not scanned"
        case .observed: "Observe mode: sent unmasked, only counted"
        case nil: "Kept across restarts in ~/.config/keepmum/history.jsonl"
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
struct SettingsPage: View {
    var store: StatsStore
    var engine: Engine
    @Binding var appearance: Appearance
    @AppStorage(DashboardWindow.dockKey) private var showInDock = true
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        VStack(spacing: Space.l) {
            PageHeader(title: "Settings", subtitle: "Setup, masking, exclusions and display")
            if isSnapshot {
                form
                Spacer(minLength: 0)
            } else {
                ScrollView { form.padding(.bottom, Space.xl) }.scrollIndicators(.automatic)
            }
        }
    }

    private var form: some View {
        let s = store.snapshot
        return VStack(alignment: .leading, spacing: Space.xl) {
            SetupSection(engine: engine)
            MaskingSection(engine: engine, observing: s.observe == true, modelError: s.detection.modelError)
            ExclusionsSection(engine: engine)
            FormSection(title: "Display") {
                FormRow(title: "Appearance") {
                    Segmented(options: Appearance.allCases.map { ($0, $0.title) }, selection: $appearance)
                }
                Hairline()
                FormRow(title: "Show in Dock", detail: "While the dashboard is open, so you can switch back to it; keepmum always stays in the menu bar") {
                    Switch(isOn: showInDock) { showInDock = $0; DashboardWindow.applyDockPolicy() }.accessibilityLabel("Show in Dock")
                }
                Hairline()
                FormRow(title: "Preview data", detail: "Synthetic numbers on every page, clearly labeled, to see the dashboard without real traffic") {
                    Switch(isOn: store.sampleMode) { store.sampleMode = $0 }.accessibilityLabel("Preview data")
                }
            }
            FormSection(title: "Connection",
                        footer: "This app reads keepmum's stats feed: counts, types and timestamps, and for the Logs page the first and last character of long masked values (turn that off with [log] previews = false). It never receives a whole value or a placeholder, talks only to 127.0.0.1, and sends no telemetry.") {
                row("Status") { StatusDot(connection: store.connection, observing: s.observe == true) }
                row("Endpoint", "127.0.0.1:\(store.port)")
                row("Mode", s.modeLabel)
                row("Personal data", s.pii.map { $0 ? "masked" : "not masked" } ?? "—")
                row("Detector", s.detector ?? "—")
                row("Upstream", s.upstream ?? "—")
                row("Version", s.version ?? "—")
                row("Token", "~/.config/keepmum/stats-token", last: true)
            }
        }
        .frame(maxWidth: 760, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ key: String, _ value: String, last: Bool = false) -> some View {
        row(key, last: last) {
            Text(value).font(.km(.mono)).foregroundStyle(Theme.textSecondary).lineLimit(1).truncationMode(.middle).textSelection(.enabled)
        }
    }

    private func row(_ key: String, last: Bool = false, @ViewBuilder _ value: () -> some View) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text(key).font(.km(.body)).foregroundStyle(Theme.text)
                Spacer(minLength: Space.m)
                value()
            }
            .padding(.vertical, Space.s)
            if !last { Hairline() }
        }
    }
}

struct ActivityTable: View {
    var events: [Snapshot.Event]
    var rows = 14
    var compact = false
    /// `events` already newest first (history); otherwise the feed's oldest-first `recent`.
    var newestFirst = false
    /// Shown at the end of a scrolling list to load older events.
    var footer: (() -> Void)? = nil
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        VStack(spacing: 0) {
            ColumnHeader {
                Text("Time").frame(width: 70, alignment: .leading)
                Text("Outcome").frame(width: 90, alignment: .leading)
                if !compact { Text("Endpoint").frame(width: 170, alignment: .leading) }
                Text("Masked").frame(maxWidth: .infinity, alignment: .leading)
                if !compact { Text("Restored").frame(width: 60, alignment: .trailing) }
                Text("Scan").frame(width: 56, alignment: .trailing)
            }
            let shown = newestFirst ? Array(events.prefix(rows)) : Array(events.suffix(rows).reversed())
            if newestFirst && !isSnapshot && !events.isEmpty {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(shown) { e in row(e) }
                        if let footer {
                            PillButton(title: "Load older", icon: "arrow.down", action: footer).padding(.vertical, Space.s + Space.xxs)
                        }
                    }
                }
            } else {
                ForEach(shown) { e in row(e) }
            }
            if events.isEmpty { EmptyNote(text: "No requests yet") }
            Spacer(minLength: 0)
        }
    }

    private func row(_ e: Snapshot.Event) -> some View {
        // Clean, but carrying values masked in earlier turns.
        let earlier = e.kind == .clean && !e.redacted.isEmpty
        let restored = e.restored.values.reduce(0, +)
        return TableRow {
            Text(e.date, format: .dateTime.hour().minute().second())
                .font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary).frame(width: 70, alignment: .leading)
            OutcomeLabel(kind: e.kind)
                .help(earlier ? "Nothing new to mask; earlier turns, which Claude Code resends, still held masked values" : "")
                .frame(width: 90, alignment: .leading)
            if !compact {
                Text(e.path).font(.km(.monoSmall)).foregroundStyle(Theme.text).lineLimit(1)
                    .frame(width: 170, alignment: .leading)
            }
            HStack(spacing: Space.s + Space.xxs) {
                ForEach(e.redacted.sorted { $0.key < $1.key }, id: \.key) { type, n in
                    HStack(spacing: Space.xs) {
                        Image(systemName: Theme.icon(type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                        Text("\(Theme.label(type)) \(n)").font(.km(.callout)).foregroundStyle(earlier ? Theme.textSecondary : Theme.text)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if !compact {
                Text(restored > 0 ? "\(restored)" : "—")
                    .font(.km(.callout)).monospacedDigit().foregroundStyle(restored > 0 ? Theme.restored : Theme.textSecondary)
                    .frame(width: 60, alignment: .trailing)
            }
            Text(milliseconds(e.scanMs))
                .font(.km(.callout)).monospacedDigit()
                .foregroundStyle((e.scanMs ?? 0) > LatencyChart.budget ? Theme.warning : Theme.textSecondary)
                .frame(width: 56, alignment: .trailing)
        }
    }
}
