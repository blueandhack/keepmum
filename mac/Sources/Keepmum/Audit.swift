import AppKit
import SwiftUI

/// Audit: whether keepmum's tamper-evident log is intact, and every change to its protection,
/// newest first, with the ones that turned protection down marked. Requests are in the log too
/// (`keepmum audit list`); this page shows the rest. Counts and names only, never values.
struct AuditPage: View {
    var engine: Engine
    @State private var onlyWeakening = false
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let shown = engine.audit.filter { !onlyWeakening || $0.weakens }
        VStack(spacing: Space.l) {
            PageHeader(title: "Audit", subtitle: "A record keepmum cannot quietly change: every setting that turned protection down, and when keepmum ran.")
            chain
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
                    ForEach(check.problems.prefix(4), id: \.self) { p in
                        Text(p).font(.km(.callout)).foregroundStyle(Theme.text)
                    }
                    Text("Run `keepmum audit verify` for the full list.").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
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
        default: "circle"
        }
    }
}
