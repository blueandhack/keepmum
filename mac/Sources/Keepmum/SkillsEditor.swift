import AppKit
import SwiftUI

/// The Skills window: write, import, edit and delete the skills local mode gives the local model
/// (`keepmum guard local skill`), without another editor. keepmum keeps them in its own folder,
/// ~/.config/keepmum/skills, one folder per skill with its SKILL.md.
struct SkillsEditor: View {
    var engine: Engine
    /// The skill to show first (1 is the first), for snapshots.
    var initial: Int?

    @State private var selection: Int?
    @State private var loaded: Engine.SkillText?
    @State private var name = ""
    @State private var summary = ""
    @State private var text = ""
    @State private var loading = false
    /// A selection asked for while the open skill has unsaved changes.
    @State private var pending: Int??
    @State private var naming = false
    @State private var newName = ""
    @State private var confirmingDelete = false
    @Environment(\.isSnapshot) private var isSnapshot

    static let limit = 20_000

    init(engine: Engine, initial: Int? = nil) {
        self.engine = engine
        self.initial = initial
        // A snapshot renders at once: the preview skill's text is there from the start.
        if let initial, let t = engine.previewSkillTexts[initial] {
            _selection = State(initialValue: initial)
            _loaded = State(initialValue: t)
            _name = State(initialValue: t.name)
            _summary = State(initialValue: t.description)
            _text = State(initialValue: t.text)
        }
    }

    private var skills: [Engine.LocalSkills.Skill] { engine.localSkills?.skills ?? [] }
    private var dirty: Bool {
        guard let loaded else { return false }
        return name != loaded.name || summary != loaded.description || text != loaded.text
    }
    private var current: Engine.LocalSkills.Skill? {
        guard let selection, skills.indices.contains(selection - 1) else { return nil }
        return skills[selection - 1]
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 250)
            Divider()
            detail.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.canvas)
        .task {
            await engine.loadLocalSkills()
            if selection == nil { select(initial ?? engine.skillToEdit ?? (skills.isEmpty ? nil : 1)) }
            engine.skillToEdit = nil
        }
        .onChange(of: engine.skillToEdit) { _, n in
            if let n { ask(n); engine.skillToEdit = nil }
        }
        .alert("Save changes to \u{201C}\(loaded?.name ?? "this skill")\u{201D}?",
               isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } })) {
            Button("Save") { let next = pending.flatMap { $0 }; Task { if await save() { select(next) } } }
            Button("Don\u{2019}t Save", role: .destructive) { let next = pending.flatMap { $0 }; select(next) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Your edits are lost if you don\u{2019}t save them.")
        }
        .alert("New skill", isPresented: $naming) {
            TextField("Name, such as careful-answers", text: $newName)
            Button("Cancel", role: .cancel) {}
            Button("Create") {
                let n = newName.trimmingCharacters(in: .whitespaces)
                if !n.isEmpty { Task { if let made = await engine.newLocalSkill(name: n) { select(made) } } }
            }
        } message: {
            Text("keepmum writes it in its skills folder with a starting point to edit. The local model sees it by this name.")
        }
        .alert("Delete \u{201C}\(current?.name ?? "this skill")\u{201D}?", isPresented: $confirmingDelete) {
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) {
                guard let n = selection else { return }
                Task { await engine.deleteLocalSkill(n); select(skills.isEmpty ? nil : min(n, skills.count)) }
            }
        } message: {
            Text(current?.owned == true ? "Local turns stop getting it, and its folder goes to the Trash, where you can put it back."
                                       : "Local turns stop getting it. Its file is outside keepmum's folder, so it stays where it is.")
        }
    }

    // MARK: The list

    private var sidebar: some View {
        VStack(spacing: 0) {
            Text("Skills for local turns").font(.km(.headline)).foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, Space.l).padding(.top, Space.l).padding(.bottom, Space.s)
            if isSnapshot {  // a snapshot cannot draw a scroll view
                list.frame(maxHeight: .infinity, alignment: .top)
            } else {
                ScrollView { list }
            }
            Divider()
            HStack(spacing: Space.s) {
                if isSnapshot {
                    Image(systemName: "plus").foregroundStyle(Theme.text)
                } else {
                    Menu {
                        Button("New Skill…") { newName = ""; naming = true }
                        Button("Import a SKILL.md or Folder…") { importSkill() }
                    } label: {
                        Image(systemName: "plus")
                    }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .help("Write a new skill, or copy one in")
                    .accessibilityLabel("Add a skill")
                }
                Button { confirmingDelete = true } label: { Image(systemName: "minus").foregroundStyle(Theme.text) }
                    .buttonStyle(.plain).disabled(current == nil).opacity(current == nil ? 0.35 : 1)
                    .help("Delete the selected skill").accessibilityLabel("Delete the selected skill")
                Spacer()
                if let folder = engine.localSkills?.folder {
                    Button { NSWorkspace.shared.open(URL(fileURLWithPath: folder, isDirectory: true)) } label: {
                        Image(systemName: "folder").foregroundStyle(Theme.textSecondary)
                    }
                        .buttonStyle(.plain).help("Open keepmum's skills folder in Finder")
                        .accessibilityLabel("Open keepmum's skills folder")
                        .disabled(!FileManager.default.fileExists(atPath: folder) && !isSnapshot)
                }
            }
            .padding(.horizontal, Space.m).padding(.vertical, Space.s)
            .disabled(!engine.isBundled || engine.busy)
        }
        .background(Theme.sidebar)
    }

    private var list: some View {
        VStack(spacing: Space.xxs) {
            ForEach(Array(skills.enumerated()), id: \.offset) { i, skill in
                sidebarRow(i + 1, skill)
            }
            if skills.isEmpty {
                Text("No skills yet").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(Space.s)
            }
        }
        .padding(.horizontal, Space.s)
    }

    /// A snapshot cannot draw AppKit's text fields: their text in a box shaped like one.
    private func still(_ value: String, mono: Bool = false) -> some View {
        Text(value).font(.km(mono ? .mono : .body)).foregroundStyle(Theme.text)
            .frame(maxWidth: .infinity, maxHeight: mono ? .infinity : nil, alignment: .topLeading)
            .padding(.horizontal, Space.s - 2).padding(.vertical, Space.xs)
            .background(RoundedRectangle(cornerRadius: Radius.s - 2).fill(Theme.raised))
            .overlay(RoundedRectangle(cornerRadius: Radius.s - 2).stroke(Theme.separator))
    }

    private func sidebarRow(_ n: Int, _ skill: Engine.LocalSkills.Skill) -> some View {
        let selected = selection == n
        return Button { ask(n) } label: {
            HStack(spacing: Space.s) {
                VStack(alignment: .leading, spacing: Space.xxs) {
                    Text(skill.name ?? (skill.path as NSString).lastPathComponent).font(.km(.body))
                        .foregroundStyle(Theme.text).lineLimit(1).truncationMode(.tail)
                    Text(skill.problem == nil ? Self.checksText(skill.checks) : "Left out: can\u{2019}t be read")
                        .font(.km(.caption)).foregroundStyle(skill.problem == nil ? Theme.textSecondary : Theme.warning)
                }
                Spacer(minLength: 0)
                if skill.owned == false {
                    Image(systemName: "arrow.up.forward.square").font(.km(.caption)).foregroundStyle(Theme.textTertiary)
                        .help("Used where it is, outside keepmum's folder")
                }
            }
            .padding(.horizontal, Space.s).padding(.vertical, Space.s - 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Radius.s).fill(selected ? Theme.fillStrong : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    static func checksText(_ checks: [String]) -> String {
        checks.count == 2 ? "Both checks" : checks.first == "harm" ? "Harm check only" : "Injection check only"
    }

    // MARK: The skill

    @ViewBuilder private var detail: some View {
        if let skill = current, loaded != nil {
            editor(skill)
        } else if loading {
            ProgressView().controlSize(.small)
        } else {
            VStack(spacing: Space.m) {
                Text(skills.isEmpty ? "No skills yet" : "Choose a skill").font(.km(.title)).foregroundStyle(Theme.text)
                Text("A skill is your own instructions for the local model, given on the turns a check keeps on this Mac, after keepmum\u{2019}s note.")
                    .font(.km(.body)).foregroundStyle(Theme.textSecondary).multilineTextAlignment(.center).frame(maxWidth: 420)
                HStack(spacing: Space.s) {
                    PillButton(title: "New Skill…", icon: "plus", prominent: true) { newName = ""; naming = true }
                    PillButton(title: "Import…", icon: "square.and.arrow.down") { importSkill() }
                }
                .disabled(!engine.isBundled || engine.busy)
            }
            .padding(Space.xxl)
        }
    }

    private func editor(_ skill: Engine.LocalSkills.Skill) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            field("Name", hint: "What the local model sees it as") {
                if isSnapshot { still(name) } else { TextField("Name", text: $name).textFieldStyle(.roundedBorder).labelsHidden() }
            }
            field("Description", hint: "One line on what it is for; the local model gets it too") {
                if isSnapshot {
                    still(summary)
                } else {
                    TextField("Description", text: $summary).textFieldStyle(.roundedBorder).labelsHidden()
                }
            }
            field("Used on", hint: "The checks whose local turns get it. Saved at once.") {
                Segmented(options: [("both", "Both checks"), ("injection", "Injection only"), ("harm", "Harm only")],
                          selection: Binding(get: { skill.checks.count == 2 ? "both" : skill.checks[0] },
                                             set: { new in
                                                 guard let n = selection else { return }
                                                 let checks = new == "both" ? ["injection", "harm"] : [new]
                                                 Task { await engine.setLocalSkill(n, checks: checks) }
                                             }))
                .fixedSize()
                .disabled(!engine.isBundled || engine.busy)
            }
            VStack(alignment: .leading, spacing: Space.xs) {
                Text("Instructions").font(.km(.headline)).foregroundStyle(Theme.text)
                if isSnapshot {
                    still(text, mono: true).frame(minHeight: 220, maxHeight: .infinity)
                } else {
                    TextEditor(text: $text)
                        .font(.km(.mono))
                        .scrollContentBackground(.hidden)
                        .padding(Space.s)
                        .background(RoundedRectangle(cornerRadius: Radius.s).fill(Theme.raised))
                        .overlay(RoundedRectangle(cornerRadius: Radius.s).stroke(Theme.separator))
                        .frame(minHeight: 220, maxHeight: .infinity)
                        .accessibilityLabel("Instructions")
                }
            }
            footer(skill)
        }
        .padding(Space.xl)
    }

    private func field(_ title: String, hint: String, @ViewBuilder control: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Text(title).font(.km(.headline)).foregroundStyle(Theme.text)
                Text(hint).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
            control()
        }
    }

    private func footer(_ skill: Engine.LocalSkills.Skill) -> some View {
        let count = text.trimmingCharacters(in: .whitespacesAndNewlines).count
        let over = count > Self.limit
        return VStack(alignment: .leading, spacing: Space.s) {
            if let error = engine.error {
                Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked).fixedSize(horizontal: false, vertical: true)
            }
            if skill.owned == false {
                Text("This skill is used where it is, outside keepmum\u{2019}s folder: saving changes that file.")
                    .font(.km(.caption)).foregroundStyle(Theme.warning)
            }
            HStack(spacing: Space.m) {
                Text("\(count.formatted()) of \(Self.limit.formatted()) characters")
                    .font(.km(.caption)).monospacedDigit().foregroundStyle(over ? Theme.blocked : Theme.textSecondary)
                if let file = loaded?.file {
                    Button(Self.tilde(file)) { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: file)]) }
                        .buttonStyle(.plain).font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(1).truncationMode(.middle).help("Show in Finder")
                }
                Spacer(minLength: Space.m)
                PillButton(title: "Revert") { if let loaded { fill(loaded) } }
                    .disabled(!dirty)
                PillButton(title: "Save", prominent: true, loading: engine.busy && dirty) { Task { await save() } }
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(!dirty || over || count == 0 || name.trimmingCharacters(in: .whitespaces).isEmpty
                              || !engine.isBundled || engine.busy)
            }
        }
    }

    static func tilde(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    // MARK: Actions

    /// Show skill `n`, asking first if the open one has unsaved changes.
    private func ask(_ n: Int?) {
        guard n != selection else { return }
        if dirty { pending = .some(n) } else { select(n) }
    }

    private func select(_ n: Int?) {
        pending = nil
        selection = n
        loaded = nil
        guard let n else { return }
        loading = true
        Task {
            let t = await engine.loadSkillText(n)
            guard selection == n else { return }
            loading = false
            if let t { fill(t) }
        }
    }

    private func fill(_ t: Engine.SkillText) {
        loaded = t
        name = t.name
        summary = t.description
        text = t.text
    }

    @discardableResult private func save() async -> Bool {
        guard let n = selection else { return false }
        let ok = await engine.saveLocalSkill(n, name: name, description: summary, text: text)
        if ok, let t = await engine.loadSkillText(n) { fill(t) }
        return ok
    }

    private func importSkill() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Import"
        panel.message = "A SKILL.md, or a skill's folder. keepmum copies it into its skills folder, so editing it here leaves the original alone."
        if panel.runModal() == .OK, let url = panel.url {
            Task {
                let before = skills.count
                await engine.addLocalSkill(url)
                if skills.count > before { select(skills.count) }
            }
        }
    }
}

/// Opening the Skills window from Settings, on one skill or none.
@MainActor
enum SkillsWindow {
    static let id = "skills"

    static func show(_ openWindow: OpenWindowAction, engine: Engine, skill: Int? = nil) {
        engine.skillToEdit = skill
        openWindow(id: id)
        DispatchQueue.main.async {
            NSApp.activate()
            NSApp.windows.first { $0.identifier?.rawValue.hasPrefix(id) == true }?.makeKeyAndOrderFront(nil)
        }
    }
}
