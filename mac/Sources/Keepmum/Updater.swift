import AppKit
import Observation
import Sparkle

/// App updates with Sparkle: a signed feed at keepmum.app (SUFeedURL, SURequireSignedFeed) lists each
/// release's notarized DMG with its EdDSA signature (SUPublicEDKey), checked before it is unpacked.
///
/// Nothing is checked until the user says so. On the first launch keepmum asks, once, whether to
/// check automatically (once a day); Settings › General › Updates changes it, and "Check now" always works.
/// Sparkle's own permission prompt is off, and it sends no system profile. A check is one request
/// for the feed: Cloudflare, which serves keepmum.app, sees the IP address and keepmum's version,
/// and keepmum.app counts checks per hour and version (worker/), keeping no addresses. An update's DMG
/// comes through keepmum.app/update/ too, counted the same way, from GitHub Releases.
///
/// So that keepmum.app can count each Mac once a day, week and month without an identifier, a check
/// says which of those it is this Mac's first in (`first=day,week,month` on the feed URL, UTC
/// periods, weeks from Monday), and `new` on the first check this Mac ever makes. The day's first
/// check also says the Mac's macOS major version and chip (`os=15&chip=arm64`), so keepmum.app can
/// count Macs per macOS version once a day. Nothing else, and none of it with Count this Mac off.
///
/// keepmum runs in the menu bar, so a scheduled check that finds an update does not interrupt: the
/// menu shows it (`available`, Sparkle's gentle reminders), and Install opens Sparkle's window.
/// Builds without a feed and key in Info.plist (development builds) have no updater.
///
/// keepmum.app's feed and downloads go through a Cloudflare Worker (worker/). If its feed does not
/// load (the Worker down, over its daily limit, or gone), the check is made again, quietly, against
/// the same signed feed on GitHub; if that one loads, checks read GitHub for three days, update
/// downloads come straight from GitHub Releases, and nothing is counted (no `first`, `os` or
/// `chip` goes to GitHub). Offline, both fail and nothing changes.
@MainActor @Observable
final class Updates: NSObject {
    static let shared = Updates()

    /// The version a scheduled check found, until the user looks at it.
    private(set) var available: String?
    /// Sparkle can start a check now (not one under way).
    private(set) var canCheck = false
    private(set) var lastCheck: Date?
    private(set) var automaticallyChecks = false
    private(set) var automaticallyDownloads = false

    @ObservationIgnored private var controller: SPUStandardUpdaterController?
    @ObservationIgnored private var observers: [NSKeyValueObservation] = []
    @ObservationIgnored private var started = false

    static let promptedKey = "updatesPrompted"
    static let countedKey = "updatesCounted"
    /// The UTC day, week and month of the last check keepmum.app counted, and whether this Mac's
    /// first check is still to be counted (`new`).
    static let lastCountedKeys = (day: "countedDay", week: "countedWeek", month: "countedMonth")
    static let newKey = "countNew"
    /// Until when checks read the feed on GitHub, keepmum.app's having failed while GitHub's loaded.
    static let fallbackUntilKey = "updatesFallbackUntil"
    /// The same signed feed keepmum.app serves (the Worker serves this file byte for byte).
    static let fallbackFeed = "https://raw.githubusercontent.com/blueandhack/keepmum/main/mac/appcast.xml"
    static let fallbackDays = 3.0
    /// The check under way reads GitHub's feed; it loaded its feed; the check it is a retry of.
    @ObservationIgnored private var sessionOnGitHub = false
    @ObservationIgnored private var loadedFeed = false
    @ObservationIgnored private var retryOf: SPUUpdateCheck?
    /// What the check under way said it was first in, saved once its feed has loaded.
    @ObservationIgnored private var pendingFirsts: (periods: Periods, new: Bool)?
    /// This Mac's update checks and update downloads are in keepmum.app's totals (Settings ›
    /// General › Updates › Count this Mac). Off: every request says Keepmum-Count: off, and
    /// keepmum.app counts none of them (worker/). Updates work the same either way.
    private(set) var counted = UserDefaults.standard.object(forKey: countedKey) as? Bool ?? true
    /// The full version (0.1.0a2), not Apple's numeric one.
    static let version = Bundle.main.object(forInfoDictionaryKey: "KeepmumVersion") as? String
        ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
    /// This build has a feed and a key to check updates with.
    static let configured = ["SUFeedURL", "SUPublicEDKey"].allSatisfy {
        !((Bundle.main.object(forInfoDictionaryKey: $0) as? String) ?? "").isEmpty
    }

    var isConfigured: Bool { controller != nil }

    /// Start once, at launch: on the first launch, ask about automatic checks first.
    func start() {
        guard !started, Self.configured else { return }
        started = true
        let controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self, userDriverDelegate: self)
        self.controller = controller
        let updater = controller.updater
        observers = [
            updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] u, _ in
                MainActor.assumeIsolated { self?.canCheck = u.canCheckForUpdates }
            },
            updater.observe(\.lastUpdateCheckDate, options: [.initial, .new]) { [weak self] u, _ in
                MainActor.assumeIsolated { self?.lastCheck = u.lastUpdateCheckDate }
            },
            updater.observe(\.automaticallyChecksForUpdates, options: [.initial, .new]) { [weak self] u, _ in
                MainActor.assumeIsolated { self?.automaticallyChecks = u.automaticallyChecksForUpdates }
            },
            updater.observe(\.automaticallyDownloadsUpdates, options: [.initial, .new]) { [weak self] u, _ in
                MainActor.assumeIsolated { self?.automaticallyDownloads = u.automaticallyDownloadsUpdates }
            },
        ]
        applyCounting()
        let defaults = UserDefaults.standard
        if updater.lastUpdateCheckDate == nil, defaults.object(forKey: Self.lastCountedKeys.day) == nil,
           defaults.object(forKey: Self.newKey) == nil {
            defaults.set(true, forKey: Self.newKey) // never checked: the first check counts as a new Mac
        }
        if !UserDefaults.standard.bool(forKey: Self.promptedKey) {
            updater.automaticallyChecksForUpdates = askToCheckAutomatically()
            UserDefaults.standard.set(true, forKey: Self.promptedKey)
        }
        controller.startUpdater()
    }

    func setAutomaticallyChecks(_ on: Bool) {
        controller?.updater.automaticallyChecksForUpdates = on
        if !on { controller?.updater.automaticallyDownloadsUpdates = false }
    }

    func setCounted(_ on: Bool) {
        counted = on
        UserDefaults.standard.set(on, forKey: Self.countedKey)
        applyCounting()
    }

    /// Sparkle sends these headers with the feed check, release notes and update downloads.
    private func applyCounting() {
        controller?.updater.httpHeaders = counted ? [:] : ["Keepmum-Count": "off"]
    }

    func setAutomaticallyDownloads(_ on: Bool) {
        controller?.updater.automaticallyDownloadsUpdates = on
    }

    /// A check the user asked for: Sparkle's window shows what it finds, even "up to date".
    func checkNow() {
        NSApp.activate()
        controller?.checkForUpdates(nil)
    }

    private func askToCheckAutomatically() -> Bool {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "Check for keepmum updates automatically?"
        alert.informativeText = """
            keepmum can look for a new version once a day and tell you when one is ready to install. \
            Each check downloads keepmum's update list from keepmum.app (from GitHub while keepmum.app \
            is down), which sees your IP address and \
            keepmum's version and counts checks and updates per hour, keeping no addresses (Count this Mac, \
            in Settings, turns that off). To count each Mac once, a check also says whether it is this \
            Mac's first of the day, week or month, and the day's first says its macOS version and chip \
            (macOS 15, Apple silicon). Nothing else is sent: no identifier, no usage data.

            You can change this, or check now, in Settings › General › Updates.
            """
        alert.addButton(withTitle: "Check Automatically")
        alert.addButton(withTitle: "Don't Check")
        return alert.runModal() == .alertFirstButtonReturn
    }
}

extension Updates {
    struct Periods: Equatable {
        var day: String, week: String, month: String
    }

    /// The UTC day (2026-11-10), ISO week (2026-W46, from Monday) and month (2026-11) of a moment.
    static func periods(_ date: Date) -> Periods {
        var calendar = Calendar(identifier: .iso8601)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let c = calendar.dateComponents([.year, .month, .day, .yearForWeekOfYear, .weekOfYear], from: date)
        return Periods(
            day: String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!),
            week: String(format: "%04d-W%02d", c.yearForWeekOfYear!, c.weekOfYear!),
            month: String(format: "%04d-%02d", c.year!, c.month!))
    }

    /// Which of this UTC day, week and month a check now would be this Mac's first counted check in,
    /// and `new` for its first ever.
    static func firsts(_ periods: Periods, defaults: UserDefaults = .standard) -> [String] {
        var out: [String] = []
        if defaults.string(forKey: lastCountedKeys.day) != periods.day { out.append("day") }
        if defaults.string(forKey: lastCountedKeys.week) != periods.week { out.append("week") }
        if defaults.string(forKey: lastCountedKeys.month) != periods.month { out.append("month") }
        if defaults.bool(forKey: newKey) { out.append("new") }
        return out
    }
}

extension Updates: SPUUpdaterDelegate {
    /// keepmum asks on the first launch (above), with its own words about what a check sends.
    func updaterShouldPromptForPermissionToCheck(forUpdates updater: SPUUpdater) -> Bool { false }

    /// keepmum.app's feed (Info.plist), or GitHub's while keepmum.app's fails (see the type's comment).
    func feedURLString(for updater: SPUUpdater) -> String? {
        sessionOnGitHub = Self.onGitHub(retrying: retryOf != nil)
        loadedFeed = false
        return sessionOnGitHub ? Self.fallbackFeed : nil
    }

    static func onGitHub(retrying: Bool, defaults: UserDefaults = .standard, now: Date = Date()) -> Bool {
        retrying || (defaults.object(forKey: fallbackUntilKey) as? Date).map { $0 > now } ?? false
    }

    /// `first=day,week,month,new` on the feed URL, for a counted Mac (see the type's comment), and
    /// with the day's first, `os` and `chip`. None to GitHub: it counts nothing.
    func feedParameters(for updater: SPUUpdater, sendingSystemProfile: Bool) -> [[String: String]] {
        guard counted, !Self.onGitHub(retrying: retryOf != nil) else { pendingFirsts = nil; return [] }
        let periods = Self.periods(Date())
        let firsts = Self.firsts(periods)
        pendingFirsts = (periods, firsts.contains("new"))
        return Self.parameters(firsts)
    }

    /// The feed URL's parameters for a counted check that is first in `firsts`.
    static func parameters(_ firsts: [String]) -> [[String: String]] {
        guard !firsts.isEmpty else { return [] }
        var out = [["key": "first", "value": firsts.joined(separator: ",")]]
        if firsts.contains("day") {
            out.append(["key": "os", "value": String(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)])
            out.append(["key": "chip", "value": chip])
        }
        return out
    }

    /// The Mac's chip as the feed URL says it: arm64 (Apple silicon) or x86_64 (Intel).
    static var chip: String {
        #if arch(arm64)
        "arm64"
        #else
        "x86_64"
        #endif
    }

    /// The feed came back, so keepmum.app counted the check: remember its periods.
    func updater(_ updater: SPUUpdater, didFinishLoading appcast: SUAppcast) {
        loadedFeed = true
        guard let pending = pendingFirsts else { return }
        pendingFirsts = nil
        let defaults = UserDefaults.standard
        defaults.set(pending.periods.day, forKey: Self.lastCountedKeys.day)
        defaults.set(pending.periods.week, forKey: Self.lastCountedKeys.week)
        defaults.set(pending.periods.month, forKey: Self.lastCountedKeys.month)
        if pending.new { defaults.removeObject(forKey: Self.newKey) }
    }

    /// While on GitHub, an update's DMG comes straight from GitHub Releases, not keepmum.app/update/.
    /// The DMG's EdDSA signature is checked as ever, wherever it came from.
    func updater(_ updater: SPUUpdater, willDownloadUpdate item: SUAppcastItem, with request: NSMutableURLRequest) {
        guard sessionOnGitHub, let url = request.url, let direct = Self.githubDownload(url) else { return }
        request.url = direct
    }

    /// keepmum.app/update/v<version>/<file> as the GitHub Releases asset it redirects to; nil otherwise.
    static func githubDownload(_ url: URL) -> URL? {
        let parts = url.pathComponents  // ["/", "update", "v0.1.0a7", "Keepmum-0.1.0a7.dmg"]
        guard url.host == "keepmum.app", parts.count == 4, parts[1] == "update", parts[2].hasPrefix("v") else { return nil }
        return URL(string: "https://github.com/blueandhack/keepmum/releases/download/\(parts[2])/\(parts[3])")
    }

    /// keepmum.app's feed did not load: try GitHub's once, in the background. A download from
    /// keepmum.app that failed after its feed loaded moves the next checks to GitHub.
    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: (any Error)?) {
        let origin = retryOf
        retryOf = nil
        let code = (error as NSError?).flatMap { $0.domain == SUSparkleErrorDomain ? $0.code : nil }
        if let origin {  // the retry against GitHub finished
            guard loadedFeed else { return }  // GitHub failed too: offline, most likely; nothing changes
            UserDefaults.standard.set(Date().addingTimeInterval(Self.fallbackDays * 86_400), forKey: Self.fallbackUntilKey)
            if origin == .updates { after { [weak self] in self?.controller?.checkForUpdates(nil) } }  // what the user asked for
            return
        }
        guard !sessionOnGitHub, let code, !Self.notFailures.contains(code) else { return }
        if !loadedFeed {
            retryOf = updateCheck
            after { [weak self] in
                guard let self, let updater = self.controller?.updater, updater.canCheckForUpdates else {
                    self?.retryOf = nil
                    return
                }
                updater.checkForUpdatesInBackground()
            }
        } else if code == Int(SUError.downloadError.rawValue) {
            UserDefaults.standard.set(Date().addingTimeInterval(Self.fallbackDays * 86_400), forKey: Self.fallbackUntilKey)
        }
    }

    /// Endings that say nothing about keepmum.app: no update, or the user said no.
    static let notFailures: Set<Int> = [Int(SUError.noUpdateError.rawValue), Int(SUError.installationCanceledError.rawValue),
                                        Int(SUError.installationAuthorizeLaterError.rawValue)]

    /// A moment after the update cycle has ended, when Sparkle takes a new check.
    private func after(_ work: @escaping @MainActor () -> Void) {
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(1))
            work()
        }
    }
}

extension Updates: @preconcurrency SPUStandardUserDriverDelegate {
    var supportsGentleScheduledUpdateReminders: Bool { true }

    /// A scheduled check's update waits in the menu, unless keepmum is in front already.
    func standardUserDriverShouldHandleShowingScheduledUpdate(_ update: SUAppcastItem, andInImmediateFocus immediateFocus: Bool) -> Bool {
        immediateFocus
    }

    func standardUserDriverWillHandleShowingUpdate(_ handleShowingUpdate: Bool, forUpdate update: SUAppcastItem, state: SPUUserUpdateState) {
        if !state.userInitiated { available = update.displayVersionString }
        if handleShowingUpdate { NSApp.activate() }
    }

    func standardUserDriverDidReceiveUserAttention(forUpdate update: SUAppcastItem) {
        available = nil
    }

    func standardUserDriverWillFinishUpdateSession() {
        available = nil
    }
}
