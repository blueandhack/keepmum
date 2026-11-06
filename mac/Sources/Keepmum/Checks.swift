import SwiftUI

/// Settings › Checks: the optional checks (`keepmum guard`). Injection looks at what tools return for
/// planted instructions; harm looks at what you type. Each model is downloaded only from here or
/// `keepmum guard setup`, loads on first use and unloads after 10 idle minutes.
struct ChecksSection: View {
    var engine: Engine
    @State private var confirmingRemove: String?
    @Environment(\.isSnapshot) private var isSnapshot

    private var checks: Engine.Checks? { engine.status?.guard }
    private let modes = [("off", "Off"), ("warn", "Warn"), ("block", "Block")]
    /// Injection and harm can also send a flagged request to the local model instead (local.py).
    private let modesWithLocal = [("off", "Off"), ("warn", "Warn"), ("block", "Block"), ("local", "Local")]
    /// This Mac's memory, for the 4B's warning.
    private var memoryGB: Double { Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824 }

    var body: some View {
        FormSection(title: "Checks",
                    footer: "Optional, and off until you turn one on. Warn sends a request at once and checks it afterwards, so it adds no delay; Block checks first and refuses what it flags, and every retry of it. Local checks first, like Block, and sends what it flags to your local model instead of the cloud. Risky commands reads the model's replies instead: Block hands the agent a refusal in place of the command. A hit keeps the check, the score and the length, never the text: see Logs and Activity. None of them replaces masking.") {
            if let checks {
                injection(checks.injection)
                Hairline()
                harm(checks.harm)
                if let commands = checks.commands {
                    Hairline()
                    self.commands(commands)
                }
                if let local = checks.local {
                    Hairline()
                    localModel(local, used: [checks.injection.mode, checks.harm.mode].contains("local"))
                }
            } else {
                FormRow(title: "Not available", detail: engine.isBundled ? "This engine has no optional checks" : "Needs the bundled engine") { EmptyView() }
            }
        }
        .alert(confirmingRemove == "harm" ? "Remove the harm check's models?" : "Remove the injection check's model?",
               isPresented: Binding(get: { confirmingRemove != nil }, set: { if !$0 { confirmingRemove = nil } })) {
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) {
                let c = confirmingRemove
                Task { if let c { await engine.removeCheck(c) } }
            }
        } message: {
            Text("The check turns off and its model is deleted from ~/.local/share/keepmum/guard. Setting it up again downloads it again.")
        }
    }

    // MARK: Injection

    @ViewBuilder private func injection(_ c: Engine.Checks.Injection) -> some View {
        FormRow(title: "Prompt injection",
                detail: c.installed ? injectionDetail(c.mode) : "Checks what tools return (web pages, files, MCP replies) for instructions planted for the agent",
                detailTone: c.mode == "off" ? Theme.textSecondary : Theme.text) {
            if c.installed {
                modePicker(c.mode, options: modesWithLocal) { await engine.setCheck("injection", mode: $0) }
            } else if engine.checkDownload?.what == "injection" {
                progress(engine.checkDownload?.progress ?? 0)
            } else {
                PillButton(title: "Download (164 MB)", icon: "arrow.down.circle") {
                    Task { await engine.setUpCheck("injection") }
                }
                .disabled(!engine.isBundled || engine.busy)
            }
        }
        if c.installed {
            modelLine("Prompt Injection Guard Small by Horizon Labs · Apache-2.0 · 0.4 GB while in use", remove: "injection")
        }
    }

    private func injectionDetail(_ mode: String) -> String {
        switch mode {
        case "local": "Tool results are checked before each request is sent; one with a likely injection is answered by your local model instead"
        case "warn": "Tool results are checked after each request is sent; a likely injection is logged"
        case "block": "Requests carrying a likely injection in a tool result are refused"
        default: "Off: tool results are not checked"
        }
    }

    // MARK: Harm

    @ViewBuilder private func harm(_ c: Engine.Checks.Harm) -> some View {
        let have = c.installed[c.model] ?? false
        let downloading = engine.checkDownload?.what.hasPrefix("harm") == true
        FormRow(title: "Harmful requests",
                detail: have ? harmDetail(c.mode) : "Checks what you type, without what agents add to your turn, for requests a policy may not allow",
                detailTone: c.mode == "off" ? Theme.textSecondary : Theme.text) {
            if have {
                modePicker(c.mode, options: modesWithLocal) { await engine.setCheck("harm", mode: $0) }
            } else if downloading {
                progress(engine.checkDownload?.progress ?? 0)
            } else {
                PillButton(title: "Download (\(Self.downloadSize[c.model] ?? ""))", icon: "arrow.down.circle") {
                    Task { await engine.setUpCheck("harm", model: c.model) }
                }
                .disabled(!engine.isBundled || engine.busy)
            }
        }
        Hairline()
        FormRow(title: "Harm model", detail: sizeDetail(c), detailTone: c.model == "4b" && memoryGB < 32 ? Theme.warning : Theme.textSecondary) {
            if isSnapshot {
                Segmented(options: Self.sizes, selection: .constant(c.model))
            } else {
                Segmented(options: Self.sizes, selection: Binding(get: { c.model }, set: { size in
                    guard size != c.model else { return }
                    Task {
                        if !(c.installed[size] ?? false) {
                            await engine.setUpCheck("harm", model: size)
                            guard engine.status?.guard?.harm.installed[size] == true else { return }
                        }
                        await engine.setCheck("harm", mode: engine.status?.guard?.harm.mode ?? c.mode, model: size)
                    }
                }))
                .disabled(!engine.isBundled || engine.busy || downloading)
            }
        }
        if c.installed.values.contains(true) {
            modelLine("Content Safety Guard by Horizon Labs (Small), Qwen3Guard-Gen by the Qwen team (0.6B, 4B) · Apache-2.0 · downloaded: "
                      + Self.sizes.filter { c.installed[$0.0] == true }.map(\.1).joined(separator: ", "), remove: "harm")
        }
        if c.server == nil {
            modelLine("llama-server, which both checks run in, is not found: the app bundles it; on the command line, brew install llama.cpp", tone: Theme.warning)
        }
    }

    // MARK: Local model

    /// The model local mode sends a flagged request to: a menu of what Ollama has.
    @ViewBuilder private func localModel(_ c: Engine.Checks.Local, used: Bool) -> some View {
        let have = engine.localModels?.models
        FormRow(title: "Local model", detail: localDetail(c, have: have, used: used),
                detailTone: used && (c.model.isEmpty || !(have ?? []).contains(c.model)) ? Theme.warning : Theme.textSecondary) {
            if let have, !have.isEmpty, !isSnapshot {
                Menu(c.model.isEmpty ? "Choose…" : c.model) {
                    ForEach(have, id: \.self) { name in
                        Button(name) { Task { await engine.setLocalModel(name) } }
                    }
                }
                .menuStyle(.borderlessButton).fixedSize()
                .disabled(!engine.isBundled || engine.busy)
            } else if isSnapshot {
                Text(c.model.isEmpty ? "Choose…" : c.model).font(.km(.callout)).foregroundStyle(Theme.text)
            }
        }
        .task(id: c.url + c.model) { await engine.loadLocalModels() }
    }

    private func localDetail(_ c: Engine.Checks.Local, have: [String]?, used: Bool) -> String {
        let what = "For Local mode: a flagged request is answered by this model on your Mac, without tools, instead of being sent."
        guard let have else { return "Ollama does not answer at \(c.url): open the Ollama app or run ollama serve. " + what }
        if have.isEmpty { return "No models in Ollama yet: ollama pull qwen3:8b, for example. " + what }
        if !c.model.isEmpty && !have.contains(c.model) { return "Ollama has no \(c.model): ollama pull \(c.model). " + what }
        return what + " Use a capable model (8B or larger): a small one may repeat a planted instruction as advice."
    }

    // MARK: Commands

    @ViewBuilder private func commands(_ c: Engine.Checks.Commands) -> some View {
        FormRow(title: "Risky commands", detail: commandsDetail(c.mode), detailTone: c.mode == "off" ? Theme.textSecondary : Theme.text) {
            modePicker(c.mode) { await engine.setCheck("commands", mode: $0) }
        }
        modelLine("Rules, no download: " + c.rules.joined(separator: " · "))
    }

    private func commandsDetail(_ mode: String) -> String {
        switch mode {
        case "warn": "Shell commands in the model's replies are checked as they arrive; a risky one is logged, then handed to the agent"
        case "block": "A risky shell command in a reply is replaced by one that tells the agent why and fails"
        default: "Checks the shell commands a model asks your agent to run: curl | sh, rm -rf ~, keys sent out"
        }
    }

    private func harmDetail(_ mode: String) -> String {
        switch mode {
        case "local": "What you type is checked before each request is sent; a flagged request is answered by your local model instead"
        case "warn": "What you type is checked after each request is sent; a flagged request is logged"
        case "block": "Requests whose typed text the model flags are refused, with its categories"
        default: "Off: what you type is not checked"
        }
    }

    private static let sizes = [("small", "Small"), ("0.6b", "0.6B"), ("4b", "4B")]
    private static let downloadSize = ["small": "164 MB", "0.6b": "805 MB", "4b": "4.7 GB"]

    private func sizeDetail(_ c: Engine.Checks.Harm) -> String {
        let note = "Small: 66% of harmful prompts caught, 0.4 GB while in use, the fastest · 0.6B: 76%, 1.1 GB · 4B: 82%, 4.9 GB, a little slower"
        if c.model == "4b" && memoryGB < 32 {
            return "The 4B takes 4.9 GB while in use, a lot for this Mac's \(Int(memoryGB.rounded())) GB. " + note
        }
        return note
    }

    // MARK: Pieces

    private func modePicker(_ mode: String, options: [(String, String)]? = nil, set: @escaping (String) async -> Void) -> some View {
        let options = options ?? modes
        return Group {
            if isSnapshot {
                Segmented(options: options, selection: .constant(mode))
            } else {
                Segmented(options: options, selection: Binding(get: { mode }, set: { new in
                    if new != mode { Task { await set(new) } }
                }))
            }
        }
        .disabled(!engine.isBundled || engine.busy)
    }

    private func progress(_ p: Double) -> some View {
        HStack(spacing: Space.s) {
            ProgressView(value: p).progressViewStyle(.linear).frame(width: 140)
            Text("\(Int((p * 100).rounded()))%").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                .frame(width: 40, alignment: .trailing)
        }
    }

    private func modelLine(_ text: String, tone: Color = Theme.textSecondary, remove: String? = nil) -> some View {
        HStack(spacing: Space.s) {
            Text(text).font(.km(.caption)).foregroundStyle(tone).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Space.s)
            if let remove {
                Button("Remove") { confirmingRemove = remove }
                    .buttonStyle(.plain).font(.km(.caption)).foregroundStyle(Theme.accent)
                    .disabled(!engine.isBundled || engine.busy)
            }
        }
        .padding(.bottom, Space.s + Space.xxs)
    }
}
