import SwiftUI

/// The Detection page: what keepmum found, by type and by detector, and what gets masked.
/// Counts and your own settings only: it never shows a masked value.
struct DetectionPage: View {
    var store: StatsStore
    var engine: Engine

    var body: some View {
        let d = store.snapshot.detection
        VStack(spacing: 16) {
            PageHeader(title: "Detection", subtitle: "What keepmum finds, and what you let through. Counts only.") { PreviewToggle(store: store) }
            HStack(alignment: .top, spacing: 12) {
                Card(title: "By type", subtitle: "Found in newly scanned text, then allowed by an exception, passed by your settings, or masked") {
                    TypeTable(detection: d, engine: engine)
                }
                VStack(spacing: 12) {
                    Card(title: "Who found it", subtitle: "Masked values by detector") {
                        SourceBars(sources: d.sources)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    Card(title: "Top rules", subtitle: "Masked values per rule; turn off a built-in rule that masks what it should not") {
                        RuleList(rules: d.rules, engine: engine)
                    }
                }
                .frame(width: 330)
            }
            RulesCard(engine: engine)
                .frame(maxHeight: .infinity)
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
            HStack(spacing: 12) {
                Text("Type").frame(maxWidth: .infinity, alignment: .leading)
                Text("Found").frame(width: 56, alignment: .trailing)
                Text("Allowed").frame(width: 60, alignment: .trailing)
                Text("Passed").frame(width: 56, alignment: .trailing)
                Text("Masked").frame(width: 60, alignment: .trailing)
                Text("Mask it").frame(width: 70, alignment: .trailing)
            }
            .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.secondary).padding(.bottom, 6)
            Rectangle().fill(Theme.border).frame(height: 1)
            ForEach(Theme.typeOrder, id: \.self) { type in row(type) }
            if !piiOn {
                Text("Personal data is off, so only secrets and your own rules are masked. Turn it on in config: [detect] pii = true.")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.secondary).padding(.top, 10)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = engine.error {
                Text(error).font(.system(size: 11.5)).foregroundStyle(Theme.danger).padding(.top, 6)
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
        return VStack(spacing: 0) {
            HStack(spacing: 12) {
                HStack(spacing: 8) {
                    Image(systemName: Theme.icon(type)).font(.system(size: 11)).foregroundStyle(Theme.secondary).frame(width: 16)
                    Text(Theme.label(type)).font(.system(size: 12.5)).foregroundStyle(Theme.text)
                    if pass.contains(type) {
                        Text("passes through").font(.system(size: 10.5, weight: .medium)).foregroundStyle(Theme.secondary)
                            .padding(.horizontal, 6).padding(.vertical, 2).background(Capsule().fill(Theme.faint))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                number(c.found, width: 56)
                number(c.allowed, width: 60)
                number(c.passed, width: 56)
                number(c.masked, width: 60, color: c.masked > 0 ? Theme.accent : Theme.secondary)
                Switch(isOn: masked, enabled: engine.isBundled && !engine.busy && (type == "SECRET" || piiOn)) { on in
                    if !on && type == "SECRET" { confirmSecrets = true } else { Task { await engine.setPass(type, !on) } }
                }
                .accessibilityLabel("Mask \(Theme.label(type))")
                .frame(width: 70, alignment: .trailing)
            }
            .padding(.vertical, 7)
            Rectangle().fill(Theme.border).frame(height: 1)
        }
    }

    private func number(_ n: Int, width: CGFloat, color: Color = Theme.text) -> some View {
        Text(n > 0 ? n.compact : "—").font(.system(size: 12.5, design: .rounded)).monospacedDigit()
            .foregroundStyle(n > 0 ? color : Theme.secondary).frame(width: width, alignment: .trailing)
    }
}

// MARK: - Who found it, top rules

private struct SourceBars: View {
    var sources: [String: Int]

    var body: some View {
        let rows: [(String, String, Color)] = [("rules", "Built-in rules", Theme.accent), ("model", "Local model", Theme.success),
                                               ("user", "Your rules", Color(light: NSColor(hex: 0xD97706), dark: NSColor(hex: 0xF59E0B)))]
        let total = max(rows.reduce(0) { $0 + (sources[$1.0] ?? 0) }, 1)
        VStack(spacing: 10) {
            ForEach(rows, id: \.0) { key, label, color in
                let n = sources[key] ?? 0
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(label).font(.system(size: 12)).foregroundStyle(Theme.text)
                        Spacer()
                        Text("\(n.compact) · \(percent(n, of: total))").font(.system(size: 11.5)).monospacedDigit().foregroundStyle(Theme.secondary)
                    }
                    GeometryReader { geo in
                        Capsule().fill(Theme.faint)
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
                Text("No rule has masked anything yet").font(.system(size: 12)).foregroundStyle(Theme.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ForEach(shown) { r in
                HStack(spacing: 8) {
                    Text(r.rule).font(.system(size: 12, design: r.user ? .default : .monospaced)).foregroundStyle(Theme.text).lineLimit(1)
                    if r.user {
                        Text("yours").font(.system(size: 10, weight: .medium)).foregroundStyle(Theme.accent)
                            .padding(.horizontal, 5).padding(.vertical, 1).background(Capsule().fill(Theme.accentSoft))
                    }
                    Spacer()
                    Text(r.n.compact).font(.system(size: 12)).monospacedDigit().foregroundStyle(Theme.secondary)
                    if !r.user {
                        Button {
                            if r.type == "SECRET" || r.type == nil { confirming = r.rule } else { Task { await engine.setRuleOff(r.rule, true) } }
                        } label: {
                            Image(systemName: "nosign").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(Theme.secondary)
                        }
                        .buttonStyle(.plain).disabled(!engine.isBundled || engine.busy)
                        .help("Turn off this built-in rule").accessibilityLabel("Turn off \(r.rule)")
                    }
                }
                .padding(.vertical, 5)
            }
            if !off.isEmpty {
                Text("Turned off").font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 10).padding(.bottom, 2)
                ForEach(off, id: \.self) { rule in
                    HStack(spacing: 8) {
                        Text(rule).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.secondary).strikethrough().lineLimit(1)
                        Spacer()
                        PillButton(title: "Turn on") { Task { await engine.setRuleOff(rule, false) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                    .padding(.vertical, 3)
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
        Card(title: "Your rules", subtitle: "Always mask a pattern, or never mask something keepmum flags. Applies to new requests, no restart.") {
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 10) {
                    Segmented(options: [(0, "Always mask"), (1, "Never mask")], selection: $kind)
                    if kind == 0 {
                        HStack(alignment: .bottom, spacing: 10) {
                            field("Name", text: $name, prompt: "Employee ID")
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Mask as").font(.system(size: 11.5)).foregroundStyle(Theme.secondary)
                                if isSnapshot {
                                    stand(Theme.label(type))
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
                                .toggleStyle(.checkbox).font(.system(size: 12))
                        }
                    }
                    HStack(spacing: 10) {
                        PillButton(title: kind == 0 ? "Add rule" : "Add exception", icon: "plus", prominent: true) { add() }
                            .disabled(value.isEmpty || !engine.isBundled || engine.busy).opacity(value.isEmpty ? 0.5 : 1)
                        Text("Regular expressions; with a group, only group 1 is masked.")
                            .font(.system(size: 11)).foregroundStyle(Theme.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(width: 340, alignment: .leading)
                Rectangle().fill(Theme.border).frame(width: 1)
                let rows = VStack(alignment: .leading, spacing: 0) {
                    if rules.mask.isEmpty && rules.allowStrings.isEmpty && rules.allowPatterns.isEmpty {
                        Text("No rules yet. Built-in rules and the local model still apply.")
                            .font(.system(size: 12)).foregroundStyle(Theme.secondary)
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
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 11.5)).foregroundStyle(Theme.secondary)
            if isSnapshot {
                stand(prompt, mono: mono)
            } else {
                TextField(label, text: text, prompt: Text(prompt))
                    .textFieldStyle(.roundedBorder).font(.system(size: 12, design: mono ? .monospaced : .default))
            }
        }
    }

    /// A drawn stand-in for a native control, which snapshots cannot render.
    private func stand(_ text: String, mono: Bool = false) -> some View {
        Text(text).font(.system(size: 12, design: mono ? .monospaced : .default)).foregroundStyle(Theme.secondary)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 7).frame(height: 22)
            .background(RoundedRectangle(cornerRadius: 5).fill(Theme.surface))
            .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Theme.border))
    }

    private func ruleRow(_ kind: String, _ title: String, _ detail: String, remove: @escaping () async -> Void) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text(kind).font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(kind == "Always mask" ? Theme.accent : Theme.secondary)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(kind == "Always mask" ? Theme.accentSoft : Theme.faint))
                    .frame(width: 92, alignment: .leading)
                Text(title).font(.system(size: 12.5)).foregroundStyle(Theme.text).frame(width: 110, alignment: .leading).lineLimit(1)
                Text(detail).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.secondary)
                    .lineLimit(1).truncationMode(.middle)
                Spacer()
                Button { Task { await remove() } } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold)).foregroundStyle(Theme.secondary)
                }
                .buttonStyle(.plain).disabled(!engine.isBundled || engine.busy).accessibilityLabel("Remove rule")
            }
            .padding(.vertical, 7)
            Rectangle().fill(Theme.border).frame(height: 1)
        }
    }
}
