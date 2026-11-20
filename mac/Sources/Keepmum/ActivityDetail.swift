import SwiftUI

// The Activity page: every request in a table the user sizes, and the selected one in full beside it
// or in its place.

struct ActivityPage: View {
    var store: StatsStore
    /// For Show values to turn [log] values on; nil in snapshots.
    var engine: Engine? = nil
    @State private var filter: Snapshot.Event.Kind? = nil
    @State private var selected: String?
    @Environment(\.isSnapshot) private var isSnapshot

    /// `selected`: a row whose details show (the snapshot renderer's).
    init(store: StatsStore, engine: Engine? = nil, selected: String? = nil) {
        self.store = store
        self.engine = engine
        _selected = State(initialValue: selected)
    }

    var body: some View {
        let s = store.snapshot
        let events = store.newerThanHistory(kind: filter) + store.history
        VStack(spacing: Space.l) {
            PageHeader(title: "Activity", subtitle: store.valuesShown ? "Every request, newest first. Masked values show as they were, on this Mac only."
                                                                       : "Every request, newest first. Values show as their shape unless you choose Show values.") {
                ValuesButton(store: store, engine: engine)
            }
            ListDetail(key: "activity", ids: events.map(\.id), selection: $selected, back: "Requests",
                         hint: "Click a request, or use ↑ ↓ and Return, for what keepmum did with it and why.") {
                VStack(spacing: Space.l) {
                    Card(title: "Last hour", subtitle: "Masked values per minute; a red dot is a blocked request") {
                        MaskedTimeline(minutes: s.timeline).frame(height: 110)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    Card(title: "Requests", subtitle: subtitle(events.count)) {
                        Segmented(options: [(Optional<Snapshot.Event.Kind>.none, "All"), (.masked, "Masked"), (.clean, "Clean"),
                                            (.blocked, "Blocked"), (.flagged, "Flagged"), (.observed, "Observed")], selection: $filter)
                    } content: {
                        DataTable(key: "activity", columns: Self.columns, items: events, selection: $selected,
                                  empty: "No requests yet", snapshotRows: 11, pageKey: filter?.rawValue ?? "",
                                  footer: !store.canLoadOlder ? nil : { Task { await store.loadHistory(kind: filter, more: true) } }) { e, column in
                            ActivityCell(event: e, column: column)
                        }
                    }
                }
            } detail: {
                if let e = selectedEvent(events) {
                    ActivityDetail(event: e, findings: matchedFindings(e, in: store.sampleMode ? Finding.samples : store.findings),
                                   previewsOn: store.previewsOn)
                }
            }
        }
        .environment(\.revealValues, store.valuesShown)
        .task(id: "\(filter?.rawValue ?? ""):\(store.sampleMode)") { await store.loadHistory(kind: filter) }
        // The values masked in a request are in the Logs feed: kept in step for the details.
        .task(id: store.sampleMode) {
            while !Task.isCancelled && !isSnapshot {
                await store.loadFindings()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func selectedEvent(_ events: [Snapshot.Event]) -> Snapshot.Event? {
        selected.flatMap { s in events.first { $0.id == s } }
    }

    static let columns: [DataColumn] = [
        DataColumn(id: "time", title: "Time", width: 118, min: 70, hideable: false),
        DataColumn(id: "outcome", title: "Outcome", width: 92, min: 60),
        DataColumn(id: "agent", title: "Agent", width: 106, min: 50),
        DataColumn(id: "provider", title: "Sent to", width: 80, min: 50, hiddenByDefault: true),
        DataColumn(id: "endpoint", title: "Endpoint", width: 150, min: 60, hiddenByDefault: true),
        DataColumn(id: "masked", title: "Masked", width: 200, min: 100, flexible: true),
        DataColumn(id: "checks", title: "Checks", width: 150, min: 50),
        DataColumn(id: "tools", title: "Tools", width: 130, min: 50),
        DataColumn(id: "restored", title: "Restored", width: 66, min: 44, alignment: .trailing),
        DataColumn(id: "status", title: "Status", width: 56, min: 40, alignment: .trailing, hiddenByDefault: true),
        DataColumn(id: "scan", title: "Scan", width: 60, min: 40, alignment: .trailing),
    ]

    private func subtitle(_ n: Int) -> String {
        let what = switch filter {
        case .masked: "Requests whose new text had something to mask"
        case .clean: "Nothing new to mask (earlier turns may still hold masked values)"
        case .blocked: "Never sent: scanning failed, the endpoint is not scanned, or a check refused it"
        case .flagged: "What the optional checks flagged: sent with a warning, or refused (Settings › Checks)"
        case .observed: "Observe mode: sent unmasked, only counted"
        case .local: "Answered by your local model because a check flagged them: never sent (Settings › Checks)"
        case nil: "Kept across restarts in ~/.config/keepmum/history.jsonl"
        }
        return "\(what) · \(n.formatted()) shown"
    }
}

/// The values masked in a request's new text, from the Logs feed: the findings the same agent's
/// request recorded when it was scanned (its event is recorded when the reply ends, so a little
/// later), as many as the event's new values. nil when the feed has no such batch: it is in memory
/// only, and starts empty each time keepmum starts.
func matchedFindings(_ e: Snapshot.Event, in all: [Finding]) -> [Finding]? {
    guard let n = e.new, n > 0 else { return nil }
    let candidates = all.filter { $0.client == e.client && $0.ts <= e.ts + 0.001 && $0.ts > e.ts - 900 }
    let batches = Dictionary(grouping: candidates, by: \.ts)
    guard let ts = batches.filter({ $0.value.count == n }).keys.max() else { return nil }
    return batches[ts]?.sorted { $0.seq < $1.seq }
}

/// The request a masked value went out in: the first event after it, from the same agent, whose
/// new values are the value's batch.
func matchedEvent(_ f: Finding, findings: [Finding], events: [Snapshot.Event]) -> Snapshot.Event? {
    let batch = findings.filter { $0.ts == f.ts && $0.client == f.client }.count
    return events.filter { $0.client == f.client && $0.ts >= f.ts - 0.001 && $0.ts < f.ts + 900 && $0.new == batch }
        .min { $0.ts < $1.ts }
}

/// A row's date and time, so rows kept across days read apart ("Nov 13 19:11:12").
struct TimeCell: View {
    var date: Date
    var body: some View {
        Text(verbatim: date.formatted(.dateTime.month(.abbreviated).day()) + " " + date.formatted(.dateTime.hour().minute().second()))
            .font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary)
    }
}

/// An agent: its color and name.
struct AgentCell: View {
    var client: String?
    var body: some View {
        HStack(spacing: Space.s - Space.xxs) {
            Circle().fill(clientColor(client ?? "claude-code")).frame(width: 7, height: 7)
            Text(agentName(client))
        }
        .font(.km(.callout)).foregroundStyle(Theme.text)
    }
}

func providerName(_ provider: String?) -> String {
    switch provider ?? "anthropic" {
    case "anthropic": "Anthropic"
    case "openai": "OpenAI"
    case "google": "Google"
    case let other: other
    }
}

private struct ActivityCell: View {
    var event: Snapshot.Event
    var column: String

    var body: some View {
        let e = event
        switch column {
        case "time": TimeCell(date: e.date)
        case "outcome": OutcomeLabel(kind: e.kind)
        case "agent": AgentCell(client: e.client)
        case "provider": Text(providerName(e.provider)).font(.km(.callout)).foregroundStyle(Theme.text)
        case "endpoint": Text(e.path).font(.km(.monoSmall)).foregroundStyle(Theme.text).truncationMode(.middle)
        case "masked":
            if e.redacted.isEmpty {
                Text("—").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            } else {
                // Clean, but carrying values masked in earlier turns: drawn quieter.
                let earlier = e.kind == .clean
                HStack(spacing: Space.s + Space.xxs) {
                    ForEach(Theme.ordered(e.redacted), id: \.0) { type, n in
                        HStack(spacing: Space.xs) {
                            Image(systemName: Theme.icon(type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                            Text("\(Theme.label(type)) \(n)").font(.km(.callout)).foregroundStyle(earlier ? Theme.textSecondary : Theme.text)
                        }
                        .fixedSize()
                    }
                }
            }
        case "checks":
            if e.checks.isEmpty {
                Text("—").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            } else {
                HStack(spacing: Space.xs) {
                    ForEach(e.checks, id: \.self) { c in
                        Chip(text: checkLabel(c), tone: c.action == "blocked" ? Theme.blocked : Theme.warning)
                    }
                }
            }
        case "tools":
            let tools = e.tools ?? []
            if tools.isEmpty {
                Text("—").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            } else {
                HStack(spacing: Space.xs) {
                    ForEach(tools, id: \.self) { t in
                        Chip(text: "\(toolLabel(t.tool)) \(t.action == "restore" ? "got" : "kept") \(t.values)",
                             tone: t.action == "restore" ? Theme.restored : e.keptTools.contains(t) ? Theme.warning : Theme.textSecondary)
                    }
                }
            }
        case "restored":
            let restored = e.restored.values.reduce(0, +)
            Text(restored > 0 ? "\(restored)" : "—")
                .font(.km(.callout)).monospacedDigit().foregroundStyle(restored > 0 ? Theme.restored : Theme.textSecondary)
        case "status":
            Text("\(e.status)").font(.km(.callout)).monospacedDigit()
                .foregroundStyle(e.status >= 400 ? Theme.blocked : Theme.textSecondary)
        case "scan":
            Text(milliseconds(e.scanMs)).font(.km(.callout)).monospacedDigit()
                .foregroundStyle((e.scanMs ?? 0) > LatencyChart.budget ? Theme.warning : Theme.textSecondary)
        default: EmptyView()
        }
    }
}

extension Theme {
    /// Counts by type in the app's order of types, any other type after them.
    static func ordered(_ counts: [String: Int]) -> [(String, Int)] {
        counts.sorted { a, b in
            let i = typeOrder.firstIndex(of: a.key) ?? typeOrder.count, j = typeOrder.firstIndex(of: b.key) ?? typeOrder.count
            return i == j ? a.key < b.key : i < j
        }
    }
}

// MARK: - Details

/// One request in full: what keepmum did with it and why, what it masked, what the tools got, and
/// what the checks flagged.
struct ActivityDetail: View {
    var event: Snapshot.Event
    /// The values masked in its new text, from the Logs feed; nil when they are not there.
    var findings: [Finding]?
    var previewsOn = true

    var body: some View {
        let e = event
        let agent = agentName(e.client)
        let provider = providerName(e.provider)
        VStack(alignment: .leading, spacing: Space.l) {
            HStack(spacing: Space.s) {
                OutcomeLabel(kind: e.kind).font(.km(.headline))
                Text(e.path).font(.km(.mono)).foregroundStyle(Theme.textSecondary).lineLimit(1).truncationMode(.middle)
                    .textSelection(.enabled)
            }
            DetailColumns {
                Text(story(e, agent: agent, provider: provider)).font(.km(.body)).foregroundStyle(Theme.text)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                DetailGrid {
                    DetailField("Time", detailTime(e.date))
                    DetailField("Agent", agent)
                    DetailField(e.kind == .blocked || e.kind == .local ? "Meant for" : "Sent to", provider)
                    DetailField("Endpoint", e.path, mono: true)
                    DetailField("Status", statusText(e.status), tone: e.status >= 400 ? Theme.blocked : Theme.text)
                    DetailField("Scan time", scanText(e.scanMs),
                                tone: (e.scanMs ?? 0) > LatencyChart.budget ? Theme.warning : Theme.text)
                    DetailField("Masked", maskedText(e))
                    DetailField("Restored", restoredText(e), tone: e.restored.isEmpty ? Theme.text : Theme.restored)
                    if let u = e.unresolved, u > 0 {
                        DetailField("Not restored", "\(u) placeholder\(u == 1 ? "" : "s") in the reply keepmum could not put back "
                                    + "(made by another keepmum, or before a restart): \(agent) got them as placeholders.", tone: Theme.warning)
                    }
                    if let h = e.handedBack { DetailField("Hand-back", handBackText(h, provider: provider)) }
                    if let f = e.flight { DetailField("Request no.", "\(f) since keepmum started") }
                }
                if let advice = advice(e) {
                    DetailSection(title: "What to do") {
                        Text(advice).font(.km(.callout)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                    }
                }
            } right: {
                if !e.redacted.isEmpty { maskedSection(e) }
                if !(e.tools ?? []).isEmpty { toolsSection(e) }
                if !e.checks.isEmpty { checksSection(e) }
                if e.redacted.isEmpty && (e.tools ?? []).isEmpty && e.checks.isEmpty {
                    DetailSection(title: "Values") {
                        Text("Nothing in this request was masked, no tool call held a placeholder, and no check flagged it.")
                            .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(Space.l + Space.xs)
    }

    // MARK: Sections

    private func maskedSection(_ e: Snapshot.Event) -> some View {
        DetailSection(title: "Masked in this request") {
            VStack(alignment: .leading, spacing: Space.xs) {
                ForEach(Theme.ordered(e.redacted), id: \.0) { type, n in
                    HStack(spacing: Space.s) {
                        Image(systemName: Theme.icon(type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 16)
                        Text(Theme.label(type)).font(.km(.callout)).foregroundStyle(Theme.text).frame(width: 90, alignment: .leading)
                        Text("\(n)").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.text)
                    }
                }
            }
            if let new = e.new, new > 0 {
                Text("New text in this request").eyebrow().padding(.top, Space.s)
                if let findings {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(findings) { f in FoundValue(finding: f) }
                    }
                    Text("From the Logs page: the values the same agent's request had when it was scanned, as many as it masked anew.")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("\(new) value\(new == 1 ? "" : "s"). Their details are no longer on the Logs page: it keeps them in memory only, "
                         + "and starts empty each time keepmum starts.")
                        .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func toolsSection(_ e: Snapshot.Event) -> some View {
        DetailSection(title: "Tool calls with placeholders") {
            VStack(alignment: .leading, spacing: Space.s) {
                ForEach(e.tools ?? [], id: \.self) { t in
                    VStack(alignment: .leading, spacing: Space.xxs) {
                        HStack(spacing: Space.s) {
                            Chip(text: t.action == "restore" ? "Got real values" : "Kept placeholders",
                                 tone: t.action == "restore" ? Theme.restored : e.keptTools.contains(t) ? Theme.warning : Theme.textSecondary)
                            Text(t.tool).font(.km(.mono)).foregroundStyle(Theme.text).textSelection(.enabled)
                        }
                        Text(toolText(t)).font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                }
            }
        }
    }

    private func checksSection(_ e: Snapshot.Event) -> some View {
        DetailSection(title: "Checks") {
            VStack(alignment: .leading, spacing: Space.s) {
                ForEach(e.checks, id: \.self) { c in
                    VStack(alignment: .leading, spacing: Space.xxs) {
                        Chip(text: checkLabel(c), tone: c.action == "blocked" ? Theme.blocked : Theme.warning)
                        Text(checkHelp(c)).font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                }
                Text("The Logs page has each hit's text and what you had asked, while keepmum runs.")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
        }
    }

    // MARK: Words

    /// What happened to the request, in a sentence or two.
    private func story(_ e: Snapshot.Event, agent: String, provider: String) -> String {
        let total = e.redacted.values.reduce(0, +)
        let new = e.new ?? 0
        let carried = max(total - new, 0)
        let carriedNote = carried > 0 ? " \(carried) more, from earlier turns that \(agent) sends again each time, were masked as before." : ""
        let failed = e.outcome == "upstream_error" ? " \(provider) could not be reached, so \(agent) got an error." : ""
        switch e.kind {
        case .masked:
            return "keepmum found \(new) value\(new == 1 ? "" : "s") to mask in the new text \(agent) sent, and \(provider) got "
                + "placeholders such as ‹SECRET› in their place.\(carriedNote)\(failed)"
        case .clean:
            return (total > 0 ? "Nothing new to mask.\(carriedNote)" : "Nothing to mask: \(provider) got the request as \(agent) wrote it.") + failed
        case .observed:
            return "Observe mode: \(provider) got this request unmasked. keepmum only counted the \(total) value\(total == 1 ? "" : "s") it would have masked."
        case .flagged:
            return "A check flagged this request after it went to \(provider) (Warn mode sends first and checks after).\(carriedNote)"
        case .local:
            return "A check flagged this request, so a model on this Mac answered it: \(provider) received nothing."
        case .blocked:
            if e.checks.contains(where: { $0.action == "blocked" }) {
                return "A check refused this request before it left this Mac: \(provider) received nothing, and \(agent) got an error saying why."
            }
            return switch e.status {
            case 400: "Never sent: keepmum could not read the request body, so it sent nothing rather than send it unscanned."
            case 500: "Never sent: scanning failed, and keepmum never sends a request it could not scan."
            case 404: "Never sent: no gateway by that name in ~/.config/keepmum/config.toml."
            default: "Never sent: keepmum does not scan this endpoint, so it refused to carry content there."
            }
        }
    }

    private func advice(_ e: Snapshot.Event) -> String? {
        if e.kind == .observed { return "Observe mode lets values out. Turn it off to have them masked." }
        if e.kind == .blocked && !e.checks.contains(where: { $0.action == "blocked" }) && e.status == 500 {
            return "Try again. If scanning keeps failing, Settings › Doctor shows what is wrong."
        }
        if !e.keptTools.isEmpty {
            return "A tool got placeholders where it may have needed real values. Settings › Tools sets what each tool may receive."
        }
        if (e.unresolved ?? 0) > 0 { return "Placeholders the agent got stay placeholders: ask it to read the value again." }
        return nil
    }

    private func statusText(_ status: Int) -> String {
        let phrase = switch status {
        case 200: "OK"
        case 400: "Bad request"
        case 403: "Forbidden"
        case 404: "Not found"
        case 500: "Error in keepmum"
        case 502: "The provider could not be reached"
        default: HTTPURLResponse.localizedString(forStatusCode: status).capitalized
        }
        return "\(status) \(phrase)"
    }

    private func scanText(_ ms: Double?) -> String {
        guard let ms else { return "Not scanned" }
        let over = ms > LatencyChart.budget ? ", over the \(Int(LatencyChart.budget)) ms budget" : ""
        return "\(milliseconds(ms)) before it left\(over); only text new in this request is scanned"
    }

    private func maskedText(_ e: Snapshot.Event) -> String {
        let total = e.redacted.values.reduce(0, +)
        guard total > 0 else { return "Nothing" }
        let new = e.new ?? 0
        return "\(total) value\(total == 1 ? "" : "s"): \(new) new, \(max(total - new, 0)) from earlier turns"
    }

    private func restoredText(_ e: Snapshot.Event) -> String {
        let n = e.restored.values.reduce(0, +)
        guard n > 0 else { return "Nothing: the reply held no placeholders to put back" }
        let types = Theme.ordered(e.restored).map { typeCount($0.1, $0.0) }.joined(separator: ", ")
        return "\(n) placeholder\(n == 1 ? "" : "s") in the reply turned back into real values on this Mac (\(types))"
    }

    private func handBackText(_ strategy: String, provider: String) -> String {
        switch strategy {
        case "answer": "The first request to carry a local answer back to \(provider), with a notice in the flagged result's place"
        case "summary": "The first request to carry a local summary of a flagged result to \(provider), in its place"
        case "rewrite": "The first request to carry a local rewrite of flagged text to \(provider), in its place"
        case "withhold": "The first request to carry a notice to \(provider) in a flagged result's place"
        default: strategy
        }
    }

    private func toolText(_ t: Snapshot.Event.ToolCall) -> String {
        let n = "\(t.values) value\(t.values == 1 ? "" : "s")"
        let types = (t.types ?? [:]).isEmpty ? ""
            : " (" + Theme.ordered(t.types ?? [:]).map { typeCount($0.1, $0.0) }.joined(separator: ", ") + ")"
        let why: String = switch t.reason ?? "" {
        case "local": "it runs on this Mac, so the real values went into its input"
        case "own": "the server had returned these values itself"
        case "allowed": "its setting lets these types through"
        case "placeholders": "its setting is placeholders only"
        case "secret": "secrets never go to a network tool"
        case "paused": "its tools changed since you trusted them, so it gets placeholders until you look again (Settings › Tools)"
        case "not_allowed": "its setting does not include these types (Settings › Tools)"
        case "too_many": "one call would have received more than 5 real values"
        case "destination": "the command would have sent a token to a host its issuer does not use; if that host is yours, add it in Settings › Tools › Shell commands"
        default: t.action == "restore" ? "real values went in" : "placeholders stayed"
        }
        return "\(n)\(types): \(why)."
    }
}

/// One masked value, in a list: its type, its shape, who found it, and the text around it.
struct FoundValue: View {
    var finding: Finding
    @Environment(\.revealValues) private var reveal

    var body: some View {
        let f = finding
        VStack(alignment: .leading, spacing: Space.xxs) {
            HStack(spacing: Space.s) {
                Image(systemName: Theme.icon(f.type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 16)
                Text(Theme.label(f.type)).font(.km(.callout)).foregroundStyle(Theme.text)
                Text(f.shown(reveal)).font(.km(.mono)).foregroundStyle(Theme.masked).lineLimit(1).truncationMode(.middle)
                    .textSelection(.enabled)
                Text("\(f.chars) chars").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                Text(f.foundBy).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            }
            if f.before != nil || f.after != nil {
                Text(contextLine(f, reveal: reveal)).font(.km(.monoSmall)).lineLimit(2).padding(.leading, 24)
                    .textSelection(.enabled)
            }
        }
        .padding(.vertical, Space.xs)
    }
}

/// The text around a value on one line: before, the value's shape, after, line breaks as spaces.
func contextLine(_ f: Finding, around: Int? = nil, reveal: Bool = false) -> AttributedString {
    func flat(_ s: String?) -> String { (s ?? "").replacingOccurrences(of: "\n", with: " ⏎ ") }
    var before = flat(f.before)
    if let around, before.count > around { before = "…" + before.suffix(around) }
    var out = quietTinted(before)
    var value = AttributedString(f.shown(reveal))
    value.foregroundColor = Theme.masked
    value.backgroundColor = Theme.soft(Theme.masked)
    out += value
    out += quietTinted(flat(f.after))
    return out
}

/// Text in the secondary color, its masked values (`‹SECRET›`) tinted.
func quietTinted(_ s: String) -> AttributedString {
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
