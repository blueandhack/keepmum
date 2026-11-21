import AppKit
import SwiftUI

/// Local turns: what the local model does with a turn a check keeps on this Mac. Both
/// checks get the same card: what follows the local turn, the local model, and each step that model
/// takes (answer on this Mac, rewrite for the cloud) with what it reads there: an instruction and
/// the skills picked for it. Then whether you look before something goes to the cloud.
struct LocalHandbackSection: View {
    var engine: Engine
    /// Opens another Settings pane (Local models, Checks); nil on the Protection map, which has none.
    var open: ((SettingsPane) -> Void)?
    @Environment(\.isSnapshot) private var isSnapshot
    @Environment(\.openWindow) private var openWindow
    @State private var editing: EditTarget?

    init(engine: Engine, open: ((SettingsPane) -> Void)? = nil) {
        self.engine = engine
        self.open = open
    }

    /// The instruction the editor sheet is open on.
    struct EditTarget: Identifiable {
        var check: String
        var strategy: String
        var id: String { check }
    }

    static let choices: [(id: String, label: String, detail: String)] = [
        ("stay", "Stay local", "The conversation stays on the local model until you rewind or clear it."),
        ("answer", "Local answer, then cloud", "The local model answers the flagged turn. After that the result is withheld, and the cloud model carries on with the local answer."),
        ("summary", "Local summary, then cloud", "The local model summarises the flagged result first; the summary goes to the cloud model in its place."),
        ("rewrite", "Local rewrite, then cloud", "The local model takes out what the result says to the agent; the rest goes to the cloud model."),
        ("withhold", "Withhold, then cloud", "No local model: a notice takes the result's place, and the turn goes to the cloud model."),
    ]

    static let harmChoices: [(id: String, label: String, detail: String)] = [
        ("stay", "Stay local", "The local model answers what you typed, and the conversation stays on it."),
        ("rewrite", "Local rewrite, then cloud",
         "The local model takes the flagged part out of what you typed and keeps the rest; that goes to the cloud model. Still flagged, or nothing left: it stays local."),
    ]

    private var current: String { engine.localHandback?.handback ?? engine.status?.guard?.local?.handback ?? "stay" }
    private var harmCurrent: String { engine.localHandback?.harm ?? engine.status?.guard?.local?.handbackHarm ?? "stay" }
    private var skills: [Engine.LocalSkills.Skill] { engine.localSkills?.skills ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xl) {
            checkSection("harm")
            checkSection("injection")
            if let review = engine.localHandback?.review {
                FormSection(title: "Review before it goes",
                            footer: "keepmum holds the request while you look, in a panel that does not take the keyboard. With this app closed, and no keepmum review --watch in a terminal, nothing waits.",
                            details: "Your answer holds for that conversation's history; the same text new in a later request (typed again, or returned again) is asked about again, except within 30 s, for the agent's own request with the turn or a retry. With Every flagged turn, the whole turn waits in one larger window instead: every flagged piece listed, each with the original beside what goes in its place, and its own answer (a rewrite or, for a tool result, a summary, written by the local model while you look; the original, after a warning; or removed); then Send to the Cloud or Keep the Turn on This Mac. In the panel you can send what the local model wrote, edit it yourself, ask the local model to change it (the time left stops while it writes), go back to the original (keepmum warns first: the cloud model then gets what the check flagged), withhold a tool result, or keep the turn on this Mac. An edit of yours is checked like the local model's text; if the check flags it, it is still sent, as you wrote it, and the hit is logged. A rewrite of what you typed that the harm check still flags waits here too, told why, and with no answer stays local.") {
                    reviewRows(review)
                }
            }
        }
        .task(id: engine.status?.guard?.local) {
            await engine.loadLocalHandback()
            await engine.loadLocalSkills()
        }
        .sheet(item: $editing) { t in InstructionEditor(engine: engine, check: t.check, strategy: t.strategy) }
    }

    // MARK: One check

    @ViewBuilder private func checkSection(_ check: String) -> some View {
        let harm = check == "harm"
        let mode = harm ? engine.status?.guard?.harm.mode : engine.status?.guard?.injection.mode
        let strategy = harm ? harmCurrent : current
        let options = harm ? Self.harmChoices : Self.choices
        let choice = options.first { $0.id == strategy } ?? options[0]
        FormSection(title: harm ? "What you typed" : "Tool results",
                    footer: harm ? "The harm check reads what you type. The cloud model never gets what it flagged, in any wording."
                                 : "The injection check reads what tools return. The cloud model never gets a flagged result, only what takes its place.",
                    details: harm
                        ? "A rewrite only takes words out. Whatever its instruction and skills say, keepmum sends it only if every piece is in what you typed, word for word and in order, and the harm check passes it whole and sentence by sentence. Reworded, still flagged, or nothing left: the turn stays on this Mac (or, under review, waits for you, told why)."
                        : "What takes the flagged result's place is masked like the rest of the request and checked by the injection check first; if the check flags what the local model wrote, that turn stays local. A result too long for the local model gets no summary or rewrite: under review you decide on keepmum's notice instead.") {
            if let mode, mode != "local" {
                FormRow(title: "Not in Local mode",
                        detail: "The \(harm ? "harm" : "injection") check is set to \(mode.capitalizedFirst): this applies once it is Local",
                        detailTone: Theme.warning) {
                    if let open { PillButton(title: "Checks", icon: "chevron.right") { open(.checks) } }
                }
                Hairline()
            }
            FormRow(title: "After a local turn", detail: choice.detail) {
                ChoiceMenu(engine: engine, label: choice.label, items: options.map { c in
                    (c.label, { if harm { await engine.setLocalHandbackHarm(c.id) } else { await engine.setLocalHandback(c.id) } })
                })
                .help(harm ? "What follows a turn the harm check kept on this Mac" : "What follows a turn the injection check kept on this Mac")
            }
            Hairline()
            modelRow(check)
            if harm || strategy != "withhold" {
                Hairline()
                step(1, "Answer on this Mac",
                     harm ? (strategy == "rewrite" ? "When a rewrite can\u{2019}t go: the local model answers what you typed"
                                                   : "The local model answers what you typed")
                          : strategy == "answer" ? "The local model answers the flagged turn, then the cloud takes over"
                          : strategy == "stay" ? "The local model answers, and the conversation stays on it"
                          : "When a \(strategy) can\u{2019}t go: the local model answers the turn") {
                    line("Instruction") {
                        HStack(spacing: Space.s) {
                            Chip(text: "Built in", tone: Theme.textSecondary)
                            Text("keepmum\u{2019}s note: why the turn stayed on this Mac, and that the model has no tools. Your own guidance goes in skills.")
                                .font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    line("Skills") { skillChips(harm ? "harm" : "injection") }
                }
            } else {
                Hairline()
                FormRow(title: "No local model", detail: "keepmum\u{2019}s notice takes the result\u{2019}s place, and the turn goes to the cloud model") { EmptyView() }
            }
            if harm ? strategy == "rewrite" : ["summary", "rewrite"].contains(strategy) {
                Hairline()
                step(2, harm ? "Rewrite for the cloud" : strategy == "summary" ? "Summary for the cloud" : "Rewrite for the cloud",
                     harm ? "The flagged part out, the rest word for word" : "What the cloud model reads in the result\u{2019}s place") {
                    line("Instruction") { instructionControl(check, strategy: harm ? "rewrite" : strategy) }
                    line("Skills") {
                        VStack(alignment: .leading, spacing: Space.xs) {
                            skillChips(harm ? "harm_handback" : "handback")
                            if harm {
                                Text("None unless you pick one: a skill written for answers can make the rewrite drop your task. keepmum still checks every rewrite.")
                                    .font(.km(.caption)).foregroundStyle(Theme.textTertiary).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
        }
    }

    /// The local model this check's turns go to: the first server for it, and how many more back it up.
    private func modelRow(_ check: String) -> some View {
        let live = (engine.localServers?.servers ?? []).filter { $0.checks.contains(check) && !$0.model.isEmpty }
        let set = (engine.status?.guard?.local?.servers ?? []).filter { $0.checks.contains(check) && !$0.model.isEmpty }
        let first = live.first.map { (model: $0.base ?? $0.model, url: $0.url, problem: $0.problem) }
            ?? set.first.map { (model: $0.model, url: $0.url, problem: nil) }
        let count = max(live.count, set.count)
        let name = first.map { f in
            engine.localServers?.picks?.first { $0.model == f.model || $0.splash == f.model }.map { LocalServersSection.pickShortName($0.name) }
                ?? LocalServersSection.shortName(f.model)
        }
        let host = first.map { $0.url.replacingOccurrences(of: "http://", with: "").replacingOccurrences(of: "https://", with: "") }
        let detail = name.map { n in
            n + " at \(host ?? "")" + (first?.problem != nil ? ", not answering now" : "")
                + (count > 1 ? "; then \(count - 1) more if it can\u{2019}t answer" : "")
        } ?? "None set: what this check keeps local is refused"
        return FormRow(title: "Local model", detail: detail, detailTone: name == nil || first?.problem != nil ? Theme.warning : Theme.textSecondary,
                       help: "Every step below uses it. Servers, models and speed are in Local models.") {
            if let open { PillButton(title: "Local models", icon: "chevron.right") { open(.localModels) } }
        }
    }

    // MARK: A step and its lines

    private func step<Rows: View>(_ number: Int, _ title: String, _ detail: String, @ViewBuilder rows: () -> Rows) -> some View {
        VStack(alignment: .leading, spacing: Space.s + Space.xxs) {
            HStack(spacing: Space.s) {
                Text("\(number)").font(.km(.caption).weight(.semibold)).monospacedDigit().foregroundStyle(Theme.accent)
                    .frame(width: 20, height: 20).background(Circle().fill(Theme.soft(Theme.accent)))
                Text(title).font(.km(.body).weight(.medium)).foregroundStyle(Theme.text)
                Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(2)
            }
            VStack(alignment: .leading, spacing: Space.s + Space.xxs) { rows() }
                .padding(.leading, 20 + Space.s)
        }
        .padding(.vertical, Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func line<Content: View>(_ label: String, @ViewBuilder _ content: () -> Content) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.m) {
            Text(label).font(.km(.callout)).foregroundStyle(Theme.textSecondary).frame(width: 84, alignment: .leading)
            content().frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: Instruction

    @ViewBuilder private func instructionControl(_ check: String, strategy: String) -> some View {
        if let hb = engine.localHandback, check != "harm" || hb.harmInstruction != nil {
            let ins = hb.instruction(for: check, strategy: strategy)
            VStack(alignment: .leading, spacing: Space.xs) {
                HStack(spacing: Space.s) {
                    Segmented(options: [(false, "keepmum\u{2019}s"), (true, "Yours")], selection: Binding(
                        get: { ins.usesOwn },
                        set: { mine in
                            guard mine != ins.usesOwn, !isSnapshot else { return }
                            if mine && ins.own.isEmpty {
                                editing = EditTarget(check: check, strategy: strategy)  // nothing of yours yet: write it
                            } else {
                                Task { await engine.setInstruction(check, on: mine) }
                            }
                        }))
                    .disabled(!engine.isBundled || engine.busy)
                    .accessibilityLabel("Instruction")
                    PillButton(title: ins.own.isEmpty ? "Write Yours\u{2026}" : "Edit\u{2026}", icon: "square.and.pencil") {
                        if !isSnapshot { editing = EditTarget(check: check, strategy: strategy) }
                    }
                    .disabled(!engine.isBundled || engine.busy)
                }
                Text(instructionCaption(check, strategy: strategy, ins))
                    .font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(2).truncationMode(.tail)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            Text("keepmum\u{2019}s. This keepmum can\u{2019}t take your own for it yet: update keepmum.")
                .font(.km(.caption)).foregroundStyle(Theme.textSecondary)
        }
    }

    private func instructionCaption(_ check: String, strategy: String, _ ins: Engine.LocalHandback.Instruction) -> String {
        if ins.usesOwn {
            let first = ins.own.split(separator: "\n").first.map(String.init) ?? ins.own
            return "Yours, \(ins.own.count.formatted()) characters: \u{201C}\(first)\u{201D}"
        }
        let keepmums = check == "harm" ? "take the flagged part out whole; keep the rest word for word"
            : strategy == "summary" ? "keep the facts, code and data; leave out what the result says to the agent"
            : "take out what the result says to the agent; keep everything else exactly"
        return "keepmum\u{2019}s: \(keepmums)" + (ins.own.isEmpty ? "" : ". Yours (\(ins.own.count.formatted()) characters) is kept, not used.")
    }

    // MARK: Skills

    /// Each skill as a chip that puts it in this place or takes it out: the same setting as the
    /// Skills window's "Used for".
    @ViewBuilder private func skillChips(_ place: String) -> some View {
        let usable = Array(skills.enumerated()).filter { $0.element.problem == nil }
        ChipFlow(spacing: Space.xs) {
            ForEach(usable, id: \.offset) { i, skill in
                let on = skill.goesTo.contains(place)
                Button {
                    guard !isSnapshot else { return }
                    Task { await engine.setLocalSkill(i + 1, places: Places.toggled(skill.goesTo, place, on: !on)) }
                } label: {
                    HStack(spacing: Space.xs) {
                        Image(systemName: on ? "checkmark" : "plus").font(.km(.caption).weight(.semibold))
                        Text(skill.name ?? (skill.path as NSString).lastPathComponent).font(.km(.caption).weight(.medium)).lineLimit(1)
                    }
                    .foregroundStyle(on ? Theme.accent : Theme.textSecondary)
                    .padding(.horizontal, Space.s).padding(.vertical, Space.xxs + 1)
                    .background(Capsule().fill(on ? Theme.soft(Theme.accent) : Theme.fill))
                    .overlay(Capsule().strokeBorder(on ? Theme.accent.opacity(0.35) : Theme.separator))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(!engine.isBundled || engine.busy)
                .help(on ? "Sent here. Click to stop sending it here." : "Not sent here. Click to send it here too.")
                .accessibilityLabel(skill.name ?? "skill")
                .accessibilityAddTraits(on ? .isSelected : [])
            }
            Button {
                if !isSnapshot { SkillsWindow.show(openWindow, engine: engine) }
            } label: {
                Text(usable.isEmpty ? "No skills yet: write one\u{2026}" : "Skills\u{2026}")
                    .font(.km(.caption).weight(.medium)).foregroundStyle(Theme.accent)
                    .padding(.horizontal, Space.xs).padding(.vertical, Space.xxs + 1)
            }
            .buttonStyle(.plain)
            .help("Write, import and edit skills in the Skills window")
        }
    }

    // MARK: Review

    static let reviewModes: [(id: String, label: String)] = [
        ("off", "Off"), ("rewrite", "Rewrites only"), ("all", "Answers, summaries and rewrites"),
        ("turn", "Every flagged turn, in one window"),
    ]
    static let waits = [30, 60, 120, 300]

    private func seconds(_ s: Int) -> String { s < 120 ? "\(s) s" : "\(s / 60) min" }

    @ViewBuilder private func reviewRows(_ review: Engine.LocalHandback.Review) -> some View {
        let label = Self.reviewModes.first { $0.id == review.mode }?.label ?? review.mode
        let skipped = review.mode == "rewrite" && current != "rewrite"
        let typedOnly = !["answer", "summary", "rewrite"].contains(current)  // only the harm check's rewrite is reviewed
        let nothing = typedOnly && harmCurrent != "rewrite" && review.mode != "turn"
        FormRow(title: "Review first",
                detail: review.mode == "turn" ? "Every turn a check keeps on this Mac opens one window: each flagged piece, what you typed and each tool result, with the local model\u{2019}s draft; keep it, rewrite it, send the original or remove it, then send the turn or keep it here. Your settings above are the defaults"
                    : nothing ? "Nothing to review: both checks stay local. Pick Every flagged turn to decide each turn yourself"
                    : review.mode == "off" ? "Off: what the local model wrote goes straight to the cloud"
                    : typedOnly ? "A panel shows the local model\u{2019}s rewrite of what you typed: send it, change it, or keep the turn local"
                    : skipped ? "Rewrites only: this hand-back goes without a review"
                        + (harmCurrent == "rewrite" ? "; a rewrite of what you typed waits for one" : "")
                    : current == "answer" ? "After the local answer, a panel shows what the next turn sends in the result\u{2019}s place"
                    : "A panel shows what the local model wrote: send it, change it, go back to the original, or keep the turn local") {
            ChoiceMenu(engine: engine, label: label,
                       items: Self.reviewModes.map { m in (m.label, { await engine.setLocalHandbackReview(mode: m.id) }) })
        }
        Hairline()
        FormRow(title: "See how it looks",
                detail: "A demo on made-up text: the review panel, or the whole-turn window. Try every answer; nothing is sent") {
            HStack(spacing: Space.s) {
                PillButton(title: "Try the Panel", icon: "rectangle.portrait") { if !isSnapshot { ReviewCenter.shared.showDemoPanel() } }
                PillButton(title: "Try the Window", icon: "macwindow") { if !isSnapshot { ReviewCenter.shared.showDemoTurn() } }
            }
        }
        if review.mode != "off" {
            Hairline()
            FormRow(title: "Wait", detail: "The agent waits too, up to this long; not while the local model changes the text for you") {
                ChoiceMenu(engine: engine, label: seconds(review.wait),
                           items: (Self.waits.contains(review.wait) ? Self.waits : (Self.waits + [review.wait]).sorted())
                               .map { w in (seconds(w), { await engine.setLocalHandbackReview(wait: w) }) })
            }
            Hairline()
            FormRow(title: "If no answer",
                    detail: review.timeout == "send" ? "What the local model wrote goes as written" : "The turn stays on this Mac, answered by the local model") {
                ChoiceMenu(engine: engine, label: review.timeout == "send" ? "Send as written" : "Keep it local",
                           items: [("Keep it local", { await engine.setLocalHandbackReview(timeout: "stay") }),
                                   ("Send as written", { await engine.setLocalHandbackReview(timeout: "send") })])
            }
        }
    }
}

/// Writing your own instruction for a check's rewrite, in place of keepmum's: by hand, from
/// keepmum's, or by asking the local model. Saving uses it; keepmum's text saved as is means keepmum's.
struct InstructionEditor: View {
    var engine: Engine
    /// "harm", or "injection" (its summary and rewrite share one instruction).
    var check: String
    var strategy: String
    @Environment(\.dismiss) private var dismiss
    @Environment(\.isSnapshot) private var isSnapshot
    @State private var text = ""
    @State private var request = ""
    @State private var note: String?
    @State private var error: String?
    @State private var undo: [String] = []
    @State private var started = false

    static let limit = 4_000

    private var ins: Engine.LocalHandback.Instruction? { engine.localHandback?.instruction(for: check, strategy: strategy) }
    private var keepmums: String { ins?.keepmums ?? "" }
    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var isKeepmums: Bool { trimmed == keepmums.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var harm: Bool { check == "harm" }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            VStack(alignment: .leading, spacing: Space.xs) {
                Text(harm ? "Instruction for rewriting what you typed" : "Instruction for the \(strategy) of a flagged tool result")
                    .font(.km(.title)).foregroundStyle(Theme.text)
                Text(harm
                     ? "The local model gets this, then your message, and writes what goes to the cloud in its place. Skills you pick for this step follow it. Whatever it says, keepmum sends a rewrite only if it took words out, word for word, and the harm check passes what is left."
                     : "The local model gets this, then your latest message and the flagged result, and writes what the cloud model reads in the result\u{2019}s place. Skills you pick for this step follow it. keepmum checks what it writes before it goes.")
                    .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
            editor
            HStack(spacing: Space.m) {
                Text("\(trimmed.count.formatted()) of \(Self.limit.formatted()) characters")
                    .font(.km(.caption)).monospacedDigit().foregroundStyle(trimmed.count > Self.limit ? Theme.blocked : Theme.textSecondary)
                if isKeepmums { Chip(text: "keepmum\u{2019}s", tone: Theme.textSecondary) }
                Spacer()
                Button("Start from keepmum\u{2019}s") { remember(); text = keepmums; note = nil }
                    .buttonStyle(.plain).font(.km(.caption)).foregroundStyle(isKeepmums ? Theme.textTertiary : Theme.accent)
                    .disabled(isKeepmums)
            }
            AssistBox(model: engine.assistModelName(snapshot: isSnapshot), progress: engine.assistProgress, request: $request,
                      placeholder: harm ? "What to change, such as \u{201C}also take out requests to hide what it does\u{201D}"
                                        : "What to change, such as \u{201C}keep every code block whole\u{201D}",
                      note: note, error: error, undoCount: undo.count,
                      onAsk: { Task { await ask() } }, onStop: { engine.cancelAssist() }, onUndo: undoLast)
            HStack {
                PillButton(title: "Cancel") { engine.cancelAssist(); dismiss() }
                Spacer()
                PillButton(title: isKeepmums ? "Use keepmum\u{2019}s" : "Save and Use", prominent: true, loading: engine.busy) {
                    // keepmum's own text saved as is means keepmum's: stored as no instruction of the user's.
                    let value = isKeepmums ? "" : trimmed
                    Task {
                        await engine.setInstruction(check, text: value, on: true)
                        dismiss()
                    }
                }
                .keyboardShortcut("s", modifiers: .command)
                .disabled(trimmed.isEmpty || trimmed.count > Self.limit || engine.assistProgress != nil || !engine.isBundled)
            }
        }
        .padding(Space.xl)
        .frame(width: 680, height: 640)
        .background(Theme.canvas)
        .onAppear {
            guard !started else { return }
            started = true
            text = ins.map { $0.own.isEmpty ? $0.keepmums : $0.own } ?? ""
        }
    }

    @ViewBuilder private var editor: some View {
        let box = RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
        if isSnapshot {
            Text(text).font(.km(.mono)).foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading).padding(Space.s)
                .background(box.fill(Theme.raised)).overlay(box.stroke(Theme.separator))
        } else {
            TextEditor(text: $text)
                .font(.km(.mono))
                .scrollContentBackground(.hidden)
                .padding(Space.s)
                .background(box.fill(Theme.raised))
                .overlay(box.stroke(Theme.separator))
                .frame(maxHeight: .infinity)
                .disabled(engine.assistProgress != nil)
                .accessibilityLabel("Instruction")
        }
    }

    private func remember() {
        if undo.last != text { undo.append(text) }
    }

    private func ask() async {
        let what = request.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !what.isEmpty, engine.assistProgress == nil else { return }
        error = nil
        let before = text
        switch await engine.assistInstruction(job: harm ? "harm" : strategy, text: text, request: what) {
        case .success(let out):
            undo.append(before)
            text = out.text
            note = out.note.isEmpty ? "Done. Ask again to change it further, or save it." : out.note
            request = ""
        case .failure(let e):
            error = e.message
        }
    }

    private func undoLast() {
        guard let last = undo.popLast() else { return }
        text = last
        note = undo.isEmpty ? nil : "Put back the text from before the last change."
        error = nil
    }
}

/// "Ask the local model": what to change, the model that will, its progress, its note on what it
/// changed, and Undo. The Skills window, the instruction editor and the review panel use it.
struct AssistBox: View {
    /// The local model's short name; nil when none answers.
    var model: String?
    /// nil: not asking; 0: thinking; more: characters written so far.
    var progress: Int?
    @Binding var request: String
    var placeholder: String
    var note: String?
    var error: String?
    var undoCount: Int
    /// The review panel's: one line, no heading.
    var compact = false
    var onAsk: () -> Void
    var onStop: () -> Void
    var onUndo: () -> Void
    @Environment(\.isSnapshot) private var isSnapshot

    private var empty: Bool { request.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? Space.xs : Space.s) {
            if !compact {
                HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                    Image(systemName: "sparkles").foregroundStyle(Theme.accent)
                    Text("Ask the local model").font(.km(.headline)).foregroundStyle(Theme.text)
                    Text(model.map { "\($0), on this Mac. It edits the draft; Save keeps it." }
                         ?? "No local model answers: set one up in Settings \u{203A} Local models.")
                        .font(.km(.caption)).foregroundStyle(model == nil ? Theme.warning : Theme.textSecondary)
                        .lineLimit(1).truncationMode(.tail)
                    Spacer(minLength: Space.s)
                    undoButton
                }
            }
            HStack(alignment: .center, spacing: Space.s) {
                if compact { Image(systemName: "sparkles").font(.km(.caption)).foregroundStyle(Theme.accent) }
                if isSnapshot {
                    Text(request.isEmpty ? placeholder : request).font(.km(compact ? .callout : .body))
                        .foregroundStyle(request.isEmpty ? Theme.textTertiary : Theme.text).lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, Space.s - 2).padding(.vertical, Space.xs)
                        .background(RoundedRectangle(cornerRadius: Radius.s - 2).fill(Theme.raised))
                        .overlay(RoundedRectangle(cornerRadius: Radius.s - 2).stroke(Theme.separator))
                } else {
                    TextField(placeholder, text: $request, axis: .vertical)
                        .textFieldStyle(.roundedBorder).lineLimit(1...4)
                        .onSubmit { if !empty && model != nil { onAsk() } }
                        .disabled(progress != nil)
                }
                if let progress {
                    HStack(spacing: Space.s) {
                        ProgressView().controlSize(.small)
                        Text(progress == 0 ? (compact ? "Writing\u{2026}" : "Thinking\u{2026}") : "Writing\u{2026} \(progress.formatted()) characters")
                            .font(.km(.caption)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                        PillButton(title: "Stop") { onStop() }
                    }
                } else {
                    PillButton(title: "Ask", icon: "arrow.up", prominent: !compact) { onAsk() }
                        .disabled(model == nil || empty)
                        .help(model.map { "Ask \($0), on this Mac, to change the text as you say" } ?? "No local model answers")
                    if compact { undoButton }
                }
            }
            if let error {
                Text(error).font(.km(.caption)).foregroundStyle(Theme.blocked).fixedSize(horizontal: false, vertical: true)
            } else if let note {
                Text(note).font(.km(.caption)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(compact ? 0 : Space.m)
        .background { if !compact { RoundedRectangle(cornerRadius: Radius.m).fill(Theme.surface) } }
    }

    @ViewBuilder private var undoButton: some View {
        if undoCount > 0 {
            PillButton(title: undoCount > 1 ? "Undo (\(undoCount))" : "Undo", icon: "arrow.uturn.backward") { onUndo() }
                .disabled(progress != nil)
                .help("Put back the text from before the local model\u{2019}s last change")
        }
    }
}
