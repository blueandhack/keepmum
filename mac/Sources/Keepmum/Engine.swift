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
    }

    struct Client: Decodable, Equatable {
        var installed: Bool?
        var on: Bool?
        var error: String?
        /// On, but some of its traffic still goes around keepmum (opencode 1.x with a ChatGPT login).
        var warning: String?
        /// Another gateway the user set for this tool: keepmum leaves it alone.
        var other: OtherValue?

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
    private(set) var busy = false
    var error: String?
    /// The menu bar asks the dashboard to open on Settings, where Setup is.
    var settingsRequested = false

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

    /// Register the LaunchAgent. macOS may ask the user to allow it in Login Items first.
    func startService() async {
        await act {
            do {
                try self.service.register()
            } catch let e as NSError where e.code == Int(kSMErrorAlreadyRegistered) {
                // already registered: fine
            }
            UserDefaults.standard.set(Self.version, forKey: "registeredVersion")
            if self.service.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
            try? await Task.sleep(for: .seconds(2))  // let the proxy load the model
        }
    }

    /// Stopping the service while Claude Code is pointed at it would cut Claude Code off: turn
    /// protection off first.
    func stopService() async {
        await act {
            if self.anyProtected { try await self.applyProtection(false, client: nil) }
            try await self.service.unregister()
        }
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

    // MARK: Audit (`keepmum audit`)

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

    /// For snapshots: an engine that is set up and protecting Claude Code.
    static func preview() -> Engine {
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
                          clients: ["codex": .init(installed: true, on: true), "pi": .init(installed: true, on: false)])
        e.serviceStatus = .enabled
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
        e.auditCheck = .init(ok: true, entries: 4211, first: .init(seq: 1, hash: "5c0e…"), last: .init(seq: 4211, hash: "9be41f07c2d8a35e1b60f4c9d27a8e53"), problems: [])
        return e
    }
}

struct EngineError: Error {
    var message: String
    init(_ message: String) { self.message = message }
}

private final class LockedData: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var data = Data()
    func append(_ d: Data) { lock.lock(); data.append(d); lock.unlock() }
}
