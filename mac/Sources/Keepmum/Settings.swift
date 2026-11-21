import SwiftUI

/// Settings, one pane at a time: a rail of panes in groups beside one column of sections, so each
/// pane holds a few related things and stays short enough to scan.
enum SettingsPane: String, CaseIterable, Identifiable {
    case setup, masking, tools, checks, extensions, localModels, handback, skills, general, doctor
    var id: String { rawValue }

    /// The page it is shown on: Check this Mac is a section of Settings, scrolled to.
    var shown: SettingsPane { self == .doctor ? .general : self }

    var title: String {
        switch self {
        case .setup: "Setup"
        case .masking: "Masking"
        case .tools: "Tools"
        case .checks: "Checks"
        case .extensions: "Extensions"
        case .localModels: "Local models"
        case .handback: "Local turns"
        case .skills: "Skills"
        case .general: "Settings"
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
        case .handback: "arrow.triangle.branch"
        case .skills: "text.book.closed"
        case .general: "gearshape"
        case .doctor: "stethoscope"
        }
    }

    var subtitle: String {
        switch self {
        case .setup: "The local model, the background service, and the coding agents keepmum protects"
        case .masking: "What is masked before a request leaves this Mac, what keepmum found, your own rules, and the projects left out"
        case .tools: "What each tool that reaches the network may receive, and where shell commands may send tokens"
        case .checks: "Optional checks on what tools return, what you type, and the commands in replies"
        case .extensions: "The plugins, hooks and MCP servers in your coding agents' settings, and what other apps added"
        case .localModels: "The models on this Mac that answer what a check keeps local, and how fast they answer"
        case .handback: "What the local model does with a turn a check keeps on this Mac, what it reads, and whether you look first"
        case .skills: "Your own guidance for the local model, written once and picked for each step in Local turns"
        case .general: "Appearance, opening at login, updates, what the Logs page shows, and checks of this Mac"
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
    @AppStorage(Palette.key) private var palette: Palette = .standard
    @Environment(\.isSnapshot) private var isSnapshot

    /// The column's widest: rows past this get hard to read across. Masking is wider, for the
    /// table of what keepmum found beside who found it.
    private var column: CGFloat { pane == .masking ? 1180 : 780 }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.l) {
            PageHeader(title: pane.shown.title, subtitle: pane.shown.subtitle)
            if isSnapshot {
                content
            } else {
                ScrollViewReader { proxy in
                    ScrollView { content.padding(.bottom, Space.xl) }
                        .scrollIndicators(.automatic)
                        .onChange(of: pane, initial: true) { _, pane in
                            if pane == .doctor { DispatchQueue.main.async { proxy.scrollTo(SettingsPane.doctor, anchor: .top) } }
                        }
                }
                .id(pane.shown)  // each pane opens at its top
            }
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: Space.xl) { sections }
            .frame(maxWidth: column, alignment: .topLeading)
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
            DetectionSections(store: store, engine: engine)
            ExclusionsSection(engine: engine)
        case .tools:
            ToolsSection(store: store, engine: engine)
            if engine.isBundled { ShellDestinationsSection(engine: engine) }
        case .checks:
            MapLink(engine: engine, text: "See where each check acts on a request, and where a hit goes, on the Protection map.")
            ChecksSection(engine: engine)
            if localUsed { LocalModeSummary(engine: engine) { pane = $0 } }
        case .extensions:
            ExtensionsSection(engine: engine)
        case .localModels:
            localOff
            MapLink(engine: engine, text: "See where Local mode sends a hit, and what goes back to the cloud, on the Protection map.")
            LocalServersSection(engine: engine)
            LocalSpeedSection(engine: engine)
        case .handback:
            localOff
            MapLink(engine: engine, text: "See a hand-back's route back to the cloud on the Protection map.")
            LocalHandbackSection(engine: engine) { pane = $0 }
        case .skills:
            localOff
            LocalSkillsSection(engine: engine)
        case .general, .doctor:
            display
            if engine.isBundled { StartupSection() }
            UpdatesSection()
            if engine.isBundled { LogsPageSection(engine: engine) }
            Group {
                if engine.isBundled {
                    DoctorSection(engine: engine)
                } else {
                    FormSection(title: "Check this Mac") {
                        FormRow(title: "Not available", detail: "Needs the bundled engine") { EmptyView() }
                    }
                }
            }
            .id(SettingsPane.doctor)
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
            FormRow(title: "Colors", detail: "Color-blind safe keeps blocked, restored, masked and warnings apart with red-green or blue-yellow color blindness") {
                Segmented(options: Palette.allCases.map { ($0, $0.title) }, selection: $palette)
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

/// What needs a look in each pane, beside it in the sidebar: setup not finished, tools to review,
/// checks on, an update, failed checks of this Mac.
extension SettingsPane {
    @MainActor func badge(store: StatsStore, engine: Engine) -> (String, Color)? {
        switch self {
        case .setup:
            return engine.isBundled && !engine.setupDone ? ("To do", Theme.warning) : nil
        case .tools:
            let waiting = store.toolServers.filter { $0.new || $0.paused }
            return waiting.isEmpty ? nil : ("\(waiting.count)", waiting.contains { $0.paused } ? Theme.warning : Theme.accent)
        case .checks:
            guard let g = engine.status?.guard else { return nil }
            // A check left on without its model checks nothing until it is downloaded.
            if g.injection.mode != "off" && !g.injection.installed { return (g.injection.outdated == true ? "Update" : "To do", Theme.warning) }
            if g.harm.mode != "off" && g.harm.installed[g.harm.model] != true { return ("To do", Theme.warning) }
            let on = [g.injection.mode, g.harm.mode, g.commands?.mode ?? "off"].filter { $0 != "off" }.count
            return on == 0 ? nil : ("\(on) on", Theme.textSecondary)
        case .extensions:
            let new = engine.status?.extensions?.new.count ?? 0
            return new == 0 ? nil : ("\(new)", Theme.warning)
        case .general, .doctor:
            // Settings holds Check this Mac: a failed check there comes first.
            let failed = engine.doctor?.filter { $0.status == "fail" }.count ?? 0
            if failed > 0 { return ("\(failed)", Theme.blocked) }
            return Updates.shared.available == nil ? nil : ("Update", Theme.accent)
        case .masking:
            return store.snapshot.observe == true ? ("Off", Theme.blocked) : nil
        case .localModels:
            // A check answers locally with no server, or none of them ready.
            guard let g = engine.status?.guard, [g.injection.mode, g.harm.mode].contains("local") else { return nil }
            let servers = engine.localServers?.servers ?? []
            if (g.local?.servers ?? []).isEmpty { return ("To do", Theme.warning) }
            return !servers.isEmpty && servers.allSatisfy({ $0.problem != nil }) ? ("Down", Theme.warning) : nil
        case .handback:
            let waiting = ReviewCenter.shared.pending.count + ReviewCenter.shared.turns.count
            return waiting == 0 ? nil : ("\(waiting)", Theme.accent)
        case .skills:
            return nil
        }
     }
}
