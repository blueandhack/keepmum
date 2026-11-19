import Observation
import ServiceManagement
import SwiftUI

/// Keepmum.app as a login item (SMAppService.mainApp): the menu bar app opens when you log in.
/// Protection does not wait for it: the background service is its own LaunchAgent (RunAtLoad), so
/// the proxy runs at login either way. This is the menu bar, the dashboard and their notices.
///
/// The setting lives with macOS, not in UserDefaults: System Settings › Login Items can turn it off,
/// so the status is read again whenever it is shown.
@MainActor @Observable
final class LoginItem {
    static let shared = LoginItem()

    private(set) var status: SMAppService.Status
    private(set) var error: String?
    private let service: SMAppService

    init(service: SMAppService = .mainApp) {
        self.service = service
        status = service.status
    }

    var isOn: Bool { status == .enabled }
    /// Turned on, but macOS waits for the user to allow it in Login Items.
    var needsApproval: Bool { status == .requiresApproval }

    func refresh() { status = service.status }

    func set(_ on: Bool) {
        error = nil
        do {
            if on {
                try service.register()
            } else {
                try service.unregister()
            }
        } catch let e as NSError where on && e.code == Int(kSMErrorAlreadyRegistered) {
            // already a login item: fine
        } catch let e as NSError where !on && e.code == Int(kSMErrorJobNotFound) {
            // not a login item: fine
        } catch {
            self.error = error.localizedDescription
        }
        refresh()
        if needsApproval { SMAppService.openSystemSettingsLoginItems() }
    }
}

/// Settings › General › Startup.
struct StartupSection: View {
    var login = LoginItem.shared
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        FormSection(title: "Startup",
                    footer: "Protection starts at login either way: keepmum's background service runs on its own. This opens the menu bar and its notices with it.") {
            FormRow(title: "Open at login", detail: detail) {
                if login.needsApproval {
                    PillButton(title: "Allow…", icon: "gearshape") { SMAppService.openSystemSettingsLoginItems() }
                } else {
                    Switch(isOn: login.isOn) { login.set($0) }
                        .accessibilityLabel("Open keepmum at login")
                }
            }
        }
        .onAppear { if !isSnapshot { login.refresh() } }
    }

    private var detail: String {
        if let error = login.error { return "macOS refused: \(error)" }
        if login.needsApproval { return "Allow keepmum in System Settings › General › Login Items" }
        return "keepmum opens in the menu bar when you log in to this Mac"
    }
}
