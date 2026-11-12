import AppKit
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
                    footer: "Each check is off until you turn it on. Warn logs a hit, Block refuses it, and Local answers it on this Mac. None of them replaces masking.",
                    details: "Optional, and off until you turn one on. Warn sends a request at once and checks it afterwards, so it adds no delay; Block checks first and refuses what it flags, and every retry of it. Local checks first, like Block, and sends what it flags to a model on this Mac instead of the cloud (Local servers). Risky commands reads the model's replies instead: Block hands the agent a refusal in place of the command. Logs shows each hit with what it flagged and what you had asked, masked values as their types, in memory only (with text around values on); the audit log keeps the check, the score and the length, never the text. None of them replaces masking.") {
            if let checks {
                injection(checks.injection)
                Hairline()
                harm(checks.harm)
                if let commands = checks.commands {
                    Hairline()
                    self.commands(commands)
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
        if c.installed, let levels = c.levels, let threshold = c.threshold {
            sensitivity(c, levels: levels, threshold: threshold)
        }
        if c.installed {
            modelLine("Prompt Injection Guard Small by Horizon Labs · Apache-2.0 · 0.4 GB while in use", remove: "injection")
        }
    }

    private static let levelOrder = [("high", "High"), ("medium", "Medium"), ("low", "Low"), ("lowest", "Lowest")]

    /// How readily the injection check flags: four levels from the benchmark, or a custom number
    /// set with `keepmum guard sensitivity injection`.
    @ViewBuilder private func sensitivity(_ c: Engine.Checks.Injection, levels: [String: Engine.Checks.Injection.Level],
                                          threshold: Double) -> some View {
        let current = levels.first { abs($0.value.threshold - threshold) < 0.0005 }?.key
        let options = Self.levelOrder + (current == nil ? [("custom", "Custom")] : [])
        let isDefault = abs(threshold - (c.defaultThreshold ?? 0.65)) < 0.0005
        FormRow(title: "Sensitivity", detail: sensitivitySummary(c, levels: levels, current: current, threshold: threshold, isDefault: isDefault),
                help: sensitivityDetail(c, levels: levels, current: current, threshold: threshold, isDefault: isDefault)) {
            modePicker(current ?? "custom", options: options) { level in
                if level != "custom" { await engine.setInjectionThreshold(level) }
            }
        }
    }

    /// The level and its two numbers that matter; the rest is in the row's tooltip.
    private func sensitivitySummary(_ c: Engine.Checks.Injection, levels: [String: Engine.Checks.Injection.Level],
                                    current: String?, threshold: Double, isDefault: Bool) -> String {
        let pct = { (x: Double) in x == 0 ? "none" : x < 0.01 ? "<1%" : "\(Int((x * 100).rounded()))%" }
        guard let current, let l = levels[current] else {
            return "Custom: flags a tool result scoring \(String(format: "%.2f", threshold)) or more. Lower is stricter."
        }
        let which = current.capitalized + (isDefault ? " (the default)" : "")
        let flags = l.sessions.map { "flags \(pct($0)) of real tool results" } ?? "flags \(pct(l.ordinary)) of ordinary pages"
        return "\(which): catches \(pct(l.caught)) of injections in keepmum\u{2019}s tests, \(flags)"
    }

    private func sensitivityDetail(_ c: Engine.Checks.Injection, levels: [String: Engine.Checks.Injection.Level],
                                   current: String?, threshold: Double, isDefault: Bool) -> String {
        let pct = { (x: Double) in x == 0 ? "none" : x < 0.01 ? "<1%" : "\(Int((x * 100).rounded()))%" }
        let score = String(format: "%.2f", threshold)
        let short = c.shortThreshold.flatMap { t in c.shortChars.map { " (\(String(format: "%.3g", t)) under \($0) characters)" } } ?? ""
        guard let current, let l = levels[current] else {
            return "Custom: flags a tool result scoring \(score) or more\(short) (keepmum guard sensitivity injection). Lower is stricter."
        }
        let which = current.capitalized + (isDefault ? " (the default)" : "")
        let catches = "On keepmum's test set: catches \(pct(l.caught)) of injections, and flags \(pct(l.ordinary)) of ordinary pages and \(pct(l.security)) of security code and writing."
        let sessions = l.sessions.map { " On real Claude Code and Codex sessions: flags \(pct($0)) of tool results." } ?? ""
        let advice = current == "high" ? " More false alarms than Medium, for a few more catches." :
                     current == "lowest" ? " Misses more injections: subtle ones score lower." : ""
        return "\(which): flags a tool result scoring \(score) or more\(short). \(catches)\(sessions)\(advice)"
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
        if have, let levels = c.levels, let level = c.sensitivity, levels[level] != nil {
            Hairline()
            FormRow(title: "Sensitivity", detail: harmSensitivitySummary(levels, level, model: c.model),
                    help: harmSensitivityDetail(levels, level, model: c.model)) {
                modePicker(level, options: Self.levelOrder) { await engine.setHarmSensitivity($0) }
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

    private func harmSensitivitySummary(_ levels: [String: Engine.Checks.Harm.Level], _ level: String, model: String) -> String {
        guard let l = levels[level] else { return "" }
        let pct = { (x: Double) in x == 0 ? "none" : x < 0.01 ? "<1%" : "\(Int((x * 100).rounded()))%" }
        let which = level == "medium" ? "Medium (the default)" : level.capitalized
        return "\(which): catches \(pct(l.caught)) of harmful prompts, flags \(pct(l.safe)) of safe ones"
    }

    private func harmSensitivityDetail(_ levels: [String: Engine.Checks.Harm.Level], _ level: String, model: String) -> String {
        guard let l = levels[level] else { return "" }
        let pct = { (x: Double) in x == 0 ? "none" : x < 0.01 ? "<1%" : "\(Int((x * 100).rounded()))%" }
        let name = Self.sizes.first { $0.0 == model }?.1 ?? model
        let which = level == "medium" ? "Medium (the default)" : level.capitalized
        let advice = level == "high" ? " Flags one safe prompt in ten." :
                     level == "lowest" || level == "low" ? " Misses more: about half of harmful prompts at Lowest." : ""
        return "\(which) on \(name): on keepmum's test set, catches \(pct(l.caught)) of harmful prompts, and flags \(pct(l.safe)) of safe ones and \(pct(l.coding)) of coding prompts that only sound harmful.\(advice) The level carries over if you change the model."
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
        let note = "At Medium sensitivity, Small: 66% of harmful prompts caught, 0.4 GB while in use, the fastest · 0.6B: 76%, 1.1 GB · 4B: 82%, 4.9 GB, a little slower"
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

/// Settings › Checks, beside the checks: the servers on this Mac that local mode sends a flagged
/// request to, in the order tried (`keepmum guard local`). Each has a model, and answers for both
/// checks or one; the first that answers for the check gives the answer.
struct LocalServersSection: View {
    var engine: Engine
    @State private var addingOther = false
    @State private var otherURL = "http://localhost:"
    /// The Splash command just copied, to say what to do with it.
    @State private var copiedSplash: String?
    /// The command that installs Splash, just copied.
    @State private var installCopied: String?
    /// Downloading another model to Ollama server `number`: a Hugging Face link or an Ollama name.
    @State private var pullingTo: Int?
    @State private var pullRef = ""
    @State private var pullQuant = ""
    @Environment(\.isSnapshot) private var isSnapshot

    private var picks: [Engine.LocalServers.Pick] { engine.localServers?.picks ?? [] }
    private var defaultPick: Engine.LocalServers.Pick? {
        picks.first { $0.id == engine.localServers?.defaultPick } ?? picks.first
    }
    private var memoryGB: Double { Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824 }

    private var rows: [Engine.LocalServers.Server] {
        engine.localServers?.servers ?? (engine.status?.guard?.local?.servers ?? []).map {
            .init(url: $0.url, model: $0.model, checks: $0.checks, api: $0.api, kind: nil, models: nil, problem: nil)
        }
    }
    private var used: Bool {
        guard let g = engine.status?.guard else { return false }
        return [g.injection.mode, g.harm.mode].contains("local")
    }

    var body: some View {
        FormSection(title: "Local servers",
                    footer: "Where Local mode sends a flagged request: the first server here that answers for its check, on this Mac, without tools or your API key.",
                    details: "For Local mode. A flagged request goes to the first server here that answers for its check; one that is not running, lacks its model or fails is passed over, and if none answers the request is refused. Each gets the request on this Mac only, without tools or your API key. Ollama, LM Studio and llama.cpp take Claude Code's and Codex's requests as they are; other servers, such as MLX, get them as Chat Completions, and so does every server for the Antigravity CLI and Gemini CLI, whose answers come back in Gemini's format. An Ollama server's model menu offers keepmum's tested picks, downloaded for you, and any other model from Ollama or Hugging Face. keepmum gives an Ollama model a 64k context, which takes several GB less than Ollama's own setting (the model's longest); its Context menu offers 16k to 128k, or Ollama's own. It does this with its own copy of the model in Ollama (keepmum/…), which shares the downloaded files: no more disk. Use a capable model (8B or larger): a small one may repeat a planted instruction, or help with what the harm check flagged.") {
            if rows.isEmpty {
                FormRow(title: "No local servers",
                        detail: used ? "A check is in Local mode with nowhere to send what it flags, so it refuses it. Add a server."
                            : emptyDetail,
                        detailTone: used ? Theme.warning : Theme.textSecondary) {
                    HStack(spacing: Space.s) {
                        if let p = engine.localPull, p.server == nil {
                            pullProgress(p.progress)
                        } else if let pick = defaultPick {
                            usePickMenu(pick)
                        }
                        addMenu
                    }
                }
            } else {
                ForEach(Array(rows.enumerated()), id: \.offset) { i, server in
                    row(i + 1, server)
                    Hairline()
                }
                HStack { Spacer(); addMenu }.padding(.vertical, Space.s)
            }
        }
        .task(id: engine.status?.guard?.local) { await engine.loadLocalServers() }
        .alert("Install Splash", isPresented: Binding(get: { installCopied != nil }, set: { if !$0 { installCopied = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("This command is copied. Paste it into Terminal, then choose Use keepmum\u{2019}s pick \u{203A} With Splash again.\n\n\(installCopied ?? "")")
        }
        .alert("Start Splash in Terminal", isPresented: Binding(get: { copiedSplash != nil }, set: { if !$0 { copiedSplash = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("This command is copied. Paste it into Terminal and leave it running; the first start downloads the model and its draft.\n\n\(copiedSplash ?? "")")
        }
        .alert("Download a model to Ollama", isPresented: Binding(get: { pullingTo != nil }, set: { if !$0 { pullingTo = nil } })) {
            TextField("https://huggingface.co/owner/repo, or qwen3:8b", text: $pullRef)
            TextField("Quant, for Hugging Face: Q4_K_M, IQ3_M…", text: $pullQuant)
            Button("Cancel", role: .cancel) {}
            Button("Download") {
                let (ref, quant, n) = (pullRef.trimmingCharacters(in: .whitespaces), pullQuant.trimmingCharacters(in: .whitespaces), pullingTo)
                if !ref.isEmpty { Task { await engine.pullLocalModel(ref, quant: quant, server: n) } }
            }
        } message: {
            Text("A GGUF repo on Hugging Face, by its link (a link to one .gguf file names its quant), or a name from Ollama's library. Ollama downloads it, and it becomes this server's model.")
        }
        .alert("Add a local server", isPresented: $addingOther) {
            TextField("http://localhost:8000", text: $otherURL)
            Button("Cancel", role: .cancel) {}
            Button("Add") { let url = otherURL; Task { await engine.addLocalServer(url: url) } }
        } message: {
            Text("Its URL, on this Mac: http://localhost, 127.0.0.1 or [::1], with its port.")
        }
    }

    // MARK: A server

    @ViewBuilder private func row(_ n: Int, _ server: Engine.LocalServers.Server) -> some View {
        HStack(alignment: .center, spacing: Space.m) {
            Text("\(n)").font(.km(.caption).weight(.semibold)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                .frame(width: 20, height: 20).background(Circle().fill(Theme.fill))
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(title(server)).font(.km(.body)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
                Text(detail(server)).font(.km(.caption))
                    .foregroundStyle(server.problem == nil ? Theme.textSecondary : Theme.warning)
                    .lineLimit(2).truncationMode(.tail)
                    .help(server.managed == true ? (server.progress ?? "") : server.start.map { "Or start it in Terminal: \($0)" } ?? server.problem ?? "")
            }
            Spacer(minLength: Space.m)
            if server.start != nil || server.kind == "Splash" { splashControl(server) }
            modelMenu(n, server)
            if server.kind == "Ollama", server.contextMax != nil { contextMenu(n, server) }
            checksMenu(n, server)
            moreMenu(n, server)
        }
        .padding(.vertical, Space.s)
    }

    /// keepmum's pick, with Splash (faster, where this Mac runs it) or with Ollama.
    @ViewBuilder private func usePickMenu(_ pick: Engine.LocalServers.Pick) -> some View {
        let splash = engine.localServers?.splash
        let splashReady = splash?.installed == true && splash?.supported == true && pick.splash != nil
        if isSnapshot {
            PillButton(title: "Use keepmum\u{2019}s pick", icon: "chevron.down", prominent: true) {}
        } else {
            Menu {
                if let spec = pick.splash, splash?.supported != false {
                    Button(splashReady ? "With Splash: faster, runs in the background" : "With Splash: install it first\u{2026}") {
                        if splashReady {
                            Task { await engine.startSplash(url: "http://localhost:8000", model: spec) }
                        } else {
                            copyInstall(splash?.install ?? "brew install incoai/tap/splash")
                        }
                    }
                }
                Button("With Ollama: download \(Self.gb(pick.downloadGB))") { Task { await engine.pullLocalModel(pick.id) } }
                if let why = splash?.why, pick.splash != nil {
                    Divider()
                    Text(why)
                }
            } label: {
                Label("Use keepmum\u{2019}s pick", systemImage: splashReady ? "bolt.fill" : "arrow.down.circle")
            }
            .menuStyle(.button).buttonStyle(.borderedProminent).fixedSize()
            .tint(Theme.accentFill)
            .disabled(!engine.isBundled || engine.busy)
            .help("\(pick.name). With Splash it writes about 1.6 times as fast and Splash downloads it the first time; with Ollama, Ollama downloads it. Ollama or Splash must be installed.")
        }
    }

    private func copyInstall(_ command: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(command, forType: .string)
        installCopied = command
    }

    /// Start or Stop for a Splash server: keepmum runs it in the background, and again at login.
    @ViewBuilder private func splashControl(_ server: Engine.LocalServers.Server) -> some View {
        let running = server.kind == "Splash"
        if server.managed == true {
            HStack(spacing: Space.s) {
                if !running { ProgressView().controlSize(.small) }
                PillButton(title: "Stop") { Task { await engine.stopSplash() } }
                    .disabled(!engine.isBundled || engine.busy)
                    .help("Stops Splash, and it no longer starts at login")
            }
            .task(id: server.url) { if !running && !isSnapshot { await engine.followSplash(url: server.url) } }
        } else if !running {
            PillButton(title: "Start", icon: "play.fill", prominent: true) {
                Task { await engine.startSplash(url: server.url, model: server.model) }
            }
            .disabled(!engine.isBundled || engine.busy || server.model.isEmpty)
            .help("keepmum runs Splash in the background, starts it again at login, and it frees its memory after 5 minutes idle. The first start downloads the model.")
        }
    }

    private func title(_ server: Engine.LocalServers.Server) -> String {
        let host = server.url.replacingOccurrences(of: "http://", with: "").replacingOccurrences(of: "https://", with: "")
        return (server.kind ?? (server.start != nil ? "Splash" : nil)).map { "\($0) · \(host)" } ?? host
    }

    private func detail(_ server: Engine.LocalServers.Server) -> String {
        if let problem = server.problem { return Self.plainProblem(problem, server) }
        let checks = server.checks.count == 2 ? "both checks" : "the \(server.checks[0]) check only"
        var out = "Answers for \(checks)" + (server.api == "chat" ? ", as Chat Completions" : "")
        if server.kind == "Ollama", let most = server.contextMax {
            out += server.context.map { ", with a \(Self.tokens($0)) context" }
                ?? ", at Ollama\u{2019}s own context: up to \(Self.tokens(most)), the most memory"
        }
        return out
    }

    private var contexts: [Int] { engine.localServers?.contexts ?? [16384, 32768, 65536, 131072] }
    private var defaultContext: Int { engine.localServers?.defaultContext ?? 65536 }

    static func tokens(_ n: Int) -> String { n.isMultiple(of: 1024) ? "\(n / 1024)k" : n.formatted() }

    /// An Ollama model's context size: keepmum sets it through its own copy of the model, which
    /// shares the downloaded files. Less takes less memory; a long session needs more.
    @ViewBuilder private func contextMenu(_ n: Int, _ server: Engine.LocalServers.Server) -> some View {
        let label = server.context.map { Self.tokens($0) } ?? "Max"
        if isSnapshot {
            menuLabel(label)
        } else {
            Menu(label) {
                Section("Context size: less takes less memory") {
                    ForEach(contexts, id: \.self) { c in
                        Button(contextLabel(c, server)) { Task { await engine.setLocalContext(n, size: String(c)) } }
                            .disabled(c == server.context || (server.contextMax.map { c > $0 } ?? false))
                    }
                    Button("Ollama\u{2019}s own setting, up to \(server.contextMax.map { Self.tokens($0) } ?? "the model\u{2019}s longest"): the most memory") {
                        Task { await engine.setLocalContext(n, size: "default") }
                    }
                    .disabled(server.context == nil)
                }
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .help("How much of a conversation the model takes in. A long agent session needs more; what does not fit is cut.")
        }
    }

    private func contextLabel(_ c: Int, _ server: Engine.LocalServers.Server) -> String {
        var label = "\(Self.tokens(c)) tokens"
        if c == defaultContext { label += " (keepmum\u{2019}s default)" }
        if c == server.context { label += " \u{2713}" }
        return label
    }

    private var emptyDetail: String {
        guard let pick = defaultPick else { return "Add Ollama, LM Studio, llama.cpp, MLX or another server on this Mac" }
        let fit = memoryGB + 0.5 < Double(pick.macGB)
            ? " It suits a Mac with \(pick.macGB) GB or more; this one has \(Int(memoryGB.rounded())) GB, so choose a lighter pick after." : ""
        let runner = pick.splash != nil && engine.localServers?.splash?.supported == true ? "runs in Splash (faster) or Ollama" : "downloads to Ollama"
        return "keepmum's pick, \(Self.pickShortName(pick.name)), \(runner) and takes about \(Self.gb(pick.memoryGB)) while it answers.\(fit) Or add another server."
    }

    /// A pick's menu item: its name, what it takes, and whether it downloads first or suits this Mac.
    private func pickLabel(_ pick: Engine.LocalServers.Pick, have: [String]) -> String {
        let star = pick.id == engine.localServers?.defaultPick ? " (recommended)" : ""
        let size = have.contains(pick.model) ? "about \(Self.gb(pick.memoryGB)) in use" : "download \(Self.gb(pick.downloadGB))"
        let fit = memoryGB + 0.5 < Double(pick.macGB) ? ", needs a \(pick.macGB) GB Mac" : ""
        return "\(pick.name)\(star): \(size)\(fit)"
    }

    /// The command that starts Splash with a model, as the engine writes it (localmodels.splash_command).
    static func splashCommand(_ spec: String, url: String) -> String {
        let port = url.split(separator: ":").last.map(String.init) ?? "8000"
        return "splash serve --model \(spec) --language-only --max-context 64K" + (port == "8000" ? "" : " --port \(port)")
    }

    private func copySplash(_ command: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(command, forType: .string)
        copiedSplash = command
    }

    /// A pick's name for a label: the model and its quant, without who made it.
    private func pickShort(_ model: String) -> String? {
        if let name = picks.first(where: { $0.model == model || $0.splash == model })?.name { return Self.pickShortName(name) }
        // Splash serving a local file names it by its repo alone, so the quant is not in it.
        guard let name = picks.first(where: { $0.splash?.split(separator: ":").first.map(String.init) == model })?.name else { return nil }
        return Self.pickShortName(name).components(separatedBy: ", ").first
    }

    /// "Qwen3.6 35B-A3B Uncensored (HauhauCS), IQ3_M" -> "Qwen3.6 35B-A3B, IQ3_M".
    static func pickShortName(_ name: String) -> String {
        let quant = name.components(separatedBy: ", ").count > 1 ? name.components(separatedBy: ", ").last! : ""
        var model = name.components(separatedBy: ", ").first ?? name
        if let paren = model.range(of: " (") { model = String(model[..<paren.lowerBound]) }
        model = model.replacingOccurrences(of: " Uncensored", with: "")
        return quant.isEmpty ? model : "\(model), \(quant)"
    }

    static func gb(_ x: Double) -> String { String(format: "%.1f GB", x) }

    private func pullProgress(_ p: Double) -> some View {
        HStack(spacing: Space.s) {
            ProgressView(value: p).progressViewStyle(.linear).frame(width: 120)
            Text("\(Int((p * 100).rounded()))%").font(.km(.callout)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                .frame(width: 40, alignment: .trailing)
        }
        .help("Downloading to Ollama")
    }

    /// A model's name, short: a path's last part, or a Hugging Face cache folder's repo.
    /// What a server's problem means, in a few words: the engine's own text names whole model names
    /// and commands, which the row has no room for (it stays in the tooltip).
    static func plainProblem(_ problem: String, _ server: Engine.LocalServers.Server) -> String {
        if problem.hasPrefix("no model chosen") { return "No model chosen yet" }
        if server.start != nil, problem.hasPrefix("nothing answers") {
            if server.managed == true { return "Starting: " + (server.progress ?? "loading the model") }
            return "Not running: click Start"
        }
        if server.kind == "Splash", problem.contains("has no model") { return "Splash serves another model: restart it with this one" }
        if problem.hasPrefix("nothing answers") {
            return server.kind == "Ollama" || server.url.hasSuffix(":11434") ? "Not running: open the Ollama app" : "Not running"
        }
        if problem.contains("has no model") {
            return server.kind == "Ollama" ? "Not downloaded yet: pick it in the model menu"
                                           : "Not on this server: pick a model it has"
        }
        return problem.prefix(1).uppercased() + problem.dropFirst()
    }

    /// A model's name, short enough for a row: keepmum's pick by its own short name, a Hugging
    /// Face repo without its owner and "-GGUF", a file path by its last part.
    static func shortName(_ model: String) -> String {
        if model.hasPrefix("hf.co/") || model.hasPrefix("huggingface.co/") {
            let parts = model.split(separator: "/", maxSplits: 2).map(String.init)
            var repo = parts.count == 3 ? parts[2] : model
            for suffix in ["-GGUF", "-gguf", "_GGUF"] { repo = repo.replacingOccurrences(of: suffix + ":", with: ":") }
            if repo.hasSuffix("-GGUF") { repo.removeLast(5) }
            return repo
        }
        guard model.hasPrefix("/") else { return model }
        if let r = model.range(of: #"models--([^/]+)--([^/]+)"#, options: .regularExpression) {
            let parts = model[r].dropFirst("models--".count).components(separatedBy: "--")
            if parts.count == 2 { return parts.joined(separator: "/") }
        }
        return (model as NSString).lastPathComponent
    }

    @ViewBuilder private func modelMenu(_ n: Int, _ server: Engine.LocalServers.Server) -> some View {
        let label = server.model.isEmpty ? "Choose model…" : pickShort(server.base ?? server.model) ?? Self.shortName(server.base ?? server.model)
        let restartSplash = server.managed == true
        let ollama = server.kind == "Ollama"
        let splash = server.kind == "Splash" || server.start != nil
        if let p = engine.localPull, p.server == n {
            pullProgress(p.progress)
        } else if isSnapshot {
            menuLabel(label).frame(maxWidth: 230, alignment: .trailing)
        } else {
            Menu(label) {
                if splash, picks.contains(where: { $0.splash != nil }) {
                    Section("keepmum\u{2019}s picks for Splash") {
                        ForEach(picks.filter { $0.splash != nil }) { pick in
                            Button(Self.pickShortName(pick.name) + (pick.id == engine.localServers?.defaultPick ? " (recommended)" : "")) {
                                let spec = pick.splash ?? ""
                                Task {
                                    if restartSplash {
                                        await engine.startSplash(url: server.url, model: spec)  // runs it with this one
                                    } else {
                                        await engine.setLocalServer(n, model: spec)
                                    }
                                }
                            }
                            .help(pick.note)
                        }
                    }
                }
                if ollama, !picks.isEmpty {
                    Section("keepmum's picks") {
                        ForEach(picks) { pick in
                            Button(pickLabel(pick, have: server.models ?? [])) {
                                Task { await engine.pullLocalModel(pick.id, server: n) }
                            }
                            .help(pick.note)
                        }
                    }
                }
                Section(ollama ? "On this server" : "") {
                    if let have = server.models, !have.isEmpty {
                        ForEach(have.filter { !$0.hasPrefix("keepmum/") }, id: \.self) { name in
                            Button(Self.shortName(name)) {
                                Task {
                                    await engine.setLocalServer(n, model: name)
                                    if ollama { await engine.setLocalContext(n, size: String(defaultContext)) }
                                }
                            }
                        }
                    } else {
                        Text(server.models == nil ? "Not answering" : "No models there yet")
                    }
                }
                if ollama {
                    Divider()
                    Button("From Hugging Face or Ollama…") { pullRef = ""; pullQuant = ""; pullingTo = n }
                }
            }
            .menuStyle(.borderlessButton).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 230, alignment: .trailing)
            .disabled(!engine.isBundled || engine.busy)
            .help(server.model)
        }
    }

    @ViewBuilder private func checksMenu(_ n: Int, _ server: Engine.LocalServers.Server) -> some View {
        let label = server.checks.count == 2 ? "Both" : server.checks[0] == "harm" ? "Harm" : "Injection"
        if isSnapshot {
            menuLabel(label)
        } else {
            Menu(label) {
                Button("Both checks") { Task { await engine.setLocalServer(n, checks: ["injection", "harm"]) } }
                Button("Injection only") { Task { await engine.setLocalServer(n, checks: ["injection"]) } }
                Button("Harm only") { Task { await engine.setLocalServer(n, checks: ["harm"]) } }
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .help("Which checks' flagged requests this server answers")
        }
    }

    @ViewBuilder private func moreMenu(_ n: Int, _ server: Engine.LocalServers.Server) -> some View {
        if isSnapshot {
            Image(systemName: "ellipsis.circle").foregroundStyle(Theme.textSecondary)
        } else {
            Menu {
                if let start = server.start {
                    Button("Copy Splash Command") { copySplash(start) }
                    Divider()
                }
                Button("Move up") { Task { await engine.moveLocalServer(n, to: n - 1) } }.disabled(n == 1)
                Button("Move down") { Task { await engine.moveLocalServer(n, to: n + 1) } }.disabled(n == rows.count)
                Divider()
                Button("Remove", role: .destructive) { Task { await engine.removeLocalServer(n) } }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .accessibilityLabel("More for server \(n)")
        }
    }

    @ViewBuilder private var addMenu: some View {
        if isSnapshot {
            PillButton(title: "Add server", icon: "plus") {}
        } else {
            Menu {
                ForEach(engine.localServers?.presets ?? []) { p in
                    Button("\(p.name)  \(p.url.replacingOccurrences(of: "http://", with: ""))") {
                        Task { await engine.addLocalServer(preset: p.id) }
                    }
                }
                Divider()
                Button("Other…") { otherURL = "http://localhost:"; addingOther = true }
            } label: {
                Label("Add server", systemImage: "plus")
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
        }
    }

    private func menuLabel(_ text: String) -> some View {
        HStack(spacing: Space.xxs) {
            Text(text).font(.km(.callout)).foregroundStyle(Theme.text).lineLimit(1).truncationMode(.middle)
            Image(systemName: "chevron.down").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
        }
    }
}

/// Settings › Checks, under the local servers: the user's skills for local turns (`keepmum guard
/// local skill`). Each is a SKILL.md, or a folder holding one, read on every local turn and given to
/// the local model after keepmum's note, in the places the user chose (Places).
struct LocalSkillsSection: View {
    var engine: Engine
    @Environment(\.isSnapshot) private var isSnapshot
    @Environment(\.openWindow) private var openWindow

    private var rows: [Engine.LocalSkills.Skill] {
        engine.localSkills?.skills ?? (engine.status?.guard?.local?.skills ?? []).map {
            .init(path: $0.path, checks: $0.checks, name: nil, description: "", chars: nil, problem: nil)
        }
    }

    var body: some View {
        FormSection(title: "Skills for local turns",
                    footer: "Your instructions for the local model on the turns keepmum keeps on this Mac. Write and edit them in the Skills window.",
                    details: "Your own instructions for the local model, in Claude Code's SKILL.md format, kept in keepmum's folder (~/.config/keepmum/skills): write, import, edit and delete them in the Skills window. An imported skill is copied there, so editing it leaves the original alone. Each skill goes where you choose: answering a flagged tool result, summaries and rewrites for the cloud, or answering a flagged request (none: parked). Each local turn there gets its text after keepmum's note, which says why the turn stayed on this Mac and that the model has no tools. Edits apply on the next turn; one that cannot be read, or is over 20,000 characters, is left out.") {
            if rows.isEmpty {
                FormRow(title: "No skills", detail: "Local models get keepmum's note and the agent's own instructions. keepmum's example says how to answer on these turns: a starting point to edit.") {
                    HStack(spacing: Space.s) {
                        PillButton(title: "Add the example", icon: "doc.text") {
                            Task { if let n = await engine.addExampleSkill() { SkillsWindow.show(openWindow, engine: engine, skill: n) } }
                        }
                        .disabled(!engine.isBundled || engine.busy)
                        addButton
                    }
                }
            } else {
                ForEach(Array(rows.enumerated()), id: \.offset) { i, skill in
                    row(i + 1, skill)
                    Hairline()
                }
                HStack { Spacer(); addButton }.padding(.vertical, Space.s)
            }
        }
        .task(id: engine.status?.guard?.local) { await engine.loadLocalSkills() }
    }

    @ViewBuilder private func row(_ n: Int, _ skill: Engine.LocalSkills.Skill) -> some View {
        HStack(alignment: .center, spacing: Space.m) {
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(skill.name ?? (skill.path as NSString).lastPathComponent).font(.km(.body)).foregroundStyle(Theme.text)
                    .lineLimit(1).truncationMode(.middle)
                Text(detail(skill)).font(.km(.caption))
                    .foregroundStyle(skill.problem == nil ? Theme.textSecondary : Theme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .help("Edit in the Skills window: " + skill.path)
            .contentShape(Rectangle())
            .onTapGesture { if !isSnapshot { SkillsWindow.show(openWindow, engine: engine, skill: n) } }
            Spacer(minLength: Space.m)
            placesMenu(n, skill)
            moreMenu(n, skill)
        }
        .padding(.vertical, Space.s)
    }

    private func detail(_ skill: Engine.LocalSkills.Skill) -> String {
        if let problem = skill.problem { return "Left out: " + problem }
        if skill.goesTo.isEmpty { return "Parked: kept, but sent nowhere" }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let path = skill.path.hasPrefix(home) ? "~" + skill.path.dropFirst(home.count) : skill.path
        return skill.description.isEmpty ? path : "\(skill.description) · \(path)"
    }

    @ViewBuilder private func placesMenu(_ n: Int, _ skill: Engine.LocalSkills.Skill) -> some View {
        let label = Places.summary(skill.goesTo)
        if isSnapshot {
            HStack(spacing: Space.xxs) {
                Text(label).font(.km(.callout)).foregroundStyle(skill.goesTo.isEmpty ? Theme.textSecondary : Theme.text)
                Image(systemName: "chevron.down").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
        } else {
            Menu(label) {
                ForEach(Places.all, id: \.self) { place in
                    Toggle(Places.names[place] ?? place, isOn: Binding(
                        get: { skill.goesTo.contains(place) },
                        set: { on in
                            let next = Places.all.filter { $0 == place ? on : skill.goesTo.contains($0) }
                            Task { await engine.setLocalSkill(n, places: next) }
                        }))
                }
                Divider()
                Button("Park It") { Task { await engine.setLocalSkill(n, places: []) } }.disabled(skill.goesTo.isEmpty)
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .help("The local turns that get this skill. None: parked, kept but sent nowhere.")
        }
    }

    @ViewBuilder private func moreMenu(_ n: Int, _ skill: Engine.LocalSkills.Skill) -> some View {
        if isSnapshot {
            Image(systemName: "ellipsis.circle").foregroundStyle(Theme.textSecondary)
        } else {
            Menu {
                Button("Edit…") { SkillsWindow.show(openWindow, engine: engine, skill: n) }
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: skill.path)]) }
                Divider()
                Button(skill.owned == true ? "Move to Trash" : "Remove", role: .destructive) {
                    Task { await engine.deleteLocalSkill(n) }
                }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .accessibilityLabel("More for skill \(n)")
        }
    }

    @ViewBuilder private var addButton: some View {
        if isSnapshot {
            PillButton(title: "Open Skills…", icon: "square.and.pencil") {}
        } else {
            PillButton(title: "Open Skills…", icon: "square.and.pencil") { SkillsWindow.show(openWindow, engine: engine) }
                .disabled(!engine.isBundled)
        }
    }
}

/// What follows a local turn of the injection check: stay on the local model, or hand the
/// conversation back to the cloud with the flagged result replaced (handback.py).
struct LocalHandbackSection: View {
    var engine: Engine
    @Environment(\.isSnapshot) private var isSnapshot
    @State private var editing = false

    static let choices: [(id: String, label: String, detail: String)] = [
        ("stay", "Stay local", "The conversation stays on the local model until you rewind or clear it."),
        ("answer", "Local answer, then cloud", "The local model answers the flagged turn. After that the result is withheld, and the cloud model carries on with the local answer."),
        ("summary", "Local summary, then cloud", "The local model summarises the flagged result first; the summary goes to the cloud model in its place."),
        ("rewrite", "Local rewrite, then cloud", "The local model takes out what the result says to the agent; the rest goes to the cloud model."),
        ("withhold", "Withhold, then cloud", "No local model: a notice takes the result's place, and the turn goes to the cloud model."),
    ]

    private var current: String { engine.localHandback?.handback ?? engine.status?.guard?.local?.handback ?? "stay" }
    private var own: String { engine.localHandback?.instructions ?? engine.status?.guard?.local?.handbackInstructions ?? "" }
    private var choice: (id: String, label: String, detail: String) { Self.choices.first { $0.id == current } ?? Self.choices[0] }

    var body: some View {
        FormSection(title: "After a local turn",
                    footer: "For the injection check. The cloud model never gets the flagged result, only what takes its place.",
                    details: "For the injection check. The cloud model never gets the flagged result itself. What takes its place is masked like the rest of the request and checked first: if the check flags what the local model wrote, that turn stays local. A harm hit always stays local: it is what you typed, and handing it back would send it to the provider the check kept it from.") {
            FormRow(title: "Then", detail: choice.detail) { strategyMenu }
            if current == "summary" || current == "rewrite" {
                Hairline()
                FormRow(title: "Instructions",
                        detail: own.isEmpty ? "keepmum's: keep the facts, code and data; leave out what the result says to the agent"
                                            : "Your own, \(own.count.formatted()) characters") {
                    if isSnapshot {
                        PillButton(title: "Edit…") {}
                    } else {
                        PillButton(title: "Edit…") { editing = true }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                }
            }
            if ["answer", "summary", "rewrite"].contains(current), let review = engine.localHandback?.review {
                Hairline()
                reviewRows(review)
            }
        }
        .task(id: engine.status?.guard?.local) { await engine.loadLocalHandback() }
        .sheet(isPresented: $editing) {
            HandbackInstructionsSheet(engine: engine, strategy: current, own: own,
                                      keepmums: engine.localHandback?.defaultInstructions[current] ?? "")
        }
    }

    static let reviewModes: [(id: String, label: String)] = [
        ("off", "Off"), ("rewrite", "Rewrites only"), ("all", "Answers, summaries and rewrites"),
    ]
    static let waits = [30, 60, 120, 300]

    private func seconds(_ s: Int) -> String { s < 120 ? "\(s) s" : "\(s / 60) min" }

    @ViewBuilder private func reviewRows(_ review: Engine.LocalHandback.Review) -> some View {
        let label = Self.reviewModes.first { $0.id == review.mode }?.label ?? review.mode
        let skipped = review.mode == "rewrite" && current != "rewrite"
        FormRow(title: "Review first",
                detail: review.mode == "off" ? "Off: what the local model wrote goes straight to the cloud"
                    : skipped ? "Rewrites only: this hand-back goes without a review"
                    : current == "answer" ? "After the local answer, a panel shows what the next turn sends in the result's place: send it, edit it or keep the conversation local"
                    : "A panel shows what the local model wrote: send it, edit it, withhold the result or keep the turn local",
                help: "keepmum holds the request while the panel waits for you. It does not take the keyboard: click into it to edit. Each flagged result is reviewed once; later turns reuse your answer. With the app closed (and no `keepmum review --watch`), nothing waits.") {
            choiceMenu(label, Self.reviewModes.map { m in (m.label, { await engine.setLocalHandbackReview(mode: m.id) }) })
        }
        if review.mode != "off" {
            Hairline()
            FormRow(title: "Wait", detail: "The agent waits too, up to this long") {
                choiceMenu(seconds(review.wait),
                           (Self.waits.contains(review.wait) ? Self.waits : (Self.waits + [review.wait]).sorted())
                               .map { w in (seconds(w), { await engine.setLocalHandbackReview(wait: w) }) })
            }
            Hairline()
            FormRow(title: "If no answer",
                    detail: review.timeout == "send" ? "What the local model wrote goes as written" : "The turn stays on this Mac, answered by the local model") {
                choiceMenu(review.timeout == "send" ? "Send as written" : "Keep it local",
                           [("Keep it local", { await engine.setLocalHandbackReview(timeout: "stay") }),
                            ("Send as written", { await engine.setLocalHandbackReview(timeout: "send") })])
            }
        }
    }

    @ViewBuilder private func choiceMenu(_ label: String, _ items: [(String, @MainActor () async -> Void)]) -> some View {
        if isSnapshot {
            HStack(spacing: Space.xxs) {
                Text(label).font(.km(.callout)).foregroundStyle(Theme.text)
                Image(systemName: "chevron.down").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
        } else {
            Menu(label) {
                ForEach(items.indices, id: \.self) { i in
                    Button(items[i].0) { Task { await items[i].1() } }
                }
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
        }
    }

    @ViewBuilder private var strategyMenu: some View {
        if isSnapshot {
            HStack(spacing: Space.xxs) {
                Text(choice.label).font(.km(.callout)).foregroundStyle(Theme.text)
                Image(systemName: "chevron.down").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
        } else {
            Menu(choice.label) {
                ForEach(Self.choices, id: \.id) { c in
                    Button(c.label) { Task { await engine.setLocalHandback(c.id) } }
                }
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .help("What follows a turn the injection check kept on this Mac")
        }
    }
}

/// The user's own instruction for the local model's summary or rewrite, in place of keepmum's.
struct HandbackInstructionsSheet: View {
    var engine: Engine
    var strategy: String
    var own: String
    var keepmums: String
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text("Instructions for the \(strategy)").font(.km(.headline)).foregroundStyle(Theme.text)
                Text("The local model gets these, then the flagged result and your latest message, and writes what the cloud model reads in the result's place. Say what to keep and what to leave out. Skills for the injection check follow them.")
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
            TextEditor(text: $text)
                .font(.km(.callout))
                .frame(minHeight: 220)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Theme.textSecondary.opacity(0.3)))
            HStack {
                PillButton(title: "Use keepmum's") { text = keepmums }
                    .disabled(text == keepmums)
                Spacer()
                PillButton(title: "Cancel") { dismiss() }
                PillButton(title: "Save", prominent: true) {
                    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    // keepmum's own text saved as is means keepmum's: stored as no instruction of the user's.
                    let value = trimmed == keepmums.trimmingCharacters(in: .whitespacesAndNewlines) ? "" : trimmed
                    Task { await engine.setLocalHandbackInstructions(value) }
                    dismiss()
                }
                .disabled(text.count > 4000)
            }
        }
        .padding(Space.l)
        .frame(width: 520)
        .onAppear { text = own.isEmpty ? keepmums : own }
    }
}
