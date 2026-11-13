import SwiftUI

/// Settings, one pane at a time: a rail of panes in groups beside one column of sections, so each
/// pane holds a few related things and stays short enough to scan.
enum SettingsPane: String, CaseIterable, Identifiable {
    case setup, masking, tools, checks, extensions, localModels, handback, skills, general, doctor
    var id: String { rawValue }

    /// The rail's groups, in order; the first has no heading.
    enum Group: CaseIterable {
        case start, protection, local, app
        var title: String? {
            switch self {
            case .start: nil
            case .protection: "Protection"
            case .local: "Local mode"
            case .app: "This app"
            }
        }
        var panes: [SettingsPane] { SettingsPane.allCases.filter { $0.group == self } }
    }

    var group: Group {
        switch self {
        case .setup: .start
        case .masking, .tools, .checks, .extensions: .protection
        case .localModels, .handback, .skills: .local
        case .general, .doctor: .app
        }
    }

    var title: String {
        switch self {
        case .setup: "Setup"
        case .masking: "Masking"
        case .tools: "Tools"
        case .checks: "Checks"
        case .extensions: "Extensions"
        case .localModels: "Local models"
        case .handback: "Hand-back"
        case .skills: "Skills"
        case .general: "General"
        case .doctor: "Check this Mac"
        }
    }

    var icon: String {
        switch self {
        case .setup: "power"
        case .masking: "eye.slash"
        case .tools: "network"
        case .checks: "checkmark.shield"
        case .extensions: "puzzlepiece.extension"
        case .localModels: "cpu"
        case .handback: "arrow.uturn.up"
        case .skills: "text.book.closed"
        case .general: "slider.horizontal.3"
        case .doctor: "stethoscope"
        }
    }

    var subtitle: String {
        switch self {
        case .setup: "The local model, the background service, and the coding agents keepmum protects"
        case .masking: "What is masked before a request leaves this Mac, and the projects left out"
        case .tools: "What each tool that reaches the network may receive, and where shell commands may send tokens"
        case .checks: "Optional checks on what tools return, what you type, and the commands in replies"
        case .extensions: "The plugins, hooks and MCP servers in your coding agents' settings, and what other apps added"
        case .localModels: "The models on this Mac that answer what a check keeps local, and how fast they answer"
        case .handback: "What the cloud model gets after a turn the injection check kept on this Mac, and whether you look first"
        case .skills: "Your own instructions for the local model on the turns keepmum keeps on this Mac"
        case .general: "Appearance, updates, and what the Logs page shows"
        case .doctor: "The same checks as keepmum doctor, and this app's connection to keepmum"
        }
    }
}

struct SettingsPage: View {
    var store: StatsStore
    var engine: Engine
    @Binding var appearance: Appearance
    @Binding var pane: SettingsPane
    @AppStorage(DashboardWindow.dockKey) private var showInDock = true
    @AppStorage(FlowPulseStyle.key) private var flowStyle: FlowPulseStyle = .comet
    @Environment(\.isSnapshot) private var isSnapshot

    /// The column's widest: rows past this get hard to read across.
    private static let column: CGFloat = 780

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            PageHeader(title: pane.title, subtitle: pane.subtitle)
            HStack(alignment: .top, spacing: Space.xl) {
                SettingsRail(store: store, engine: engine, selection: $pane).frame(width: 200)
                if isSnapshot {
                    content
                } else {
                    ScrollView { content.padding(.bottom, Space.xl) }.scrollIndicators(.automatic)
                }
            }
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: Space.xl) { sections }
            .frame(maxWidth: Self.column, alignment: .topLeading)
            .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    /// A check answers on this Mac: the local mode panes apply.
    private var localUsed: Bool {
        guard let g = engine.status?.guard else { return false }
        return [g.injection.mode, g.harm.mode].contains("local")
    }

    @ViewBuilder private var sections: some View {
        let s = store.snapshot
        switch pane {
        case .setup:
            SetupSection(engine: engine, part: .service)
            if engine.isBundled { SetupSection(engine: engine, part: .agents) }
        case .masking:
            MaskingSection(engine: engine, observing: s.observe == true, modelError: s.detection.modelError)
            ExclusionsSection(engine: engine)
        case .tools:
            ToolsSection(store: store, engine: engine)
            if engine.isBundled { ShellDestinationsSection(engine: engine) }
        case .checks:
            ChecksSection(engine: engine)
            if localUsed { LocalModeSummary(engine: engine) { pane = $0 } }
        case .extensions:
            ExtensionsSection(engine: engine)
        case .localModels:
            localOff
            LocalServersSection(engine: engine)
            LocalSpeedSection(engine: engine)
        case .handback:
            localOff
            LocalHandbackSection(engine: engine)
        case .skills:
            localOff
            LocalSkillsSection(engine: engine)
        case .general:
            display
            UpdatesSection()
            if engine.isBundled { LogsPageSection(engine: engine) }
        case .doctor:
            if engine.isBundled {
                DoctorSection(engine: engine)
            } else {
                FormSection(title: "Check this Mac") {
                    FormRow(title: "Not available", detail: "Needs the bundled engine") { EmptyView() }
                }
            }
            connection
        }
    }

    /// On a local mode pane while no check answers on this Mac: these settings wait for one.
    @ViewBuilder private var localOff: some View {
        if !localUsed {
            Banner(tone: .info, icon: "cpu", title: "Local mode is off.",
                   message: "These apply once Prompt injection or Harmful requests is set to Local.") {
                PillButton(title: "Open Checks") { pane = .checks }
            }
        }
    }

    private var display: some View {
        FormSection(title: "Display") {
            FormRow(title: "Appearance") {
                Segmented(options: Appearance.allCases.map { ($0, $0.title) }, selection: $appearance)
            }
            Hairline()
            FormRow(title: "Live flow", detail: "How each request moves across the dashboard's Live flow: a dot, or a dot with a tail") {
                Segmented(options: FlowPulseStyle.allCases.map { ($0, $0.title) }, selection: $flowStyle)
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
    }

    private var connection: some View {
        let s = store.snapshot
        return FormSection(title: "Connection",
                           footer: "This app reads counts and types from keepmum on 127.0.0.1, never a whole value, and sends no telemetry.",
                    details: "This app reads keepmum's stats feed: counts, types and timestamps, and for the Logs page the first and last character of long masked values (Logs page, above, turns that off). It never receives a whole value or a placeholder and talks only to 127.0.0.1, apart from update checks (Updates). It sends no telemetry.") {
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

/// The panes, each with what needs a look in it: setup not finished, tools to review, checks on,
/// an update, failed checks of this Mac.
private struct SettingsRail: View {
    var store: StatsStore
    var engine: Engine
    @Binding var selection: SettingsPane

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            ForEach(SettingsPane.Group.allCases, id: \.self) { group in
                if let title = group.title {
                    Text(title).eyebrow().padding(.horizontal, Space.s).padding(.top, Space.m).padding(.bottom, Space.xs)
                }
                ForEach(group.panes) { pane in
                    RailItem(pane: pane, badge: badge(pane), selection: $selection)
                }
            }
        }
    }

    private func badge(_ pane: SettingsPane) -> (String, Color)? {
        switch pane {
        case .setup:
            return engine.isBundled && !engine.setupDone ? ("To do", Theme.warning) : nil
        case .tools:
            let waiting = store.toolServers.filter { $0.new || $0.paused }
            return waiting.isEmpty ? nil : ("\(waiting.count)", waiting.contains { $0.paused } ? Theme.warning : Theme.accent)
        case .checks:
            guard let g = engine.status?.guard else { return nil }
            let on = [g.injection.mode, g.harm.mode, g.commands?.mode ?? "off"].filter { $0 != "off" }.count
            return on == 0 ? nil : ("\(on) on", Theme.textSecondary)
        case .extensions:
            let new = engine.status?.extensions?.new.count ?? 0
            return new == 0 ? nil : ("\(new)", Theme.warning)
        case .general:
            return Updates.shared.available == nil ? nil : ("Update", Theme.accent)
        case .doctor:
            let failed = engine.doctor?.filter { $0.status == "fail" }.count ?? 0
            return failed == 0 ? nil : ("\(failed)", Theme.blocked)
        case .masking:
            return store.snapshot.observe == true ? ("Off", Theme.blocked) : nil
        case .localModels:
            // A check answers locally with no server, or none of them ready.
            guard let g = engine.status?.guard, [g.injection.mode, g.harm.mode].contains("local") else { return nil }
            let servers = engine.localServers?.servers ?? []
            if (g.local?.servers ?? []).isEmpty { return ("To do", Theme.warning) }
            return !servers.isEmpty && servers.allSatisfy({ $0.problem != nil }) ? ("Down", Theme.warning) : nil
        case .handback:
            let waiting = ReviewCenter.shared.pending.count
            return waiting == 0 ? nil : ("\(waiting)", Theme.accent)
        case .skills:
            return nil
        }
    }
}

private struct RailItem: View {
    var pane: SettingsPane
    var badge: (String, Color)?
    @Binding var selection: SettingsPane
    @State private var hovering = false

    var body: some View {
        let on = selection == pane
        Button { selection = pane } label: {
            HStack(spacing: Space.s) {
                Image(systemName: pane.icon).font(.km(.body)).frame(width: 18)
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
                Text(pane.title).font(.km(.body).weight(on ? .medium : .regular)).foregroundStyle(Theme.text).lineLimit(1)
                Spacer(minLength: Space.xs)
                if let (text, color) = badge {
                    Text(text).font(.km(.caption).weight(.medium)).foregroundStyle(color)
                        .padding(.horizontal, Space.xs + 1).padding(.vertical, 1)
                        .background(Capsule().fill(color.opacity(0.14)))
                }
            }
            .padding(.horizontal, Space.s).padding(.vertical, Space.xs + 2)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                .fill(on ? Theme.fillStrong : hovering ? Theme.fill : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel(pane.title + (badge.map { ", \($0.0)" } ?? ""))
    }
}
