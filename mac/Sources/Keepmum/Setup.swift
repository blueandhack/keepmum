import SwiftUI

/// Three steps from a fresh install to every coding agent going through keepmum: download the
/// model, start the background service, then switch on each agent (the second section).
struct SetupSection: View {
    var engine: Engine
    /// Both parts, or one: Settings puts them side by side when the window is wide.
    enum Part { case all, service, agents }
    var part: Part = .all

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            if part != .agents { service }
            if part != .service { agents }
        }
    }

    @ViewBuilder private var service: some View {
            FormSection(title: "Setup", footer: engine.setupDone ? nil : "Three steps, once: the model, the background service, then your coding agents.") {
                if !engine.isBundled {
                    FormRow(title: "No bundled engine",
                            detail: "Build the app with mac/build-app.sh, or use the keepmum command line.") { EmptyView() }
                } else {
                    step(1, "Local model", done: engine.canScan, detail: modelDetail) {
                        if let p = engine.download {
                            ProgressView(value: p).frame(width: 120)
                            Text("\(Int(p * 100))%").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                        } else if !engine.canScan {
                            PillButton(title: "Download", icon: "arrow.down.circle", prominent: true) { Task { await engine.downloadModel() } }
                                .disabled(engine.busy)
                        }
                    }
                    Hairline()
                    step(2, "Background service", done: engine.serviceRunning, detail: serviceDetail) {
                        if let change = engine.serviceChange {
                            PillButton(title: change == .starting ? "Starting…" : "Stopping…", loading: true) {}
                        } else if engine.serviceStatus == .requiresApproval {
                            PillButton(title: "Open Login Items", icon: "gearshape") { engine.openLoginItems() }
                        } else if engine.serviceRunning {
                            PillButton(title: "Stop") { Task { await engine.stopService() } }
                                .disabled(engine.busy)
                        } else if engine.serviceStatus == .enabled {
                            PillButton(title: "Restart", icon: "arrow.clockwise") { Task { await engine.restartService() } }
                                .disabled(engine.busy)
                        } else {
                            PillButton(title: "Start", icon: "play.fill", prominent: engine.canScan) { Task { await engine.startService() } }
                                .disabled(!engine.canScan || engine.busy)
                        }
                    }
                }
                if let error = engine.error {
                    Hairline()
                    Text(error).font(.km(.callout)).foregroundStyle(Theme.blocked).fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, Space.s + Space.xxs)
                }
            }
    }

    @ViewBuilder private var agents: some View {
            if engine.isBundled {
                FormSection(title: "Coding agents",
                            footer: "Each switch points that agent at keepmum. An agent already set up for another gateway, such as Magpie or LiteLLM, keeps it: keepmum goes in front and forwards there, and turning the switch off puts it back. Claude Code's open sessions switch right away; the others on their next session. The Antigravity CLI and Gemini CLI are protected one run at a time, through keepmum agy and keepmum gemini. Gemini CLI takes a Google sign-in only with a Code Assist Standard or Enterprise licence since June 18, 2026; for a personal Google account, use the Antigravity CLI.") {
                    ClientRow(engine: engine, id: "claude", name: "Claude Code", icon: "CC", detail: "~/.claude/settings.json")
                    Hairline()
                    ClientRow(engine: engine, id: "codex", name: "Codex", icon: "Cx", detail: "~/.codex/config.toml")
                    Hairline()
                    ClientRow(engine: engine, id: "pi", name: "pi", icon: "π", detail: "~/.pi/agent/models.json")
                    Hairline()
                    ClientRow(engine: engine, id: "opencode", name: "opencode", icon: "oc", detail: "~/.config/opencode/opencode.json")
                    Hairline()
                    TerminalOnlyRow(name: "Antigravity CLI", icon: "A", command: "keepmum agy",
                                    why: "keepmum points the Antigravity CLI at itself through its environment (CLOUD_CODE_URL), one run at a time")
                    Hairline()
                    TerminalOnlyRow(name: "Gemini CLI", icon: "G", command: "keepmum gemini",
                                    why: "Gemini CLI has no setting keepmum can rely on: any project's .env file would override it. Since June 18, 2026 it takes an API key, or a Google sign-in with a Code Assist Standard or Enterprise licence")
                }
            }
    }

    private var modelDetail: String {
        if !engine.modelOn { return "Off: the rules work alone (Masking › Detector)" }
        return engine.hasModel ? "Privacy Filter, on this Mac" : "1.6 GB, downloaded once and checked against its SHA-256"
    }

    private var serviceDetail: String {
        let port = engine.status?.port ?? 7788
        switch engine.serviceChange {
        case .starting: return engine.modelOn && engine.hasModel ? "Loading the model; this takes a few seconds" : "Waiting for it to answer"
        case .stopping: return engine.anyProtected ? "Turning protection off first, so no agent loses its connection" : "Stopping…"
        case nil: break
        }
        switch engine.serviceStatus {
        case .requiresApproval: return "Allow \"keepmum\" in System Settings › Login Items"
        case .enabled where !engine.serviceRunning: return "Registered, but not answering on 127.0.0.1:\(port) yet"
        default: return engine.serviceRunning ? "Running, on 127.0.0.1:\(port)" : "Runs the proxy in the background, at login"
        }
    }

    private func step(_ n: Int, _ title: String, done: Bool, detail: String, @ViewBuilder trailing: () -> some View) -> some View {
        HStack(spacing: Space.m) {
            ZStack {
                Circle().fill(done ? Theme.soft(Theme.restored) : Theme.fill).frame(width: 24, height: 24)
                if done {
                    Image(systemName: "checkmark").font(.km(.caption).weight(.bold)).foregroundStyle(Theme.restored)
                } else {
                    Text("\(n)").font(.km(.callout).weight(.semibold)).foregroundStyle(Theme.textSecondary)
                }
            }
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(title).font(.km(.body)).foregroundStyle(Theme.text)
                Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(2)
            }
            Spacer(minLength: Space.s)
            trailing()
        }
        .padding(.vertical, Space.s + Space.xxs)
    }
}

/// A coding agent keepmum protects one run at a time, from a terminal: no switch, and why.
private struct TerminalOnlyRow: View {
    var name: String
    var icon: String
    var command: String
    var why: String

    var body: some View {
        HStack(spacing: Space.m) {
            Text(icon).font(.system(size: 13, weight: .bold)).foregroundStyle(Theme.textSecondary)
                .frame(width: 24, height: 24).background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(name).font(.km(.body)).foregroundStyle(Theme.text)
                (Text("Run ") + Text(command).font(.km(.monoSmall)) + Text(" in a terminal"))
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
            .help(why)
            Spacer(minLength: Space.s)
            Chip(text: "Terminal")
        }
        .padding(.vertical, Space.s)
    }
}

/// One coding agent: its switch, or why it has none.
private struct ClientRow: View {
    var engine: Engine
    var id: String
    var name: String
    var icon: String
    var detail: String

    var body: some View {
        let c = engine.client(id)
        let installed = c?.installed ?? false
        let on = c?.on == true
        HStack(spacing: Space.m) {
            Text(icon).font(.system(size: icon.count == 1 ? 13 : 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
                .frame(width: 24, height: 24).background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(name).font(.km(.body)).foregroundStyle(installed ? Theme.text : Theme.textSecondary)
                Text(subtitle(c, installed: installed, on: on)).font(.km(.caption))
                    .foregroundStyle(c?.error != nil || (on && c?.warning != nil) || c?.around != nil ? Theme.blocked : Theme.textSecondary)
                    .lineLimit(1).truncationMode(.middle).help(c?.warning ?? "")
            }
            Spacer(minLength: Space.s)
            if installed && c?.error == nil {
                let canTurnOn = engine.serviceRunning && engine.canScan
                Switch(isOn: on, enabled: !engine.busy && (on || canTurnOn)) { new in
                    Task { await engine.setProtection(new, client: id) }
                }
                .accessibilityLabel("Protect \(name)")
            }
        }
        .padding(.vertical, Space.s)
    }

    private func subtitle(_ c: Engine.Client?, installed: Bool, on: Bool) -> String {
        if !installed { return "Not installed" }
        if let error = c?.error { return error }
        if let around = c?.around { return "Goes to \(hostPort(around)) directly, around keepmum. Switch on again" }
        if !on, let other = c?.other { return "Uses \(hostPort(c?.otherUrl ?? other.text)); switch on to put keepmum in front" }
        if on, let warning = c?.warning { return warning }
        if on, let via = c?.via, let some = c?.viaProviders {
            return "Through keepmum; \(some.joined(separator: ", ")) then to \(hostPort(via))"
        }
        if on, let via = c?.via { return "Through keepmum to \(hostPort(via))" }
        return detail
    }
}

/// A gateway's address without its scheme: "127.0.0.1:3425/v1", "openrouter.ai/api".
func hostPort(_ url: String) -> String {
    url.split(separator: ",").map { part in
        let s = part.trimmingCharacters(in: .whitespaces)
        return s.replacingOccurrences(of: "https://", with: "").replacingOccurrences(of: "http://", with: "")
    }.joined(separator: ", ")
}

/// The menu bar's one-line answer to "is Claude Code protected right now?"
/// The background service in the menu bar panel: what it is doing, with Start, Stop or Restart, so
/// keepmum can be brought back without opening Settings. Stop turns protection off first, as it
/// does in Settings, so no agent is left pointed at a proxy that is gone.
struct ServiceRow: View {
    var engine: Engine

    var body: some View {
        HStack(spacing: Space.s + Space.xxs) {
            Group {
                if engine.serviceChange != nil {
                    Spinner(size: 12).foregroundStyle(Theme.textSecondary)
                } else {
                    Circle().fill(dot).frame(width: 8, height: 8)
                }
            }
            .frame(width: 14)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text("Background service").font(.km(.callout).weight(.medium)).foregroundStyle(Theme.text)
                Text(state).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Space.s)
            controls
        }
        .padding(.leading, Space.m).padding(.trailing, Space.s).padding(.vertical, Space.s)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
    }

    @ViewBuilder private var controls: some View {
        if engine.serviceChange != nil {
            EmptyView()  // nothing to do but wait
        } else if engine.serviceStatus == .requiresApproval {
            PillButton(title: "Allow…") { engine.openLoginItems() }
        } else if engine.serviceRunning {
            HStack(spacing: Space.xxs) {
                IconButton(symbol: "arrow.clockwise", help: "Restart the background service") {
                    Task { await engine.restartService() }
                }
                PillButton(title: "Stop") { Task { await engine.stopService() } }
                    .help("Turns protection off first, so no agent loses its connection")
            }
            .disabled(engine.busy)
        } else if engine.serviceStatus == .enabled {
            // Registered, but not answering: a restart is what brings it back.
            PillButton(title: "Restart", icon: "arrow.clockwise", prominent: true) { Task { await engine.restartService() } }
                .disabled(engine.busy)
        } else {
            PillButton(title: "Start", icon: "play.fill", prominent: engine.canScan) { Task { await engine.startService() } }
                .disabled(!engine.canScan || engine.busy)
        }
    }

    private var state: String {
        switch engine.serviceChange {
        case .starting: return engine.modelOn && engine.hasModel ? "Loading the model…" : "Starting…"
        case .stopping: return engine.anyProtected ? "Turning protection off, then stopping" : "Stopping…"
        case nil: break
        }
        switch engine.serviceStatus {
        case .requiresApproval: return "Allow it in Login Items"
        case .enabled where !engine.serviceRunning: return "Not answering"
        default:
            if engine.serviceRunning { return "Running, port \(engine.status?.port ?? 7788)" }
            return engine.canScan ? "Stopped" : "Needs the model: Settings › Setup"
        }
    }

    private var dot: Color {
        if engine.serviceRunning { return Theme.restored }
        if engine.serviceStatus == .enabled || engine.serviceStatus == .requiresApproval { return Theme.warning }
        return Theme.neutral
    }
}

struct ProtectionRow: View {
    var engine: Engine
    var openSetup: () -> Void

    var body: some View {
        // The service starting or stopping is the service row's to show, just above this one.
        HStack(spacing: Space.s + Space.xxs) {
            Image(systemName: engine.anyProtected ? (engine.serviceRunning ? "checkmark.shield.fill" : "exclamationmark.shield.fill") : "shield")
                .font(.km(.title).weight(.regular)).foregroundStyle(color)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(title).font(.km(.body).weight(.medium)).foregroundStyle(Theme.text)
                    .fixedSize(horizontal: false, vertical: true)
                Text(subtitle).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
            if engine.serviceChange != nil {
                EmptyView()  // nothing to do but wait
            } else if engine.canScan && engine.serviceRunning {
                PillButton(title: "Agents", action: openSetup)  // one switch per agent, in Settings
            } else if engine.isBundled {
                PillButton(title: "Set up", prominent: true, action: openSetup)
            }
        }
        .padding(.horizontal, Space.m).padding(.vertical, Space.s + Space.xxs)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.fill))
    }

    /// The coding agents protected right now, by name.
    private var protectedNames: [String] {
        engine.agentNames.compactMap { engine.client($0.0)?.on == true ? $0.1 : nil }
    }

    private var title: String {
        let names = protectedNames
        if names.isEmpty { return "No coding agent is protected" }
        if engine.serviceChange == .starting {
            return names.count == 1 ? "\(names[0]) is waiting" : "\(names.count) agents are waiting"
        }
        if !engine.serviceRunning { return "keepmum is not running" }
        return names.count == 1 ? "\(names[0]) is protected" : "\(names.count) agents are protected"
    }

    private var subtitle: String {
        let names = protectedNames
        if !names.isEmpty && engine.serviceChange == .starting { return "They connect as soon as keepmum answers" }
        if !names.isEmpty && !engine.serviceRunning { return "They cannot connect until keepmum restarts" }
        if names.count > 1 { return names.joined(separator: ", ") }
        if names.count == 1 { return "Every session goes through keepmum" }
        if !engine.isBundled { return "Use keepmum claude, codex, agy or gemini in a terminal" }
        return engine.canScan && engine.serviceRunning ? "Only agents started with keepmum claude, codex, agy or gemini are scanned" : "Finish setup to protect every session"
    }

    private var color: Color {
        // Starting is not a fault: the agents wait, and the service row shows the progress.
        if engine.anyProtected && engine.serviceChange == .starting { return Theme.textSecondary }
        if engine.anyProtected { return engine.serviceRunning ? Theme.restored : Theme.blocked }
        return Theme.textSecondary
    }
}
