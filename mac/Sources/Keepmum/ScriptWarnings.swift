import Foundation
import UserNotifications

/// keepmum's script check (codemode.ScriptCheck in the engine): a Codex script handed values it
/// works out itself, such as a command's output, to a network tool. keepmum could not check them, so
/// it warns, never refuses: a note in the agent, the hit on the Logs page, the menu bar dot until
/// you have looked, and a notification for each new one.
@MainActor
enum ScriptWarnings {
    /// The newest script hit the user has looked at (its time), and the newest one notified.
    private static let seenKey = "scriptWarningSeen"
    private static let notifiedKey = "scriptWarningNotified"

    static func hits(_ all: [Snapshot.GuardHit]) -> [Snapshot.GuardHit] { all.filter { $0.check == "script" } }

    static var seen: Double {
        get { UserDefaults.standard.double(forKey: seenKey) }
        set { UserDefaults.standard.set(newValue, forKey: seenKey) }
    }

    /// The newest hit not looked at yet, for the menu bar dot and its row.
    static func unseen(_ all: [Snapshot.GuardHit]) -> Snapshot.GuardHit? {
        hits(all).filter { $0.ts > seen }.max { $0.ts < $1.ts }
    }

    static func markSeen(_ all: [Snapshot.GuardHit]) {
        if let newest = hits(all).map(\.ts).max(), newest > seen { seen = newest }
    }

    /// Posts a notification for each script hit newer than the last one notified. The first time
    /// this runs it only remembers where things stand, so older hits never pop up at once.
    static func notifyNew(_ all: [Snapshot.GuardHit]) {
        let defaults = UserDefaults.standard
        let newest = hits(all).map(\.ts).max() ?? 0
        guard defaults.object(forKey: notifiedKey) != nil else {
            defaults.set(max(newest, Date().timeIntervalSince1970), forKey: notifiedKey)
            return
        }
        let last = defaults.double(forKey: notifiedKey)
        let fresh = hits(all).filter { $0.ts > last }.sorted { $0.ts < $1.ts }
        guard !fresh.isEmpty else { return }
        defaults.set(newest, forKey: notifiedKey)
        // A notification needs the app bundle (not `swift run`).
        guard Bundle.main.bundleIdentifier != nil else { return }
        let requests = fresh.suffix(3).map { hit in
            let content = UNMutableNotificationContent()
            content.title = "A script sent values keepmum could not check"
            let to = (hit.categories ?? []).first.map { $0.replacingOccurrences(of: "computed values sent to ", with: "") } ?? "a network tool"
            content.body = "\(agentName(hit.client)) passed values it worked out, such as a command's output, to \(to). "
                + "A secret in them may have reached it. Open keepmum › Logs for details."
            content.sound = .default
            return UNNotificationRequest(identifier: "keepmum-script-\(hit.id)", content: content, trigger: nil)
        }
        Task {
            // macOS asks the user once; declined, nothing is posted, and the dot and Logs still show it.
            let center = UNUserNotificationCenter.current()
            guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
            for request in requests { try? await center.add(request) }
        }
    }
}
