import LocalAuthentication
import SwiftUI

// Show values: the Logs and Activity pages draw each masked value as it was, not its shape. The
// values come only with [log] values on (off by default, kept in keepmum's memory only), and show
// only after Touch ID or the login password, until Hide values or the app quits.

extension EnvironmentValues {
    /// Draw a masked value as it was, where the feed has it, in place of its shape.
    @Entry var revealValues = false
}

extension Finding {
    /// The value as it was when values show and the feed has it; its shape otherwise.
    func shown(_ reveal: Bool) -> String {
        reveal ? value ?? masked : masked
    }
}

/// Touch ID or the login password, asked before masked values show on screen.
enum OwnerCheck {
    static func confirm(_ reason: String) async -> Bool {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            // A Mac with no login password has nothing to ask for.
            return (error as? LAError)?.code == .passcodeNotSet
        }
        return (try? await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)) ?? false
    }
}

/// The Logs and Activity pages' Show values / Hide values button. With [log] values off, Show
/// values turns it on first: values masked from then on can show; keepmum never kept earlier ones.
struct ValuesButton: View {
    var store: StatsStore
    /// Turns [log] values on; nil where the page has no engine (snapshots).
    var engine: Engine?
    @State private var working = false

    var body: some View {
        let shown = store.valuesShown
        PillButton(title: shown ? "Hide values" : "Show values", icon: shown ? "eye.slash" : "eye", loading: working) {
            if shown {
                store.valuesRevealed = false
            } else {
                Task { await show() }
            }
        }
        .disabled(store.sampleMode || (!store.valuesOn && engine?.isBundled != true))
        .help(shown ? "Show each masked value as its shape again, like g**********w"
              : store.valuesOn ? "Show each masked value as it was, on this Mac only. Asks for Touch ID or your password."
              : "Show each masked value as it was, on this Mac only: turns on Original values (Settings › General › Logs page) "
                + "and asks for Touch ID or your password. Values masked from then on; keepmum did not keep earlier ones.")
        .accessibilityLabel(shown ? "Hide values" : "Show values")
    }

    private func show() async {
        working = true
        defer { working = false }
        guard await OwnerCheck.confirm("show the values keepmum masked") else { return }
        if !store.valuesOn, let engine {
            await engine.setLog("values", true)
            await engine.refresh()
        }
        store.valuesRevealed = true
        await store.loadFindings()
    }
}
