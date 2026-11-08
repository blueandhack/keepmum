import Foundation
import Observation
import ServiceManagement

/// The engine bundled in Keepmum.app (bundle-runtime.sh): keepmum's Python runtime, pf-serve, and the
/// background service that runs `keepmum serve`. Everything goes through the bundled `keepmum`
/// command, so the app and the CLI share one implementation of setup and protection.
@MainActor
@Observable
final class Engine {
    /// `keepmum status --json`.
    struct Status: Decodable, Equatable {
        struct Protect: Decodable, Equatable {
            var settings: String?
            var on: Bool?
            var baseUrl: String?
            var error: String?
        }
        var model: String?
        var modelSize: Int
        var pfServe: String?
        var protect: Protect
        var port: Int
        var serveRunning: Bool
        var excluded: [Excluded] = []
        var observe: Bool?
        /// `detect.model`: the local model after the rules (nil from older engines: on).
        var modelOn: Bool?
        var pii: Bool?
        var pass: [String]?
        var rules: Rules?
        /// Per coding agent: claude, codex, pi, opencode (`keepmum protect status`).
        var clients: [String: Client]?
        /// `[destinations]`: hosts you added, per rule, that shell commands may send its tokens to.
        var destinations: [String: [String]]?
        /// `[log] previews` and `context`: what the Logs page may show (nil from older engines: on).
        var previews: Bool?
        var context: Bool?
        /// `[audit] keep_days`.
        var keepDays: Int?
        /// The optional checks (`keepmum guard --json`); nil from older engines.
        var `guard`: Checks?
        /// What other apps added to the coding agents' settings since you last looked; nil from older engines.
        var extensions: Extensions? = nil
    }

    /// A plugin, plugin marketplace, hook or MCP server in a coding agent's settings (`keepmum extensions`).
    struct Extension: Decodable, Equatable, Identifiable {
        var id: String
        var agent: String
        var kind: String
        var name: String
        /// What it runs or where it connects; for a Claude Code plugin, its hooks and MCP servers.
        var detail: String
        var file: String
        /// It runs at every session start or prompt, where its output reaches the model as instructions.
        var addsContext: Bool
        var removable: Bool
        /// How to remove it with the agent itself, when keepmum does not edit that file.
        var how: String
        var new: Bool?
    }

    /// In `keepmum status`: what is new, for the menu bar.
    struct Extensions: Decodable, Equatable {
        var watching: Bool
        var new: [Extension]
        var count: Int?
    }

    /// `keepmum extensions --json`: every item, and what went away since you last looked.
    struct ExtensionList: Decodable, Equatable {
        struct Gone: Decodable, Equatable { var agent: String; var kind: String; var name: String }
        var items: [Extension]
        var watching: Bool
        var gone: [Gone]
        var problems: [String]
    }

    /// `keepmum guard --json`: each optional check's mode and whether its model is set up.
    struct Checks: Decodable, Equatable {
        struct Injection: Decodable, Equatable {
            var mode: String
            var installed: Bool
            /// llama-server's path (both checks run in it); nil when it is not found.
            var server: String?
            var licence: String
            /// The score at which it flags, keepmum's default, and the sensitivity levels with what
            /// each caught and flagged on the benchmark (nil from older engines).
            var threshold: Double?
            var defaultThreshold: Double?
            struct Level: Decodable, Equatable { var threshold: Double; var caught: Double; var ordinary: Double; var security: Double }
            var levels: [String: Level]?
        }
        struct Harm: Decodable, Equatable {
            var mode: String
            /// small, 0.6b or 4b.
            var model: String
            var installed: [String: Bool]
            /// llama-server's path; nil when it is not found.
            var server: String?
            var licence: String
            /// high, medium, low or lowest, and each level on this model: its threshold and what it
            /// caught and flagged on the benchmark (nil from older engines).
            var sensitivity: String?
            struct Level: Decodable, Equatable { var threshold: Double; var caught: Double; var safe: Double; var coding: Double }
            var levels: [String: Level]?
        }
        /// The commands check: rules on the shell commands in replies, no model.
        struct Commands: Decodable, Equatable {
            var mode: String
            var rules: [String]
        }
        var injection: Injection
        var harm: Harm
        /// nil from older engines.
        var commands: Commands?
        /// Where local mode sends a flagged request: the servers, in the order tried (local.py).
        struct Local: Decodable, Equatable {
            struct Server: Decodable, Equatable { var url: String; var model: String; var checks: [String]; var api: String }
            var servers: [Server]
        }
        var local: Local?
    }

    /// `keepmum guard local --json`: each local server, in the order tried, with what it is, the
    /// models it has (nil: it did not answer) and why it cannot answer yet; and the usual servers'
    /// URLs, for Add.
    struct LocalServers: Decodable, Equatable {
        struct Server: Decodable, Equatable {
            var url: String
            var model: String
            var checks: [String]
            var api: String
            var kind: String?
            var models: [String]?
            var problem: String?
        }
        struct Preset: Decodable, Equatable, Identifiable { var id: String; var name: String; var url: String }
        var servers: [Server]
        var presets: [Preset]
    }
    private(set) var localServers: LocalServers?

    /// `keepmum rules builtin --json`: one built-in rule, its type, and whether it is on.
    struct BuiltinRule: Decodable, Equatable, Identifiable {
        var id: String
        var type: String
        var on: Bool
    }

    /// `keepmum destinations --json`: each issuer's own hosts, and the rules that find secrets.
    struct DestinationInfo: Decodable, Equatable {
        var issuers: [String: [String]]
        var rules: [String]
    }

    /// `keepmum doctor --json`.
    struct DoctorCheck: Decodable, Equatable, Identifiable {
        var status: String
        var text: String
        var id: String { status + text }
    }

    struct Client: Decodable, Equatable {
        var installed: Bool?
        var on: Bool?
        var error: String?
        /// On, but some of its traffic still goes around keepmum (opencode 1.x with a ChatGPT login).
        var warning: String?
        /// Another gateway the user set for this tool: turning protection on puts keepmum in front of it.
        var other: OtherValue?
        /// Where that other setting goes (`other` is a provider name for Codex).
        var otherUrl: String?
        /// On, in front of another gateway: where keepmum forwards this agent's requests.
        var via: String?
        /// pi and opencode: the providers that go on to `via` when the others go to their provider directly.
        var viaProviders: [String]?
        /// Straight to a gateway keepmum was in front of: its app most likely wrote itself back.
        var around: String?

        /// Codex reports one provider name, pi and opencode a list of provider names.
        enum OtherValue: Decodable, Equatable {
            case one(String), many([String])
            init(from decoder: Decoder) throws {
                let c = try decoder.singleValueContainer()
                if let s = try? c.decode(String.self) { self = .one(s) } else { self = .many((try? c.decode([String].self)) ?? []) }
            }
            var text: String { switch self { case .one(let s): s; case .many(let a): a.joined(separator: ", ") } }
        }
    }

    /// `[rules] mask` and the allowlist, as `keepmum status` reports them.
    struct Rules: Decodable, Equatable {
        struct Mask: Decodable, Equatable, Identifiable {
            var name: String
            var pattern: String
            var type: String
            var id: String { name + pattern }
        }
        var mask: [Mask] = []
        var allowStrings: [String] = []
        var allowPatterns: [String] = []
        /// Built-in rules turned off (`keepmum rules off`).
        var off: [String] = []
    }

    /// A project sent straight to the API, around keepmum (`keepmum exclude`).
    struct Excluded: Decodable, Equatable, Identifiable {
        var path: String
        var url: String
        var active: Bool
        var id: String { path }
    }

    /// One entry of the audit log (`keepmum audit list --json`): what happened, never a value.
    struct AuditEntry: Decodable, Equatable, Identifiable {
        var seq: Int
        var ts: Double
        var event: String
        var summary: String
        var weakens: Bool
        var by: String?
        var id: Int { seq }
        var date: Date { Date(timeIntervalSince1970: ts) }
    }

    /// `keepmum audit verify --json`: whether the chain holds, checked against the head the app saved last time.
    struct AuditCheck: Decodable, Equatable {
        struct Head: Decodable, Equatable {
            var seq: Int
            var hash: String
        }
        var ok: Bool
        var entries: Int
        var first: Head?
        var last: Head?
        var problems: [String]
    }

    private(set) var status: Status?
    private(set) var builtinRules: [BuiltinRule] = []
    private(set) var destinationInfo: DestinationInfo?
    /// The last `keepmum doctor` run, and whether one is under way.
    private(set) var doctor: [DoctorCheck]?
    private(set) var doctorRunning = false
    /// After an export: where it went, or why it failed.
    var exportNote: String?
    private(set) var audit: [AuditEntry] = []
    private(set) var auditCheck: AuditCheck?
    /// The head saved at the last good check: a rewrite of the log since then fails against it.
    @ObservationIgnored private var auditAnchor: String? {
        get { UserDefaults.standard.string(forKey: "auditAnchor") }
        set { UserDefaults.standard.set(newValue, forKey: "auditAnchor") }
    }
    private(set) var serviceStatus: SMAppService.Status = .notRegistered
    /// 0...1 while the model downloads.
    private(set) var download: Double?
    /// A check's model downloading: which (injection, harm small, harm 0.6b, harm 4b), and 0...1.
    private(set) var checkDownload: (what: String, progress: Double)?
    private(set) var busy = false
    /// The background service on its way up or down: from the click until it answers, or stops.
    enum ServiceChange { case starting, stopping }
    private(set) var serviceChange: ServiceChange?
    var error: String?
    /// The menu bar asks the dashboard to open Settings on a pane: Setup, or Tools to review one.
    var settingsRequested: SettingsPane?
    /// The menu bar asks the dashboard to open a page: Logs, for a script keepmum could not check.
    var pageRequested: Page?

    /// Contents/Resources/keepmum. Missing when the app was built without the engine (SKIP_RUNTIME).
    let cli = Bundle.main.url(forResource: "keepmum", withExtension: nil)
    private let service = SMAppService.agent(plistName: "app.keepmum.agent.plist")
    private var task: Task<Void, Never>?

    private(set) var previewing = false
    var isBundled: Bool { cli != nil || previewing }
    var hasModel: Bool { status?.model != nil }
    /// The model is wanted (`keepmum model on`, the default), or the rules work alone.
    var modelOn: Bool { status?.modelOn ?? true }
    /// Enough to scan: the model downloaded, or the rules alone.
    var canScan: Bool { hasModel || !modelOn }
    var serviceRunning: Bool { status?.serveRunning == true }
    var protected: Bool { status?.protect.on == true }

    func client(_ id: String) -> Client? {
        status?.clients?[id] ?? (id == "claude" ? Client(installed: true, on: protected, error: status?.protect.error) : nil)
    }
    var anyProtected: Bool { protected || (status?.clients?.values.contains { $0.on == true } ?? false) }
    /// The first coding agent that goes straight to a gateway keepmum is in front of: (id, name, URL).
    var aroundClient: (id: String, name: String, url: String)? {
        for (id, name) in agentNames { if let url = client(id)?.around { return (id, name, url) } }
        return nil
    }
    let agentNames = [("claude", "Claude Code"), ("codex", "Codex"), ("pi", "pi"), ("opencode", "opencode")]
    var setupDone: Bool { canScan && serviceRunning && anyProtected }

    func start() {
        guard task == nil, isBundled else { return }
        task = Task { [weak self] in
            await self?.reregisterAfterUpdate()
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(15))
            }
        }
    }

    func refresh() async {
        serviceStatus = service.status
        guard let out = try? await run(["status", "--json"]), out.code == 0 else { return }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        if let s = try? decoder.decode(Status.self, from: Data(out.stdout.utf8)) { status = s }
    }

    /// `keepmum setup`: the model, once, with progress from its "NN% of 1.6 GB" lines.
    func downloadModel() async {
        await act {
            self.download = 0
            defer { self.download = nil }
            let out = try await self.run(["setup"]) { line in
                if let pct = line.split(separator: "%").first.flatMap({ Double($0.trimmingCharacters(in: .whitespaces)) }) {
                    Task { @MainActor in self.download = pct / 100 }
                }
            }
            if out.code != 0 { throw EngineError(out.lastLine) }
        }
    }

    /// Register the LaunchAgent, then wait until the proxy answers: loading the model takes a few
    /// seconds. macOS may ask the user to allow it in Login Items first.
    func startService() async {
        await act {
            self.serviceChange = .starting
            defer { self.serviceChange = nil }
            try self.registerService()
            if self.service.status == .requiresApproval {
                SMAppService.openSystemSettingsLoginItems()
                return
            }
            try await self.waitForService(running: true)
        }
    }

    /// Stopping the service while Claude Code is pointed at it would cut Claude Code off: turn
    /// protection off first.
    func stopService() async {
        await act {
            self.serviceChange = .stopping
            defer { self.serviceChange = nil }
            if self.anyProtected { try await self.applyProtection(false, client: nil) }
            try await self.service.unregister()
            try await self.waitForService(running: false)
        }
    }

    /// Registered but not answering: unregister and register again. Protection stays as it is, so
    /// the agents pointed at keepmum reach it again once it answers.
    func restartService() async {
        await act {
            self.serviceChange = .starting
            defer { self.serviceChange = nil }
            try? await self.service.unregister()
            try self.registerService()
            try await self.waitForService(running: true)
        }
    }

    private func registerService() throws {
        do {
            try service.register()
        } catch let e as NSError where e.code == Int(kSMErrorAlreadyRegistered) {
            // already registered: fine
        }
        UserDefaults.standard.set(Self.version, forKey: "registeredVersion")
    }

    /// Check the service until it is up (or down), so the page moves on the moment it does.
    private func waitForService(running: Bool) async throws {
        let seconds = running ? 60.0 : 15.0
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            await refresh()
            if serviceRunning == running { return }
            if running && service.status != .enabled { return }  // unregistered, or waiting for approval
            try await Task.sleep(for: .milliseconds(700))
        }
        throw EngineError(running ? "The background service did not answer within \(Int(seconds)) seconds. Run keepmum serve in a terminal to see why."
                                  : "The background service still answers after \(Int(seconds)) seconds.")
    }

    /// One coding agent (claude, codex, pi, opencode) on or off; `client: nil` is every one of them.
    func setProtection(_ on: Bool, client: String? = "claude") async {
        await act { try await self.applyProtection(on, client: client) }
    }

    private func applyProtection(_ on: Bool, client: String?) async throws {
        if on && !serviceRunning { throw EngineError("Start the background service first.") }
        let out = try await run(["protect", on ? "on" : "off"] + (client.map { ["--client", $0] } ?? []))
        if out.code != 0 { throw EngineError(out.lastLine) }
    }

    func openLoginItems() { SMAppService.openSystemSettingsLoginItems() }

    /// The result of the last exclude or include, for the card to show.
    var exclusionNote: String?

    /// Observe only (`keepmum observe on`): count, but send requests unmasked. Running proxies
    /// pick it up on their next request.
    func setObserve(_ on: Bool) async {
        await act {
            let out = try await self.run(["observe", on ? "on" : "off"])
            if out.code != 0 { throw EngineError(out.lastLine) }
        }
    }

    /// The rules alone, or the rules and the local model (`keepmum model`). Turning the model on
    /// downloads it first if needed. Running proxies pick it up on their next request.
    func setModel(_ on: Bool) async {
        if on && !hasModel {
            await downloadModel()
            await refresh()
            guard hasModel else { return }
        }
        await act {
            let out = try await self.run(["model", on ? "on" : "off"])
            if out.code != 0 { throw EngineError(out.lastLine) }
        }
    }

    /// Mask personal data too, or only secrets and your own rules (`keepmum pii`). Running proxies
    /// pick it up on their next request.
    func setPII(_ on: Bool) async {
        await act { try await self.check(["pii", on ? "on" : "off"]) }
    }

    // MARK: The optional checks (`keepmum guard`)

    /// Download a check's model with progress from its "NN% of N MB" lines.
    func setUpCheck(_ check: String, model: String = "small") async {
        await act {
            let what = check == "harm" ? "harm \(model)" : "injection"
            self.checkDownload = (what, 0)
            defer { self.checkDownload = nil }
            var args = ["guard", "setup", check]
            if check == "harm" { args += ["--model", model] }
            let out = try await self.run(args) { line in
                if let pct = line.split(separator: "%").first.flatMap({ Double($0.trimmingCharacters(in: .whitespaces)) }) {
                    Task { @MainActor in self.checkDownload = (what, pct / 100) }
                }
            }
            if out.code != 0 { throw EngineError(out.lastLine) }
        }
    }

    /// A check's mode: off, warn or block; for harm, also its model size. Running proxies switch on
    /// their next request.
    /// The injection check's sensitivity: a level (high, medium, low, lowest) or a number.
    func setInjectionThreshold(_ value: String) async {
        await act { try await self.check(["guard", "sensitivity", "injection", value]) }
    }

    /// The harm check's sensitivity: high, medium, low or lowest, on whichever model it uses.
    func setHarmSensitivity(_ level: String) async {
        await act { try await self.check(["guard", "sensitivity", "harm", level]) }
    }

    func setCheck(_ check: String, mode: String, model: String? = nil) async {
        await act { try await self.check(["guard", check, mode] + (model.map { ["--model", $0] } ?? [])) }
    }

    /// The local servers and what each has, for Settings › Checks. Asks each (on this Mac) every time.
    func loadLocalServers() async {
        guard let out = try? await run(["guard", "local", "--json"]), out.code == 0,
              let m = try? JSONDecoder().decode(LocalServers.self, from: Data(out.stdout.utf8)) else { return }
        localServers = m
    }

    /// Adds a local server, last in the order: one of the usual ones, or a URL on this Mac. Its
    /// model is chosen after, from what it has.
    func addLocalServer(preset: String? = nil, url: String? = nil) async {
        var args = ["guard", "local", "add"]
        if let preset { args += ["--preset", preset] }
        if let url { args += ["--url", url] }
        await act { try await self.check(args) }
        await loadLocalServers()
    }

    /// Changes local server `number` (1 is tried first): its model, or the checks it answers for.
    func setLocalServer(_ number: Int, model: String? = nil, checks: [String]? = nil) async {
        var args = ["guard", "local", "set", String(number)]
        if let model { args += ["--model", model] }
        for c in checks ?? [] { args += ["--check", c] }
        await act { try await self.check(args) }
        await loadLocalServers()
    }

    func moveLocalServer(_ number: Int, to: Int) async {
        await act { try await self.check(["guard", "local", "move", String(number), String(to)]) }
        await loadLocalServers()
    }

    func removeLocalServer(_ number: Int) async {
        await act { try await self.check(["guard", "local", "remove", String(number)]) }
        await loadLocalServers()
    }

    /// Turn a check off and delete its model.
    func removeCheck(_ check: String) async {
        await act { try await self.check(["guard", "remove", check]) }
    }

    // MARK: What gets masked (`keepmum types`, `keepmum rules`)

    /// Let a type through unmasked, or mask it again. Secrets need `confirmed`.
    func setPass(_ type: String, _ pass: Bool, confirmed: Bool = false) async {
        await act {
            var args = ["types", pass ? "pass" : "mask", type]
            if confirmed { args.append("--yes") }
            try await self.check(args)
        }
    }

    func addMaskRule(name: String, pattern: String, type: String) async {
        await act { try await self.check(["rules", "mask", "--name", name, "--type", type, "--", pattern]) }
    }

    func addException(_ value: String, pattern: Bool) async {
        await act { try await self.check(["rules", "allow"] + (pattern ? ["--pattern"] : []) + ["--", value]) }
    }

    /// Turn a built-in rule off, or back on. Rules that find secrets need `confirmed`.
    func setRuleOff(_ rule: String, _ off: Bool, confirmed: Bool = false) async {
        await act { try await self.check(["rules", off ? "off" : "on", rule] + (confirmed ? ["--yes"] : [])) }
    }

    /// kind: mask, allow or allow-pattern; index as `keepmum rules list` numbers them.
    func removeRule(_ kind: String, _ index: Int) async {
        await act { try await self.check(["rules", "remove", kind, String(index)]) }
    }

    // MARK: Extensions (`keepmum extensions`): what other apps added to the coding agents

    private(set) var extensionList: ExtensionList?

    func loadExtensions() async {
        if previewing {
            extensionList = .init(items: Self.sampleExtensions, watching: true, gone: [], problems: [])
            return
        }
        guard let out = try? await run(["extensions", "--json"]), out.code == 0 else { return }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        if let list = try? decoder.decode(ExtensionList.self, from: Data(out.stdout.utf8)) { extensionList = list }
    }

    /// Mark items as yours; none: every new one.
    func keepExtensions(_ ids: [String] = []) async {
        await act { try await self.check(["extensions", "keep"] + ids) }
        await loadExtensions()
    }

    /// Take one item out of its agent's settings, after a backup (`keepmum extensions remove`).
    func removeExtension(_ id: String) async {
        await act { try await self.check(["extensions", "remove", id]) }
        await loadExtensions()
    }

    /// For snapshots: an ordinary setup, and what the Postman app added to Codex and Claude Code in October 2026.
    static let sampleExtensions: [Extension] = [
        .init(id: "a1", agent: "Codex", kind: "trusted hook", name: "postman@postman",
              detail: "postman@postman:hooks/codex-hooks.json:session_start:0:0", file: "~/.codex/config.toml",
              addsContext: true, removable: true, how: "", new: true),
        .init(id: "a2", agent: "Codex", kind: "plugin", name: "postman@postman", detail: "", file: "~/.codex/config.toml",
              addsContext: false, removable: true, how: "", new: true),
        .init(id: "a3", agent: "Codex", kind: "MCP server", name: "postman", detail: "https://mcp.postman.com/mcp",
              file: "~/.codex/config.toml", addsContext: false, removable: true, how: "", new: true),
        .init(id: "a4", agent: "Claude Code", kind: "plugin", name: "postman@claude-plugins-official",
              detail: "hooks: SessionStart; MCP: postman", file: "~/.claude/settings.json",
              addsContext: true, removable: true, how: "", new: true),
        .init(id: "b1", agent: "Claude Code", kind: "plugin", name: "frontend-design@claude-plugins-official", detail: "",
              file: "~/.claude/settings.json", addsContext: false, removable: true, how: "", new: false),
        .init(id: "b2", agent: "Claude Code", kind: "hook", name: "Stop", detail: "afplay /System/Library/Sounds/Glass.aiff",
              file: "~/.claude/settings.json", addsContext: false, removable: true, how: "", new: false),
        .init(id: "b3", agent: "Codex", kind: "plugin", name: "github@openai-curated", detail: "", file: "~/.codex/config.toml",
              addsContext: false, removable: true, how: "", new: false),
        .init(id: "b4", agent: "Gemini CLI", kind: "extension", name: "code-review", detail: "", file: "~/.gemini/extensions/code-review",
              addsContext: false, removable: false, how: "gemini extensions uninstall code-review", new: false),
    ]

    // MARK: Tools (`keepmum tools`): what each network tool may receive

    /// setting: placeholders, own, contacts or default. A public tool (web search) given more needs `confirmed`.
    func setTool(_ server: String, _ setting: String, confirmed: Bool = false) async {
        await act { try await self.check(["tools", "set", server, setting] + (confirmed ? ["--yes"] : [])) }
    }

    func allowTool(_ server: String, _ types: [String], confirmed: Bool = false) async {
        await act { try await self.check(["tools", "allow", server] + types + (confirmed ? ["--yes"] : [])) }
    }

    /// A server whose tools changed: trust them as they are now.
    func trustTool(_ server: String) async {
        await act { try await self.check(["tools", "trust", server]) }
    }

    /// Keep every new server on its setting, and stop pointing at them.
    func toolsReviewed() async {
        await act { try await self.check(["tools", "reviewed"]) }
    }

    // MARK: Shell destinations (`keepmum destinations`), the Logs page (`keepmum log`)

    func loadDestinationInfo() async {
        if previewing {
            destinationInfo = .init(issuers: ["github-token": ["github.com", "githubusercontent.com", "ghcr.io"],
                                              "gitlab-token": ["gitlab.com"]], rules: ["github-token", "gitlab-token", "vault-token"])
            return
        }
        if destinationInfo != nil { return }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        if let out = try? await run(["destinations", "--json"]), out.code == 0 {
            destinationInfo = try? decoder.decode(DestinationInfo.self, from: Data(out.stdout.utf8))
        }
    }

    /// Let shell commands send a rule's tokens to one more host. Weakens protection: the UI asks first.
    func addDestination(_ rule: String, _ host: String) async {
        await act { try await self.check(["destinations", "add", rule, host, "--yes"]) }
    }

    func removeDestination(_ rule: String, _ host: String) async {
        await act { try await self.check(["destinations", "remove", rule, host]) }
    }

    /// key: previews or context.
    func setLog(_ key: String, _ on: Bool) async {
        await act { try await self.check(["log", key, on ? "on" : "off"]) }
    }

    // MARK: Built-in rules, the doctor

    func previewRules() {
        builtinRules = [.init(id: "github-token", type: "SECRET", on: true), .init(id: "gitlab-token", type: "SECRET", on: true),
                        .init(id: "key-in-prose", type: "SECRET", on: true), .init(id: "email", type: "EMAIL", on: true),
                        .init(id: "phone-labeled", type: "PHONE", on: false), .init(id: "us-ssn", type: "ACCOUNT", on: true)]
    }

    func loadBuiltinRules() async {
        if previewing { previewRules(); return }
        if let out = try? await run(["rules", "builtin", "--json"]), out.code == 0,
           let rules = try? JSONDecoder().decode([BuiltinRule].self, from: Data(out.stdout.utf8)) {
            builtinRules = rules
        }
    }

    /// `keepmum doctor`: this Mac, the model (it loads it once to test it), the files, and anything
    /// that routes an agent around keepmum. Read-only.
    func runDoctor() async {
        guard !doctorRunning else { return }
        doctorRunning = true
        defer { doctorRunning = false }
        struct Report: Decodable { var checks: [DoctorCheck] }
        do {
            let out = try await run(["doctor", "--json"])
            if let report = try? JSONDecoder().decode(Report.self, from: Data(out.stdout.utf8)) {
                doctor = report.checks
            } else {
                doctor = [.init(status: "fail", text: out.lastLine)]
            }
        } catch {
            doctor = [.init(status: "fail", text: (error as? EngineError)?.message ?? error.localizedDescription)]
        }
    }

    // MARK: Audit (`keepmum audit`)

    /// How long the log keeps entries. A shorter time weakens protection: the UI asks first.
    func setAuditKeep(_ days: Int) async {
        await act { try await self.check(["audit", "keep", String(days)]) }
    }

    /// Every entry, requests included: JSONL (as written, so its hashes still check) or CSV.
    func exportAudit(to url: URL, csv: Bool) async {
        exportNote = nil
        do {
            let out = try await run(["audit", "export", "--since", "3650d", "--format", csv ? "csv" : "jsonl", "--output", url.path])
            exportNote = out.code == 0 ? out.lastLine.replacingOccurrences(of: url.path, with: url.lastPathComponent).capitalizedFirst
                                       : out.lastLine
        } catch {
            exportNote = (error as? EngineError)?.message ?? error.localizedDescription
        }
    }

    /// The audit log's events other than requests, and a check of its chain. The head of a good
    /// check is kept, so the next check also catches the whole log being rewritten in between.
    func loadAudit() async {
        if previewing { return }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        if let out = try? await run(["audit", "list", "--json", "--no-requests", "--since", "3650d", "--limit", "500"]), out.code == 0,
           let entries = try? decoder.decode([AuditEntry].self, from: Data(out.stdout.utf8)) {
            audit = entries.reversed()
        }
        let args = ["audit", "verify", "--json"] + (auditAnchor.map { ["--anchor", $0] } ?? [])
        if let out = try? await run(args), let check = try? decoder.decode(AuditCheck.self, from: Data(out.stdout.utf8)) {
            auditCheck = check
            if check.ok, let last = check.last { auditAnchor = "\(last.seq):\(last.hash)" }
        }
    }

    private func check(_ args: [String]) async throws {
        let out = try await run(args)
        if out.code != 0 { throw EngineError(out.lastLine) }
    }

    func exclude(_ folder: URL) async {
        await act {
            let out = try await self.run(["exclude", folder.path])
            if out.code != 0 { throw EngineError(out.lastLine) }
            self.exclusionNote = "Excluded. It applies once Claude Code trusts the folder."
        }
    }

    func include(_ path: String) async {
        await act {
            let out = try await self.run(["include", path])
            if out.code != 0 { throw EngineError(out.lastLine) }
            self.exclusionNote = "Scanned again. Restart Claude Code sessions open in that project."
        }
    }

    // MARK: -

    private static let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"

    /// launchd keeps running the old executable after an update until the agent is registered again.
    private func reregisterAfterUpdate() async {
        guard service.status == .enabled, UserDefaults.standard.string(forKey: "registeredVersion") != Self.version else { return }
        try? await service.unregister()
        try? service.register()
        UserDefaults.standard.set(Self.version, forKey: "registeredVersion")
    }

    private func act(_ body: @escaping @MainActor () async throws -> Void) async {
        guard !busy else { return }
        busy = true
        error = nil
        do { try await body() } catch { self.error = (error as? EngineError)?.message ?? error.localizedDescription }
        await refresh()
        busy = false
    }

    struct Output {
        var code: Int32
        var stdout: String
        var stderr: String
        var lastLine: String {
            stderr.split(whereSeparator: { $0 == "\n" || $0 == "\r" }).last.map(String.init) ?? "keepmum exited with \(code)"
        }
    }

    /// Run the bundled `keepmum` off the main thread. `onLine` sees stderr lines as they arrive.
    private func run(_ args: [String], onLine: (@Sendable (String) -> Void)? = nil) async throws -> Output {
        guard let cli else { throw EngineError("This copy of Keepmum has no bundled engine.") }
        return try await Task.detached {
            let p = Process()
            p.executableURL = cli
            p.arguments = args
            // The audit log records settings changes as made from the app.
            p.environment = ProcessInfo.processInfo.environment.merging(["KEEPMUM_CALLER": "Keepmum.app"]) { _, new in new }
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            let collected = LockedData()
            err.fileHandleForReading.readabilityHandler = { h in
                let chunk = h.availableData
                collected.append(chunk)
                if let onLine, let text = String(data: chunk, encoding: .utf8) {
                    for line in text.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) { onLine(String(line)) }
                }
            }
            try p.run()
            let stdout = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            err.fileHandleForReading.readabilityHandler = nil
            collected.append(err.fileHandleForReading.readDataToEndOfFile())
            return Output(code: p.terminationStatus, stdout: String(decoding: stdout, as: UTF8.self),
                          stderr: String(decoding: collected.data, as: UTF8.self))
        }.value
    }

    /// For snapshots: an engine that is set up and protecting Claude Code; with `starting`, one
    /// whose background service was just started and has yet to answer.
    /// With `around`, Claude Code was pointed back at the gateway keepmum was in front of.
    static func preview(starting: Bool = false, around: Bool = false) -> Engine {
        let e = Engine()
        e.previewing = true
        e.status = Status(model: "~/.local/share/keepmum/models/privacy-filter-q8.gguf", modelSize: 1_637_572_192,
                          pfServe: "Keepmum.app/Contents/Resources/bin/pf-serve",
                          protect: .init(settings: "~/.claude/settings.json", on: true, baseUrl: "http://127.0.0.1:7788"),
                          port: 7788, serveRunning: true,
                          excluded: [.init(path: "~/Projects/open-source-docs", url: "https://api.anthropic.com", active: true)],
                          pii: true, pass: ["PERSON", "DATE"],
                          rules: .init(mask: [.init(name: "Employee ID", pattern: "EMP-\\d{6}", type: "ACCOUNT")],
                                       allowStrings: ["sk_test_docs_example"], allowPatterns: ["^demo-[a-z]+$"], off: ["phone-labeled"]),
                          clients: ["codex": .init(installed: true, on: true, via: "http://127.0.0.1:3425/v1"),
                                    "pi": .init(installed: true, on: false, other: .many(["openai"]), otherUrl: "https://llm.corp.example/v1")],
                          destinations: ["gitlab-token": ["gitlab.corp.example"]], previews: true, context: true, keepDays: 90,
                          guard: .init(injection: .init(mode: "warn", installed: true,
                                                        server: "Keepmum.app/Contents/Resources/bin/llama-server", licence: "Apache-2.0",
                                                        threshold: 0.8, defaultThreshold: 0.65,
                                                        levels: ["high": .init(threshold: 0.65, caught: 0.993, ordinary: 0.017, security: 0.069),
                                                                 "medium": .init(threshold: 0.8, caught: 0.972, ordinary: 0, security: 0.05),
                                                                 "low": .init(threshold: 0.9, caught: 0.938, ordinary: 0, security: 0.04),
                                                                 "lowest": .init(threshold: 0.98, caught: 0.882, ordinary: 0, security: 0.01)]),
                                       harm: .init(mode: "block", model: "small", installed: ["small": true, "0.6b": false, "4b": false],
                                                   server: "Keepmum.app/Contents/Resources/bin/llama-server", licence: "Apache-2.0",
                                                   sensitivity: "medium",
                                                   levels: ["high": .init(threshold: 0.59, caught: 0.784, safe: 0.099, coding: 0.136),
                                                            "medium": .init(threshold: 0.82, caught: 0.662, safe: 0.047, coding: 0.068),
                                                            "low": .init(threshold: 0.89, caught: 0.586, safe: 0.031, coding: 0.045),
                                                            "lowest": .init(threshold: 0.955, caught: 0.479, safe: 0.02, coding: 0)]),
                                       commands: .init(mode: "warn", rules: ["a download run as a script", "decoded text run as a script",
                                                                             "deleting your home folder or the system", "erasing a disk",
                                                                             "a fork bomb", "credential files sent over the network",
                                                                             "environment variables sent over the network"]),
                                       local: .init(servers: [
                                           .init(url: "http://localhost:11434", model: "qwen3:8b", checks: ["injection", "harm"], api: "auto"),
                                           .init(url: "http://localhost:1234", model: "qwen/qwen3-14b", checks: ["harm"], api: "auto"),
                                           .init(url: "http://localhost:8080", model: "", checks: ["injection", "harm"], api: "auto")])))
        e.localServers = .init(servers: [
            .init(url: "http://localhost:11434", model: "qwen3:8b", checks: ["injection", "harm"], api: "auto", kind: "Ollama",
                  models: ["gemma3:12b", "qwen3:8b"], problem: nil),
            .init(url: "http://localhost:1234", model: "qwen/qwen3-14b", checks: ["harm"], api: "auto", kind: "LM Studio",
                  models: ["qwen/qwen3-14b"], problem: nil),
            .init(url: "http://localhost:8080", model: "", checks: ["injection", "harm"], api: "auto", kind: nil,
                  models: nil, problem: "no model chosen yet")],
            presets: [.init(id: "ollama", name: "Ollama", url: "http://localhost:11434"),
                      .init(id: "lmstudio", name: "LM Studio", url: "http://localhost:1234"),
                      .init(id: "llamacpp", name: "llama.cpp", url: "http://localhost:8080"),
                      .init(id: "mlx", name: "MLX", url: "http://localhost:8080")])
        e.status?.extensions = .init(watching: true, new: sampleExtensions.filter { $0.new == true }, count: sampleExtensions.count)
        e.extensionList = .init(items: sampleExtensions, watching: true, gone: [], problems: [])
        e.serviceStatus = .enabled
        if around {
            e.status?.protect = .init(settings: "~/.claude/settings.json", on: false, baseUrl: "http://127.0.0.1:3425")
            e.status?.clients?["claude"] = .init(installed: true, on: false, other: .one("http://127.0.0.1:3425"),
                                                 otherUrl: "http://127.0.0.1:3425", around: "http://127.0.0.1:3425")
        }
        if starting {
            e.status?.serveRunning = false
            e.serviceChange = .starting
            e.busy = true
        }
        let now = Date().timeIntervalSince1970
        e.audit = [
            .init(seq: 4211, ts: now - 600, event: "config", summary: "detect.observe true -> false", weakens: false, by: "keepmum observe off (from Keepmum.app)"),
            .init(seq: 4188, ts: now - 2_400, event: "config", summary: "detect.observe false -> true", weakens: true, by: "keepmum observe on (from Keepmum.app)"),
            .init(seq: 3906, ts: now - 9_000, event: "exclude", summary: "~/Projects/open-source-docs excluded: goes straight to the provider, unscanned", weakens: true, by: "keepmum exclude (from Keepmum.app)"),
            .init(seq: 3874, ts: now - 26_000, event: "start", summary: "keepmum serve started (pid 812, rules + pf-serve)", weakens: false, by: nil),
            .init(seq: 3873, ts: now - 26_100, event: "stop", summary: "keepmum serve stopped (pid 640)", weakens: false, by: nil),
            .init(seq: 2950, ts: now - 90_000, event: "config", summary: "allowlist.strings +3f9a0c21b7e4", weakens: true, by: "keepmum rules allow (from Keepmum.app)"),
            .init(seq: 2911, ts: now - 95_000, event: "config", summary: "rules.off +phone-labeled", weakens: true, by: "keepmum rules off (from Keepmum.app)"),
            .init(seq: 1, ts: now - 400_000, event: "config", summary: "settings recorded; detect.pii false -> true", weakens: false, by: "keepmum serve"),
        ]
        e.destinationInfo = .init(issuers: ["github-token": ["github.com", "githubusercontent.com", "ghcr.io"],
                                            "gitlab-token": ["gitlab.com"]], rules: ["github-token", "gitlab-token", "vault-token"])
        e.doctor = [.init(status: "ok", text: "macOS 15.4 (14 or later)"),
                    .init(status: "ok", text: "model runs on MTL: Apple M4 and catches a canary"),
                    .init(status: "ok", text: "audit log intact (4211 entries)"),
                    .init(status: "warn", text: "~/.config/keepmum/config.toml is readable by other users (mode 644); chmod 600 recommended"),
                    .init(status: "ok", text: "Claude Code goes through keepmum")]
        e.auditCheck = .init(ok: true, entries: 4211, first: .init(seq: 1, hash: "5c0e…"), last: .init(seq: 4211, hash: "9be41f07c2d8a35e1b60f4c9d27a8e53"), problems: [])
        return e
    }
}

struct EngineError: Error {
    var message: String
    init(_ message: String) { self.message = message }
}

extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

private final class LockedData: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var data = Data()
    func append(_ d: Data) { lock.lock(); data.append(d); lock.unlock() }
}
