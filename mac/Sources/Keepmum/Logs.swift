import SwiftUI

/// One value keepmum masked in new text (`GET /_keepmum/findings`). Never the value: its type, the
/// rule that found it, its length, for values of 8 or more characters the first and last one, and
/// the text around it with every masked value shown as its type (`‹SECRET›`).
struct Finding: Decodable, Sendable, Identifiable {
    var ts: Double
    var seq: Int
    var type: String
    /// rules, model, user (your own rule) or known (masked earlier in the conversation).
    var detector: String
    var rule: String
    var chars: Int
    /// `g…w`, `•••` for short values, nil with [log] previews = false.
    var preview: String?
    /// masked, or observed (observe mode: sent unmasked).
    var outcome: String
    var client: String?
    var provider: String?
    /// The text before and after the value, nil with [log] context = false.
    var before: String?
    var after: String?
    /// Every detector that found it, yours first: `yours:<name>`, `rule:<id>`, `model`. For a value
    /// seen earlier in the conversation (`earlier`), also who found it then.
    var by: [String]?
    var earlier: Bool?

    enum Finder: Hashable {
        case yours(String), rule(String), model
    }

    /// Who found it. A keepmum from before Nov 1, 2026 sent only `detector` and `rule`.
    var finders: [Finder] {
        if let by {
            return by.compactMap { x in
                if x == "model" { return .model }
                if x.hasPrefix("rule:") { return .rule(String(x.dropFirst(5))) }
                if x.hasPrefix("yours:") { return .yours(String(x.dropFirst(6))) }
                return nil
            }
        }
        switch detector {
        case "rules": return [.rule(rule)]
        case "user": return [.yours(rule)]
        case "model": return [.model]
        default: return []
        }
    }

    var seenEarlier: Bool { earlier ?? (detector == "known") }

    /// The Logs page's "Found by" filter.
    enum Source: String, CaseIterable {
        case all, rules, model, yours, earlier
        var title: String {
            switch self {
            case .all: "Found by anyone"
            case .rules: "Built-in rules"
            case .model: "Local model"
            case .yours: "Your rules"
            case .earlier: "Seen earlier"
            }
        }
    }

    func found(by source: Source) -> Bool {
        switch source {
        case .all: true
        case .rules: finders.contains { if case .rule = $0 { true } else { false } }
        case .model: finders.contains(.model)
        case .yours: finders.contains { if case .yours = $0 { true } else { false } }
        case .earlier: seenEarlier
        }
    }

    var id: String { "\(ts)-\(seq)" }

    /// The value's shape: its first and last character around one star per hidden character,
    /// `g**************Q`, or only stars when nothing of it may be shown.
    var masked: String {
        let star = Character("*")
        func stars(_ n: Int) -> String { String(repeating: star, count: min(max(n, 1), Self.maxStars)) }
        guard let p = preview, p != "•••", let first = p.first, let last = p.last, chars >= 2 else { return stars(chars) }
        return "\(first)\(stars(chars - 2))\(last)"
    }

    /// Long values are drawn this many stars at most; the length column says how long.
    static let maxStars = 20

    var providerName: String {
        switch provider ?? "anthropic" {
        case "anthropic": "Anthropic"
        case "openai": "OpenAI"
        case "google": "Google"
        case let other: other
        }
    }
    var date: Date { Date(timeIntervalSince1970: ts) }

    /// Who found it, in words, for search and the details.
    var foundBy: String {
        let names = finders.map { f in
            switch f {
            case .rule(let id): "built-in rule \(id)"
            case .yours(let name): "your rule \(name)"
            case .model: "the local model"
            }
        }
        let who = names.count > 1 ? names.dropLast().joined(separator: ", ") + " and " + names.last! : names.first ?? "unknown"
        return seenEarlier ? "Seen earlier in the conversation; first found by \(who)" : who.prefix(1).uppercased() + who.dropFirst()
    }

    var clientName: String { agentName(client) }

    func matches(_ query: String) -> Bool {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        if q.isEmpty { return true }
        return [preview ?? "", type, Theme.label(type), foundBy, clientName, outcome, before ?? "", after ?? ""].contains { $0.lowercased().contains(q) }
    }

    /// Synthetic rows for preview mode, clearly not real values.
    static let samples: [Finding] = {
        let now = Date().timeIntervalSince1970
        let rows: [(String, String, String, String?, Int, String)] = [
            ("SECRET", "rules", "github-token", "g…Q", 40, "claude-code"), ("EMAIL", "rules", "email", "j…m", 21, "claude-code"),
            ("SECRET", "model", "", "W…7", 16, "claude-code"), ("PERSON", "model", "", "P…n", 11, "codex"),
            ("SECRET", "rules", "secret-assignment", "x…!", 18, "codex"), ("ACCOUNT", "user", "Employee ID", "•••", 6, "claude-code"),
            ("SECRET", "rules", "openai-project-key", "s…A", 164, "pi"), ("PHONE", "rules", "phone-nanp", "(…1", 14, "claude-code"),
            ("SECRET", "known", "", "g…Q", 40, "claude-code"), ("SECRET", "rules", "connection-string-password", "Z…k", 12, "opencode"),
            ("ADDRESS", "model", "", "4…1", 38, "claude-code"), ("SECRET", "rules", "aws-access-key-id", "A…V", 20, "codex"),
            ("DATE", "rules", "date-of-birth", "1…2", 10, "claude-code"), ("SECRET", "rules", "key-in-prose", "3…a", 32, "claude-code"),
        ]
        // Synthetic text around each, as the feed sends it: every masked value shown as its type.
        let around: [(String, String)] = [
            ("…the deploy failed with 401. In .env I have GITHUB_TOKEN=", " and NODE_ENV=production; can you check the workflow file…"),
            ("Draft a reply to ", " saying the invoice was paid on ‹DATE› and attach the receipt."),
            ("…curl -H \"Authorization: Bearer ", "\" https://internal.example.dev/api/v2/jobs returns 403 since this morning…"),
            ("…the migration was reviewed by ", " and merged; ‹PERSON› will run it after the freeze…"),
            ("…config.yaml:\n  database:\n    password: ", "\n    pool: 20\n  cache:\n    url: ‹URL›"),
            ("Look up badge ", " in the HR export and tell me which team it belongs to."),
            ("…export OPENAI_API_KEY=", "\nexport OPENAI_ORG=‹ACCOUNT›\npython eval.py --model gpt…"),
            ("…call the on-call at ", " if the alert fires again before ‹DATE›…"),
            ("…same error as before, the token ", " still fails in CI but works locally…"),
            ("…DATABASE_URL=postgres://app:", "@db.internal:5432/app?sslmode=require…"),
            ("Ship the replacement unit to ", ", attention ‹PERSON›, before Friday."),
            ("…aws configure set aws_access_key_id ", "; aws configure set aws_secret_access_key ‹SECRET›…"),
            ("…patient record: name ‹PERSON›, born ", ", referred by ‹PERSON›…"),
            ("…the vendor said the key for staging is ", " and that it rotates monthly…"),
        ]
        // Some values found by a rule and the model both; the 9th was masked earlier in the conversation.
        let finders: [[String]] = [
            ["rule:github-token", "model"], ["rule:email"], ["model"], ["model"], ["rule:secret-assignment"], ["yours:Employee ID"],
            ["rule:openai-project-key", "model"], ["rule:phone-nanp"], ["rule:github-token", "model"], ["rule:connection-string-password"],
            ["model"], ["rule:aws-access-key-id"], ["rule:date-of-birth"], ["rule:key-in-prose", "model"],
        ]
        return rows.enumerated().map { i, r in
            Finding(ts: now - Double(i * 47 + 12), seq: 0, type: r.0, detector: r.1, rule: r.2, chars: r.4, preview: r.3,
                    outcome: i == 4 ? "observed" : "masked", client: r.5, provider: r.5 == "codex" ? "openai" : "anthropic",
                    before: around[i].0, after: around[i].1, by: finders[i], earlier: i == 8)
        }
    }()
}

/// Logs: every value keepmum masked in new text, newest first, searchable. Kept in keepmum's memory
/// only, so the list starts empty after keepmum restarts.
func agentName(_ client: String?) -> String {
    switch client ?? "claude-code" {
    case "claude-code": "Claude Code"
    case "codex": "Codex"
    case "gemini": "Gemini CLI"
    case "antigravity": "Antigravity CLI"
    case let other: other
    }
}

/// What the optional checks flagged, newest first: the check, where (a tool, or what was typed),
/// the categories, the score and the length. Never the text. Its details show beside or in place of it.
struct ChecksCard: View {
    var store: StatsStore
    var hits: [Snapshot.GuardHit]
    /// The selected hit's id.
    @Binding var selection: String?

    var body: some View {
        let r = store.snapshot.guard?.requests ?? [:]
        Card(title: "Checks", subtitle: hits.isEmpty ? "Nothing flagged since keepmum started"
                 : "\(hits.count) flagged: \(r["blocked"] ?? 0) stopped, \(r["local"] ?? 0) answered on this Mac, \(r["warned"] ?? 0) sent with a warning") {
            DataTable(key: "checks", columns: Self.columns, items: hits, selection: $selection,
                      empty: "Turned-on checks flag here. Select one for what was flagged, what you had asked, and what keepmum did.",
                      snapshotRows: 6) { h, column in
                CheckCell(hit: h, column: column)
            }
        }
    }

    static let columns: [DataColumn] = [
        DataColumn(id: "time", title: "Time", width: 118, min: 70, hideable: false),
        DataColumn(id: "agent", title: "Agent", width: 106, min: 50),
        DataColumn(id: "check", title: "Check", width: 104, min: 50),
        DataColumn(id: "flagged", title: "Flagged", width: 220, min: 80, flexible: true),
        DataColumn(id: "score", title: "Score", width: 58, min: 40, alignment: .trailing),
        DataColumn(id: "length", title: "Length", width: 66, min: 40, alignment: .trailing),
        DataColumn(id: "mode", title: "Mode", width: 64, min: 40, hiddenByDefault: true),
        DataColumn(id: "model", title: "Model", width: 160, min: 50, hiddenByDefault: true),
        DataColumn(id: "provider", title: "Sent to", width: 84, min: 50, hiddenByDefault: true),
        DataColumn(id: "endpoint", title: "Endpoint", width: 170, min: 60, hiddenByDefault: true),
        DataColumn(id: "outcome", title: "Outcome", width: 96, min: 60),
    ]
}

private struct CheckCell: View {
    var hit: Snapshot.GuardHit
    var column: String

    var body: some View {
        let h = hit
        switch column {
        case "time": TimeCell(date: h.date)
        case "agent": AgentCell(client: h.client)
        case "check":
            HStack(spacing: Space.s - Space.xxs) {
                Image(systemName: h.checkIcon).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 14)
                Text(h.checkName)
            }
            .font(.km(.callout)).foregroundStyle(Theme.text)
        case "flagged": Text(h.flagged).font(.km(.callout)).foregroundStyle(Theme.text).truncationMode(.tail)
        case "score":
            Text(h.isRule ? "—" : String(format: "%.2f", h.score)).font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
        case "length": Text(h.chars.formatted()).font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
        case "mode": Text((h.mode ?? "—").capitalized).font(.km(.callout)).foregroundStyle(Theme.textSecondary)
        case "model": Text(h.model ?? "—").font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary).truncationMode(.middle)
        case "provider": Text(h.providerName).font(.km(.callout)).foregroundStyle(Theme.text)
        case "endpoint": Text(h.path ?? "—").font(.km(.monoSmall)).foregroundStyle(Theme.text).truncationMode(.middle)
        case "outcome": CheckOutcome(hit: h)
        default: EmptyView()
        }
    }
}

extension Snapshot.GuardHit {
    var checkName: String {
        check == "harm" ? "Harm" : check == "commands" ? "Command" : check == "script" ? "Script" : "Injection"
    }
    var checkTitle: String {
        check == "harm" ? "Harmful request" : check == "commands" ? "Risky command"
            : check == "script" ? "Values keepmum could not check" : "Prompt injection"
    }
    var checkIcon: String {
        check == "harm" ? "exclamationmark.bubble" : check == "commands" ? "terminal" : check == "script" ? "curlybraces" : "syringe"
    }
    /// A rule matched (the commands and script checks): no score, and the reply is what was flagged.
    var isRule: Bool { check == "commands" || check == "script" }
    var cats: [String] { categories ?? [] }

    /// Where it was flagged, short, for the row.
    var flagged: String {
        switch check {
        case "harm": "What was typed" + (cats.isEmpty ? "" : ": \(cats.joined(separator: ", "))")
        case "commands": "\(source ?? "Shell") command: \(cats.joined(separator: ", "))"
        case "script": "Codex script: \(cats.joined(separator: ", "))"
        default: "\(toolLabel(source ?? "a tool")) result" + (earlier == true ? " (flagged before)" : "")
        }
    }

    /// The outcome in a word, its icon and tone.
    var outcome: (String, String, Color) {
        switch action {
        case "blocked" where check == "commands": ("Replaced", "hand.raised.fill", Theme.blocked)
        case "blocked": ("Refused", "hand.raised.fill", Theme.blocked)
        case "local" where localError != nil: ("Refused", "hand.raised.fill", Theme.blocked)
        case "local" where handedBack?.kept == nil && ["summary", "rewrite", "withhold"].contains(handedBack?.strategy ?? ""):
            ("Replaced", "arrow.uturn.up", Theme.restored)
        case "local": ("Local", "desktopcomputer", Theme.restored)
        default: ("Warned", "flag.fill", Theme.warning)
        }
    }

    var providerName: String {
        switch provider ?? "anthropic" {
        case "anthropic": "Anthropic"
        case "openai": "OpenAI"
        case "google": "Google"
        case let other: other
        }
    }

    /// What keepmum did, in a sentence or two.
    var whatHappened: String {
        let agent = agentName(client)
        switch (check, action) {
        case ("commands", "blocked"):
            return "\(agent) got a command that prints why it was stopped and fails, in place of this one. The command never ran."
        case ("commands", _):
            return "The reply went to \(agent) unchanged: the command ran only if \(agent) or you approved it. Check what it did."
        case ("script", _):
            return "The script went to \(agent) unchanged, with a note from keepmum after it. It hands values it works out itself, such as a command's output, to a network tool. keepmum sees only what goes to the model and back, so it could not check those values: a secret in them may have reached the server."
        case (_, "blocked"):
            return "The request was refused before it left this Mac: \(providerName) received nothing, and \(agent) got an error saying why."
        case (_, "local"):
            if let e = localError {
                return "The request was meant for a local model, but it was refused instead: \(e). \(providerName) received nothing."
            }
            if let h = handedBack {
                if let kept = h.kept {
                    return "A model on this Mac answered instead of \(providerName), without tools: \(kept), so the conversation stayed local. \(providerName) received nothing."
                }
                switch h.strategy {
                case "summary": return "A model on this Mac summarised this result, without tools, and the summary went to \(providerName) in its place, masked and checked. \(providerName) never received the result itself."
                case "rewrite": return "A model on this Mac rewrote this result without what it said to the agent, and that went to \(providerName) in its place, masked and checked. \(providerName) never received the result itself."
                case "withhold": return "\(providerName) got a notice in this result's place, and the turn went on. It never received the result itself."
                case "answer": return "A model on this Mac answered the turn instead of \(providerName), without tools. Then the conversation went back to \(providerName), with the local answer and a notice in this result's place."
                default: break
                }
            }
            return "A model on this Mac answered instead of \(providerName), without tools; \(providerName) received nothing."
        default:
            return "The request was sent to \(providerName) at once and checked afterwards, so this was logged, not stopped."
        }
    }

    /// What to do about it.
    var advice: String {
        switch (check, action) {
        case ("commands", "blocked"):
            return "If the command was what you wanted, run it yourself, or set Risky commands to Warn."
        case ("commands", _):
            return "To have such commands replaced before the agent gets them, set Risky commands to Block (Settings › Checks)."
        case ("script", _):
            return "Check what the server received, and rotate any secret that went out. To keep secrets out, ask the agent to make the network call in a step of its own: what it sends then goes through keepmum."
        case ("harm", "blocked"):
            return "Rephrase the request, or rewind (or /clear). Harm in Warn mode would send such requests with a warning."
        case ("harm", "local"):
            return "Later requests that still carry this text go to the local model too, until you rewind or /clear."
        case ("harm", _):
            return "To refuse such requests instead, set Harmful requests to Block (Settings › Checks)."
        case (_, "blocked"):
            return "Every request that still carries this result is refused: rewind to before it, or /clear, to go on."
        case (_, "local"):
            return "Later requests that still carry this result stay on this Mac too, until you rewind to before it or /clear."
        default:
            return "If the agent acted on it, check what it did. Block refuses such requests; Local answers them on this Mac."
        }
    }
}

/// A check's outcome in the Logs table and details.
struct CheckOutcome: View {
    var hit: Snapshot.GuardHit

    var body: some View {
        let (label, icon, tone) = hit.outcome
        HStack(spacing: Space.xs + 1) {
            Image(systemName: icon).font(.km(.caption))
            Text(label)
        }
        .font(.km(.callout)).foregroundStyle(tone)
    }
}

/// One check's hit in full: what it found and where, how sure, what keepmum did, and what to do.
struct CheckDetail: View {
    var hit: Snapshot.GuardHit
    /// The flagged text and the user's prompt, masked values as types; nil: not kept.
    var texts: StatsStore.CheckText? = nil
    var contextOn = true
    /// In the Logs page's details, which scroll as a whole: the texts are shown in full, on the canvas.
    var inPane = false
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let h = hit
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Image(systemName: h.checkIcon).foregroundStyle(Theme.textSecondary)
                Text(h.checkTitle).font(.km(.headline)).foregroundStyle(Theme.text)
                Spacer()
                CheckOutcome(hit: h)
            }
            DetailColumns { left(h) } right: { right(h) }
        }
        .padding(Space.l + Space.xs)
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private func left(_ h: Snapshot.GuardHit) -> some View {
            Text(h.whatHappened).font(.km(.body)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Space.l, verticalSpacing: Space.s) {
                field("Found in", found(h))
                if !h.cats.isEmpty { field(h.isRule ? "Rule" : "Categories", h.cats.joined(separator: ", ")) }
                field("Score", score(h))
                field("Length", "\(h.chars.formatted()) characters")
                if let mode = h.mode { field("Mode", modeName(mode)) }
                if let model = h.model { field(h.isRule ? "Checked by" : "Model", model) }
                field("Agent", agentName(h.client))
                field((h.action == "warned" && !h.isRule) || h.outcome.0 == "Replaced" ? "Sent to" : h.isRule ? "Reply from" : "Meant for",
                      h.providerName)
                if let b = h.handedBack, b.kept == nil, let model = b.model, let url = b.url {
                    field(b.strategy == "summary" ? "Summarised by" : "Rewritten by",
                          "\(LocalServersSection.shortName(model)) at \(url.replacingOccurrences(of: "http://", with: ""))")
                }
                if let a = h.answeredBy {
                    field("Answered by", "\(LocalServersSection.shortName(a.model)) at \(a.url.replacingOccurrences(of: "http://", with: ""))"
                          + (a.api == "chat" ? ", as Chat Completions" : ""))
                    if let skipped = a.passedOver, !skipped.isEmpty {
                        field("Passed over", skipped.joined(separator: "\n"))
                    }
                }
                if let path = h.path { field("Request", path, mono: true) }
                if h.earlier == true { field("Flagged before", "On an earlier request; still in the conversation, so every request that carries it is checked again") }
                field("Time", isSnapshot ? h.date.formatted(date: .abbreviated, time: .standard) : detailTime(h.date))
            }
            VStack(alignment: .leading, spacing: Space.xs) {
                Text("What to do").eyebrow()
                Text(h.advice).font(.km(.callout)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
            }
    }

    @ViewBuilder private func right(_ h: Snapshot.GuardHit) -> some View {
            if h.check != "harm", let prompt = texts?.prompt {
                block("What you asked", prompt, maxHeight: 90)
            }
            if let text = texts?.text {
                block(flaggedTitle(h), text, maxHeight: 220)
            } else {
                VStack(alignment: .leading, spacing: Space.xs) {
                    Text(flaggedTitle(h)).eyebrow()
                    Text(contextOn ? "Not kept for this one: it was flagged before keepmum kept checks' text, or the text has since been dropped."
                                   : "Not kept. Text around values is off (Settings › General › Logs page).")
                        .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Text("The text is as keepmum saw it, with every masked value shown as its type, like ‹SECRET›. It is kept in keepmum's memory only, never in the audit log or on disk, and goes when keepmum exits or text around values is turned off.")
                .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
    }

    private func flaggedTitle(_ h: Snapshot.GuardHit) -> String {
        switch h.check {
        case "harm": "What you typed"
        case "commands": "The command"
        case "script": "The script"
        default: "What \(toolLabel(h.source ?? "the tool")) returned"
        }
    }

    /// A text in a box: monospaced, masked values tinted, scrolling past `maxHeight`.
    @ViewBuilder private func block(_ title: String, _ text: String, maxHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(title).eyebrow()
            let body = Text(typesTinted(text)).font(.km(.mono)).lineSpacing(3).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity, alignment: .leading).padding(Space.m)
            Group {
                if isSnapshot || inPane { body } else { ScrollView { body }.frame(maxHeight: maxHeight).fixedSize(horizontal: false, vertical: true) }
            }
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(inPane ? Theme.canvas : Theme.surface))
        }
    }

    private func found(_ h: Snapshot.GuardHit) -> String {
        switch h.check {
        case "harm": return "What you typed"
        case "commands": return "A \(h.source ?? "shell") command in the model's reply"
        case "script": return "A Codex script (\(h.source ?? "exec")) in the model's reply"
        default:
            return "What \(toolLabel(h.source ?? "a tool")) returned"
                + (h.earlier == true ? ", flagged on an earlier request and still in the conversation" : "")
        }
    }

    private func score(_ h: Snapshot.GuardHit) -> String {
        if h.isRule { return "A rule matched" }
        guard let t = h.threshold else { return String(format: "%.2f", h.score) }
        let threshold = ChecksSection.score(t)
        // As many decimals as the threshold has, so a score just over it does not read as equal.
        let s = String(format: threshold.count > 4 ? "%.3f" : "%.2f", h.score)
        return "\(s), at or over the \(threshold) threshold (1.00 is certain)"
    }

    private func modeName(_ mode: String) -> String {
        switch mode {
        case "warn": "Warn: send, then check"
        case "block": "Block: check first, refuse what it flags"
        case "local": "Local: check first, answer what it flags on this Mac"
        default: mode.capitalized
        }
    }

    private func field(_ key: String, _ value: String, mono: Bool = false) -> some View {
        GridRow {
            Text(key).font(.km(.callout)).foregroundStyle(Theme.textSecondary).gridColumnAlignment(.trailing)
            Text(value).font(mono ? .km(.mono) : .km(.callout)).foregroundStyle(Theme.text).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct LogsPage: View {
    var store: StatsStore
    @State private var query = ""
    @State private var type = "ALL"
    @State private var source = Finding.Source.all
    /// Which list shows: what the checks flagged, or the masked values.
    @AppStorage("logs.tab") private var tab = Tab.checks
    @State private var selectedHit: String?
    @State private var selectedFinding: String?
    @Environment(\.isSnapshot) private var isSnapshot

    enum Tab: String { case checks, masked }

    init(store: StatsStore, selectedHit: String? = nil) {
        self.store = store
        _selectedHit = State(initialValue: selectedHit)
    }

    var body: some View {
        let all = store.sampleMode ? Finding.samples : store.findings
        let shown = all.filter { (type == "ALL" || $0.type == type) && $0.found(by: source) && $0.matches(query) }
        let hits = store.snapshot.guardHits.sorted { $0.ts > $1.ts }
        let showChecks = store.snapshot.checking || !hits.isEmpty
        let current = showChecks ? tab : .masked
        VStack(spacing: Space.l) {
            PageHeader(title: "Logs", subtitle: showChecks
                       ? "What the checks flagged and what keepmum masked in new text, newest first. Kept in memory until keepmum exits."
                       : "What keepmum masked in new text, newest first. Kept in memory until keepmum exits.") {
                if showChecks {
                    Segmented(options: [(Tab.checks, "Checks · \(hits.count.formatted())"),
                                        (Tab.masked, "Masked values · \(all.count.formatted())")], selection: $tab)
                }
            }
            switch current {
            case .checks:
                ListDetail(key: "checks", ids: hits.map(\.id), selection: $selectedHit, back: "Checks",
                             hint: "Click a check, or use ↑ ↓ and Return, for what was flagged, what you had asked, and what keepmum did.") {
                    ChecksCard(store: store, hits: hits, selection: $selectedHit)
                } detail: {
                    if let h = hits.first(where: { $0.id == selectedHit }) {
                        CheckDetail(hit: h, texts: h.seq.flatMap { store.checkTexts[$0] }, contextOn: store.contextOn, inPane: true)
                    }
                }
            case .masked:
                ListDetail(key: "logs", ids: shown.map(\.id), selection: $selectedFinding, back: "Masked values",
                             hint: "Click a value, or use ↑ ↓ and Return, for everything keepmum knows about it.") {
                    maskedCard(all, shown)
                } detail: {
                    if let f = shown.first(where: { $0.id == selectedFinding }) {
                        FindingDetail(finding: f,
                                      related: all.filter { $0.id != f.id && $0.type == f.type && $0.masked == f.masked && $0.chars == f.chars },
                                      request: matchedEvent(f, findings: all, events: store.snapshot.recent + store.history),
                                      previewsOn: store.previewsOn)
                    }
                }
            }
        }
        .task(id: store.sampleMode) {
            while !Task.isCancelled && !isSnapshot {
                await store.loadFindings()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func maskedCard(_ all: [Finding], _ shown: [Finding]) -> some View {
        Card(title: "Masked values", subtitle: maskedSubtitle(all, shown)) {
            HStack(spacing: Space.s) {
                search
                typePicker
                sourcePicker
            }
        } content: {
            VStack(spacing: 0) {
                if !store.previewsOn {
                    Text("Previews are off (Settings › General › Logs page): types and rules only.")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, Space.s)
                }
                DataTable(key: "logs", columns: Self.columns, items: shown, selection: $selectedFinding,
                          empty: all.isEmpty ? "Nothing yet. Values masked in new text appear here; the list starts empty each time keepmum starts."
                                             : "No match", pageKey: "\(type)|\(source)|\(query)") { f, column in
                    FindingCell(finding: f, column: column)
                }
            }
        }
    }

    static let columns: [DataColumn] = [
        DataColumn(id: "time", title: "Time", width: 118, min: 70, hideable: false),
        DataColumn(id: "agent", title: "Agent", width: 106, min: 50),
        DataColumn(id: "type", title: "Type", width: 100, min: 50),
        DataColumn(id: "value", title: "Value", width: 172, min: 60),
        DataColumn(id: "length", title: "Length", width: 56, min: 40, alignment: .trailing),
        DataColumn(id: "foundBy", title: "Found by", width: 180, min: 60),
        DataColumn(id: "context", title: "Context", width: 240, min: 120, flexible: true),
        DataColumn(id: "provider", title: "Sent to", width: 80, min: 50, hiddenByDefault: true),
        DataColumn(id: "outcome", title: "Outcome", width: 96, min: 60, hiddenByDefault: true),
    ]

    private func maskedSubtitle(_ all: [Finding], _ shown: [Finding]) -> String {
        if all.isEmpty { return "Nothing masked since keepmum started" }
        let observed = all.filter { $0.outcome == "observed" }.count
        return "\(shown.count) of \(all.count)" + (observed > 0 ? " · \(observed) sent unmasked in observe mode" : "")
    }

    private var search: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Image(systemName: "magnifyingglass").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            if isSnapshot {
                Text("Search").font(.km(.callout)).foregroundStyle(Theme.textTertiary)
            } else {
                TextField("Search", text: $query, prompt: Text("Search: g…w, email, github-token, Codex, text around"))
                    .textFieldStyle(.plain).font(.km(.callout))
            }
            Spacer(minLength: 0)
            if !query.isEmpty && !isSnapshot {
                Button { query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textTertiary) }
                    .buttonStyle(.plain).accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, Space.s).frame(minWidth: 150, idealWidth: 260, maxWidth: 260).frame(height: 26)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
    }

    @ViewBuilder private var typePicker: some View {
        if isSnapshot {
            Text("All types").font(.km(.callout)).foregroundStyle(Theme.text)
                .padding(.horizontal, Space.s).frame(height: 26)
                .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
        } else {
            Picker("Type", selection: $type) {
                Text("All types").tag("ALL")
                ForEach(Theme.typeOrder, id: \.self) { Text(Theme.label($0)).tag($0) }
            }
            .labelsHidden().frame(width: 130)
        }
    }

    @ViewBuilder private var sourcePicker: some View {
        if isSnapshot {
            Text(source.title).font(.km(.callout)).foregroundStyle(Theme.text)
                .padding(.horizontal, Space.s).frame(height: 26)
                .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
        } else {
            Picker("Found by", selection: $source) {
                ForEach(Finding.Source.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            .labelsHidden().frame(width: 150)
        }
    }
}

private struct FindingCell: View {
    var finding: Finding
    var column: String

    var body: some View {
        let f = finding
        switch column {
        case "time": TimeCell(date: f.date)
        case "agent": AgentCell(client: f.client)
        case "type":
            HStack(spacing: Space.s - Space.xxs) {
                Image(systemName: Theme.icon(f.type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 14)
                Text(Theme.label(f.type))
            }
            .font(.km(.callout)).foregroundStyle(Theme.text)
        case "value": Text(f.masked).font(.km(.mono)).foregroundStyle(Theme.masked)
        case "length": Text("\(f.chars)").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
        case "foundBy": FinderLabel(finding: f)
        case "context":
            if f.before == nil && f.after == nil {
                Text("—").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            } else {
                Text(contextLine(f, around: 14)).font(.km(.monoSmall)).truncationMode(.tail)
            }
        case "provider": Text(f.providerName).font(.km(.callout)).foregroundStyle(Theme.text)
        case "outcome": OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
        default: EmptyView()
        }
    }
}

/// One masked value in full: what happened to it, everything the feed knows about it, the request
/// it went out in, other times it was seen, and the text around it.
struct FindingDetail: View {
    var finding: Finding
    /// Other rows with the same type, shape and length: most likely the same value.
    var related: [Finding] = []
    /// The Activity row of the request it went out in, when it is still there.
    var request: Snapshot.Event? = nil
    var previewsOn = true

    var body: some View {
        let f = finding
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Image(systemName: Theme.icon(f.type)).foregroundStyle(Theme.textSecondary)
                Text(Theme.label(f.type)).font(.km(.headline)).foregroundStyle(Theme.text)
                Text(f.masked).font(.km(.mono).weight(.semibold)).foregroundStyle(Theme.masked).textSelection(.enabled)
                Spacer(minLength: Space.s)
                OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
            }
            DetailColumns {
                Text(story(f)).font(.km(.body)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                DetailGrid {
                    DetailField("Shape", "\(f.masked)\n\(shapeNote(f))", mono: false)
                    DetailField("Length", "\(f.chars) characters")
                    GridRow {
                        Text("Found by").font(.km(.callout)).foregroundStyle(Theme.textSecondary).gridColumnAlignment(.trailing)
                        VStack(alignment: .leading, spacing: Space.xs) {
                            FinderLabel(finding: f, wraps: true)
                            if f.seenEarlier {
                                Text("Masked earlier in this conversation; matched again as the same value.")
                                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                            } else if f.finders.count > 1 {
                                Text("Found by more than one detector: the most certain kind of find.")
                                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    DetailField("Agent", f.clientName)
                    DetailField("Sent to", f.providerName)
                    DetailField("Time", detailTime(f.date))
                    if let e = request {
                        DetailField("Request", "\(e.path) · \(e.status) · scanned in \(milliseconds(e.scanMs)) · "
                                    + "\(e.new ?? 0) new value\(e.new == 1 ? "" : "s") masked in it (Activity, \(e.date.formatted(date: .omitted, time: .standard)))")
                    }
                    if !related.isEmpty {
                        let times = (related + [f]).map(\.date).sorted()
                        DetailField("Seen", "\(related.count + 1) times on this page, likely the same value (same type, shape and length): "
                                    + "first \(times.first!.formatted(date: .abbreviated, time: .standard)), "
                                    + "last \(times.last!.formatted(date: .abbreviated, time: .standard))")
                    }
                }
            } right: {
                DetailSection(title: "Context") {
                    if f.before != nil || f.after != nil {
                        Text(contextText(f)).font(.km(.mono)).lineSpacing(3)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(Space.m)
                            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.canvas))
                        let others = otherValues(f)
                        Text((f.outcome == "observed"
                              ? "Observe mode sent this text unmasked; here every masked value is shown as its type, like ‹SECRET›."
                              : "As the provider received it, with every masked value shown as its type, like ‹SECRET›.")
                             + (others.isEmpty ? "" : " Also masked in this text: \(others)."))
                            .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("Not kept. Text around values is off (Settings › General › Logs page), or this value was masked before it was turned on.")
                            .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                DetailSection(title: "What happens next") {
                    Text(next(f)).font(.km(.callout)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(Space.l + Space.xs)
    }

    private func story(_ f: Finding) -> String {
        if f.outcome == "observed" {
            return "Observe mode: \(f.providerName) got this \(singular(f.type)) as \(f.clientName) wrote it. keepmum only counted it."
        }
        return "keepmum took this \(f.chars)-character \(singular(f.type)) out of what \(f.clientName) sent: \(f.providerName) got "
            + "‹\(f.type)› in its place, never the value."
    }

    private func next(_ f: Finding) -> String {
        if f.outcome == "observed" { return "Nothing was masked. Turn observe mode off to have values like this one masked." }
        return "Where the reply uses its placeholder, keepmum puts the real value back on this Mac before \(f.clientName) sees it. "
            + "Tools that run on this Mac get the real value; network tools get it only as their setting allows (Settings › Tools)."
    }

    private func shapeNote(_ f: Finding) -> String {
        if f.preview == nil { return previewsOn ? "Only its length was kept" : "Previews are off: only its length is shown" }
        if f.preview == "•••" { return "Shorter than 8 characters: none of it is shown" }
        return "Its first and last character; a star per hidden character, at most \(Finding.maxStars)"
    }

    private func singular(_ type: String) -> String {
        switch type {
        case "SECRET": "secret"
        case "ACCOUNT": "account number"
        case "PERSON": "name"
        case "EMAIL": "email address"
        case "PHONE": "phone number"
        case "ADDRESS": "address"
        case "URL": "URL"
        case "DATE": "date"
        default: type.lowercased()
        }
    }

    /// The other masked values in the text around it, by type: "2 secrets, 1 URL".
    private func otherValues(_ f: Finding) -> String {
        let text = (f.before ?? "") + (f.after ?? "")
        var counts: [String: Int] = [:]
        var rest = Substring(text)
        while let open = rest.firstIndex(of: "‹"), let close = rest[open...].firstIndex(of: "›") {
            counts[String(rest[rest.index(after: open)..<close]), default: 0] += 1
            rest = rest[rest.index(after: close)...]
        }
        return Theme.ordered(counts).map { typeCount($0.1, $0.0) }.joined(separator: ", ")
    }

    /// Before and after in the secondary color, the value itself highlighted, other values (‹TYPE›) tinted.
    private func contextText(_ f: Finding) -> AttributedString {
        var out = quietTinted(f.before ?? "")
        var value = AttributedString(f.masked)
        value.foregroundColor = Theme.masked
        value.backgroundColor = Theme.soft(Theme.masked)
        value.font = .km(.mono).weight(.semibold)
        out += value
        out += quietTinted(f.after ?? "")
        return out
    }
}

/// Who found a value: each rule by name, and chips for the local model, your own rules and a value
/// seen earlier in the conversation. Colors match the Detection page's "Who found it".
struct FinderLabel: View {
    var finding: Finding
    /// In the details there is room for every finder on its own line.
    var wraps = false

    var body: some View {
        let parts = finding.finders
        let layout = wraps ? AnyLayout(VStackLayout(alignment: .leading, spacing: Space.xs)) : AnyLayout(HStackLayout(spacing: Space.s - Space.xxs))
        layout {
            if finding.seenEarlier && !wraps { Chip(text: "Earlier") }
            ForEach(parts, id: \.self) { part in
                switch part {
                case .rule(let id):
                    HStack(spacing: Space.s - Space.xxs) {
                        if wraps { Chip(text: "Rule", tone: Theme.series[0]) }
                        Text(id).font(.km(.mono)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                    }
                case .yours(let name):
                    HStack(spacing: Space.s - Space.xxs) {
                        Chip(text: "Yours", tone: Theme.series[2])
                        Text(name).font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1)
                    }
                case .model:
                    Chip(text: "Model", tone: Theme.series[1])
                }
            }
            if parts.isEmpty { Text("—").foregroundStyle(Theme.textSecondary) }
        }
    }
}

/// Text with its masked values (`‹SECRET›`) tinted, the rest in the text color.
func typesTinted(_ s: String) -> AttributedString {
    var out = AttributedString()
    var rest = Substring(s)
    while let open = rest.firstIndex(of: "‹"), let close = rest[open...].firstIndex(of: "›") {
        out += AttributedString(String(rest[..<open]))
        var mark = AttributedString(String(rest[open...close]))
        mark.foregroundColor = Theme.masked
        mark.backgroundColor = Theme.soft(Theme.masked)
        out += mark
        rest = rest[rest.index(after: close)...]
    }
    out += AttributedString(String(rest))
    return out
}
