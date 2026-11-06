import SwiftUI

/// Settings › Tools: the MCP servers and web tools agents offered, and the real values each may
/// receive (`keepmum tools`). Secrets never go to them, whatever the setting.
struct ToolsSection: View {
    var store: StatsStore
    var engine: Engine
    @State private var confirm: Pending?
    /// The action under way: "reviewed", or a server being trusted again. Its button spins.
    @State private var working: String?

    /// A change to a public tool, waiting for the user to confirm it.
    struct Pending: Identifiable {
        var server: ToolServer
        var setting: String?
        var types: [String] = []
        var id: String { server.server }
    }

    var body: some View {
        let servers = store.toolServers
        FormSection(title: "Tools that reach the network",
                    footer: "Each MCP server gets back the values it returned itself, so it can open what it found. Secrets are never sent, nor more than 5 values in one call, nor anything while a server's tools differ from the ones you trusted.") {
            if !engine.isBundled {
                Banner(tone: .warning, icon: "exclamationmark.triangle", title: "No engine",
                       message: "This copy of Keepmum was built without its engine, so it can show these settings but not change them. Use a full build (build-app.sh without SKIP_RUNTIME), or keepmum tools in a terminal.") {
                    EmptyView()
                }
                .padding(.vertical, Space.s + Space.xxs)
                Hairline()
            }
            if let error = engine.error, working == nil, lastAction != nil {
                Banner(tone: .danger, icon: "xmark.octagon", title: "Not done", message: error) {
                    PillButton(title: "Dismiss", onSolid: true) { lastAction = nil }
                }
                .padding(.vertical, Space.s + Space.xxs)
                Hairline()
            }
            if servers.contains(where: \.new) {
                Banner(tone: .info, icon: "sparkles", title: "New tools",
                       message: "They start on “Values it gave you”. Change any, or keep them as they are.") {
                    PillButton(title: "Keep these", loading: working == "reviewed") {
                        run("reviewed") { await engine.toolsReviewed() }
                    }
                    .disabled(!engine.isBundled || engine.busy)
                }
                .padding(.vertical, Space.s + Space.xxs)
                Hairline()
            }
            if servers.isEmpty {
                FormRow(title: "None yet", detail: "MCP servers and web tools show up after an agent's first request through keepmum") { EmptyView() }
            }
            ForEach(Array(servers.enumerated()), id: \.element.id) { i, server in
                ToolRow(server: server, engine: engine, busy: !engine.isBundled || engine.busy,
                        trusting: working == server.server) { setting, types in
                    if server.public && lets(server, setting: setting, types: types) {
                        confirm = Pending(server: server, setting: setting, types: types)
                    } else {
                        apply(server, setting: setting, types: types, confirmed: false)
                    }
                } trust: {
                    run(server.server) { await engine.trustTool(server.server) }
                }
                if i < servers.count - 1 { Hairline() }
            }
        }
        .alert("Send real values to \(confirm?.server.label ?? "this tool")?",
               isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }), presenting: confirm) { p in
            Button("Keep placeholders", role: .cancel) {}
            Button("Send them", role: .destructive) { apply(p.server, setting: p.setting, types: p.types, confirmed: true) }
        } message: { _ in
            Text("A web tool passes what it gets to a search engine or any web page, in public. Secrets still never go, and the audit log records the change.")
        }
    }

    @State private var lastAction: String?

    /// Run an action with its button spinning, then reload the list; an error stays on screen.
    private func run(_ what: String, _ action: @escaping () async -> Void) {
        working = what
        lastAction = what
        Task {
            await action()
            await store.loadTools()
            working = nil
            if engine.error == nil { lastAction = nil }
        }
    }

    /// Whether a change gives a public tool more than placeholders.
    private func lets(_ server: ToolServer, setting: String?, types: [String]) -> Bool {
        if !types.isEmpty { return true }
        return setting != nil && setting != "placeholders" && setting != "default"
    }

    private func apply(_ server: ToolServer, setting: String?, types: [String], confirmed: Bool) {
        Task {
            if let setting {
                await engine.setTool(server.server, setting, confirmed: confirmed)
            } else {
                await engine.allowTool(server.server, types, confirmed: confirmed)
            }
            await store.loadTools()
        }
    }
}

private struct ToolRow: View {
    var server: ToolServer
    var engine: Engine
    var busy: Bool
    /// Trust again is under way for this server.
    var trusting = false
    /// A setting (placeholders, own, contacts, default), or nil and types to add.
    var change: (String?, [String]) -> Void
    var trust: () -> Void
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .center, spacing: Space.m) {
                Image(systemName: server.public ? "globe" : "puzzlepiece.extension")
                    .font(.km(.body)).foregroundStyle(Theme.textSecondary).frame(width: 24)
                VStack(alignment: .leading, spacing: Space.xxs) {
                    HStack(spacing: Space.s) {
                        Text(server.label).font(.km(.body)).foregroundStyle(Theme.text)
                        if server.new { Chip(text: "New", tone: Theme.accent) }
                        if server.paused { Chip(text: "Paused", tone: Theme.warning) }
                        if server.public { Chip(text: "Public") }
                    }
                    Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: Space.m)
                picker.disabled(busy || server.setElsewhere || server.setting == "restore")
            }
            if server.paused {
                Banner(tone: .warning, icon: "exclamationmark.shield",
                       title: "Its tools changed",
                       message: "\(server.changed.map(short).joined(separator: ", ")) no longer \(server.changed.count == 1 ? "matches" : "match") what you trusted, so \(server.label) gets placeholders only.") {
                    PillButton(title: "Trust again", loading: trusting, action: trust).disabled(busy)
                }
            } else if let (kinds, calls) = suggestion {
                HStack(spacing: Space.s) {
                    Image(systemName: "lightbulb").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    Text("\(calls) \(calls == 1 ? "call" : "calls") this week got a placeholder for \(kinds.map { Theme.label($0).lowercased() }.joined(separator: ", "))")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    Spacer(minLength: Space.s)
                    PillButton(title: "Allow \(kinds.map { Theme.label($0).lowercased() }.joined(separator: ", "))") { change(nil, kinds) }
                        .disabled(busy || server.setElsewhere)
                }
                .padding(.leading, 24 + Space.m)
            }
        }
        .padding(.vertical, Space.s + Space.xxs)
    }

    private var detail: String {
        var parts = [server.agents.map(agentName).joined(separator: ", ")]
        if server.setting == "restore" { parts.append("every value, secrets included") }
        if server.setElsewhere { parts.append("set by restore.\(server.rule) in the config file") }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Types kept this week that the setting does not let through yet, and the busiest type's count.
    private var suggestion: ([String], Int)? {
        guard server.setting != "restore" && server.setting != "placeholders" else { return nil }
        let have = server.setting == "contacts" ? ["PERSON", "EMAIL", "PHONE", "ADDRESS"] : server.types
        let kinds = server.keptWeek.filter { !have.contains($0.key) && $0.value > 0 }
        guard let most = kinds.values.max() else { return nil }
        return (Theme.typeOrder.filter { kinds[$0] != nil }, most)
    }

    private var title: String {
        switch server.setting {
        case "placeholders": "Placeholders only"
        case "own": "Values it gave you"
        case "contacts": "Also contact details"
        case "restore": "Every value"
        default: "Also " + server.types.map { Theme.label($0).lowercased() }.joined(separator: ", ")
        }
    }

    @ViewBuilder private var picker: some View {
        if isSnapshot {
            FieldStandIn(text: title + "  ⌄").frame(width: 190)
        } else {
            Menu {
                option("Placeholders only", "placeholders", "Never a real value")
                option("Values it gave you", "own", "What it returned earlier in the conversation")
                option("Also contact details", "contacts", "Names, emails, phones and addresses too")
                if !server.default && server.ownRule {
                    Divider()
                    Button("Back to keepmum's default") { change("default", []) }
                }
            } label: {
                Text(title)
            }
            .menuStyle(.button)
            .fixedSize()
        }
    }

    private func option(_ name: String, _ setting: String, _ help: String) -> some View {
        Button {
            change(setting, [])
        } label: {
            if server.setting == setting {
                Label(name, systemImage: "checkmark")
            } else {
                Text(name)
            }
        }
        .help(help)
    }

    private func short(_ tool: String) -> String {
        tool.hasPrefix(server.server + "__") ? String(tool.dropFirst(server.server.count + 2)) : tool
    }
}

func agentName(_ client: String) -> String {
    switch client {
    case "claude-code": "Claude Code"
    case "codex": "Codex"
    case "gemini": "Gemini CLI"
    case "opencode": "opencode"
    default: client
    }
}
