import AppKit
import SwiftUI

/// Audit: whether keepmum's tamper-evident log is intact, and every change to its protection,
/// newest first, with the ones that turned protection down marked. Requests are in the log too
/// (`keepmum audit list`); this page shows the rest. Counts and names only, never values.
struct AuditPage: View {
    var engine: Engine
    @State private var onlyWeakening = false
    /// A shorter retention, waiting for confirmation.
    @State private var shortening: Int?
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let shown = engine.audit.filter { !onlyWeakening || $0.weakens }
        VStack(spacing: Space.l) {
            PageHeader(title: "Audit", subtitle: "A record keepmum cannot quietly change: every setting that turned protection down, and when keepmum ran.")
            chain
            keeping
            Card(title: "Changes and coverage", subtitle: engine.audit.isEmpty ? "Nothing recorded yet" : "\(shown.count) of \(engine.audit.count), newest first") {
                PillButton(title: onlyWeakening ? "Show everything" : "Only what weakened protection",
                           icon: onlyWeakening ? "line.3.horizontal" : "exclamationmark.shield") { onlyWeakening.toggle() }
            } content: {
                VStack(spacing: 0) {
                    if isSnapshot {
                        ForEach(shown.prefix(9)) { row($0) }
                    } else if !shown.isEmpty {
                        ScrollView { LazyVStack(spacing: 0) { ForEach(shown) { row($0) } } }
                    }
                    if shown.isEmpty {
                        EmptyNote(text: engine.audit.isEmpty ? "Changes to keepmum's settings, protection and exclusions appear here, with each start and stop of the service."
                                                             : "Nothing weakened protection in this period.")
                    }
                    Spacer(minLength: 0)
                }
            }
        }
        .task {
            while !Task.isCancelled {
                await engine.loadAudit()
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    /// The chain check: intact, or the problems `keepmum audit verify` found.
    @ViewBuilder private var chain: some View {
        let check = engine.auditCheck
        let ok = check?.ok ?? true
        HStack(alignment: .center, spacing: Space.m) {
            Image(systemName: check == nil ? "seal" : ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                .font(.km(.metric)).foregroundStyle(check == nil ? Theme.textSecondary : ok ? Theme.restored : Theme.blocked)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(check == nil ? "Not checked yet" : ok ? "Log intact" : "Log changed outside keepmum")
                    .font(.km(.headline)).foregroundStyle(Theme.text)
                if let check, ok {
                    Text("\(check.entries.formatted()) entries chain together, and the log still holds what it held at the last check.")
                        .font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                } else if let check {
                    let list = VStack(alignment: .leading, spacing: Space.xxs) {
                        ForEach(check.problems, id: \.self) { p in
                            Text(p).font(.km(.callout)).foregroundStyle(Theme.text).textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if check.problems.count > 4 && !isSnapshot {
                        ScrollView { list.frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 120)
                    } else {
                        list
                    }
                    Text("\(check.problems.count) problem\(check.problems.count == 1 ? "" : "s"). Export the log to keep a copy of it as it is now.")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                } else {
                    Text("The Audit page checks the log each time it opens.").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                }
            }
            Spacer()
            if let last = check?.last {
                VStack(alignment: .trailing, spacing: Space.xs) {
                    Text("Head").font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
                    Text(verbatim: "\(last.seq):\(last.hash.prefix(16))").font(.km(.mono)).foregroundStyle(Theme.text)
                        .textSelection(.enabled)
                    PillButton(title: "Copy head", icon: "doc.on.doc") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("\(last.seq):\(last.hash)", forType: .string)
                    }
                    .help("Keep it outside this Mac; `keepmum audit verify --anchor` then shows whether the log was rewritten since")
                }
            }
        }
        .padding(Space.l)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(ok ? Theme.surface : Theme.soft(Theme.blocked)))
    }

    /// How long the log keeps entries (`keepmum audit keep`), and an export for a review or a ticket.
    private var keeping: some View {
        let days = engine.status?.keepDays ?? 90
        let options = Array(Set([30, 90, 180, 365, 730, days])).sorted()
        return HStack(alignment: .center, spacing: Space.m) {
            Image(systemName: "clock.arrow.circlepath").font(.km(.body)).foregroundStyle(Theme.textSecondary).frame(width: 20)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text("Retention").font(.km(.body)).foregroundStyle(Theme.text)
                Text(engine.exportNote ?? "Entries older than \(label(days: days)) are removed when the background service starts; the chain still checks.")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(2)
            }
            Spacer(minLength: Space.s)
            if isSnapshot {
                FieldStandIn(text: label(days: days)).frame(width: 110)
            } else {
                Picker("Keep entries for", selection: Binding(get: { days }, set: { new in
                    if new < days { shortening = new } else { Task { await engine.setAuditKeep(new) } }
                })) {
                    ForEach(options, id: \.self) { Text(label(days: $0)).tag($0) }
                }
                .labelsHidden().frame(width: 110)
                .disabled(!engine.isBundled || engine.busy)
            }
            PillButton(title: "Export JSONL…", icon: "square.and.arrow.up") { export(csv: false) }
                .help("Every entry as written, so its hashes still check: keepmum audit verify --file")
                .disabled(!engine.isBundled)
            PillButton(title: "Export CSV…") { export(csv: true) }
                .help("One row per entry, for reading in a spreadsheet")
                .disabled(!engine.isBundled)
        }
        .padding(.horizontal, Space.l).padding(.vertical, Space.m)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
        .alert("Keep entries for \(label(days: shortening ?? days)) only?", isPresented: Binding(get: { shortening != nil }, set: { if !$0 { shortening = nil } })) {
            Button("Keep \(label(days: days))", role: .cancel) { shortening = nil }
            Button("Shorten", role: .destructive) {
                if let d = shortening { Task { await engine.setAuditKeep(d) } }
                shortening = nil
            }
        } message: {
            Text("Older entries are removed the next time the background service starts, and the change is recorded as weakening protection.")
        }
    }

    private func label(days: Int) -> String {
        days % 365 == 0 ? "\(days / 365) year\(days == 365 ? "" : "s")" : "\(days) days"
    }

    private func export(csv: Bool) {
        let panel = NSSavePanel()
        let day = Date().formatted(.iso8601.year().month().day())
        panel.nameFieldStringValue = "keepmum-audit-\(day).\(csv ? "csv" : "jsonl")"
        panel.message = csv ? "Every entry, one row each. Counts and names only, never values."
                            : "Every entry as written: keepmum audit verify --file checks the copy. Counts and names only, never values."
        panel.prompt = "Export"
        if panel.runModal() == .OK, let url = panel.url {
            Task { await engine.exportAudit(to: url, csv: csv) }
        }
    }

    private func row(_ e: Engine.AuditEntry) -> some View {
        TableRow {
            Text(e.date, format: .dateTime.month(.abbreviated).day().hour().minute())
                .font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary).frame(width: 110, alignment: .leading)
            HStack(spacing: Space.s - Space.xxs) {
                Image(systemName: icon(e.event)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 14)
                Text(label(e.event))
            }
            .font(.km(.callout)).foregroundStyle(Theme.text).frame(width: 110, alignment: .leading)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(e.summary).font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(2)
                if let by = e.by {
                    Text(by).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if e.weakens { Chip(text: "Weakened", tone: Theme.warning) }
            Text(verbatim: "#\(e.seq)").font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary)
                .frame(width: 56, alignment: .trailing)
        }
    }

    private func label(_ event: String) -> String {
        switch event {
        case "config": "Settings"
        case "protect": "Protection"
        case "exclude": "Excluded"
        case "include": "Included"
        case "start": "Started"
        case "stop": "Stopped"
        case "pruned": "Retention"
        case "extension_removed": "Extension removed"
        case "extensions_kept": "Extensions kept"
        default: event.capitalized
        }
    }

    private func icon(_ event: String) -> String {
        switch event {
        case "config": "slider.horizontal.3"
        case "protect": "shield"
        case "exclude": "arrow.uturn.right"
        case "include": "arrow.uturn.left"
        case "start": "play.circle"
        case "stop": "stop.circle"
        case "pruned": "scissors"
        case "extension_removed", "extensions_kept": "puzzlepiece.extension"
        default: "circle"
        }
    }
}
