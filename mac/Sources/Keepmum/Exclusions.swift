import AppKit
import SwiftUI

/// Projects where Claude Code skips keepmum and goes straight to the API (`keepmum exclude`).
struct ExclusionsCard: View {
    var engine: Engine

    var body: some View {
        Card(title: "Excluded projects", subtitle: "Claude Code in these goes straight to the API, unscanned") {
            PillButton(title: "Exclude a project…", icon: "plus") { pick() }
                .disabled(!engine.isBundled || engine.busy)
        } content: {
            let list = engine.status?.excluded ?? []
            VStack(alignment: .leading, spacing: 0) {
                if list.isEmpty {
                    Text("None. Every project goes through keepmum.").font(.system(size: 12)).foregroundStyle(Theme.secondary)
                }
                ForEach(list) { p in
                    HStack(spacing: 10) {
                        Image(systemName: "folder").foregroundStyle(Theme.secondary)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(display(p.path)).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.text)
                                .lineLimit(1).truncationMode(.middle)
                            Text(p.active ? "→ \(p.url)" : "setting removed by hand: goes through keepmum")
                                .font(.system(size: 11)).foregroundStyle(Theme.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 8)
                        PillButton(title: "Include") { Task { await engine.include(p.path) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                    .padding(.vertical, 7)
                }
                if let note = engine.exclusionNote {
                    Text(note).font(.system(size: 11.5)).foregroundStyle(Theme.secondary).padding(.top, 6)
                }
            }
        }
    }

    private func display(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    private func pick() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Exclude"
        panel.message = "Claude Code in this project (its git repository) will skip keepmum and send everything to the API unscanned."
        if panel.runModal() == .OK, let url = panel.url {
            Task { await engine.exclude(url) }
        }
    }
}

/// Mask (the default) or observe only.
struct ModeCard: View {
    var engine: Engine
    /// From the running proxy's feed, so the card shows what is in force right now.
    var observing: Bool

    var body: some View {
        let on = engine.status?.observe ?? observing
        Card(title: "Mode", subtitle: on ? "Observe only: requests go out unmasked; keepmum counts what it would mask"
                                         : "Mask: sensitive values are replaced before requests leave this Mac") {
            Segmented(options: [(false, "Mask"), (true, "Observe only")],
                      selection: Binding(get: { on }, set: { new in Task { await engine.setObserve(new) } }))
                .disabled(!engine.isBundled || engine.busy)
        }
    }
}

/// The rules alone, or the rules and the local model (`keepmum model`).
struct DetectorCard: View {
    var engine: Engine
    /// From the running proxy's feed: the model was asked for but did not load, and why.
    var modelError: String?

    var body: some View {
        let on = engine.modelOn
        Card(title: "Detector", subtitle: on ? "Rules, then the local model: also names, addresses, and secrets with no known shape"
                                             : "Rules only: faster and about 1.7 GB lighter, but names, addresses and unlabeled secrets go out unmasked") {
            VStack(alignment: .leading, spacing: 8) {
                Segmented(options: [(true, "Rules + model"), (false, "Rules only")],
                          selection: Binding(get: { on }, set: { new in Task { await engine.setModel(new) } }))
                    .disabled(!engine.isBundled || engine.busy || engine.download != nil)
                if on, let modelError {
                    Text(modelError).font(.system(size: 11.5)).foregroundStyle(Theme.danger).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
