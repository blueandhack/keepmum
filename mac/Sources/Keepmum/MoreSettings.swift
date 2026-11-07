import SwiftUI

/// Hosts a shell command may send a rule's tokens to, besides its issuer's own (`keepmum destinations`).
struct ShellDestinationsSection: View {
    var engine: Engine
    @State private var rule = "github-token"
    @State private var host = ""
    @State private var confirming = false
    @Environment(\.isSnapshot) private var isSnapshot

    private var added: [(String, String)] {
        (engine.status?.destinations ?? [:]).sorted { $0.key < $1.key }.flatMap { r, hosts in hosts.map { (r, $0) } }
    }

    /// Rules with an issuer list first, then the other rules that find secrets.
    private var rules: [String] {
        guard let info = engine.destinationInfo else { return [rule] }
        let listed = info.rules.filter { !(info.issuers[$0] ?? []).isEmpty }
        return listed + info.rules.filter { !listed.contains($0) }
    }

    private var cleanHost: String {
        var h = host.trimmingCharacters(in: .whitespaces).lowercased()
        for prefix in ["https://", "http://"] where h.hasPrefix(prefix) { h.removeFirst(prefix.count) }
        if let slash = h.firstIndex(of: "/") { h = String(h[..<slash]) }
        while h.hasSuffix(".") { h.removeLast() }
        return h
    }

    var body: some View {
        FormSection(title: "Shell commands",
                    footer: "In a shell command, a token goes only to the hosts its issuer uses: a GitHub token to github.com, not to any other server. Add your own hosts, such as a company GitLab or GitHub Enterprise; subdomains are included. Adding a host is recorded in the audit log.") {
            if added.isEmpty {
                FormRow(title: "No hosts added", detail: "Each token goes only to its issuer's hosts, and to this Mac") { EmptyView() }
            } else {
                ForEach(added, id: \.1) { r, h in
                    HStack(spacing: Space.m) {
                        Text(r).font(.km(.mono)).foregroundStyle(Theme.textSecondary).frame(width: 200, alignment: .leading).lineLimit(1)
                        Image(systemName: "arrow.right").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                        Text(h).font(.km(.mono)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                        Spacer(minLength: Space.s)
                        IconButton(symbol: "xmark", help: "Remove \(h)") { Task { await engine.removeDestination(r, h) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                    .padding(.vertical, Space.s)
                    Hairline()
                }
            }
            if added.isEmpty { Hairline() }
            VStack(alignment: .leading, spacing: Space.xs) {
                HStack(spacing: Space.s + Space.xxs) {
                    if isSnapshot {
                        FieldStandIn(text: rule, mono: true).frame(width: 200)
                        FieldStandIn(text: "gitlab.corp.example", mono: true)
                    } else {
                        Picker("Rule", selection: $rule) {
                            ForEach(rules, id: \.self) { Text($0).tag($0) }
                        }
                        .labelsHidden().frame(width: 200)
                        TextField("Host", text: $host, prompt: Text("gitlab.corp.example"))
                            .textFieldStyle(.roundedBorder).font(.km(.mono))
                            .onSubmit { if !cleanHost.isEmpty { confirming = true } }
                    }
                    PillButton(title: "Add host", icon: "plus") { confirming = true }
                        .disabled(cleanHost.isEmpty || !engine.isBundled || engine.busy)
                }
                Text(issuerNote).font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, Space.s + Space.xxs)
        }
        .task { await engine.loadDestinationInfo() }
        .alert("Let shell commands send \(rule) tokens to \(cleanHost)?", isPresented: $confirming) {
            Button("Cancel", role: .cancel) {}
            Button("Add host", role: .destructive) {
                let r = rule, h = cleanHost
                Task {
                    await engine.addDestination(r, h)
                    if engine.error == nil { host = "" }
                }
            }
        } message: {
            Text("A command the model writes could then send the real token to \(cleanHost) and its subdomains. Add only hosts that are yours.")
        }
    }

    private var issuerNote: String {
        guard let info = engine.destinationInfo else { return "Pick the rule that finds the token, then the host it may go to." }
        let own = info.issuers[rule] ?? []
        if own.isEmpty {
            return "\(rule) has no issuer list, so keepmum does not check where it goes. Adding a host turns the check on: only the hosts you add, and this Mac, get it."
        }
        return "\(rule) already goes to \(own.joined(separator: ", "))."
    }
}

/// What the Logs page may show about masked values (`keepmum log`). Both stay in keepmum's memory.
struct LogsPageSection: View {
    var engine: Engine

    var body: some View {
        let previews = engine.status?.previews ?? true
        let context = engine.status?.context ?? true
        FormSection(title: "Logs page",
                    footer: "Kept in keepmum's memory until it exits and sent only to this app; never written to disk. The model provider never sees them.") {
            FormRow(title: "Value previews",
                    detail: previews ? "The first and last character of long masked values, such as g…w" : "Off: types and rules only") {
                Switch(isOn: previews, enabled: engine.isBundled && !engine.busy) { on in Task { await engine.setLog("previews", on) } }
                    .accessibilityLabel("Value previews")
            }
            Hairline()
            FormRow(title: "Text around values",
                    detail: context ? "The text before and after each masked value, and what a check flagged with what you had asked, every masked value in them shown as its type"
                                    : "Off: no text around masked values, and no flagged text, is kept") {
                Switch(isOn: context, enabled: engine.isBundled && !engine.busy) { on in Task { await engine.setLog("context", on) } }
                    .accessibilityLabel("Text around values")
            }
        }
    }
}

/// `keepmum doctor` from the app: read-only checks of this Mac, the model, keepmum's files, and
/// anything that routes an agent around keepmum.
struct DoctorSection: View {
    var engine: Engine

    var body: some View {
        FormSection(title: "Check this Mac",
                    footer: "The same checks as keepmum doctor. They change nothing; testing the model loads it once, which takes a few seconds.") {
            FormRow(title: summary.0, detail: summary.1, detailTone: summary.2) {
                PillButton(title: engine.doctorRunning ? "Checking…" : engine.doctor == nil ? "Run checks" : "Check again",
                           icon: "stethoscope", loading: engine.doctorRunning) { Task { await engine.runDoctor() } }
                    .disabled(!engine.isBundled)
            }
            if let checks = engine.doctor {
                Hairline()
                VStack(alignment: .leading, spacing: Space.xs + Space.xxs) {
                    ForEach(checks) { c in
                        HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                            Image(systemName: icon(c.status)).font(.km(.caption)).foregroundStyle(tone(c.status)).frame(width: 14)
                            Text(c.text).font(.km(.callout)).foregroundStyle(c.status == "ok" ? Theme.textSecondary : Theme.text)
                                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, Space.s + Space.xxs)
            }
        }
    }

    private var summary: (String, String?, Color) {
        guard let checks = engine.doctor else { return ("Not checked yet", nil, Theme.textSecondary) }
        let failed = checks.filter { $0.status == "fail" }.count, warned = checks.filter { $0.status == "warn" }.count
        if failed > 0 { return ("\(failed) check\(failed == 1 ? "" : "s") failed", "See below", Theme.blocked) }
        if warned > 0 { return ("All checks passed", "\(warned) to look at", Theme.warning) }
        return ("All checks passed", "\(checks.count) checks", Theme.textSecondary)
    }

    private func icon(_ status: String) -> String {
        switch status {
        case "ok": "checkmark.circle.fill"
        case "warn": "exclamationmark.circle.fill"
        default: "xmark.circle.fill"
        }
    }

    private func tone(_ status: String) -> Color {
        switch status {
        case "ok": Theme.restored
        case "warn": Theme.warning
        default: Theme.blocked
        }
    }
}

/// Every built-in rule, with a switch each (`keepmum rules builtin`, `rules off|on`).
struct AllRulesSheet: View {
    var engine: Engine
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""
    @State private var confirming: String?
    @Environment(\.isSnapshot) private var isSnapshot

    private var shown: [Engine.BuiltinRule] {
        let q = search.trimmingCharacters(in: .whitespaces).lowercased()
        return engine.builtinRules.filter { q.isEmpty || $0.id.contains(q) || Theme.label($0.type).lowercased().contains(q) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack {
                VStack(alignment: .leading, spacing: Space.xxs) {
                    Text("Built-in rules").font(.km(.headline)).foregroundStyle(Theme.text)
                    let off = engine.builtinRules.filter { !$0.on }.count
                    Text("\(engine.builtinRules.count) rules\(off > 0 ? ", \(off) turned off" : ""). A rule turned off stops masking only what it alone finds.")
                        .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                PillButton(title: "Done", prominent: true) { dismiss() }
            }
            if isSnapshot {
                FieldStandIn(text: "Search rules or types")
            } else {
                TextField("Search", text: $search, prompt: Text("Search rules or types"))
                    .textFieldStyle(.roundedBorder).font(.km(.callout))
            }
            let list = LazyVStack(spacing: 0) {
                    ForEach(shown) { r in
                        HStack(spacing: Space.s) {
                            Text(r.id).font(.km(.mono)).foregroundStyle(r.on ? Theme.text : Theme.textSecondary).lineLimit(1)
                            Chip(text: Theme.label(r.type), tone: r.type == "SECRET" ? Theme.masked : Theme.series[1])
                            Spacer()
                            Switch(isOn: r.on, enabled: engine.isBundled && !engine.busy) { on in
                                if on { Task { await set(r.id, off: false) } } else if r.type == "SECRET" { confirming = r.id } else { Task { await set(r.id, off: true) } }
                            }
                            .accessibilityLabel("\(r.id) on")
                        }
                        .padding(.vertical, Space.xs + Space.xxs)
                        Hairline()
                    }
                    if shown.isEmpty { EmptyNote(text: engine.builtinRules.isEmpty ? "Loading…" : "No rule matches") }
            }
            if isSnapshot {
                list
                Spacer(minLength: 0)
            } else {
                ScrollView { list }
            }
            if let error = engine.error {
                Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked)
            }
        }
        .padding(Space.l)
        .frame(width: 520, height: 560)
        .task { await engine.loadBuiltinRules() }
        .alert("Turn off \(confirming ?? "this rule")?", isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })) {
            Button("Keep it on", role: .cancel) { confirming = nil }
            Button("Turn off", role: .destructive) {
                if let rule = confirming { Task { await set(rule, off: true, confirmed: true) } }
                confirming = nil
            }
        } message: {
            Text("This rule finds secrets. With it off, what only it finds reaches the model provider as it is; other rules and the local model still apply. To let one value through, add an exception instead.")
        }
    }

    private func set(_ rule: String, off: Bool, confirmed: Bool = false) async {
        await engine.setRuleOff(rule, off, confirmed: confirmed)
        await engine.loadBuiltinRules()
    }
}

/// Settings › General › Updates: Sparkle's settings (Updater.swift). Development builds have no feed.
struct UpdatesSection: View {
    var updates = Updates.shared

    var body: some View {
        FormSection(title: "Updates",
                    footer: "A check downloads keepmum's update list from keepmum.app, which sees your IP address and keepmum's version and counts checks per day, keeping no addresses; nothing else is sent. Updates are notarized by Apple and signed by keepmum, and both are verified before one is installed.") {
            FormRow(title: "keepmum \(Updates.version)", detail: lastChecked) {
                PillButton(title: "Check now", icon: "arrow.triangle.2.circlepath") { updates.checkNow() }
                    .disabled(!updates.isConfigured || !updates.canCheck)
            }
            if updates.isConfigured {
                Hairline()
                FormRow(title: "Check automatically", detail: "Once a day; a new version shows in the menu bar panel") {
                    Switch(isOn: updates.automaticallyChecks) { updates.setAutomaticallyChecks($0) }
                        .accessibilityLabel("Check for updates automatically")
                }
                Hairline()
                FormRow(title: "Download and install automatically",
                        detail: "A new version downloads in the background and installs when keepmum quits") {
                    Switch(isOn: updates.automaticallyDownloads, enabled: updates.automaticallyChecks) { updates.setAutomaticallyDownloads($0) }
                        .accessibilityLabel("Download and install updates automatically")
                }
            }
        }
    }

    private var lastChecked: String {
        guard updates.isConfigured else { return "This build has no update feed: updates come with the release builds" }
        guard let date = updates.lastCheck else { return "Not checked yet" }
        return "Last checked \(date.formatted(.relative(presentation: .named)))"
    }
}
