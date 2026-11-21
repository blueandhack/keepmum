import SwiftUI

// ⌘K: every page, every Settings pane, what needs you, and the everyday actions, one search away.
// The dashboard holds seven pages and ten Settings panes; this finds any of them by what it is
// for ("exceptions", "latency", "Gmail") rather than by where it lives.
//
// It raises protection directly (Protect an agent, Mask again) and never lowers it: anything that
// turns protection down opens the pane where that setting, and what it asks first, live.

struct PaletteCommand: Identifiable {
    enum Group: String, CaseIterable { case needsYou = "Needs you", actions = "Actions", pages = "Go to" }
    var id: String
    var group: Group
    var title: String
    var subtitle: String? = nil
    var icon: String
    var tint: Color = Theme.textSecondary
    var keywords = ""
    var shortcut: String? = nil
    var run: () -> Void

    /// 0 the title starts with the query, 1 every word is in the title, 2 in its keywords or
    /// subtitle; nil, not a match.
    func rank(_ query: String) -> Int? {
        let q = query.lowercased().trimmingCharacters(in: .whitespaces)
        if q.isEmpty { return 1 }
        let words = q.split(separator: " ").map(String.init)
        let t = title.lowercased()
        let hay = t + " " + keywords.lowercased() + " " + (subtitle ?? "").lowercased() + " " + group.rawValue.lowercased()
        guard words.allSatisfy(hay.contains) else { return nil }
        if t.hasPrefix(q) || t.split(separator: " ").contains(where: { $0.hasPrefix(q) }) { return 0 }
        return words.allSatisfy(t.contains) ? 1 : 2
    }
}

@MainActor
enum PaletteCommands {
    static func all(store: StatsStore, engine: Engine, appearance: Binding<Appearance>) -> [PaletteCommand] {
        var out: [PaletteCommand] = []
        for item in Attention.items(store: store, engine: engine) {
            out.append(.init(id: "attention-\(item.id)", group: .needsYou, title: item.title, subtitle: item.detail,
                             icon: item.icon, tint: item.color) { item.action.perform(store: store, engine: engine) })
        }
        if let (title, action) = Attention.status(store: store, engine: engine).action {
            out.append(.init(id: "status", group: .actions, title: title, subtitle: Attention.status(store: store, engine: engine).title,
                             icon: "bolt.fill", tint: Theme.accent, keywords: "setup protect start") { action.perform(store: store, engine: engine) })
        }
        if engine.isBundled && engine.serviceRunning {
            for (id, name) in Attention.unprotected(engine) {
                out.append(.init(id: "protect-\(id)", group: .actions, title: "Protect \(name)", subtitle: "Point \(name) at keepmum",
                                 icon: "checkmark.shield", tint: Theme.restored, keywords: "agent switch on") {
                    Task { await engine.setProtection(true, client: id) }
                })
            }
        }
        if engine.isBundled && engine.serviceRunning && engine.serviceChange == nil {
            out.append(.init(id: "restart", group: .actions, title: "Restart the background service", icon: "arrow.clockwise",
                             keywords: "service reload proxy") { Task { await engine.restartService() } })
        }
        out.append(.init(id: "rule", group: .actions, title: "Always mask a pattern…", subtitle: "Your rules, on Masking",
                         icon: "plus.circle", keywords: "add rule custom regex never mask exception allow") { engine.settingsRequested = .masking })
        out.append(.init(id: "allow", group: .actions, title: "Never mask a value…", subtitle: "Exceptions, on Masking",
                         icon: "minus.circle", keywords: "exception allow false positive whitelist") { engine.settingsRequested = .masking })
        out.append(.init(id: "exclude", group: .actions, title: "Leave a project out…", subtitle: "Excluded projects, on Masking",
                         icon: "folder.badge.minus", keywords: "exclude exclusions folder skip directory") { engine.settingsRequested = .masking })
        out.append(.init(id: "updates", group: .actions, title: "Check for updates", icon: "arrow.down.circle",
                         keywords: "version upgrade sparkle") { Updates.shared.checkNow() })
        out.append(.init(id: "preview", group: .actions, title: store.sampleMode ? "Exit preview data" : "Show preview data",
                         subtitle: "Synthetic numbers on every page", icon: "sparkles", keywords: "sample demo") { store.sampleMode.toggle() })
        for a in Appearance.allCases where a != appearance.wrappedValue {
            out.append(.init(id: "appearance-\(a.rawValue)", group: .actions, title: "Appearance: \(a.title)",
                             icon: a == .dark ? "moon" : a == .light ? "sun.max" : "circle.lefthalf.filled",
                             keywords: "theme mode dark light") { appearance.wrappedValue = a })
        }
        // Every place in the sidebar, in its order, and Check this Mac, a section of Settings.
        for place in Place.all + [.pane(.doctor)] {
            let shortcut = place.shortcut.map { "⌘\($0)" } ?? (place == .pane(.general) ? "⌘," : nil)
            switch place {
            case .page(let page):
                out.append(.init(id: place.id, group: .pages, title: page.title, icon: page.icon,
                                 keywords: page.keywords, shortcut: shortcut) { engine.pageRequested = page })
            case .pane(let pane):
                out.append(.init(id: place.id, group: .pages, title: pane.title, subtitle: pane.subtitle,
                                 icon: pane.icon, keywords: pane.keywords, shortcut: shortcut) { engine.settingsRequested = pane })
            }
        }
        return out
    }

    static func matches(_ commands: [PaletteCommand], _ query: String) -> [PaletteCommand] {
        let ranked = commands.enumerated().compactMap { i, c in c.rank(query).map { (c, $0, i) } }
        // With no query, in the order given (by group); with one, the best matches first.
        if query.trimmingCharacters(in: .whitespaces).isEmpty { return ranked.map(\.0) }
        return ranked.sorted { ($0.1, $0.2) < ($1.1, $1.2) }.map(\.0)
    }
}

extension Page {
    /// What each page is for, in the words someone might search with.
    var keywords: String {
        switch self {
        case .overview: "home status dashboard live flow protected"
        case .activity: "requests history outcomes masked blocked flagged"
        case .logs: "findings values checks flagged search injection harm"
        case .performance: "latency speed scan time budget"
        case .map: "checks diagram modes route"
        case .audit: "changes log export verify integrity"
        case .settings: "preferences options"
        }
    }
}

struct CommandPalette: View {
    var store: StatsStore
    var engine: Engine
    @Binding var appearance: Appearance
    var close: () -> Void
    /// For snapshots: the query typed.
    var initialQuery = ""
    @State private var query = ""
    @State private var selected = 0
    @FocusState private var focused: Bool
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let results = PaletteCommands.matches(PaletteCommands.all(store: store, engine: engine, appearance: $appearance), query)
        VStack(spacing: 0) {
            HStack(spacing: Space.s) {
                Image(systemName: "magnifyingglass").font(.km(.headline)).foregroundStyle(Theme.textSecondary)
                if isSnapshot {
                    Text(query.isEmpty ? "Search pages, settings and actions" : query)
                        .font(.system(size: 17)).foregroundStyle(query.isEmpty ? Theme.textTertiary : Theme.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    TextField("Search pages, settings and actions", text: $query)
                        .textFieldStyle(.plain).font(.system(size: 17)).foregroundStyle(Theme.text)
                        .focused($focused)
                        .onSubmit { run(results) }
                        .onKeyPress(.downArrow) { move(1, results.count); return .handled }
                        .onKeyPress(.upArrow) { move(-1, results.count); return .handled }
                        .onExitCommand(perform: close)
                }
                Text("esc").font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, Space.xs + 1).padding(.vertical, 1)
                    .background(RoundedRectangle(cornerRadius: Radius.s - Space.xxs).fill(Theme.fill))
            }
            .padding(.horizontal, Space.l).padding(.vertical, Space.m + Space.xxs)
            Hairline()
            if results.isEmpty {
                Text("Nothing matches \u{201C}\(query)\u{201D}").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                    .frame(maxWidth: .infinity).padding(.vertical, Space.xl)
            } else if isSnapshot {
                list(Array(results.prefix(11)))
            } else {
                ScrollViewReader { proxy in
                    ScrollView { list(results) }
                        .frame(maxHeight: 420)
                        .onChange(of: selected) { _, i in
                            if results.indices.contains(i) { proxy.scrollTo(results[i].id) }
                        }
                }
            }
            Hairline()
            HStack(spacing: Space.l) {
                hint("↑↓", "to move")
                hint("↩", "to open")
                hint("⌘1–9", "for a page")
                Spacer()
            }
            .padding(.horizontal, Space.l).padding(.vertical, Space.s)
        }
        .frame(width: 600)
        .background(RoundedRectangle(cornerRadius: Radius.l, style: .continuous).fill(Theme.canvas))
        .overlay(RoundedRectangle(cornerRadius: Radius.l, style: .continuous).strokeBorder(Theme.separator))
        .clipShape(RoundedRectangle(cornerRadius: Radius.l, style: .continuous))
        .shadow(color: .black.opacity(0.22), radius: 30, y: 12)
        .onAppear { query = initialQuery; focused = true }
        .onChange(of: query) { selected = 0 }
    }

    private func list(_ results: [PaletteCommand]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(results.enumerated()), id: \.element.id) { i, c in
                if query.trimmingCharacters(in: .whitespaces).isEmpty && (i == 0 || results[i - 1].group != c.group) {
                    Text(c.group.rawValue).eyebrow()
                        .padding(.horizontal, Space.l).padding(.top, i == 0 ? Space.s : Space.m).padding(.bottom, Space.xs)
                }
                PaletteRow(command: c, selected: i == selected)
                    .id(c.id)
                    .onTapGesture { selected = i; run(results) }
                    .onHover { if $0 { selected = i } }
            }
        }
        .padding(.horizontal, Space.s).padding(.vertical, Space.xs)
    }

    private func hint(_ key: String, _ text: String) -> some View {
        HStack(spacing: Space.xs) {
            Text(key).font(.km(.caption).weight(.semibold)).foregroundStyle(Theme.text)
            Text(text).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
        }
    }

    private func move(_ by: Int, _ count: Int) {
        guard count > 0 else { return }
        selected = (selected + by + count) % count
    }

    private func run(_ results: [PaletteCommand]) {
        guard results.indices.contains(selected) else { return }
        let command = results[selected]
        close()
        command.run()
    }
}

private struct PaletteRow: View {
    var command: PaletteCommand
    var selected: Bool

    var body: some View {
        HStack(spacing: Space.m) {
            Image(systemName: command.icon).font(.km(.body)).foregroundStyle(command.tint).frame(width: 20)
            VStack(alignment: .leading, spacing: 1) {
                Text(command.title).font(.km(.body)).foregroundStyle(Theme.text).lineLimit(1)
                if let subtitle = command.subtitle {
                    Text(subtitle).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1).truncationMode(.tail)
                }
            }
            Spacer(minLength: Space.s)
            if let shortcut = command.shortcut {
                Text(shortcut).font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
            } else if selected {
                Image(systemName: "return").font(.km(.caption).weight(.semibold)).foregroundStyle(Theme.textSecondary)
            }
        }
        .padding(.horizontal, Space.s).padding(.vertical, Space.s - Space.xxs)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(selected ? Theme.accentSoft : .clear))
        .contentShape(Rectangle())
    }
}

extension SettingsPane {
    /// What each pane holds, in the words someone might search with.
    var keywords: String {
        switch self {
        case .setup: "agents model service start stop protect claude codex pi opencode"
        case .masking: "observe pii personal data names detector detection found types rules exceptions patterns always never exclusions exclude projects"
        case .tools: "mcp servers web search fetch gmail trust destinations shell gitlab"
        case .checks: "injection harm jailbreak risky commands guard warn block local sensitivity"
        case .extensions: "plugins hooks mcp skills marketplaces instructions added"
        case .localModels: "ollama lm studio llama mlx splash server speed"
        case .handback: "hand-back review summary rewrite turn"
        case .skills: "guidance instructions"
        case .general: "preferences options general appearance dark light dock colors color-blind updates login startup logs previews"
        case .doctor: "doctor diagnose health connection version"
        }
    }
}
