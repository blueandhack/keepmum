import SwiftUI

/// One value keepmum masked in new text (`GET /_keepmum/findings`): its type, the rule that found
/// it, its length, for values of 8 or more characters the first and last one, and the text around
/// it with every masked value shown as its type (`‹SECRET›`). The value itself only with [log]
/// values on, for Show values.
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
    /// The value as it was: only with [log] values on, for Show values (RevealValues.swift).
    var value: String?
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

    /// `reveal`: values show, so a search finds them too.
    func matches(_ query: String, reveal: Bool = false) -> Bool {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        if q.isEmpty { return true }
        return [reveal ? value ?? "" : "", preview ?? "", type, Theme.label(type), foundBy, clientName, outcome, before ?? "", after ?? ""].contains { $0.lowercased().contains(q) }
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

/// A row of the Logs page: what a check flagged, or a value keepmum masked. Its id says which, so a
/// selection holds when the page goes from one table to two.
enum LogRow: Identifiable {
    case check(Snapshot.GuardHit)
    case value(Finding)

    var id: String {
        switch self {
        case .check(let h): "check|" + h.id
        case .value(let f): "value|" + f.id
        }
    }
    var ts: Double {
        switch self {
        case .check(let h): h.ts
        case .value(let f): f.ts
        }
    }
}

/// The Logs page's columns: both kinds in one table, or each kind's own.
enum LogColumns {
    static let together: [DataColumn] = [
        DataColumn(id: "time", title: "Time", width: 118, min: 70, hideable: false),
        DataColumn(id: "agent", title: "Agent", width: 106, min: 50),
        DataColumn(id: "kind", title: "Kind", width: 136, min: 60),
        DataColumn(id: "summary", title: "What", width: 280, min: 120, flexible: true),
        DataColumn(id: "evidence", title: "Found by", width: 190, min: 60),
        DataColumn(id: "length", title: "Length", width: 60, min: 40, alignment: .trailing, hiddenByDefault: true),
        DataColumn(id: "provider", title: "Sent to", width: 84, min: 50, hiddenByDefault: true),
        DataColumn(id: "outcome", title: "Outcome", width: 96, min: 60),
    ]

    static let checks: [DataColumn] = [
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

    static let masked: [DataColumn] = [
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
}

/// A Logs row's cell. Each kind draws its own columns; the mixed table's columns (kind, what,
/// found by, outcome) say the same thing of both in the same place.
struct LogCell: View {
    var row: LogRow
    var column: String
    /// A check's flagged text, one line, when keepmum kept it.
    var text: String?
    @Environment(\.revealValues) private var reveal

    var body: some View {
        switch row {
        case .check(let h):
            switch column {
            case "kind": KindBadge(icon: h.checkIcon, label: "\(h.checkName) check", tone: Theme.warning)
            case "summary":
                if let text, !text.isEmpty {
                    Text("\(Text(h.flagged).foregroundStyle(Theme.text))  \(Text(text).font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary))")
                        .font(.km(.callout)).truncationMode(.tail)
                } else {
                    Text(h.flagged).font(.km(.callout)).foregroundStyle(Theme.text).truncationMode(.tail)
                }
            case "evidence":
                if h.isRule {
                    Text("A rule").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                } else {
                    HStack(spacing: Space.s - Space.xxs) {
                        Text(String(format: "%.2f", h.score)).font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.text)
                        if let model = h.model {
                            Text(model).font(.km(.callout)).foregroundStyle(Theme.textSecondary).truncationMode(.tail)
                        }
                    }
                }
            default: CheckCell(hit: h, column: column)
            }
        case .value(let f):
            switch column {
            case "kind":
                let name = valueName(f.type)
                KindBadge(icon: Theme.icon(f.type), label: name == "URL" ? name : name.prefix(1).uppercased() + name.dropFirst(), tone: Theme.masked)
            case "summary":
                HStack(spacing: Space.s) {
                    Text(f.shown(reveal)).font(.km(.mono)).foregroundStyle(Theme.masked).lineLimit(1).fixedSize()
                    if f.before != nil || f.after != nil {
                        Text(contextLine(f, around: 14, reveal: reveal)).font(.km(.monoSmall)).truncationMode(.tail)
                    }
                }
            case "evidence": FinderLabel(finding: f)
            case "outcome": OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
            default: FindingCell(finding: f, column: column)
            }
        }
    }
}

/// What a row is, at a glance in the mixed table: an icon on its family's tint (amber for what a
/// check flagged, the masked color for a value) and a name.
private struct KindBadge: View {
    var icon: String
    var label: String
    var tone: Color

    var body: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Image(systemName: icon).font(.system(size: 9, weight: .semibold)).foregroundStyle(tone)
                .frame(width: 18, height: 18)
                .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(Theme.soft(tone)))
            Text(label).font(.km(.callout)).foregroundStyle(Theme.text)
        }
    }
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
                    Text(h.seq == nil ? "Not kept: this row is from before keepmum last started, and text is kept in memory only. The row itself is kept on disk (Settings › General › Logs page)."
                         : contextOn ? "Not kept for this one: it was flagged before keepmum kept checks' text, or the text has since been dropped."
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

/// Logs: what the checks flagged and the values keepmum masked, newest first. Together in one
/// table, or split into two, one above the other; one filter bar for both, and one details pane.
struct LogsPage: View {
    var store: StatsStore
    /// For Show values to turn [log] values on; nil in snapshots.
    var engine: Engine? = nil
    @State private var query = ""
    /// What shows: "all"; "checks", or "check:<check>"; "masked", or "type:<TYPE>".
    @State private var show = "all"
    @State private var source = Finding.Source.all
    /// Checks and masked values in one table, or each in its own.
    @AppStorage("logs.layout") private var layout = Layout.together
    /// Split: the checks table's share of the height.
    @AppStorage("logs.split") private var share = Self.defaultShare
    @State private var dragFrom: Double?
    @State private var selection: String?
    /// Snapshots: the layout to draw, whatever the user chose.
    private var drawn: Layout?
    @Environment(\.isSnapshot) private var isSnapshot

    enum Layout: String { case together, split }
    private static let defaultShare = 0.42

    init(store: StatsStore, engine: Engine? = nil, selectedHit: String? = nil, layout: Layout? = nil) {
        self.store = store
        self.engine = engine
        _selection = State(initialValue: selectedHit.map { "check|" + $0 })
        drawn = layout
    }

    var body: some View {
        let values = store.sampleMode ? Finding.samples : store.findings
        let hits = store.snapshot.guardHits.sorted { $0.ts > $1.ts }
        let checking = store.snapshot.checking || !hits.isEmpty
        // Checks turned off and none flagged: a check filter left over shows everything.
        let show = checking || !show.hasPrefix("check") ? show : "all"
        let wantsChecks = checking && (show == "all" || show.hasPrefix("check"))
        let wantsValues = show == "all" || show == "masked" || show.hasPrefix("type:")
        let valuesOnly = !wantsChecks
        let shownHits = wantsChecks ? hits.filter { h in
            (show == "all" || show == "checks" || show == "check:\(h.check)") && h.matches(query, text: text(h))
        } : []
        let shownValues = wantsValues ? values.filter { f in
            (!show.hasPrefix("type:") || show == "type:\(f.type)") && (!valuesOnly || f.found(by: source)) && f.matches(query, reveal: store.valuesShown)
        } : []
        let both = wantsChecks && wantsValues
        let current = drawn ?? layout
        let checkRows = shownHits.map(LogRow.check)
        let valueRows = shownValues.map(LogRow.value)
        // Together, newest first; split, the checks table's rows and then the masked values', as ↑ ↓ step.
        let rows = both && current == .together ? (checkRows + valueRows).sorted { $0.ts > $1.ts } : checkRows + valueRows
        VStack(spacing: Space.l) {
            PageHeader(title: "Logs", subtitle: checking
                       ? "What the checks flagged and what keepmum masked in new text, newest first. Kept in memory until keepmum exits."
                       : "What keepmum masked in new text, newest first. Kept in memory until keepmum exits.") {
                ValuesButton(store: store, engine: engine)
                if checking {
                    Segmented(options: [(Layout.together, "Together"), (Layout.split, "Split")],
                              selection: Binding(get: { current }, set: { layout = $0 }))
                        .disabled(!both)
                        .help(both ? "Together: checks and masked values in one table, newest first. Split: each in a table of its own."
                                   : "Showing one kind: choose Everything under Show to see both, together or split")
                }
            }
            filterBar(checking: checking, hits: hits, valuesOnly: valuesOnly)
            ListDetail(key: "logs", ids: rows.map(\.id), selection: $selection, back: "Logs",
                       hint: "Click a row, or use ↑ ↓ and Return, for everything keepmum knows about it: what was flagged or masked, where, and what keepmum did.") {
                if both && current == .split {
                    split(checksCard(checkRows, all: hits), checkRows.count, maskedCard(valueRows, all: values), valueRows.count)
                } else if both {
                    togetherCard(rows, hits: hits, values: values, shown: (checkRows.count, valueRows.count))
                } else if wantsChecks {
                    checksCard(checkRows, all: hits)
                } else {
                    maskedCard(valueRows, all: values)
                }
            } detail: {
                detail(hits: hits, values: values)
            }
        }
        .environment(\.revealValues, store.valuesShown)
        .task(id: store.sampleMode) {
            while !Task.isCancelled && !isSnapshot {
                await store.loadFindings()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    // MARK: Tables

    private func togetherCard(_ rows: [LogRow], hits: [Snapshot.GuardHit], values: [Finding], shown: (Int, Int)) -> some View {
        let filtered = shown.0 != hits.count || shown.1 != values.count
        let subtitle = hits.isEmpty && values.isEmpty ? "Nothing flagged or masked since keepmum started"
            : "\(counted(shown.0, of: hits.count, filtered, "check", "checks")) · \(counted(shown.1, of: values.count, filtered, "masked value", "masked values"))"
        return Card(title: "Checks and masked values", subtitle: subtitle) {
            VStack(spacing: 0) {
                previewsNote(values.isEmpty)
                table("logs.together", LogColumns.together, rows,
                      empty: hits.isEmpty && values.isEmpty
                        ? "Nothing yet. What the checks flag and the values keepmum masks appear here, newest first; the list starts empty each time keepmum starts."
                        : "No match")
            }
        }
    }

    private func checksCard(_ rows: [LogRow], all: [Snapshot.GuardHit]) -> some View {
        let r = store.snapshot.guard?.requests ?? [:]
        let subtitle = all.isEmpty ? "Nothing flagged since keepmum started"
            : (rows.count != all.count ? "\(rows.count) of \(all.count) · " : "")
            + "\(all.count) flagged: \(r["blocked"] ?? 0) stopped, \(r["local"] ?? 0) answered on this Mac, \(r["warned"] ?? 0) sent with a warning"
        return Card(title: "Checks", subtitle: subtitle) {
            table("checks", LogColumns.checks, rows,
                  empty: all.isEmpty ? "Turned-on checks flag here. Select one for what was flagged, what you had asked, and what keepmum did."
                                     : "No match", snapshotRows: 6)
        }
    }

    private func maskedCard(_ rows: [LogRow], all: [Finding]) -> some View {
        Card(title: "Masked values", subtitle: maskedSubtitle(all, rows.count)) {
            VStack(spacing: 0) {
                previewsNote(all.isEmpty)
                table("logs", LogColumns.masked, rows,
                      empty: all.isEmpty ? "Nothing yet. Values masked in new text appear here; the list starts empty each time keepmum starts."
                                         : "No match")
            }
        }
    }

    private func table(_ key: String, _ columns: [DataColumn], _ rows: [LogRow], empty: String, snapshotRows: Int = 14) -> some View {
        DataTable(key: key, columns: columns, items: rows, selection: $selection, empty: empty, snapshotRows: snapshotRows,
                  pageKey: "\(show)|\(source)|\(query)") { row, column in
            LogCell(row: row, column: column, text: column == "summary" ? summaryText(row) : nil)
        }
    }

    /// Two tables, the checks over the masked values, with a bar between them to drag. A table
    /// with fewer rows than its share holds gets only the height they need; the other gets the rest.
    private func split(_ top: some View, _ topRows: Int, _ bottom: some View, _ bottomRows: Int) -> some View {
        GeometryReader { geo in
            let room = max(geo.size.height - Space.m, 320)
            let upper = Self.upper(room, share: share, topRows, bottomRows)
            VStack(spacing: 0) {
                top.frame(height: upper)
                SplitHandle { t in
                    let from = dragFrom ?? Double(upper / room)
                    dragFrom = from
                    share = min(max(from + Double(t / room), 0.15), 0.85)
                } ended: {
                    dragFrom = nil
                } reset: {
                    share = Self.defaultShare
                }
                bottom.frame(height: room - upper)
            }
        }
    }

    /// The checks table's height in a split of `room`: its share, or less when its rows need less,
    /// or more when the masked values' rows do.
    private static func upper(_ room: CGFloat, share: Double, _ topRows: Int, _ bottomRows: Int) -> CGFloat {
        let least: CGFloat = 160
        let upper = min(max(room * share, least), room - least)
        if needed(topRows) < upper { return max(needed(topRows), least) }
        if needed(bottomRows) < room - upper { return room - max(needed(bottomRows), least) }
        return upper
    }

    /// A table card's height for this many rows (three at least, for its empty message): the card's
    /// padding and title, the table's header, rows and pager.
    private static func needed(_ rows: Int) -> CGFloat {
        let card = Space.l * 2 + 34 + Space.m
        return card + DataTable<LogRow, EmptyView>.headerHeight + 1 + DataTable<LogRow, EmptyView>.pagerHeight
            + CGFloat(max(rows, 3)) * 31 + Space.xs
    }

    @ViewBuilder private func detail(hits: [Snapshot.GuardHit], values: [Finding]) -> some View {
        if let s = selection {
            if let h = hits.first(where: { LogRow.check($0).id == s }) {
                CheckDetail(hit: h, texts: h.seq.flatMap { store.checkTexts[$0] }, contextOn: store.contextOn, inPane: true)
            } else if let f = values.first(where: { LogRow.value($0).id == s }) {
                FindingDetail(finding: f,
                              related: values.filter { $0.id != f.id && $0.type == f.type && $0.masked == f.masked && $0.chars == f.chars },
                              request: matchedEvent(f, findings: values, events: store.snapshot.recent + store.history),
                              previewsOn: store.previewsOn)
            }
        }
    }

    /// A check's flagged text, kept with [log] context on, as one line for the mixed table.
    private func text(_ h: Snapshot.GuardHit) -> String? {
        h.seq.flatMap { store.checkTexts[$0]?.text }
    }

    private func summaryText(_ row: LogRow) -> String? {
        guard case .check(let h) = row, let t = text(h) else { return nil }
        return t.split(whereSeparator: \.isNewline).joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }

    private func counted(_ n: Int, of total: Int, _ filtered: Bool, _ one: String, _ many: String) -> String {
        filtered ? "\(n.formatted()) of \(total.formatted()) \(total == 1 ? one : many)" : "\(total.formatted()) \(total == 1 ? one : many)"
    }

    private func maskedSubtitle(_ all: [Finding], _ shown: Int) -> String {
        if all.isEmpty { return "Nothing masked since keepmum started" }
        let observed = all.filter { $0.outcome == "observed" }.count
        return "\(shown) of \(all.count)" + (observed > 0 ? " · \(observed) sent unmasked in observe mode" : "")
    }

    @ViewBuilder private func previewsNote(_ empty: Bool) -> some View {
        if !store.previewsOn && !empty {
            Text("Previews are off (Settings › General › Logs page): types and rules only.")
                .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, Space.s)
        }
    }

    // MARK: Filters

    /// One bar for both kinds: a search, what to show, and, for masked values, who found them.
    private func filterBar(checking: Bool, hits: [Snapshot.GuardHit], valuesOnly: Bool) -> some View {
        HStack(spacing: Space.s) {
            search
            showPicker(checking: checking, hits: hits)
            if valuesOnly { sourcePicker }
            Spacer(minLength: 0)
            if show != "all" || source != .all || !query.isEmpty {
                Button("Show everything") { show = "all"; source = .all; query = "" }
                    .buttonStyle(.plain).font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
            }
        }
    }

    private var search: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Image(systemName: "magnifyingglass").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            if isSnapshot {
                Text("Search").font(.km(.callout)).foregroundStyle(Theme.textTertiary)
            } else {
                TextField("Search", text: $query, prompt: Text("Search: Jailbreak, g…w, email, Codex, text around"))
                    .textFieldStyle(.plain).font(.km(.callout))
            }
            Spacer(minLength: 0)
            if !query.isEmpty && !isSnapshot {
                Button { query = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.textTertiary) }
                    .buttonStyle(.plain).accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, Space.s).frame(minWidth: 160, idealWidth: 300, maxWidth: 300).frame(height: 26)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
    }

    /// The checks there are something of, in the order Settings lists them.
    private func checkKinds(_ hits: [Snapshot.GuardHit]) -> [(String, String)] {
        let names = [("injection", "Prompt injections"), ("harm", "Harmful requests"), ("commands", "Risky commands"), ("script", "Codex scripts")]
        let there = Set(hits.map(\.check))
        return names.filter { there.contains($0.0) || $0.0 == show.dropFirst(6) }
    }

    private func showTitle(_ hits: [Snapshot.GuardHit]) -> String {
        if show == "all" { return "Everything" }
        if show == "checks" { return "All checks" }
        if show == "masked" { return "All masked values" }
        if show.hasPrefix("check:") { return checkKinds(hits).first { "check:\($0.0)" == show }?.1 ?? "Checks" }
        return Theme.label(String(show.dropFirst(5)))
    }

    @ViewBuilder private func showPicker(checking: Bool, hits: [Snapshot.GuardHit]) -> some View {
        if isSnapshot {
            standIn(showTitle(hits))
        } else {
            Picker("Show", selection: $show) {
                Text("Everything").tag("all")
                if checking {
                    Divider()
                    Text("All checks").tag("checks")
                    ForEach(checkKinds(hits), id: \.0) { Text("   \($0.1)").tag("check:\($0.0)") }
                }
                Divider()
                Text("All masked values").tag("masked")
                ForEach(Theme.typeOrder, id: \.self) { Text("   \(Theme.label($0))").tag("type:\($0)") }
            }
            .labelsHidden().fixedSize()
            .onChange(of: show) { _, s in if !(s == "masked" || s.hasPrefix("type:")) { source = .all } }
        }
    }

    @ViewBuilder private var sourcePicker: some View {
        if isSnapshot {
            standIn(source.title)
        } else {
            Picker("Found by", selection: $source) {
                ForEach(Finding.Source.allCases, id: \.self) { Text($0.title).tag($0) }
            }
            .labelsHidden().fixedSize()
        }
    }

    private func standIn(_ title: String) -> some View {
        Text(title).font(.km(.callout)).foregroundStyle(Theme.text)
            .padding(.horizontal, Space.s).frame(height: 26)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
    }
}

extension Snapshot.GuardHit {
    /// For the Logs page's search: the check, what it flagged and where, and its text when kept.
    func matches(_ query: String, text: String?) -> Bool {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        if q.isEmpty { return true }
        return [checkName, checkTitle, flagged, cats.joined(separator: " "), agentName(client), providerName, model ?? "",
                outcome.0, source ?? "", path ?? "", text ?? ""].contains { $0.lowercased().contains(q) }
    }
}

private struct FindingCell: View {
    var finding: Finding
    var column: String
    @Environment(\.revealValues) private var reveal

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
        case "value": Text(f.shown(reveal)).font(.km(.mono)).foregroundStyle(Theme.masked).lineLimit(1).truncationMode(.middle)
        case "length": Text("\(f.chars)").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
        case "foundBy": FinderLabel(finding: f)
        case "context":
            if f.before == nil && f.after == nil {
                Text("—").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            } else {
                Text(contextLine(f, around: 14, reveal: reveal)).font(.km(.monoSmall)).truncationMode(.tail)
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
    @Environment(\.revealValues) private var reveal

    var body: some View {
        let f = finding
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Image(systemName: Theme.icon(f.type)).foregroundStyle(Theme.textSecondary)
                Text(Theme.label(f.type)).font(.km(.headline)).foregroundStyle(Theme.text)
                Text(f.shown(reveal)).font(.km(.mono).weight(.semibold)).foregroundStyle(Theme.masked).textSelection(.enabled)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: Space.s)
                OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
            }
            DetailColumns {
                Text(story(f)).font(.km(.body)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                DetailGrid {
                    if reveal {
                        DetailField("Value", f.value ?? "Not kept: masked before Original values was turned on, or before keepmum last started",
                                    mono: f.value != nil, tone: f.value != nil ? Theme.text : Theme.textSecondary)
                    }
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
                        Text("Not kept. Text around values is off (Settings › General › Logs page), this value was masked before it was turned on, or before keepmum last started: text is kept in memory only.")
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

    private func singular(_ type: String) -> String { valueName(type) }

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
        var value = AttributedString(f.shown(reveal))
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

/// One value of a type, in words: "secret", "email address".
func valueName(_ type: String) -> String {
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
