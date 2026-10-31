import SwiftUI

/// Three steps from a fresh install to every Claude Code session going through keepmum:
/// download the model, start the background service, point Claude Code at it.
struct SetupCard: View {
    var engine: Engine

    var body: some View {
        Card(title: "Setup", subtitle: engine.setupDone ? "Your coding agents go through keepmum" : "Three steps, once") {
            if !engine.isBundled {
                Text("This build has no bundled engine. Build the app with mac/build-app.sh, or use the keepmum command line.")
                    .font(.system(size: 12)).foregroundStyle(Theme.secondary).fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(spacing: 0) {
                    step(1, "Local model", done: engine.canScan, detail: modelDetail) {
                        if let p = engine.download {
                            ProgressView(value: p).frame(width: 120)
                            Text("\(Int(p * 100))%").font(.system(size: 11.5)).monospacedDigit().foregroundStyle(Theme.secondary)
                        } else if !engine.canScan {
                            PillButton(title: "Download", icon: "arrow.down.circle", prominent: true) { Task { await engine.downloadModel() } }
                        }
                    }
                    step(2, "Background service", done: engine.serviceRunning, detail: serviceDetail) {
                        if engine.serviceStatus == .requiresApproval {
                            PillButton(title: "Open Login Items", icon: "gearshape") { engine.openLoginItems() }
                        } else if engine.serviceRunning {
                            PillButton(title: "Stop") { Task { await engine.stopService() } }
                        } else {
                            PillButton(title: "Start", icon: "play.fill", prominent: engine.canScan) { Task { await engine.startService() } }
                                .disabled(!engine.canScan).opacity(engine.canScan ? 1 : 0.5)
                        }
                    }
                    step(3, "Protect your coding agents", done: engine.anyProtected,
                         detail: "Each one sends its requests through keepmum") { EmptyView() }
                    VStack(spacing: 0) {
                        ClientRow(engine: engine, id: "claude", name: "Claude Code", icon: "CC",
                                  detail: "~/.claude/settings.json", note: "open sessions switch now")
                        ClientRow(engine: engine, id: "codex", name: "Codex", icon: "Cx",
                                  detail: "~/.codex/config.toml", note: "new sessions")
                        ClientRow(engine: engine, id: "pi", name: "pi", icon: "π",
                                  detail: "~/.pi/agent/models.json", note: "new sessions")
                        ClientRow(engine: engine, id: "opencode", name: "opencode", icon: "oc",
                                  detail: "~/.config/opencode/opencode.json", note: "new sessions")
                    }
                    .padding(.leading, 38)
                }
                if let error = engine.error {
                    Text(error).font(.system(size: 11.5)).foregroundStyle(Theme.danger).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var modelDetail: String {
        if !engine.modelOn { return "Off: the rules work alone (Settings › Detector)" }
        return engine.hasModel ? "Privacy Filter, on this Mac" : "1.6 GB, downloaded once and checked against its SHA-256"
    }

    private var serviceDetail: String {
        switch engine.serviceStatus {
        case .requiresApproval: "Allow \"keepmum\" in System Settings › Login Items"
        case .enabled where !engine.serviceRunning: "Starting…"
        default: engine.serviceRunning ? "Running at login, on 127.0.0.1:\(engine.status?.port ?? 7788)" : "Runs the proxy in the background, at login"
        }
    }

    private func step(_ n: Int, _ title: String, done: Bool, detail: String, @ViewBuilder trailing: () -> some View) -> some View {
        HStack(spacing: 12) {
            ZStack {
                Circle().fill(done ? Theme.success.opacity(0.14) : Theme.faint).frame(width: 26, height: 26)
                if done {
                    Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(Theme.success)
                } else {
                    Text("\(n)").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.secondary)
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.text)
                Text(detail).font(.system(size: 11.5)).foregroundStyle(Theme.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
            trailing()
        }
        .padding(.vertical, 9)
    }
}

/// One coding agent under step 3: its switch, or why it has none.
private struct ClientRow: View {
    var engine: Engine
    var id: String
    var name: String
    var icon: String
    var detail: String
    var note: String

    var body: some View {
        let c = engine.client(id)
        let installed = c?.installed ?? false
        let on = c?.on == true
        HStack(spacing: 10) {
            Text(icon).font(.system(size: icon.count == 1 ? 13 : 10, weight: .bold)).foregroundStyle(Theme.secondary)
                .frame(width: 26, height: 26).background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Theme.faint))
            VStack(alignment: .leading, spacing: 1) {
                Text(name).font(.system(size: 12.5, weight: .medium)).foregroundStyle(installed ? Theme.text : Theme.secondary)
                Text(subtitle(c, installed: installed, on: on)).font(.system(size: 11))
                    .foregroundStyle(c?.error != nil || (on && c?.warning != nil) ? Theme.danger : Theme.secondary)
                    .lineLimit(1).truncationMode(.middle).help(c?.warning ?? "")
            }
            Spacer(minLength: 8)
            if installed && c?.error == nil {
                let canTurnOn = engine.serviceRunning && engine.canScan
                Switch(isOn: on, enabled: !engine.busy && (on || canTurnOn)) { new in
                    Task { await engine.setProtection(new, client: id) }
                }
                .accessibilityLabel("Protect \(name)")
            }
        }
        .padding(.vertical, 6)
    }

    private func subtitle(_ c: Engine.Client?, installed: Bool, on: Bool) -> String {
        if !installed { return "Not installed" }
        if let error = c?.error { return error }
        if !on, let other = c?.other { return "Already uses \(other.text); keepmum leaves it alone" }
        if on, let warning = c?.warning { return warning }
        return on ? "\(detail) · \(note)" : detail
    }
}

/// A switch drawn in SwiftUI, so it looks the same in both appearances and in snapshots.
struct Switch: View {
    var isOn: Bool
    var enabled = true
    var action: (Bool) -> Void

    var body: some View {
        Button { action(!isOn) } label: {
            Capsule()
                .fill(isOn ? AnyShapeStyle(Theme.success) : AnyShapeStyle(Theme.faint))
                .overlay(Capsule().strokeBorder(isOn ? Color.clear : Theme.border))
                .frame(width: 38, height: 22)
                .overlay(alignment: isOn ? .trailing : .leading) {
                    Circle().fill(.white).shadow(color: .black.opacity(0.18), radius: 1.5, y: 1).padding(2.5)
                }
                .animation(.easeOut(duration: 0.15), value: isOn)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .accessibilityValue(isOn ? "on" : "off")
    }
}

/// The menu bar's one-line answer to "is Claude Code protected right now?"
struct ProtectionRow: View {
    var engine: Engine
    var openSetup: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: engine.anyProtected ? (engine.serviceRunning ? "checkmark.shield.fill" : "exclamationmark.shield.fill") : "shield")
                .font(.system(size: 16)).foregroundStyle(color)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 12.5, weight: .medium)).foregroundStyle(Theme.text)
                Text(subtitle).font(.system(size: 11)).foregroundStyle(Theme.secondary)
            }
            Spacer()
            if engine.canScan && engine.serviceRunning {
                PillButton(title: "Agents", action: openSetup)  // one switch per agent, in Settings
            } else if engine.isBundled {
                PillButton(title: "Set up", prominent: true, action: openSetup)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.faint))
    }

    /// The coding agents protected right now, by name.
    private var protectedNames: [String] {
        [("claude", "Claude Code"), ("codex", "Codex"), ("pi", "pi"), ("opencode", "opencode")].compactMap { engine.client($0.0)?.on == true ? $0.1 : nil }
    }

    private var title: String {
        let names = protectedNames
        if names.isEmpty { return "No coding agent is protected" }
        if !engine.serviceRunning { return "keepmum is not running" }
        return names.count == 1 ? "\(names[0]) is protected" : "\(names.count) agents are protected"
    }

    private var subtitle: String {
        let names = protectedNames
        if !names.isEmpty && !engine.serviceRunning { return "They cannot connect until keepmum restarts" }
        if names.count > 1 { return names.joined(separator: ", ") }
        if names.count == 1 { return "Every session goes through keepmum" }
        if !engine.isBundled { return "Use keepmum claude or keepmum codex in a terminal" }
        return engine.canScan && engine.serviceRunning ? "Only keepmum claude and keepmum codex are scanned" : "Finish setup to protect every session"
    }

    private var color: Color {
        if engine.anyProtected { return engine.serviceRunning ? Theme.success : Theme.danger }
        return Theme.secondary
    }
}
