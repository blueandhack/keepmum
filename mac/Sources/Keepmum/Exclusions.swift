import AppKit
import SwiftUI

/// Projects where Claude Code skips keepmum and goes straight to the API (`keepmum exclude`).
struct ExclusionsSection: View {
    var engine: Engine

    var body: some View {
        let list = engine.status?.excluded ?? []
        FormSection(title: "Excluded projects", footer: engine.exclusionNote ?? "Claude Code in these projects goes straight to the API, unscanned.") {
            if list.isEmpty {
                FormRow(title: "None", detail: "Every project goes through keepmum") { addButton }
            } else {
                ForEach(list) { p in
                    HStack(spacing: Space.m) {
                        Image(systemName: "folder").font(.km(.body)).foregroundStyle(Theme.textSecondary).frame(width: 24)
                        VStack(alignment: .leading, spacing: Space.xxs) {
                            Text(display(p.path)).font(.km(.mono)).foregroundStyle(Theme.text)
                                .lineLimit(1).truncationMode(.middle)
                            Text(p.active ? "→ \(p.url)" : "Setting removed by hand: goes through keepmum")
                                .font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1)
                        }
                        Spacer(minLength: Space.s)
                        PillButton(title: "Include") { Task { await engine.include(p.path) } }
                            .disabled(!engine.isBundled || engine.busy)
                    }
                    .padding(.vertical, Space.s)
                    Hairline()
                }
                HStack {
                    Spacer()
                    addButton
                }
                .padding(.vertical, Space.s)
            }
        }
    }

    private var addButton: some View {
        PillButton(title: "Exclude a project…", icon: "plus") { pick() }
            .disabled(!engine.isBundled || engine.busy)
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

/// What gets masked: the mode (mask, or observe only) and the detector (the rules alone, or the
/// rules and the local model).
struct MaskingSection: View {
    var engine: Engine
    /// From the running proxy's feed, so the section shows what is in force right now.
    var observing: Bool
    /// From the running proxy's feed: the model was asked for but did not load, and why.
    var modelError: String?
    @State private var confirmObserve = false

    var body: some View {
        let observe = engine.status?.observe ?? observing
        let model = engine.modelOn
        FormSection(title: "Masking") {
            FormRow(title: "Mode",
                    detail: observe ? "Observe only: requests leave this Mac unmasked; keepmum only counts what it would mask"
                                    : "Sensitive values are replaced before requests leave this Mac",
                    detailTone: observe ? Theme.blocked : Theme.textSecondary) {
                Segmented(options: [(false, "Mask"), (true, "Observe only")],
                          selection: Binding(get: { observe }, set: { new in
                              if new { confirmObserve = true } else { Task { await engine.setObserve(false) } }
                          }))
                    .disabled(!engine.isBundled || engine.busy)
            }
            Hairline()
            FormRow(title: "Detector",
                    detail: model ? "Rules, then the local model: also names, addresses, and secrets with no known shape"
                                  : "Rules only: faster and about 1.7 GB lighter, but names, addresses and unlabeled secrets go out unmasked") {
                Segmented(options: [(true, "Rules + model"), (false, "Rules only")],
                          selection: Binding(get: { model }, set: { new in Task { await engine.setModel(new) } }))
                    .disabled(!engine.isBundled || engine.busy || engine.download != nil)
            }
            if model, let modelError {
                Hairline()
                Text(modelError).font(.km(.callout)).foregroundStyle(Theme.blocked).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, Space.s + Space.xxs)
            }
        }
        .alert("Send requests unmasked?", isPresented: $confirmObserve) {
            Button("Keep masking", role: .cancel) {}
            Button("Observe only", role: .destructive) { Task { await engine.setObserve(true) } }
        } message: {
            Text("Secrets and personal data would reach the model provider as they are. keepmum only counts what it would have masked, and the audit log records the change.")
        }
    }
}
