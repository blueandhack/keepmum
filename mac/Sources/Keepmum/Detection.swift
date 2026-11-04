import SwiftUI

/// The Detection page: what keepmum found, by type and by detector, and what gets masked.
/// Counts and your own settings only: it never shows a masked value.
struct DetectionPage: View {
    var store: StatsStore
    var engine: Engine
    @Environment(\.isSnapshot) private var isSnapshot
    @State private var showAllRules = false

    var body: some View {
        if isSnapshot {
            content
        } else {
            // Taller than the window once a notice shows at the top.
            ScrollView { content.padding(.bottom, Space.xl) }
                .sheet(isPresented: $showAllRules) { AllRulesSheet(engine: engine) }
        }
    }

    private var content: some View {
        let d = store.snapshot.detection
        return VStack(spacing: Space.l) {
            PageHeader(title: "Detection", subtitle: "What keepmum finds, and what you let through. Counts only.")
            HStack(alignment: .top, spacing: Space.m) {
                Card(title: "By type", subtitle: "Found in new text, then allowed by an exception, passed by your settings, or masked") {
                    TypeTable(detection: d, engine: engine)
                }
                VStack(spacing: Space.m) {
                    Card(title: "Who found it", subtitle: "Masked values by detector") {
                        SourceBars(sources: d.sources)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    Card(title: "Top rules", subtitle: "Masked values per rule") {
                        if engine.isBundled {
                            PillButton(title: "All rules…") { showAllRules = true }
                        }
                    } content: {
                        RuleList(rules: d.rules, engine: engine)
                    }
                }
                .frame(width: 330)
            }
            RulesCard(engine: engine)
                .frame(minHeight: 280, maxHeight: .infinity)
        }
    }
}

// MARK: - By type

private struct TypeTable: View {
    var detection: Detection
    var engine: Engine
    @State private var confirmSecrets = false

    private var pass: Set<String> { Set(engine.status?.pass ?? detection.pass) }
    private var piiOn: Bool { engine.status?.pii ?? true }

    var body: some View {
        VStack(spacing: 0) {
            ColumnHeader {
                Text("Type").frame(maxWidth: .infinity, alignment: .leading)
                Text("Found").frame(width: 56, alignment: .trailing)
                Text("Allowed").frame(width: 60, alignment: .trailing)
                Text("Passed").frame(width: 56, alignment: .trailing)
                Text("Masked").frame(width: 60, alignment: .trailing)
                Text("Mask it").frame(width: 70, alignment: .trailing)
            }
            ForEach(Theme.typeOrder, id: \.self) { type in row(type) }
            if !piiOn {
                Text("Personal data is off, so only secrets and your own rules are masked. Turn it on in Settings › Masking.")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).padding(.top, Space.s + Space.xxs)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = engine.error {
                Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked).padding(.top, Space.s - Space.xxs)
            }
        }
        .alert("Let secrets through unmasked?", isPresented: $confirmSecrets) {
            Button("Keep masking", role: .cancel) {}
            Button("Let secrets through", role: .destructive) { Task { await engine.setPass("SECRET", true, confirmed: true) } }
        } message: {
            Text("API keys, tokens and passwords would reach the model provider as they are. Your own rules still apply.")
        }
    }

    private func row(_ type: String) -> some View {
        let c = detection.types[type] ?? .init()
        let masked = !pass.contains(type) && (type == "SECRET" || piiOn)
        return TableRow {
            HStack(spacing: Space.s) {
                Image(systemName: Theme.icon(type)).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 16)
                Text(Theme.label(type)).font(.km(.body)).foregroundStyle(Theme.text)
                if pass.contains(type) { Chip(text: "passes through", tone: Theme.warning) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            number(c.found, width: 56)
            number(c.allowed, width: 60)
            number(c.passed, width: 56, color: Theme.warning)
            number(c.masked, width: 60, color: Theme.masked)
            Switch(isOn: masked, enabled: engine.isBundled && !engine.busy && (type == "SECRET" || piiOn)) { on in
                if !on && type == "SECRET" { confirmSecrets = true } else { Task { await engine.setPass(type, !on) } }
            }
            .accessibilityLabel("Mask \(Theme.label(type))")
            .frame(width: 70, alignment: .trailing)
        }
    }

    private func number(_ n: Int, width: CGFloat, color: Color = Theme.text) -> some View {
        Text(n > 0 ? n.compact : "—").font(.km(.body)).monospacedDigit()
            .foregroundStyle(n > 0 ? color : Theme.textSecondary).frame(width: width, alignment: .trailing)
    }
}

// MARK: - Who found it, top rules

private struct SourceBars: View {
    var sources: [String: Int]

    var body: some View {
        let rows: [(String, String, Color)] = [("rules", "Built-in rules", Theme.series[0]), ("model", "Local model", Theme.series[1]),
                                               ("user", "Your rules", Theme.series[2])]
        let total = max(rows.reduce(0) { $0 + (sources[$1.0] ?? 0) }, 1)
        VStack(spacing: Space.s + Space.xxs) {
            ForEach(rows, id: \.0) { key, label, color in
                let n = sources[key] ?? 0
                VStack(alignment: .leading, spacing: Space.xs) {
                    HStack {
                        Text(label).font(.km(.callout)).foregroundStyle(Theme.text)
                        Spacer()
                        Text("\(n.compact) · \(percent(n, of: total))").font(.km(.caption)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                    }
                    GeometryReader { geo in
                        Capsule().fill(Theme.fill)
                            .overlay(alignment: .leading) { Capsule().fill(color).frame(width: geo.size.width * CGFloat(n) / CGFloat(total)) }
                    }
                    .frame(height: 6)
                }
            }
        }
    }
}

private struct RuleList: View {
    var rules: [Detection.Rule]
    var engine: Engine
    /// A rule that finds secrets, waiting for confirmation before it is turned off.
    @State private var confirming: String?

    private var off: [String] { engine.status?.rules?.off ?? [] }

    var body: some View {
        VStack(spacing: 0) {
            let shown = rules.filter { !off.contains($0.rule) || $0.user }
            if shown.isEmpty {
                EmptyNote(text: "No rule has masked anything yet")
            }
            ForEach(shown) { r in
                HStack(spacing: Space.s) {
                    Text(r.rule).font(r.user ? .km(.callout) : .km(.mono)).foregroundStyle(Theme.text).lineLimit(1)
                    if r.user { Chip(text: "yours", tone: Theme.series[2]) }
                    Spacer()
                    Text(r.n.compact).font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                    if r.user {
                        Color.clear.frame(width: 22, height: 22)
                    } else {
                        IconButton(symbol: "nosign", help: "Turn off \(r.rule)") {
                            if r.type == "SECRET" || r.type == nil { confirming = r.rule } else { Task { await engine.setRuleOff(r.rule, true) } }
                        }
                        .disabled(!engine.isBundled || engine.busy)
                    }
                }
                .padding(.vertical, Space.xxs)
            }
            if !off.isEmpty {
                Text("Turned off").eyebrow()
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.top, Space.m).padding(.bottom, Space.xs)
                ForEach(off, id: \.self) { rule in
                    HStack(spacing: Space.s) {
                        Text(rule).font(.km(.mono)).foregroundStyle(Theme.textSecondary).strikethrough().lineLimit(1)
                        Spacer()
                        PillButton(title: "Turn on") { Task { await engine.setRuleOff(rule, false) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                    .padding(.vertical, Space.xxs)
                }
            }
            Spacer(minLength: 0)
        }
        .alert("Turn off \(confirming ?? "this rule")?", isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })) {
            Button("Keep it on", role: .cancel) { confirming = nil }
            Button("Turn off", role: .destructive) {
                if let rule = confirming { Task { await engine.setRuleOff(rule, true, confirmed: true) } }
                confirming = nil
            }
        } message: {
            Text("This rule finds secrets. With it off, what only it finds reaches the model provider as it is; other rules and the local model still apply. To let one value through, add an exception instead.")
        }
    }
}

// MARK: - Your rules

/// Patterns always masked, and exceptions never masked. Saved to keepmum's config; running
/// proxies apply them on their next request.
private struct RulesCard: View {
    var engine: Engine
    @State private var kind = 0  // 0: always mask, 1: never mask
    @State private var name = ""
    @State private var value = ""
    @State private var type = "SECRET"
    @State private var isPattern = false
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        let rules = engine.status?.rules ?? .init()
        Card(title: "Your rules", subtitle: "Always mask a pattern, or never mask something keepmum flags. Applies to the next request.") {
            HStack(alignment: .top, spacing: Space.l + Space.xs) {
                VStack(alignment: .leading, spacing: Space.s + Space.xxs) {
                    Segmented(options: [(0, "Always mask"), (1, "Never mask")], selection: $kind)
                    if kind == 0 {
                        HStack(alignment: .bottom, spacing: Space.s + Space.xxs) {
                            field("Name", text: $name, prompt: "Employee ID")
                            VStack(alignment: .leading, spacing: Space.xs) {
                                Text("Mask as").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                                if isSnapshot {
                                    FieldStandIn(text: Theme.label(type))
                                } else {
                                    Picker("Mask as", selection: $type) {
                                        ForEach(Theme.typeOrder, id: \.self) { Text(Theme.label($0)).tag($0) }
                                    }
                                    .labelsHidden()
                                }
                            }
                            .frame(width: 130)
                        }
                        field("Pattern", text: $value, prompt: "EMP-\\d{6}", mono: true)
                    } else {
                        field(isPattern ? "Pattern" : "Exact text", text: $value, prompt: isPattern ? "^demo-[a-z]+$" : "sk_test_docs_example", mono: true)
                        if !isSnapshot {
                            Toggle("Treat as a pattern (matches the whole value)", isOn: $isPattern)
                                .toggleStyle(.checkbox).font(.km(.callout))
                        }
                    }
                    HStack(spacing: Space.s + Space.xxs) {
                        PillButton(title: kind == 0 ? "Add rule" : "Add exception", icon: "plus", prominent: true) { add() }
                            .disabled(value.isEmpty || !engine.isBundled || engine.busy)
                        Text("Regular expressions; with a group, only group 1 is masked.")
                            .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(width: 340, alignment: .leading)
                Hairline(vertical: true)
                let rows = VStack(alignment: .leading, spacing: 0) {
                    if rules.mask.isEmpty && rules.allowStrings.isEmpty && rules.allowPatterns.isEmpty {
                        EmptyNote(text: "No rules yet. Built-in rules and the local model still apply.")
                    }
                    ForEach(Array(rules.mask.enumerated()), id: \.offset) { i, r in
                        ruleRow("Always mask", r.name, "/\(r.pattern)/ → \(Theme.label(r.type))") { await engine.removeRule("mask", i) }
                    }
                    ForEach(Array(rules.allowStrings.enumerated()), id: \.offset) { i, v in
                        ruleRow("Never mask", "Exact text", v) { await engine.removeRule("allow", i) }
                    }
                    ForEach(Array(rules.allowPatterns.enumerated()), id: \.offset) { i, v in
                        ruleRow("Never mask", "Pattern", "/\(v)/") { await engine.removeRule("allow-pattern", i) }
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                if isSnapshot {
                    rows
                } else {
                    ScrollView { rows }
                }
            }
        }
    }

    private func add() {
        let v = value, n = name.isEmpty ? "rule \((engine.status?.rules?.mask.count ?? 0) + 1)" : name
        Task {
            if kind == 0 { await engine.addMaskRule(name: n, pattern: v, type: type) } else { await engine.addException(v, pattern: isPattern) }
            if engine.error == nil { value = ""; name = "" }
        }
    }

    private func field(_ label: String, text: Binding<String>, prompt: String, mono: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(label).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            if isSnapshot {
                FieldStandIn(text: prompt, mono: mono)
            } else {
                TextField(label, text: text, prompt: Text(prompt))
                    .textFieldStyle(.roundedBorder).font(mono ? .km(.mono) : .km(.callout))
            }
        }
    }

    private func ruleRow(_ kind: String, _ title: String, _ detail: String, remove: @escaping () async -> Void) -> some View {
        TableRow {
            Chip(text: kind, tone: kind == "Always mask" ? Theme.masked : Theme.warning)
                .frame(width: 92, alignment: .leading)
            Text(title).font(.km(.callout)).foregroundStyle(Theme.text).frame(width: 110, alignment: .leading).lineLimit(1)
            Text(detail).font(.km(.mono)).foregroundStyle(Theme.textSecondary)
                .lineLimit(1).truncationMode(.middle)
            Spacer()
            IconButton(symbol: "xmark", help: "Remove rule") { Task { await remove() } }
                .disabled(!engine.isBundled || engine.busy)
        }
    }
}
