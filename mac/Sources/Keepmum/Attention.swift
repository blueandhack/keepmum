import SwiftUI

// One answer to "am I protected, and does anything need me?", for every place that asks it: the
// Overview's status header, the menu bar panel, the sidebar and the command palette. Each used to
// work it out on its own, so the dashboard had no word of what the menu bar knew.

/// Something keepmum wants you to do, and the one action that does it.
struct AttentionItem: Identifiable {
    enum Tone { case danger, warning, info }
    var id: String
    var tone: Tone
    var icon: String
    var title: String
    var detail: String? = nil
    var button: String
    var action: AttentionAction

    var color: Color {
        switch tone {
        case .danger: Theme.blocked
        case .warning: Theme.warning
        case .info: Theme.accent
        }
    }
}

/// What a status or an item's button does. The menu bar and the dashboard both perform it: one that
/// shows a page opens the dashboard on it.
enum AttentionAction: Equatable {
    case settings(SettingsPane)
    case page(Page)
    case maskAgain
    case protect(String)
    case startService
    case restartService
    case allowLoginItem
    case review
    case update
    case scriptWarnings

    /// It shows a page of the dashboard.
    var opensDashboard: Bool {
        switch self {
        case .settings, .page, .scriptWarnings: true
        default: false
        }
    }

    @MainActor
    func perform(store: StatsStore, engine: Engine) {
        switch self {
        case .settings(let pane): engine.settingsRequested = pane
        case .page(let page): engine.pageRequested = page
        case .scriptWarnings:
            store.markScriptWarningsSeen()
            engine.pageRequested = .logs
        case .maskAgain: Task { await engine.setObserve(false) }
        case .protect(let id): Task { await engine.setProtection(true, client: id) }
        case .startService: Task { await engine.startService() }
        case .restartService: Task { await engine.restartService() }
        case .allowLoginItem: engine.openLoginItems()
        case .review: ReviewCenter.shared.reopen()
        case .update: Updates.shared.checkNow()
        }
    }
}

/// Where protection stands, in one sentence and the one thing to do about it.
struct ProtectionStatus {
    enum Tone { case protected, danger, warning, setup, busy }
    var tone: Tone
    var icon: String
    var title: String
    var detail: String
    /// Setup's progress: (step, of).
    var step: (Int, Int)? = nil
    var action: (String, AttentionAction)? = nil
    /// Values leave this Mac unmasked: drawn solid, as the observe banner is, never routine.
    var solid = false

    var color: Color {
        switch tone {
        case .protected: Theme.restored
        case .danger: Theme.blocked
        case .warning: Theme.warning
        case .setup: Theme.accent
        case .busy: Theme.textSecondary
        }
    }
}

@MainActor
enum Attention {
    /// The coding agents keepmum protects right now, by name.
    static func protectedNames(_ engine: Engine) -> [String] {
        engine.agentNames.compactMap { engine.client($0.0)?.on == true ? $0.1 : nil }
    }

    /// Installed, and not protected.
    static func unprotected(_ engine: Engine) -> [(id: String, name: String)] {
        engine.agentNames.compactMap { id, name in
            let c = engine.client(id)
            return c?.installed == true && c?.on != true ? (id, name) : nil
        }
    }

    static func status(store: StatsStore, engine: Engine) -> ProtectionStatus {
        let s = store.snapshot
        let offline: Bool = if case .offline = store.connection { true } else { false }
        if s.observe == true && !offline {
            return .init(tone: .danger, icon: "eye.trianglebadge.exclamationmark", title: "Masking is off",
                         detail: "Requests leave this Mac unmasked; keepmum only counts what it would mask",
                         action: ("Mask again", .maskAgain), solid: true)
        }
        // The command line, without the app's engine: all there is to go on is whether keepmum answers.
        guard engine.isBundled else {
            return offline
                ? .init(tone: .warning, icon: "bolt.horizontal.circle", title: "keepmum is not running",
                        detail: "Run keepmum claude, codex, agy or gemini in a terminal")
                : .init(tone: .protected, icon: "checkmark.shield.fill", title: "keepmum is running",
                        detail: "Agents started with keepmum claude, codex, agy or gemini are scanned")
        }
        let names = protectedNames(engine)
        switch engine.serviceChange {
        case .starting:
            return .init(tone: .busy, icon: "hourglass", title: "keepmum is starting",
                         detail: names.isEmpty ? "Loading the model; this takes a few seconds"
                                               : "\(list(names)) connect\(names.count == 1 ? "s" : "") as soon as it answers")
        case .restarting:
            return .init(tone: .busy, icon: "hourglass", title: "keepmum is restarting",
                         detail: names.isEmpty ? "Stopping, then starting again"
                                               : "\(list(names)) reconnect\(names.count == 1 ? "s" : "") as soon as it answers")
        case .stopping:
            return .init(tone: .busy, icon: "hourglass", title: "keepmum is stopping",
                         detail: names.isEmpty ? "The background service is shutting down" : "Turning protection off first")
        case nil: break
        }
        if !engine.canScan {
            return .init(tone: .setup, icon: "arrow.down.circle", title: "Finish setting up keepmum",
                         detail: "Download the local model that finds secrets in context (1.6 GB, once)",
                         step: (1, 3), action: ("Continue setup", .settings(.setup)))
        }
        if engine.serviceStatus == .requiresApproval {
            return .init(tone: .warning, icon: "lock.shield", title: "Allow keepmum in Login Items",
                         detail: "macOS asks before a background service can run", step: names.isEmpty ? (2, 3) : nil,
                         action: ("Allow…", .allowLoginItem))
        }
        if !engine.serviceRunning {
            let restart = engine.serviceStatus == .enabled
            if names.isEmpty {
                return .init(tone: .setup, icon: "power", title: "Finish setting up keepmum",
                             detail: "Start the background service that scans each request", step: (2, 3),
                             action: (restart ? "Restart" : "Start", restart ? .restartService : .startService))
            }
            return .init(tone: .danger, icon: "exclamationmark.shield.fill", title: "keepmum is not running",
                         detail: "\(list(names)) cannot reach \(names.count == 1 ? "its" : "their") provider until it starts",
                         action: (restart ? "Restart" : "Start", restart ? .restartService : .startService))
        }
        if offline {
            return .init(tone: .warning, icon: "bolt.horizontal.circle", title: "keepmum is not answering",
                         detail: "The background service runs, but its stats feed cannot be reached",
                         action: ("Restart", .restartService))
        }
        if names.isEmpty {
            return .init(tone: .setup, icon: "shield", title: "Protect your coding agents",
                         detail: "One switch each points Claude Code, Codex, pi or opencode at keepmum", step: (3, 3),
                         action: ("Choose agents", .settings(.setup)))
        }
        var detail = s.pii == true ? "Secrets and personal data are masked before a request leaves this Mac"
                                   : "Secrets are masked before a request leaves this Mac"
        let left = unprotected(engine).map(\.name)
        if !left.isEmpty { detail += " · \(list(left)) \(left.count == 1 ? "is" : "are") not protected" }
        return .init(tone: .protected, icon: "checkmark.shield.fill",
                     title: "\(list(names)) \(names.count == 1 ? "is" : "are") protected", detail: detail)
    }

    /// What needs you beyond the status itself, the most urgent first.
    static func items(store: StatsStore, engine: Engine) -> [AttentionItem] {
        var out: [AttentionItem] = []
        if let around = engine.aroundClient {
            out.append(.init(id: "around", tone: .danger, icon: "exclamationmark.shield", title: "\(around.name) skips keepmum",
                             detail: "It goes to \(hostPort(around.url)) directly, unscanned",
                             button: "Protect", action: .protect(around.id)))
        }
        for (id, name) in engine.agentNames {
            if let c = engine.client(id), c.on == true, c.around == nil, let warning = c.warning {
                out.append(.init(id: "partial-\(id)", tone: .warning, icon: "exclamationmark.shield",
                                 title: "Some of \(name)'s requests skip keepmum", detail: warning,
                                 button: "Setup", action: .settings(.setup)))
            }
        }
        let waiting = ReviewCenter.shared.pending.count
        let turns = ReviewCenter.shared.turns.count
        if waiting + turns > 0 {
            out.append(.init(id: "review", tone: .info, icon: "hand.raised.square",
                             title: turns > 0 && waiting == 0 ? (turns == 1 ? "A turn waits for your review" : "\(turns) turns wait for your review")
                                 : waiting + turns == 1 ? "A hand-back waits for you" : "\(waiting + turns) reviews wait for you",
                             button: "Review", action: .review))
        }
        if let warning = store.scriptWarning {
            // The server alone ("notes"): the tool is on the Logs page.
            let to = (warning.categories ?? []).first?.replacingOccurrences(of: "computed values sent to ", with: "")
                .components(separatedBy: " › ").first ?? "a network tool"
            out.append(.init(id: "script", tone: .warning, icon: "curlybraces", title: "Unchecked values to \(to)",
                             detail: "A Codex script passed values it worked out to a network tool",
                             button: "Logs", action: .scriptWarnings))
        }
        let servers = store.toolServers.filter { $0.new || $0.paused }
        if let first = servers.first(where: { $0.paused }) ?? servers.first {
            let others = servers.count - 1
            out.append(.init(id: "tools", tone: first.paused ? .warning : .info,
                             icon: first.paused ? "exclamationmark.shield" : "puzzlepiece.extension",
                             title: (first.paused ? "\(first.label): tools changed" : "New tool: \(first.label)") + (others > 0 ? " and \(others) more" : ""),
                             detail: first.paused ? "Paused until you trust it again" : "Choose what it may receive",
                             button: "Review", action: .settings(.tools)))
        }
        if let added = engine.status?.extensions?.new, let first = added.first {
            out.append(.init(id: "extensions", tone: .warning, icon: "puzzlepiece.extension",
                             title: added.count == 1 ? "\(first.agent) changed: \(first.name)" : "\(added.count) added to your agents",
                             detail: "Another app changed a coding agent's settings",
                             button: "Review", action: .settings(.extensions)))
        }
        if let g = engine.status?.guard {
            if g.injection.mode != "off" && !g.injection.installed {
                out.append(.init(id: "injection-model", tone: .warning, icon: "checkmark.shield",
                                 title: g.injection.outdated == true ? "The injection check's model needs an update" : "The injection check has no model",
                                 detail: "It checks nothing until it is downloaded", button: "Checks", action: .settings(.checks)))
            }
            if g.harm.mode != "off" && g.harm.installed[g.harm.model] != true {
                out.append(.init(id: "harm-model", tone: .warning, icon: "checkmark.shield", title: "The harm check has no model",
                                 detail: "It checks nothing until it is downloaded", button: "Checks", action: .settings(.checks)))
            }
            if [g.injection.mode, g.harm.mode].contains("local") && (g.local?.servers ?? []).isEmpty {
                out.append(.init(id: "local", tone: .warning, icon: "cpu", title: "Local mode has no model to answer",
                                 detail: "Add a server on this Mac in Local models", button: "Set up", action: .settings(.localModels)))
            }
        }
        let failed = engine.doctor?.filter { $0.status == "fail" }.count ?? 0
        if failed > 0 {
            out.append(.init(id: "doctor", tone: .danger, icon: "stethoscope",
                             title: failed == 1 ? "A check of this Mac failed" : "\(failed) checks of this Mac failed",
                             button: "Details", action: .settings(.doctor)))
        }
        if let version = Updates.shared.available {
            out.append(.init(id: "update", tone: .info, icon: "arrow.down.circle", title: "keepmum \(version) is available",
                             button: "Install…", action: .update))
        }
        return out
    }

    /// "Claude Code", "Claude Code and Codex", "Claude Code, Codex and pi".
    static func list(_ names: [String]) -> String {
        switch names.count {
        case 0: ""
        case 1: names[0]
        default: names.dropLast().joined(separator: ", ") + " and " + names[names.count - 1]
        }
    }
}

// MARK: - Views

/// What needs you, as rows on one surface: an icon in its tone, what it is, and its one action.
struct AttentionList: View {
    var items: [AttentionItem]
    /// The menu bar panel: no details, tighter rows.
    var compact = false
    /// At most this many; the rest are counted in a last line.
    var limit = Int.max
    var perform: (AttentionAction) -> Void

    var body: some View {
        let shown = Array(items.prefix(limit))
        VStack(spacing: 0) {
            ForEach(Array(shown.enumerated()), id: \.element.id) { i, item in
                if i > 0 { Hairline().padding(.leading, compact ? Space.xl + Space.xs : Space.xxl) }
                row(item)
            }
            if items.count > shown.count {
                Hairline()
                Text("\(items.count - shown.count) more on the dashboard's Overview")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, Space.s).padding(.leading, Space.m)
            }
        }
    }

    private func row(_ item: AttentionItem) -> some View {
        HStack(spacing: Space.s + Space.xxs) {
            Image(systemName: item.icon).font(.km(.callout).weight(.medium)).foregroundStyle(item.color)
                .frame(width: compact ? 16 : 20)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.title).font(.km(compact ? .callout : .body)).foregroundStyle(Theme.text)
                    .lineLimit(1).truncationMode(.tail)
                if !compact, let detail = item.detail {
                    Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1).truncationMode(.tail)
                }
            }
            .help(item.detail ?? "")
            Spacer(minLength: Space.s)
            PillButton(title: item.button) { perform(item.action) }
        }
        .padding(.leading, Space.m).padding(.trailing, Space.s)
        .padding(.vertical, compact ? Space.s - Space.xxs : Space.s)
        .accessibilityElement(children: .combine)
    }
}

/// The Overview's opening: where protection stands, in a sentence, then what needs you.
struct StatusHeader: View {
    var store: StatsStore
    var engine: Engine
    var perform: (AttentionAction) -> Void

    var body: some View {
        let status = Attention.status(store: store, engine: engine)
        let items = Attention.items(store: store, engine: engine)
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: Space.m + Space.xxs) {
                ZStack {
                    Circle().fill(status.solid ? .white.opacity(0.2) : Theme.soft(status.color)).frame(width: 44, height: 44)
                    if status.tone == .busy {
                        Spinner(size: 18).foregroundStyle(status.color)
                    } else {
                        Image(systemName: status.icon).font(.system(size: 20, weight: .medium))
                            .foregroundStyle(status.solid ? Theme.onAccent : status.color)
                    }
                }
                VStack(alignment: .leading, spacing: Space.xxs) {
                    HStack(spacing: Space.s) {
                        Text(status.title).font(.km(.title)).foregroundStyle(status.solid ? Theme.onAccent : Theme.text).lineLimit(1)
                        if let (step, of) = status.step {
                            Chip(text: "Step \(step) of \(of)", tone: Theme.accent)
                        }
                    }
                    Text(status.detail).font(.km(.callout)).foregroundStyle(status.solid ? Theme.onAccent.opacity(0.9) : Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: Space.m)
                if let (title, action) = status.action {
                    PillButton(title: title, icon: status.solid ? "eye.slash" : nil, prominent: !status.solid, onSolid: status.solid) { perform(action) }
                        .disabled(engine.busy && !action.opensDashboard)
                } else if status.tone == .protected && engine.isBundled {
                    PillButton(title: "Agents") { perform(.settings(.setup)) }
                }
            }
            .padding(.horizontal, Space.l).padding(.vertical, Space.m + Space.xxs)
            .background(status.solid ? Theme.blockedFill : .clear)
            // What needs you, as one row of chips that wraps: the flow below keeps its height.
            if !items.isEmpty {
                Hairline()
                HStack(alignment: .firstTextBaseline, spacing: Space.m) {
                    Text("Needs you").eyebrow().fixedSize()
                    ChipFlow(spacing: Space.s) {
                        ForEach(items) { AttentionChip(item: $0) { perform($0.action) } }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Space.l).padding(.vertical, Space.s + Space.xxs)
            }
        }
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(background))
        .clipShape(RoundedRectangle(cornerRadius: Radius.m, style: .continuous))
        .overlay {
            if status.tone == .danger && !status.solid {
                RoundedRectangle(cornerRadius: Radius.m, style: .continuous).strokeBorder(Theme.blocked.opacity(0.35))
            }
        }
        .animation(Motion.settle, value: status.title)
        .animation(Motion.settle, value: items.map(\.id))
    }

    private var background: Color {
        let status = Attention.status(store: store, engine: engine)
        return status.tone == .danger && !status.solid ? Theme.soft(Theme.blocked) : Theme.surface
    }
}

/// One thing that needs you, as a chip: its icon in its tone, what it is, and its action.
struct AttentionChip: View {
    var item: AttentionItem
    var action: (AttentionItem) -> Void
    @State private var hovering = false

    var body: some View {
        Button { action(item) } label: {
            HStack(spacing: Space.xs + Space.xxs) {
                Image(systemName: item.icon).font(.km(.caption).weight(.semibold)).foregroundStyle(item.color)
                Text(item.title).font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1)
                Text(item.button.replacingOccurrences(of: "…", with: "")).font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
                Image(systemName: "chevron.right").font(.system(size: 8, weight: .bold)).foregroundStyle(Theme.accent)
            }
            .padding(.horizontal, Space.s + Space.xxs).padding(.vertical, Space.xs + 1)
            .background(Capsule().fill(hovering ? Theme.fillStrong : Theme.fill))
            .overlay(Capsule().strokeBorder(item.tone == .danger ? Theme.blocked.opacity(0.4) : .clear))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(item.detail ?? item.title)
        .accessibilityLabel("\(item.title). \(item.button)")
    }
}
