import SwiftUI

/// Settings, one pane at a time: a list of panes beside the content. A pane with two groups of
/// sections lays them side by side when the window is wide enough, and one under the other when not.
enum SettingsPane: String, CaseIterable, Identifiable {
    case setup, masking, tools, checks, extensions, general, doctor
    var id: String { rawValue }

    var title: String {
        switch self {
        case .setup: "Setup"
        case .masking: "Masking"
        case .tools: "Tools"
        case .checks: "Checks"
        case .extensions: "Extensions"
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
        case .general: "slider.horizontal.3"
        case .doctor: "stethoscope"
        }
    }

    var subtitle: String {
        switch self {
        case .setup: "The local model, the background service, and the coding agents keepmum protects"
        case .masking: "What is masked before a request leaves this Mac, and the projects left out"
        case .tools: "What each tool that reaches the network may receive, and where shell commands may send tokens"
        case .checks: "Optional checks on what tools return, what you type, and the commands in replies, and the local models that answer what they flag"
        case .extensions: "The plugins, hooks and MCP servers in your coding agents' settings, and what other apps added"
        case .general: "Appearance, updates, the Logs page, and this app's connection to keepmum"
        case .doctor: "The same checks as keepmum doctor: this Mac, the model, keepmum's files"
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

    /// A column's width when two fit side by side; under two of these, the pane is one column.
    private static let column: CGFloat = 540

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            PageHeader(title: "Settings", subtitle: pane.subtitle)
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
        let (left, right) = columns
        return ViewThatFits(in: .horizontal) {
            if let right {
                HStack(alignment: .top, spacing: Space.xl) {
                    left.frame(minWidth: Self.column, idealWidth: Self.column, maxWidth: 720, alignment: .topLeading)
                    right.frame(minWidth: Self.column, idealWidth: Self.column, maxWidth: 720, alignment: .topLeading)
                }
            }
            VStack(alignment: .leading, spacing: Space.xl) {
                left
                if let right { right }
            }
            .frame(maxWidth: 760, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    /// Each pane's sections: the left column, and the right one if it has two groups.
    private var columns: (AnyView, AnyView?) {
        let s = store.snapshot
        switch pane {
        case .setup:
            return (AnyView(SetupSection(engine: engine, part: .service)),
                    engine.isBundled ? AnyView(SetupSection(engine: engine, part: .agents)) : nil)
        case .masking:
            return (AnyView(MaskingSection(engine: engine, observing: s.observe == true, modelError: s.detection.modelError)),
                    AnyView(ExclusionsSection(engine: engine)))
        case .tools:
            return (AnyView(ToolsSection(store: store, engine: engine)),
                    engine.isBundled ? AnyView(ShellDestinationsSection(engine: engine)) : nil)
        case .checks:
            return (AnyView(ChecksSection(engine: engine)),
                    engine.status?.guard?.local != nil ? AnyView(VStack(alignment: .leading, spacing: Space.xl) {
                        LocalServersSection(engine: engine)
                        LocalHandbackSection(engine: engine)
                        LocalSkillsSection(engine: engine)
                    }) : nil)
        case .extensions:
            return (AnyView(ExtensionsSection(engine: engine)), nil)
        case .general:
            return (AnyView(VStack(alignment: .leading, spacing: Space.xl) { display; UpdatesSection() }),
                    AnyView(VStack(alignment: .leading, spacing: Space.xl) {
                        if engine.isBundled { LogsPageSection(engine: engine) }
                        connection
                    }))
        case .doctor:
            return (engine.isBundled ? AnyView(DoctorSection(engine: engine))
                                     : AnyView(FormSection(title: "Check this Mac") {
                                           FormRow(title: "Not available", detail: "Needs the bundled engine") { EmptyView() }
                                       }), nil)
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
                           footer: "This app reads keepmum's stats feed: counts, types and timestamps, and for the Logs page the first and last character of long masked values (Logs page, above, turns that off). It never receives a whole value or a placeholder and talks only to 127.0.0.1, apart from update checks (Updates). It sends no telemetry.") {
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
            ForEach(SettingsPane.allCases) { pane in
                RailItem(pane: pane, badge: badge(pane), selection: $selection)
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
