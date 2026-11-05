import AppKit
import Observation
import Sparkle

/// App updates with Sparkle: a signed feed on GitHub (SUFeedURL, SURequireSignedFeed) lists each
/// release's notarized DMG with its EdDSA signature (SUPublicEDKey), checked before it is unpacked.
///
/// Nothing is checked until the user says so. On the first launch keepmum asks, once, whether to
/// check automatically (once a day); Settings › Updates changes it, and "Check now" always works.
/// Sparkle's own permission prompt is off, and it sends no system profile. A check is one request
/// for the feed: GitHub sees the IP address and keepmum's version in the user agent.
///
/// keepmum runs in the menu bar, so a scheduled check that finds an update does not interrupt: the
/// menu shows it (`available`, Sparkle's gentle reminders), and Install opens Sparkle's window.
/// Builds without a feed and key in Info.plist (development builds) have no updater.
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
            Each check downloads keepmum's update list from github.com, so GitHub sees your IP address and \
            keepmum's version. Nothing else is sent: no usage data, no system details.

            You can change this, or check now, in Settings › Updates.
            """
        alert.addButton(withTitle: "Check Automatically")
        alert.addButton(withTitle: "Don't Check")
        return alert.runModal() == .alertFirstButtonReturn
    }
}

extension Updates: SPUUpdaterDelegate {
    /// keepmum asks on the first launch (above), with its own words about what a check sends.
    func updaterShouldPromptForPermissionToCheck(forUpdates updater: SPUUpdater) -> Bool { false }
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
