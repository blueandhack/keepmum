import SwiftUI

/// Extensions: the plugins, plugin marketplaces, hooks, MCP servers, skills and global
/// instructions in each coding agent's settings. Any app can write these files: in October 2026 the
/// Postman app enabled its plugin in Claude Code and Codex, added a hook that told every Codex session
/// to load its skill, and trusted that hook for the user (extensions.py).
///
/// One section per agent, its items grouped by kind, each saying what it is: in its author's words
/// where its manifest has some, otherwise in keepmum's (when a hook runs, what a server is). A search
/// and chips for agents, kinds, what is new and what reaches the model narrow a long list (fifty
/// items is an ordinary Mac) to the one you are after, or several.
struct ExtensionsSection: View {
    var engine: Engine
    /// For snapshots: a search typed, chips picked.
    var initialQuery = ""
    var initialKinds: Set<ExtensionKind> = []
    @State private var confirm: Engine.Extension?
    /// The action under way: "all", or an item's id. Its button spins.
    @State private var working: String?
    @State private var query = ""
    @State private var agents: Set<String> = []
    @State private var kinds: Set<ExtensionKind> = []
    @State private var onlyNew = false
    @State private var onlyContext = false
    /// Groups opened past their first few rows: "agent/kind".
    @State private var expanded: Set<String> = []
    @State private var lastFailed = false
    @Environment(\.isSnapshot) private var isSnapshot

    static let order = ["Claude Code", "Codex", "Cursor", "Antigravity CLI", "Gemini CLI", "Copilot CLI", "opencode"]
    /// Rows a group shows before "Show all".
    private static let preview = 4

    var body: some View {
        let list = engine.extensionList
        let items = list?.items ?? []
        let new = items.filter { $0.new == true }
        let shown = items.filter(matches)
        VStack(alignment: .leading, spacing: Space.xl) {
            if let error = engine.error, working != nil || lastFailed {
                Banner(tone: .danger, icon: "xmark.octagon", title: "Not done", message: error) {
                    PillButton(title: "Dismiss", onSolid: true) { lastFailed = false; engine.error = nil }
                }
            }
            if !new.isEmpty {
                Banner(tone: .warning, icon: "puzzlepiece.extension",
                       title: new.count == 1 ? "Something was added." : "\(new.count) things were added.",
                       message: "Keep what you added yourself; remove what you did not ask for.") {
                    HStack(spacing: Space.s) {
                        if !onlyNew { PillButton(title: "Show only these") { onlyNew = true } }
                        PillButton(title: "Keep all", loading: working == "all") { act("all") { await engine.keepExtensions() } }
                            .disabled(busy)
                    }
                }
            }
            if list == nil {
                FormSection(title: "Extensions") {
                    FormRow(title: "Reading your agents' settings…") { Spinner() }
                }
            } else if items.isEmpty {
                FormSection(title: "Extensions") {
                    FormRow(title: "Nothing added to your agents",
                            detail: list?.watching == true ? "Plugins, hooks, MCP servers and skills show up here as they appear"
                                                           : "keepmum starts watching when it first runs") { EmptyView() }
                }
            } else {
                filters(items, new: new.count)
                if shown.isEmpty {
                    VStack(alignment: .leading, spacing: Space.s) {
                        Text("Nothing matches").font(.km(.headline)).foregroundStyle(Theme.text)
                        Button("Show everything") { clearFilters() }
                            .buttonStyle(.plain).font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
                    }
                    .padding(.horizontal, Space.xs)
                }
                ForEach(Self.order.filter { agent in shown.contains { $0.agent == agent } }, id: \.self) { agent in
                    agentSection(agent, rows: shown.filter { $0.agent == agent }, total: items.filter { $0.agent == agent }.count)
                }
                if !filtering {
                    let none = Self.order.filter { agent in !items.contains { $0.agent == agent } }
                    if !none.isEmpty {
                        Text("Nothing in \(Attention.list(none)).").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, Space.xs).padding(.top, -Space.m)
                    }
                }
            }
            if let list, !list.gone.isEmpty || !list.problems.isEmpty {
                FormSection(title: "Also") {
                    ForEach(Array(list.gone.enumerated()), id: \.offset) { i, g in
                        if i > 0 { Hairline() }
                        FormRow(title: "\(g.agent) \(g.kind) \(g.name)", detail: "Gone since you last looked") { EmptyView() }
                    }
                    ForEach(Array(list.problems.enumerated()), id: \.offset) { i, p in
                        if i > 0 || !list.gone.isEmpty { Hairline() }
                        FormRow(title: "Not read", detail: p, detailTone: Theme.warning) { EmptyView() }
                    }
                }
            }
            Text("Any app you run can change these settings. keepmum notices, and takes an entry out only when you click Remove: it edits that one entry, after a copy of the file in ~/.config/keepmum/backups, and the agent picks it up when it restarts. A description in quotes is its author's own, from its manifest.")
                .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, Space.xs)
        }
        .task { await engine.loadExtensions() }
        .onAppear {
            if query.isEmpty { query = initialQuery }
            if kinds.isEmpty { kinds = initialKinds }
        }
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

    // MARK: Finding

    private var filtering: Bool { !query.isEmpty || !agents.isEmpty || !kinds.isEmpty || onlyNew || onlyContext }

    private func clearFilters() {
        query = ""; agents = []; kinds = []; onlyNew = false; onlyContext = false
    }

    private func matches(_ item: Engine.Extension) -> Bool {
        if !agents.isEmpty && !agents.contains(item.agent) { return false }
        if !kinds.isEmpty && !kinds.contains(ExtensionKind(item.kind)) { return false }
        if onlyNew && item.new != true { return false }
        if onlyContext && !item.addsContext { return false }
        let words = query.lowercased().split(separator: " ")
        guard !words.isEmpty else { return true }
        let hay = [item.name, item.kind, item.agent, item.detail, item.file, item.description ?? "", ExtensionAbout.text(item)]
            .joined(separator: " ").lowercased()
        return words.allSatisfy { hay.contains($0) }
    }

    /// A search, then one row of chips for what needs a look and the kinds, one for the agents.
    /// Chips in a row add up: Claude Code and Codex, plugins and hooks.
    private func filters(_ items: [Engine.Extension], new: Int) -> some View {
        let context = items.filter(\.addsContext).count
        let kindCounts = Dictionary(grouping: items, by: { ExtensionKind($0.kind) }).mapValues(\.count)
        let agentCounts = Dictionary(grouping: items, by: \.agent).mapValues(\.count)
        return VStack(alignment: .leading, spacing: Space.s) {
            HStack(spacing: Space.s) {
                search
                if filtering {
                    Button("Clear") { clearFilters() }
                        .buttonStyle(.plain).font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
                }
                Spacer(minLength: 0)
                Text(filtering ? "\(items.filter(matches).count) of \(items.count)" : "\(items.count) in all")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).monospacedDigit()
            }
            ChipFlow(spacing: Space.xs + Space.xxs) {
                if new > 0 {
                    FilterChip(title: "New", count: new, icon: "sparkle", tint: Theme.accent, on: onlyNew) { onlyNew.toggle() }
                }
                if context > 0 {
                    FilterChip(title: "Reaches the model", count: context, icon: "text.bubble", tint: Theme.warning, on: onlyContext) {
                        onlyContext.toggle()
                    }
                    .help("Runs at every session start or prompt, or is read as instructions: what it says reaches the model")
                }
                ForEach(ExtensionKind.allCases.filter { kindCounts[$0] != nil }) { kind in
                    FilterChip(title: kind.plural, count: kindCounts[kind] ?? 0, icon: kind.icon, on: kinds.contains(kind)) {
                        kinds.formSymmetricDifference([kind])
                    }
                }
            }
            if agentCounts.count > 1 {
                ChipFlow(spacing: Space.xs + Space.xxs) {
                    ForEach(Self.order.filter { agentCounts[$0] != nil }, id: \.self) { agent in
                        FilterChip(title: agent, count: agentCounts[agent] ?? 0, on: agents.contains(agent)) {
                            agents.formSymmetricDifference([agent])
                        }
                    }
                }
            }
        }
    }

    private var search: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Image(systemName: "magnifyingglass").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            if isSnapshot {
                Text(query.isEmpty ? "Search names, descriptions, commands" : query).font(.km(.callout))
                    .foregroundStyle(query.isEmpty ? Theme.textTertiary : Theme.text)
            } else {
                TextField("Search", text: $query, prompt: Text("Search names, descriptions, commands"))
                    .textFieldStyle(.plain).font(.km(.callout))
            }
            Spacer(minLength: 0)
            if !query.isEmpty && !isSnapshot {
                Button { query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textTertiary) }
                    .buttonStyle(.plain).accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, Space.s).frame(minWidth: 200, idealWidth: 320, maxWidth: 320).frame(height: 26)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
    }

    // MARK: An agent's section

    private func agentSection(_ agent: String, rows: [Engine.Extension], total: Int) -> some View {
        let groups = ExtensionKind.allCases.compactMap { kind -> (ExtensionKind, [Engine.Extension])? in
            let r = rows.filter { ExtensionKind($0.kind) == kind }
                .sorted { ($0.new == true ? 0 : 1, $0.name.lowercased()) < ($1.new == true ? 0 : 1, $1.name.lowercased()) }
            return r.isEmpty ? nil : (kind, r)
        }
        let newCount = rows.filter { $0.new == true }.count
        return VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(agent).font(.km(.headline)).foregroundStyle(Theme.text)
                Text(rows.count == total ? "\(total)" : "\(rows.count) of \(total)")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).monospacedDigit()
                if newCount > 0 { Chip(text: "\(newCount) new", tone: Theme.accent) }
                Spacer(minLength: 0)
                if !agents.contains(agent) && Set(engine.extensionList?.items.map(\.agent) ?? []).count > 1 {
                    Button("Only \(agent)") { agents = [agent] }
                        .buttonStyle(.plain).font(.km(.caption).weight(.medium)).foregroundStyle(Theme.accent)
                }
            }
            .padding(.horizontal, Space.xs)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(groups.enumerated()), id: \.element.0) { gi, group in
                    let (kind, items) = group
                    let key = "\(agent)/\(kind.rawValue)"
                    // Searching or filtering shows every match; otherwise a long group shows its first few.
                    let open = filtering || expanded.contains(key) || items.count <= Self.preview + 1
                    let visible = open ? items : Array(items.prefix(Self.preview))
                    if gi > 0 { Hairline() }
                    HStack(spacing: Space.xs) {
                        Text(kind.plural).eyebrow()
                        Text("\(items.count)").font(.km(.label)).foregroundStyle(Theme.textTertiary)
                    }
                    .padding(.top, Space.m).padding(.bottom, Space.xxs)
                    ForEach(Array(visible.enumerated()), id: \.element.id) { i, item in
                        if i > 0 { Hairline().padding(.leading, Space.xxl + Space.xs) }
                        ExtensionRow(item: item, busy: busy, working: working == item.id) {
                            act(item.id) { await engine.keepExtensions([item.id]) }
                        } remove: { confirm = item }
                    }
                    if items.count > visible.count || (expanded.contains(key) && !filtering && items.count > Self.preview + 1) {
                        Button(open ? "Show fewer" : "Show all \(items.count) \(kind.plural.lowercased())") {
                            withAnimation(Motion.quick) { expanded.formSymmetricDifference([key]) }
                        }
                        .buttonStyle(.plain).font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
                        .padding(.leading, Space.xxl + Space.xs).padding(.bottom, Space.s + Space.xxs).padding(.top, Space.xxs)
                    }
                }
            }
            .padding(.horizontal, Space.m)
            .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
        }
    }

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

/// The kinds of thing an agent's settings hold, as the page groups them: a trusted hook and a status
/// line are hooks too.
enum ExtensionKind: String, CaseIterable, Identifiable {
    case plugin, extensionKind = "extension", hook, server = "MCP server", skill, instructions, marketplace
    var id: String { rawValue }

    init(_ kind: String) {
        switch kind {
        case "trusted hook", "status line", "hook": self = .hook
        default: self = ExtensionKind(rawValue: kind) ?? .plugin
        }
    }

    var plural: String {
        switch self {
        case .plugin: "Plugins"
        case .extensionKind: "Extensions"
        case .hook: "Hooks"
        case .server: "MCP servers"
        case .skill: "Skills"
        case .instructions: "Instructions"
        case .marketplace: "Marketplaces"
        }
    }

    var icon: String {
        switch self {
        case .plugin, .extensionKind: "puzzlepiece.extension"
        case .hook: "bolt"
        case .server: "network"
        case .skill: "book.closed"
        case .instructions: "text.alignleft"
        case .marketplace: "storefront"
        }
    }
}

/// What an item is and when it acts, in keepmum's words: for what has no manifest to say so (a hook,
/// a server), and beside an author's description for what does.
enum ExtensionAbout {
    static func text(_ item: Engine.Extension) -> String {
        let agent = item.agent
        switch item.kind {
        case "hook":
            // The Antigravity CLI names a group of hooks; its events are in the detail ("PreInvocation: cmd; …").
            let events = item.agent == "Antigravity CLI"
                ? item.detail.components(separatedBy: "; ").compactMap { $0.components(separatedBy: ":").first }
                : [item.name]
            let whens = events.map(when)
            return whens.isEmpty ? "Runs a command on one of \(agent)'s events" : whens.joined(separator: "; ").sentence
        case "trusted hook":
            // "postman@postman:hooks/codex-hooks.json:session_start:0:0": its plugin, file and event.
            let bits = item.detail.components(separatedBy: ":")
            let event = bits.count > 2 ? when(bits[2]) : "runs on one of Codex's events"
            return "A hook from the \(item.name) plugin, trusted so Codex runs it without asking; it \(event)"
        case "status line": return "Draws \(agent)'s status line from a command's output"
        case "MCP server":
            if let host = URL(string: item.detail)?.host, item.detail.hasPrefix("http") {
                return "Gives \(agent) tools from a server on the internet, at \(host)"
            }
            return "Gives \(agent) tools from a program on this Mac"
        case "marketplace": return "A catalogue \(agent) can install plugins from"
        case "plugin": return "Adds skills, commands, hooks or tools to \(agent)"
        case "extension": return "Adds commands, tools or context to \(agent)"
        case "skill": return "Guidance \(agent) loads when a task matches it; its description is in every session"
        case "instructions": return "Read as instructions at the start of every \(agent) session"
        default: return ""
        }
    }

    /// When a hook event fires, as "runs when …".
    static func when(_ event: String) -> String {
        switch event {
        case "SessionStart", "session_start", "sessionStart": "runs when a session starts"
        case "SessionEnd", "session_end": "runs when a session ends"
        case "UserPromptSubmit", "user_prompt_submit", "beforeSubmitPrompt": "runs each time you send a prompt"
        case "PreToolUse", "pre_tool_use": "runs before each tool call, and can stop it"
        case "PostToolUse", "post_tool_use": "runs after each tool call"
        case "PostToolUseFailure": "runs after a tool call fails"
        case "PermissionRequest": "runs when the agent asks your permission to use a tool, and can answer for you"
        case "SubagentStart": "runs when a subagent starts"
        case "beforeShellExecution": "runs before each shell command, and can stop it"
        case "beforeMCPExecution": "runs before each MCP tool call, and can stop it"
        case "afterFileEdit": "runs after each file edit"
        case "Stop", "stop": "runs when the agent finishes a reply"
        case "SubagentStop": "runs when a subagent finishes"
        case "Notification": "runs when the agent asks for your attention"
        case "PreCompact": "runs before the conversation is compacted"
        case "PreInvocation": "runs before each model call"
        case "PostInvocation": "runs after each model call"
        case "notify": "runs when Codex finishes a turn"
        default: "runs on \(event)"
        }
    }
}

private struct ExtensionRow: View {
    var item: Engine.Extension
    var busy: Bool
    var working: Bool
    var keep: () -> Void
    var remove: () -> Void

    var body: some View {
        let author = item.description.flatMap { $0.isEmpty ? nil : $0 }
        HStack(alignment: .top, spacing: Space.m) {
            Image(systemName: ExtensionKind(item.kind).icon).font(.km(.body))
                .foregroundStyle(item.addsContext ? Theme.warning : Theme.textSecondary)
                .frame(width: 24).padding(.top, 1)
            VStack(alignment: .leading, spacing: Space.xxs + 1) {
                HStack(spacing: Space.s) {
                    Text(item.name).font(.km(.body).weight(.medium)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                    if item.kind == "trusted hook" { Chip(text: "Trusted") }
                    if item.kind == "status line" { Chip(text: "Status line") }
                    if item.new == true { Chip(text: "New", tone: Theme.accent) }
                }
                // Its author's words where it has some, in quotes: a claim, not keepmum's.
                if let author {
                    Text("\u{201C}\(author)\u{201D}").font(.km(.callout)).foregroundStyle(Theme.text.opacity(0.85))
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .help("Its author's description, from its manifest. \(ExtensionAbout.text(item)).")
                } else {
                    Text(ExtensionAbout.text(item)).font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                if item.addsContext {
                    Label(item.kind == "instructions" ? "Every session reads it as instructions"
                          : item.kind == "trusted hook" ? "Runs without asking, and its output reaches the model as instructions"
                          : "Its output reaches the model as instructions, at every session start or prompt",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.km(.caption)).foregroundStyle(Theme.warning).fixedSize(horizontal: false, vertical: true)
                }
                if !location.isEmpty {
                    Text(location).font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                        .help(location)
                }
            }
            Spacer(minLength: Space.m)
            HStack(spacing: Space.s) {
                if item.new == true {
                    PillButton(title: "Keep", loading: working, action: keep).disabled(busy)
                }
                if item.removable {
                    PillButton(title: "Remove…", destructive: true, action: remove).disabled(busy)
                } else if !item.how.isEmpty {
                    Image(systemName: "info.circle").font(.km(.callout)).foregroundStyle(Theme.textTertiary)
                        .help("keepmum does not edit this file. To remove it: \(item.how)")
                        .accessibilityLabel("To remove it: \(item.how)")
                }
            }
            .padding(.top, -Space.xxs)
        }
        .padding(.vertical, Space.s + Space.xxs)
    }

    /// What it runs or where it connects, then its file: the detail a search finds it by. A
    /// trusted hook's detail is its id, which the line above already says in words.
    private var location: String {
        let detail = item.kind == "trusted hook" ? "" : item.detail
        return [detail, item.file].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// A chip that filters: a count beside its title, filled while on.
struct FilterChip: View {
    var title: String
    var count: Int
    var icon: String? = nil
    var tint: Color = Theme.textSecondary
    var on: Bool
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.xs + 1) {
                if let icon {
                    Image(systemName: icon).font(.km(.caption).weight(.semibold)).foregroundStyle(on ? Theme.accent : tint)
                }
                Text(title).font(.km(.callout).weight(on ? .semibold : .regular)).foregroundStyle(on ? Theme.accent : Theme.text)
                Text("\(count)").font(.km(.caption).weight(.medium)).monospacedDigit()
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
            }
            .padding(.horizontal, Space.s + Space.xxs).padding(.vertical, Space.xs)
            .background(Capsule().fill(on ? Theme.accentSoft : hovering ? Theme.fillStrong : Theme.fill))
            .overlay(Capsule().strokeBorder(on ? Theme.accent.opacity(0.45) : .clear))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel("\(title), \(count)")
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}
