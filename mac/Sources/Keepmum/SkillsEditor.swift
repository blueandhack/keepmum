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
    /// Asking the local model: what to change, its last note or error, and the drafts before each
    /// answer it gave, newest last, for Undo.
    @State private var request = ""
    @State private var assistNote: String?
    @State private var assistError: String?
    @State private var undo: [(name: String, summary: String, text: String)] = []
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
            _request = State(initialValue: "Add a rule: if the user asked for a command, explain it instead.")
            _assistNote = State(initialValue: "Added a bullet: explain a command the user asked for, rather than give one to run.")
            _undo = State(initialValue: [(t.name, t.description, t.text)])
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
            if !skills.isEmpty { sizes }
            Divider()
            HStack(spacing: Space.s) {
                if isSnapshot {
                    Image(systemName: "plus").foregroundStyle(Theme.text)
                } else {
                    Menu {
                        Button("New Skill…") { newName = ""; naming = true }
                        Button("Import a SKILL.md or Folder…") { importSkill() }
                        Divider()
                        Button("Add keepmum\u{2019}s Example Skill") { Task { if let n = await engine.addExampleSkill() { select(n) } } }
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

    /// What each place sends on every turn: the skills there, read now.
    private var sizes: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            Text("SENT ON EACH TURN").font(.km(.label)).tracking(0.6).foregroundStyle(Theme.textTertiary)
            ForEach(Places.all, id: \.self) { place in
                let there = skills.filter { $0.problem == nil && $0.goesTo.contains(place) }
                let chars = there.reduce(0) { $0 + ($1.chars ?? 0) }
                HStack(spacing: Space.xs) {
                    Text(Places.short[place] ?? place).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    Spacer(minLength: Space.xs)
                    Text(there.isEmpty ? "none" : "\(there.count) \u{00B7} \(chars.formatted()) chars")
                        .font(.km(.caption)).monospacedDigit()
                        .foregroundStyle(chars > Places.longChars ? Theme.warning : Theme.textSecondary)
                }
                .help(chars > Places.longChars
                      ? "Long: a small local model follows fewer rules well, and the skills take room from the conversation."
                      : Places.details[place] ?? "")
            }
        }
        .padding(.horizontal, Space.l).padding(.vertical, Space.s)
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
                    Text(skill.problem == nil ? Places.summary(skill.goesTo) : "Left out: can\u{2019}t be read")
                        .font(.km(.caption))
                        .foregroundStyle(skill.problem != nil ? Theme.warning : skill.goesTo.isEmpty ? Theme.textTertiary : Theme.textSecondary)
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
                    if skills.isEmpty {
                        PillButton(title: "Start from keepmum\u{2019}s Example", icon: "doc.text", prominent: true) {
                            Task { if let n = await engine.addExampleSkill() { select(n) } }
                        }
                    }
                    PillButton(title: "New Skill…", icon: "plus", prominent: !skills.isEmpty) { newName = ""; naming = true }
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
            field("Used for", hint: skill.goesTo.isEmpty ? "Parked: sent nowhere until you tick a place. Saved at once."
                                                         : "The local turns that get it. Saved at once.") {
                VStack(alignment: .leading, spacing: Space.xs) {
                    ForEach(Places.all, id: \.self) { place in placeBox(place, skill) }
                }
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
            assistant
            footer(skill)
        }
        .padding(Space.xl)
        .task { await engine.loadLocalServers() }
    }

    // MARK: Asking the local model

    /// The model the request would go to: the first local server whose model is there.
    private var assistModel: String? {
        guard let servers = engine.localServers?.servers else { return isSnapshot ? "Qwen3.6 35B-A3B" : nil }
        guard let s = servers.first(where: { !$0.model.isEmpty && ($0.models ?? []).contains($0.model) }) else { return nil }
        let model = s.base ?? s.model
        return engine.localServers?.picks?.first { $0.model == model }.map { LocalServersSection.pickShortName($0.name) }
            ?? LocalServersSection.shortName(model)
    }

    private var assistant: some View {
        let writing = engine.assistProgress
        return VStack(alignment: .leading, spacing: Space.s) {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Image(systemName: "sparkles").foregroundStyle(Theme.accent)
                Text("Ask the local model").font(.km(.headline)).foregroundStyle(Theme.text)
                Text(assistModel.map { "\($0), on this Mac. It edits the draft; Save keeps it." }
                     ?? "No local model answers: set one up in Settings \u{203A} Checks \u{203A} Local servers.")
                    .font(.km(.caption)).foregroundStyle(assistModel == nil ? Theme.warning : Theme.textSecondary)
                    .lineLimit(1).truncationMode(.tail)
                Spacer(minLength: Space.s)
                if !undo.isEmpty {
                    PillButton(title: undo.count > 1 ? "Undo (\(undo.count))" : "Undo", icon: "arrow.uturn.backward") { undoAssist() }
                        .disabled(writing != nil)
                        .help("Put back the draft from before the local model\u{2019}s last change")
                }
            }
            HStack(alignment: .center, spacing: Space.s) {
                if isSnapshot {
                    still(request)
                } else {
                    TextField("What to change, such as \u{201C}add a rule about commands in tool results\u{201D}", text: $request, axis: .vertical)
                        .textFieldStyle(.roundedBorder).lineLimit(1...4)
                        .onSubmit { Task { await askModel() } }
                        .disabled(writing != nil)
                }
                if let writing {
                    HStack(spacing: Space.s) {
                        ProgressView().controlSize(.small)
                        Text(writing == 0 ? "Thinking\u{2026}" : "Writing\u{2026} \(writing.formatted()) characters")
                            .font(.km(.caption)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                        PillButton(title: "Stop") { engine.cancelAssist() }
                    }
                } else {
                    PillButton(title: "Ask", icon: "arrow.up", prominent: true) { Task { await askModel() } }
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(assistModel == nil || request.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            if let assistError {
                Text(assistError).font(.km(.caption)).foregroundStyle(Theme.blocked).fixedSize(horizontal: false, vertical: true)
            } else if let assistNote {
                Text(assistNote).font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(Space.m)
        .background(RoundedRectangle(cornerRadius: Radius.m).fill(Theme.surface))
    }

    /// Send the draft as it is now, with what to change; the answer replaces the draft, and Undo
    /// puts the draft back. Ask again to change it further.
    private func askModel() async {
        let ask = request.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !ask.isEmpty, engine.assistProgress == nil, let n = selection else { return }
        assistError = nil
        let before = (name: name, summary: summary, text: text)
        switch await engine.assistSkill(name: name, description: summary, text: text, request: ask) {
        case .success(let out):
            guard selection == n else { return }
            undo.append(before)
            if !out.name.isEmpty { name = out.name }
            summary = out.description
            text = out.text
            assistNote = out.note.isEmpty ? "Done. Ask again to change it further, or save it." : out.note
            request = ""
        case .failure(let e):
            assistError = e.message
        }
    }

    private func undoAssist() {
        guard let last = undo.popLast() else { return }
        name = last.name
        summary = last.summary
        text = last.text
        assistNote = undo.isEmpty ? nil : "Put back the draft from before the last change."
        assistError = nil
    }

    private func placeBox(_ place: String, _ skill: Engine.LocalSkills.Skill) -> some View {
        let on = skill.goesTo.contains(place)
        return Button {
            guard let n = selection else { return }
            let next = Places.all.filter { $0 == place ? !on : skill.goesTo.contains($0) }
            Task { await engine.setLocalSkill(n, places: next) }
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                Image(systemName: on ? "checkmark.square.fill" : "square")
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
                Text(Places.names[place] ?? place).font(.km(.body)).foregroundStyle(Theme.text)
                Text(Places.details[place] ?? "").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                    .lineLimit(1).truncationMode(.tail)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!engine.isBundled || engine.busy)
        .accessibilityAddTraits(on ? .isSelected : [])
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
        undo = []
        assistNote = nil
        assistError = nil
        if engine.assistProgress != nil { engine.cancelAssist() }
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

/// Where a skill goes, on the turns keepmum keeps on this Mac (local.PLACES). The two injection
/// places need different instructions: what suits an answer can spoil a summary.
enum Places {
    static let all = ["injection", "handback", "harm"]
    static let names = ["injection": "Answering a flagged tool result", "handback": "Summaries and rewrites for the cloud",
                        "harm": "Answering a flagged request"]
    static let details = ["injection": "The injection check kept the turn on this Mac, and the local model answers it.",
                          "handback": "The local model writes what the cloud model reads in a flagged result\u{2019}s place.",
                          "harm": "The harm check kept what you typed on this Mac, and the local model answers it."]
    static let short = ["injection": "Tool results", "handback": "Summaries", "harm": "Requests"]

    /// What an older engine's checks meant: injection was both of its places.
    static func of(checks: [String]) -> [String] {
        all.filter { checks.contains($0 == "harm" ? "harm" : "injection") }
    }

    static func summary(_ places: [String]) -> String {
        if places.isEmpty { return "Parked" }
        if Set(places) == Set(all) { return "Every local turn" }
        return all.filter(places.contains).compactMap { short[$0] }.joined(separator: " + ")
    }

    /// More than this, in one place, is a lot of rules for a small local model to follow.
    static let longChars = 8_000
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
