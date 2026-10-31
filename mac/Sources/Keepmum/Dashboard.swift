import SwiftUI

enum Page: String, CaseIterable, Identifiable {
    case overview, activity, logs, audit, detection, performance, settings
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var icon: String {
        switch self {
        case .overview: "square.grid.2x2"
        case .activity: "list.bullet.rectangle"
        case .logs: "text.magnifyingglass"
        case .audit: "checkmark.seal"
        case .detection: "line.3.horizontal.decrease.circle"
        case .performance: "speedometer"
        case .settings: "gearshape"
        }
    }
}

struct Dashboard: View {
    @Bindable var store: StatsStore
    @Bindable var engine: Engine
    @AppStorage("appearance") private var appearance: Appearance = .system
    @State var page: Page = .overview

    var body: some View {
        HStack(spacing: 0) {
            Sidebar(store: store, page: $page).frame(width: 196)
            Rectangle().fill(Theme.border).frame(width: 1)
            Group {
                switch page {
                case .overview: OverviewPage(store: store)
                case .activity: ActivityPage(store: store)
                case .logs: LogsPage(store: store)
                case .audit: AuditPage(engine: engine)
                case .detection: DetectionPage(store: store, engine: engine)
                case .performance: PerformancePage(store: store)
                case .settings: SettingsPage(store: store, engine: engine, appearance: $appearance)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .background(Theme.background)
        .preferredColorScheme(appearance.scheme)
        .onChange(of: engine.settingsRequested, initial: true) { _, requested in
            if requested { page = .settings; engine.settingsRequested = false }
        }
        .onChange(of: engine.status?.port, initial: true) { _, p in
            if let p { store.configuredPort = p }
        }
    }
}

struct Sidebar: View {
    var store: StatsStore
    @Binding var page: Page

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                KeepmumBadge(size: 30)
                VStack(alignment: .leading, spacing: 0) {
                    Text("keepmum").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.text)
                    Text("privacy proxy").font(.system(size: 10.5)).foregroundStyle(Theme.secondary)
                }
            }
            .padding(.horizontal, 8).padding(.top, 40).padding(.bottom, 18)
            ForEach(Page.allCases) { p in
                Button { page = p } label: {
                    HStack(spacing: 10) {
                        Image(systemName: p.icon).font(.system(size: 13)).frame(width: 18)
                        Text(p.title).font(.system(size: 13, weight: page == p ? .semibold : .regular))
                        Spacer()
                    }
                    .foregroundStyle(page == p ? Theme.accent : Theme.text)
                    .padding(.horizontal, 10).padding(.vertical, 7)
                    .background(Capsule().fill(page == p ? Theme.accentSoft : .clear))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Spacer()
            VStack(alignment: .leading, spacing: 6) {
                StatusDot(connection: store.connection)
                Text(sessionLine).font(.system(size: 11)).foregroundStyle(Theme.secondary).lineLimit(2)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.faint))
        }
        .padding(12)
        .frame(maxHeight: .infinity)
        .background(Theme.sidebar)
    }

    private var sessionLine: String {
        let s = store.snapshot
        switch store.connection {
        case .offline(let reason): return reason
        case .connecting: return "Looking for keepmum…"
        default:
            return "\(s.modeLabel) · PII \(s.pii == true ? "on" : "off") · up \(durationText(s.uptime))"
        }
    }
}

// MARK: - Shared pieces

struct PageHeader<Trailing: View>: View {
    var title: String
    var subtitle: String
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 20, weight: .semibold)).foregroundStyle(Theme.text)
                Text(subtitle).font(.system(size: 12)).foregroundStyle(Theme.secondary)
            }
            Spacer()
            trailing
        }
    }
}

struct KPI: View {
    var title: String
    var value: String
    var icon: String
    var footnote: String
    var tint: Color = Theme.accent

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint)
                Text(title).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.secondary)
            }
            Text(value).font(.system(size: 24, weight: .semibold, design: .rounded)).monospacedDigit()
                .foregroundStyle(Theme.text).contentTransition(.numericText())
            Text(footnote).font(.system(size: 10.5)).foregroundStyle(Theme.secondary).lineLimit(1)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Theme.surface))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Theme.border))
    }
}

struct OfflineBanner: View {
    var reason: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bolt.horizontal.circle").foregroundStyle(Theme.danger)
            Text("\(reason). Start the background service in Settings, or run ").foregroundStyle(Theme.text)
                + Text("keepmum claude").font(.system(size: 12.5, design: .monospaced)).foregroundStyle(Theme.accent)
                + Text(", to see live data.").foregroundStyle(Theme.text)
            Spacer()
        }
        .font(.system(size: 12.5))
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Capsule().fill(Theme.danger.opacity(0.08)))
        .overlay(Capsule().strokeBorder(Theme.danger.opacity(0.2)))
    }
}

/// Observe mode is a choice, but never a quiet one.
struct ObserveBanner: View {
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "eye.trianglebadge.exclamationmark").foregroundStyle(Theme.danger)
            Text("Observe only: requests go to Anthropic unmasked. keepmum counts what it would mask. Switch back in Settings.")
                .foregroundStyle(Theme.text)
            Spacer()
        }
        .font(.system(size: 12.5))
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Capsule().fill(Theme.danger.opacity(0.08)))
        .overlay(Capsule().strokeBorder(Theme.danger.opacity(0.2)))
    }
}

struct PreviewToggle: View {
    var store: StatsStore
    var body: some View {
        PillButton(title: store.sampleMode ? "Exit preview" : "Preview", icon: store.sampleMode ? "xmark" : "sparkles") {
            store.sampleMode.toggle()
        }
    }
}

// MARK: - Pages

struct OverviewPage: View {
    var store: StatsStore

    var body: some View {
        let s = store.snapshot
        VStack(spacing: 16) {
            PageHeader(title: "Overview", subtitle: "What left this Mac, and what stayed") { PreviewToggle(store: store) }
            if case .offline(let reason) = store.connection { OfflineBanner(reason: reason) }
            if s.observe == true { ObserveBanner() }
            HStack(spacing: 12) {
                KPI(title: "Unique values masked", value: (s.vaultSize ?? 0).compact, icon: "lock.fill",
                    footnote: "\(s.maskedTotal.compact) masks applied, incl. resends")
                KPI(title: "Requests scanned", value: s.funnel.scanned.compact, icon: "doc.text.magnifyingglass",
                    footnote: s.funnel.withNew.map { "\($0.compact) brought new sensitive values" }
                        ?? "\(percent(s.funnel.withFindings, of: s.funnel.scanned)) carried sensitive data")
                KPI(title: "Blocked before leaving", value: s.blocked.compact, icon: "hand.raised.fill",
                    footnote: "fail-closed: never sent raw", tint: s.blocked > 0 ? Theme.danger : Theme.accent)
                KPI(title: "Scan time (p95)", value: s.scanMs.p95.map { String(format: "%.0f ms", $0) } ?? "—", icon: "timer",
                    footnote: "budget 150 ms",
                    tint: (s.scanMs.p95 ?? 0) > LatencyChart.budget ? Theme.danger : Theme.accent)
            }
            Card(title: "Live flow", subtitle: "Requests out on top, replies back below. Band width is the number of requests.") {
                Segmented(options: [(3600.0, "Last hour"), (86400.0, "24 hours"), (604800.0, "7 days")],
                          selection: Binding(get: { store.flowWindow }, set: { store.flowWindow = $0 }))
            } content: {
                FlowBandsView(flows: store.flows, pulses: store.pulses, live: store.connection == .live || store.connection == .sample)
                    .frame(minHeight: 360, maxHeight: .infinity)
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
        VStack(spacing: 16) {
            PageHeader(title: "Activity", subtitle: "Every request, newest first. Counts only: keepmum never shows a value.") { PreviewToggle(store: store) }
            Card(title: "Last hour", subtitle: "Masked values per minute · red dot: a blocked request") {
                MaskedTimeline(minutes: s.timeline).frame(height: 120)
            }
            .fixedSize(horizontal: false, vertical: true)
            Card(title: "Requests", subtitle: subtitle) {
                Segmented(options: [(Optional<Snapshot.Event.Kind>.none, "All"), (.masked, "Masked"), (.clean, "Clean"),
                                    (.blocked, "Blocked"), (.observed, "Observed")], selection: $filter)
            } content: {
                let events = store.newerThanHistory(kind: filter) + store.history
                ActivityTable(events: events, rows: isSnapshot ? 11 : events.count, newestFirst: true,
                              footer: store.historyDone ? nil : { Task { await store.loadHistory(kind: filter, more: true) } })
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
        VStack(spacing: 16) {
            PageHeader(title: "Performance", subtitle: "Time keepmum adds before a request leaves") { PreviewToggle(store: store) }
            HStack(spacing: 12) {
                KPI(title: "Median (p50)", value: s.scanMs.p50.map { String(format: "%.0f ms", $0) } ?? "—", icon: "timer", footnote: "half of scans are faster")
                KPI(title: "p95", value: s.scanMs.p95.map { String(format: "%.0f ms", $0) } ?? "—", icon: "gauge.with.dots.needle.67percent",
                    footnote: "budget 150 ms", tint: (s.scanMs.p95 ?? 0) > LatencyChart.budget ? Theme.danger : Theme.accent)
                KPI(title: "Scans measured", value: s.scanMs.n.compact, icon: "number", footnote: "only new text is scanned")
                KPI(title: "Over budget", value: over.compact, icon: "exclamationmark.triangle", footnote: "of recent requests",
                    tint: over > 0 ? Theme.danger : Theme.accent)
            }
            Card(title: "Scan time", subtitle: "Recent requests") { LatencyChart(events: s.recent) }
            Card(title: "Distribution", subtitle: "Requests per 25 ms bucket") { LatencyHistogram(events: s.recent) }
                .frame(height: 210)
        }
    }
}

struct SettingsPage: View {
    var store: StatsStore
    var engine: Engine
    @Binding var appearance: Appearance
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let s = store.snapshot
        VStack(spacing: 16) {
            PageHeader(title: "Settings", subtitle: "Setup, appearance and connection") { EmptyView() }
            if isSnapshot {
                columns(s)
                Spacer()
            } else {
                ScrollView { columns(s) }
            }
        }
    }

    private func columns(_ s: Snapshot) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 12) {
                SetupCard(engine: engine)
                    .fixedSize(horizontal: false, vertical: true)
                Card(title: "Display", subtitle: "Appearance, and preview data: synthetic numbers, clearly labeled") {
                    HStack {
                        Segmented(options: Appearance.allCases.map { ($0, $0.title) }, selection: $appearance)
                        Spacer()
                        PreviewToggle(store: store)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                Card(title: "Privacy") {
                    Text("This app reads keepmum's stats feed: counts, types and timestamps, and for the Logs page the first and last character of long masked values (turn that off with [log] previews = false). It never receives a whole value or a placeholder, talks only to 127.0.0.1, and sends no telemetry.")
                        .font(.system(size: 12)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 12) {
                Card(title: "Connection") {
                    VStack(spacing: 0) {
                        row("Status", { StatusDot(connection: store.connection) })
                        row("Endpoint", "127.0.0.1:\(store.port)")
                        row("Mode", s.modeLabel)
                        row("Personal data", s.pii.map { $0 ? "masked" : "not masked" } ?? "—")
                        row("Detector", s.detector ?? "—")
                        row("Upstream", s.upstream ?? "—")
                        row("Version", s.version ?? "—")
                        row("Token", "~/.config/keepmum/stats-token")
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                ModeCard(engine: engine, observing: store.snapshot.observe == true)
                    .fixedSize(horizontal: false, vertical: true)
                DetectorCard(engine: engine, modelError: store.snapshot.detection.modelError)
                    .fixedSize(horizontal: false, vertical: true)
                ExclusionsCard(engine: engine)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func row(_ key: String, _ value: String) -> some View {
        row(key, { Text(value).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle) })
    }

    private func row(_ key: String, @ViewBuilder _ value: () -> some View) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text(key).font(.system(size: 12)).foregroundStyle(Theme.secondary)
                Spacer()
                value()
            }
            .padding(.vertical, 8)
            Rectangle().fill(Theme.border).frame(height: 1)
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
            HStack(spacing: 12) {
                Text("Time").frame(width: 70, alignment: .leading)
                Text("Outcome").frame(width: 90, alignment: .leading)
                if !compact { Text("Endpoint").frame(width: 170, alignment: .leading) }
                Text("Masked").frame(maxWidth: .infinity, alignment: .leading)
                if !compact { Text("Restored").frame(width: 60, alignment: .trailing) }
                Text("Scan").frame(width: 56, alignment: .trailing)
            }
            .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.secondary)
            .padding(.bottom, 6)
            Rectangle().fill(Theme.border).frame(height: 1)
            let shown = newestFirst ? Array(events.prefix(rows)) : Array(events.suffix(rows).reversed())
            if newestFirst && !isSnapshot {
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(shown) { e in row(e) }
                        if let footer {
                            PillButton(title: "Load older", icon: "arrow.down", action: footer).padding(.vertical, 10)
                        }
                    }
                }
            } else {
                ForEach(shown) { e in row(e) }
            }
            if events.isEmpty {
                Text("No requests yet").font(.system(size: 12)).foregroundStyle(Theme.secondary).padding(.top, 12)
            }
            Spacer(minLength: 0)
        }
    }

    private func row(_ e: Snapshot.Event) -> some View {
        let kind = e.kind
        let (label, icon, tint): (String, String, Color) = switch kind {
        case .blocked: ("Blocked", "hand.raised.fill", Theme.danger)
        case .observed: ("Observed", "eye", Theme.secondary)
        case .masked: ("Masked", "eye.slash", Theme.accent)
        case .clean: ("Clean", "checkmark.circle", Theme.secondary)
        }
        // Clean, but carrying values masked in earlier turns.
        let earlier = kind == .clean && !e.redacted.isEmpty
        return VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text(e.date, format: .dateTime.hour().minute().second())
                    .font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Theme.secondary).frame(width: 70, alignment: .leading)
                HStack(spacing: 5) {
                    Image(systemName: icon)
                    Text(label)
                }
                .help(earlier ? "Nothing new to mask; earlier turns, which Claude Code resends, still held masked values" : "")
                .font(.system(size: 11.5)).foregroundStyle(tint)
                .frame(width: 90, alignment: .leading)
                if !compact {
                    Text(e.path).font(.system(size: 11.5, design: .monospaced)).foregroundStyle(Theme.text).lineLimit(1)
                        .frame(width: 170, alignment: .leading)
                }
                HStack(spacing: 10) {
                    ForEach(e.redacted.sorted { $0.key < $1.key }, id: \.key) { type, n in
                        HStack(spacing: 4) {
                            Image(systemName: Theme.icon(type)).font(.system(size: 9.5)).foregroundStyle(Theme.secondary)
                            Text("\(Theme.label(type)) \(n)").font(.system(size: 11.5)).foregroundStyle(earlier ? Theme.secondary : Theme.text)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if !compact {
                    Text(e.restored.values.reduce(0, +) > 0 ? "\(e.restored.values.reduce(0, +))" : "—")
                        .font(.system(size: 11.5)).monospacedDigit().foregroundStyle(Theme.secondary).frame(width: 60, alignment: .trailing)
                }
                Text(e.scanMs.map { String(format: "%.0f ms", $0) } ?? "—")
                    .font(.system(size: 11.5)).monospacedDigit()
                    .foregroundStyle((e.scanMs ?? 0) > LatencyChart.budget ? Theme.danger : Theme.secondary)
                    .frame(width: 56, alignment: .trailing)
            }
            .padding(.vertical, 7)
            Rectangle().fill(Theme.border).frame(height: 1)
        }
    }
}
