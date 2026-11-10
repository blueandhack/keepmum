import SwiftUI

/// Settings › Extensions: the plugins, plugin marketplaces, hooks and MCP servers in each coding
/// agent's settings, with what changed since you last looked first. Any app can write these files:
/// in October 2026 the Postman app enabled its plugin in Claude Code and Codex, added a hook that
/// told every Codex session to load its skill, and trusted that hook for the user (extensions.py).
struct ExtensionsSection: View {
    var engine: Engine
    @State private var confirm: Engine.Extension?
    /// The action under way: "all", or an item's id. Its button spins.
    @State private var working: String?

    private static let order = ["Claude Code", "Codex", "Cursor", "Antigravity CLI", "Gemini CLI", "Copilot CLI", "opencode"]

    var body: some View {
        let list = engine.extensionList
        let items = list?.items ?? []
        let new = items.filter { $0.new == true }
        VStack(alignment: .leading, spacing: Space.xl) {
            FormSection(title: "New since you last looked",
                        footer: "Any app you run can change these settings. keepmum notices, and takes an entry out only when you click Remove: it edits that one entry, after a copy of the file in ~/.config/keepmum/backups, and the agent picks it up when it restarts.") {
                if let error = engine.error, working != nil || lastFailed {
                    Banner(tone: .danger, icon: "xmark.octagon", title: "Not done", message: error) {
                        PillButton(title: "Dismiss", onSolid: true) { lastFailed = false; engine.error = nil }
                    }
                    .padding(.vertical, Space.s + Space.xxs)
                    Hairline()
                }
                if new.isEmpty {
                    FormRow(title: list == nil ? "Reading your agents' settings…" : "Nothing new",
                            detail: list == nil ? nil : list?.watching == true
                                ? "Plugins, hooks and MCP servers that appear in your agents' settings show up here"
                                : "keepmum starts watching when it first runs") { EmptyView() }
                } else {
                    Banner(tone: .warning, icon: "puzzlepiece.extension",
                           title: new.count == 1 ? "Something was added" : "\(new.count) things were added",
                           message: "Keep what you added yourself; remove what you did not ask for.") {
                        PillButton(title: "Keep all", loading: working == "all") { act("all") { await engine.keepExtensions() } }
                            .disabled(busy)
                    }
                    .padding(.vertical, Space.s + Space.xxs)
                    ForEach(new) { item in
                        Hairline()
                        ExtensionRow(item: item, showAgent: true, busy: busy, working: working == item.id) {
                            act(item.id) { await engine.keepExtensions([item.id]) }
                        } remove: { confirm = item }
                    }
                }
            }
            // What was already there, by agent; new items are listed once, above.
            let known = items.filter { $0.new != true }
            ForEach(Self.order.filter { agent in known.contains { $0.agent == agent } }, id: \.self) { agent in
                let rows = known.filter { $0.agent == agent }.sorted { ($0.kind, $0.name) < ($1.kind, $1.name) }
                FormSection(title: agent) {
                    ForEach(Array(rows.enumerated()), id: \.element.id) { i, item in
                        ExtensionRow(item: item, showAgent: false, busy: busy, working: working == item.id) {
                            act(item.id) { await engine.keepExtensions([item.id]) }
                        } remove: { confirm = item }
                        if i < rows.count - 1 { Hairline() }
                    }
                }
            }
            if let list, !list.gone.isEmpty || !list.problems.isEmpty {
                FormSection(title: "Also") {
                    ForEach(Array(list.gone.enumerated()), id: \.offset) { _, g in
                        FormRow(title: "\(g.agent) \(g.kind) \(g.name)", detail: "Gone since you last looked") { EmptyView() }
                    }
                    ForEach(list.problems, id: \.self) { p in
                        FormRow(title: "Not read", detail: p, detailTone: Theme.warning) { EmptyView() }
                    }
                }
            }
        }
        .task { await engine.loadExtensions() }
        .alert(confirm.map { "Remove \($0.name) from \($0.agent)?" } ?? "", isPresented: Binding(
            get: { confirm != nil }, set: { if !$0 { confirm = nil } })) {
            Button("Remove", role: .destructive) {
                if let item = confirm { act(item.id) { await engine.removeExtension(item.id) } }
                confirm = nil
            }
            Button("Cancel", role: .cancel) { confirm = nil }
        } message: {
            if let item = confirm {
                Text("keepmum takes this \(item.kind) out of \(item.file), after a copy of the file in ~/.config/keepmum/backups. Restart \(item.agent) afterwards.")
            }
        }
    }

    @State private var lastFailed = false
    private var busy: Bool { !engine.isBundled || engine.busy }

    private func act(_ what: String, _ body: @escaping () async -> Void) {
        working = what
        Task {
            await body()
            lastFailed = engine.error != nil
            working = nil
        }
    }
}

private struct ExtensionRow: View {
    var item: Engine.Extension
    var showAgent: Bool
    var busy: Bool
    var working: Bool
    var keep: () -> Void
    var remove: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: Space.m) {
            Image(systemName: icon).font(.km(.body)).foregroundStyle(item.addsContext ? Theme.warning : Theme.textSecondary)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: Space.xxs) {
                HStack(spacing: Space.s) {
                    Text(item.name).font(.km(.body)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                    Chip(text: showAgent ? "\(item.agent) \(item.kind)" : item.kind)
                    if item.new == true { Chip(text: "New", tone: Theme.accent) }
                }
                if item.addsContext {
                    Text(item.kind == "instructions" ? "Every session reads it as instructions"
                         : item.kind == "trusted hook"
                         ? "Runs at every session start without asking, and its output reaches the model as instructions"
                         : "Runs at every session start or prompt, and its output reaches the model as instructions")
                        .font(.km(.caption)).foregroundStyle(Theme.warning).fixedSize(horizontal: false, vertical: true)
                }
                Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .lineLimit(2).truncationMode(.middle).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Space.m)
            HStack(spacing: Space.s) {
                if item.new == true {
                    PillButton(title: "Keep", loading: working, action: keep).disabled(busy)
                }
                if item.removable {
                    PillButton(title: "Remove…", destructive: true, action: remove).disabled(busy)
                }
            }
        }
        .padding(.vertical, Space.s + Space.xxs)
    }

    private var detail: String {
        let parts = [item.detail, item.removable ? item.file : "Remove with \(item.how)"].filter { !$0.isEmpty }
        return parts.joined(separator: " · ")
    }

    private var icon: String {
        switch item.kind {
        case "MCP server": "network"
        case "marketplace": "storefront"
        case "hook", "trusted hook", "status line": "bolt"
        case "skill": "book.closed"
        case "instructions": "text.alignleft"
        default: "puzzlepiece.extension"
        }
    }
}
