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
        case let other: other
        }
    }
    var date: Date { Date(timeIntervalSince1970: ts) }

    var foundBy: String {
        switch detector {
        case "rules": rule
        case "user": "your rule: \(rule)"
        case "known": "seen earlier"
        default: "local model"
        }
    }

    var clientName: String {
        switch client ?? "claude-code" {
        case "claude-code": "Claude Code"
        case "codex": "Codex"
        case let other: other
        }
    }

    func matches(_ query: String) -> Bool {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        if q.isEmpty { return true }
        return [preview ?? "", type, Theme.label(type), foundBy, clientName, outcome].contains { $0.lowercased().contains(q) }
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
        return rows.enumerated().map { i, r in
            Finding(ts: now - Double(i * 47 + 12), seq: 0, type: r.0, detector: r.1, rule: r.2, chars: r.4, preview: r.3,
                    outcome: i == 4 ? "observed" : "masked", client: r.5, provider: r.5 == "codex" ? "openai" : "anthropic",
                    before: around[i].0, after: around[i].1)
        }
    }()
}

/// Logs: every value keepmum masked in new text, newest first, searchable. Kept in keepmum's memory
/// only, so the list starts empty after keepmum restarts.
struct LogsPage: View {
    var store: StatsStore
    @State private var query = ""
    @State private var type = "ALL"
    /// The row whose details are open.
    @State private var selected: String?
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let all = store.sampleMode ? Finding.samples : store.findings
        let shown = all.filter { (type == "ALL" || $0.type == type) && $0.matches(query) }
        VStack(spacing: Space.l) {
            PageHeader(title: "Logs", subtitle: "What keepmum masked in new text, newest first. Click a row for its details. Kept in memory until keepmum exits.")
            Card(title: "Masked values", subtitle: all.isEmpty ? "Nothing masked since keepmum started" : "\(shown.count) of \(all.count)") {
                HStack(spacing: Space.s) {
                    search
                    typePicker
                }
            } content: {
                VStack(spacing: 0) {
                    if !store.previewsOn {
                        Text("Previews are off ([log] previews = false): types and rules only.")
                            .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, Space.s)
                    }
                    ColumnHeader {
                        Text("Time").frame(width: 70, alignment: .leading)
                        Text("Agent").frame(width: 92, alignment: .leading)
                        Text("Type").frame(width: 100, alignment: .leading)
                        Text("Value").frame(width: 168, alignment: .leading)
                        Text("Length").frame(width: 50, alignment: .trailing)
                        Text("Found by").frame(maxWidth: .infinity, alignment: .leading).padding(.leading, Space.s)
                        Text("Outcome").frame(width: 80, alignment: .leading)
                    }
                    .padding(.horizontal, Space.xs)  // in line with the rows' highlight inset
                    if isSnapshot {
                        ForEach(shown.prefix(14)) { row($0) }
                    } else if !shown.isEmpty {
                        ScrollView { LazyVStack(spacing: 0) { ForEach(shown) { row($0) } } }
                    }
                    if shown.isEmpty {
                        EmptyNote(text: all.isEmpty ? "Nothing yet. Values masked in new text appear here; the list starts empty each time keepmum starts."
                                                    : "No match")
                    }
                    Spacer(minLength: 0)
                }
            }
        }
        .task(id: store.sampleMode) {
            while !Task.isCancelled {
                await store.loadFindings()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private var search: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Image(systemName: "magnifyingglass").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            if isSnapshot {
                Text("Search").font(.km(.callout)).foregroundStyle(Theme.textTertiary)
            } else {
                TextField("Search", text: $query, prompt: Text("Search: g…w, email, github-token, Codex"))
                    .textFieldStyle(.plain).font(.km(.callout))
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Space.s).frame(width: 260, height: 26)
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

    private func row(_ f: Finding) -> some View {
        FindingRow(finding: f, selected: Binding(get: { selected == f.id }, set: { selected = $0 ? f.id : nil })) {
            cells(f)
        }
    }

    @ViewBuilder private func cells(_ f: Finding) -> some View {
            Text(f.date, format: .dateTime.hour().minute().second())
                .font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary).frame(width: 70, alignment: .leading)
            HStack(spacing: Space.s - Space.xxs) {
                Circle().fill(clientColor(f.client ?? "claude-code")).frame(width: 7, height: 7)
                Text(f.clientName).lineLimit(1)
            }
            .font(.km(.callout)).foregroundStyle(Theme.text).frame(width: 92, alignment: .leading)
            HStack(spacing: Space.s - Space.xxs) {
                Image(systemName: Theme.icon(f.type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 14)
                Text(Theme.label(f.type))
            }
            .font(.km(.callout)).foregroundStyle(Theme.text).frame(width: 100, alignment: .leading)
            Text(f.masked).font(.km(.mono)).foregroundStyle(Theme.masked).lineLimit(1)
                .frame(width: 168, alignment: .leading)
                .help(f.preview == nil ? "Previews are off: only the length is shown"
                      : f.preview == "•••" ? "Shorter than 8 characters: none of it is shown" : "First and last character only")
            Text("\(f.chars)").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                .frame(width: 50, alignment: .trailing)
            Text(f.foundBy).font(f.detector == "rules" ? .km(.mono) : .km(.callout))
                .foregroundStyle(f.detector == "known" ? Theme.textSecondary : Theme.text).lineLimit(1).truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.leading, Space.s)
            OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
                .help(f.outcome == "observed" ? "Observe mode: sent unmasked, only counted" : "")
                .frame(width: 80, alignment: .leading)
    }
}

/// A Logs row: hover to highlight, click for its details in a popover.
private struct FindingRow<Cells: View>: View {
    var finding: Finding
    @Binding var selected: Bool
    @ViewBuilder var cells: Cells
    @State private var hovering = false

    var body: some View {
        Button { selected.toggle() } label: {
            VStack(spacing: 0) {
                HStack(spacing: Space.m) { cells }
                    .padding(.vertical, Space.s - 1).padding(.horizontal, Space.xs)
                    .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                        .fill(selected ? Theme.accentSoft : hovering ? Theme.fill : .clear))
                    .contentShape(Rectangle())
                Hairline()
            }
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .popover(isPresented: $selected, arrowEdge: .bottom) {
            // Opaque, like the menu panel: the theme's colors do not adapt to a material.
            FindingDetail(finding: finding).frame(width: 440).background(Theme.canvas)
                .presentationBackground(Theme.canvas)
        }
        .accessibilityHint("Shows the details of this value")
    }
}

/// One masked value in full: everything the feed knows about it, and the text around it.
struct FindingDetail: View {
    var finding: Finding

    var body: some View {
        let f = finding
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                Image(systemName: Theme.icon(f.type)).foregroundStyle(Theme.textSecondary)
                Text(Theme.label(f.type)).font(.km(.headline)).foregroundStyle(Theme.text)
                Spacer()
                OutcomeLabel(kind: f.outcome == "observed" ? .observed : .masked)
            }
            Text(f.masked).font(.km(.mono).weight(.semibold)).foregroundStyle(Theme.masked)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Space.l, verticalSpacing: Space.s) {
                field("Found by", f.foundBy, mono: f.detector == "rules")
                field("Length", "\(f.chars) characters")
                field("Agent", f.clientName)
                field("Sent to", f.providerName)
                field("Time", f.date.formatted(date: .abbreviated, time: .standard))
            }
            VStack(alignment: .leading, spacing: Space.s) {
                Text("Context").eyebrow()
                if f.before != nil || f.after != nil {
                    Text(contextText(f)).font(.km(.mono)).lineSpacing(3)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(Space.m)
                        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.surface))
                    Text(f.outcome == "observed"
                         ? "Observe mode sent this text unmasked; here every masked value is shown as its type, like ‹SECRET›."
                         : "As the provider received it, with every masked value shown as its type, like ‹SECRET›.")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Not kept. Context is off ([log] context = false), or this value was masked before it was turned on.")
                        .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(Space.l + Space.xs)
    }

    private func field(_ key: String, _ value: String, mono: Bool = false) -> some View {
        GridRow {
            Text(key).font(.km(.callout)).foregroundStyle(Theme.textSecondary).gridColumnAlignment(.trailing)
            Text(value).font(mono ? .km(.mono) : .km(.callout)).foregroundStyle(Theme.text).textSelection(.enabled)
        }
    }

    /// Before and after in the secondary color, the value itself highlighted, other values (‹TYPE›) tinted.
    private func contextText(_ f: Finding) -> AttributedString {
        var out = plain(f.before ?? "")
        var value = AttributedString(f.masked)
        value.foregroundColor = Theme.masked
        value.backgroundColor = Theme.soft(Theme.masked)
        value.font = .km(.mono).weight(.semibold)
        out += value
        out += plain(f.after ?? "")
        return out
    }

    private func plain(_ s: String) -> AttributedString {
        var out = AttributedString()
        var rest = Substring(s)
        while let open = rest.firstIndex(of: "‹"), let close = rest[open...].firstIndex(of: "›") {
            var text = AttributedString(String(rest[..<open]))
            text.foregroundColor = Theme.textSecondary
            var mark = AttributedString(String(rest[open...close]))
            mark.foregroundColor = Theme.masked
            out += text
            out += mark
            rest = rest[rest.index(after: close)...]
        }
        var tail = AttributedString(String(rest))
        tail.foregroundColor = Theme.textSecondary
        out += tail
        return out
    }
}
