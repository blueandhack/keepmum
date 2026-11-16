import SwiftUI

/// What a demo review would have done, shown once it is answered: in the panel or in the turn window.
struct DemoOutcome: Equatable {
    var window: Bool
    var title: String
    var lines: [String]
}

/// Trying the review panel and the turn window on made-up content. A demo never reaches keepmum:
/// its answers, its "Write Again" and its "Ask the local model" are played here, and answering it
/// says what a real review would have done.
extension ReviewCenter {
    static func isDemo(_ id: String) -> Bool { id.hasPrefix("demo-") }

    var demoInPanel: Bool { demoReview != nil || demoOutcome.map { !$0.window } == true }
    var demoInWindow: Bool { demoTurn != nil || demoOutcome?.window == true }

    /// The review panel, on a rewrite of a web page with an instruction planted in it.
    func showDemoPanel() {
        demoOutcome = nil
        demoReview = .demo()
        ReviewPanel.show(self)
    }

    /// The turn window, on a turn with what you typed and two tool results flagged; one is still
    /// being written, as a real one would be when the window opens.
    func showDemoTurn() {
        demoOutcome = nil
        demoTurn = .demo()
        TurnWindow.show(self)
        Task {
            try? await Task.sleep(for: .seconds(3))
            guard var turn = demoTurn, let i = turn.pieces.firstIndex(where: \.drafting) else { return }
            turn.pieces[i].drafting = false
            turn.pieces[i].kind = "summary"
            turn.pieces[i].draft = Self.demoTexts["exec.summary"]
            demoTurn = turn
        }
    }

    func endDemo(window: Bool) {
        if window { demoTurn = nil } else { demoReview = nil }
        if demoOutcome?.window == window { demoOutcome = nil }
    }

    func finishDemoPanel(_ action: String, text: String?) {
        guard let review = demoReview else { return }
        let what = "what \(review.source ?? "the tool") returned"
        let line: String = switch action {
        case "send": "The cloud model gets the local model\u{2019}s rewrite in place of \(what). The planted line never leaves your Mac."
        case "edit": "The cloud model gets your edit in place of \(what). keepmum checks it with the injection check and notes a flag, but sends it as you wrote it."
        case "original": "The cloud model gets \(what) as it was, the planted line too. That is why keepmum warns first, and asks for a second click."
        case "withhold": "The cloud model gets keepmum\u{2019}s notice that the result was withheld, and nothing of it."
        default: "Your local model answers this turn on your Mac. Nothing goes to the cloud."
        }
        demoReview = nil
        demoOutcome = DemoOutcome(window: false, title: Self.demoTitles[action] ?? "Done",
                                  lines: [line, "No answer in time does what Settings \u{203A} Local turns \u{203A} If no answer says."])
    }

    func finishDemoTurn(_ action: String, pieces: [String: [String: String]]) {
        guard let turn = demoTurn else { return }
        var lines: [String] = []
        if action == "local" {
            lines = ["The whole turn stays on your Mac: your local model answers it, and nothing of it goes to the cloud.",
                     "This conversation\u{2019}s later turns stay local too."]
        } else {
            lines = turn.pieces.map { p in
                let a = pieces[p.id]?["action"] ?? p.default
                let what: String = switch a {
                case "original": "goes as it was, flagged part and all."
                case "remove": p.isTyped ? "nothing of it goes; keepmum\u{2019}s note says part of your message was taken out."
                    : "nothing of it goes; keepmum\u{2019}s notice takes its place."
                default: "its \(a) goes in its place" + (pieces[p.id]?["text"].map { "; it begins \u{201C}\($0.prefix(60))\($0.count > 60 ? "\u{2026}" : "")\u{201D}." } ?? ".")
                }
                return "\(p.title): " + what
            } + ["Then the turn goes to the cloud. This conversation\u{2019}s later turns resend these pieces as you answered them; typed or returned again, they are asked about again."]
        }
        demoTurn = nil
        demoOutcome = DemoOutcome(window: true, title: action == "local" ? "Kept on this Mac" : "Sent to the cloud", lines: lines)
    }

    /// "Ask the local model" in the panel's demo: a set change, after a moment.
    func demoAssist(text: String, request: String) async -> Result<(text: String, model: String), EngineError> {
        try? await Task.sleep(for: .seconds(1.5))
        let kept = text.components(separatedBy: "\n## Usage").first ?? text
        return .success((kept == text ? text + "\n" : kept, "The demo"))
    }

    /// "Write Again" and "Ask the local model" in the turn window's demo.
    func demoDraft(piece: String, kind: String, request: String?, text: String?) async -> Result<PendingTurn.Piece, EngineError> {
        try? await Task.sleep(for: .seconds(1.5))
        guard var turn = demoTurn, let i = turn.pieces.firstIndex(where: { $0.id == piece }) else {
            return .failure(EngineError("The demo was closed"))
        }
        var p = turn.pieces[i]
        let key = (p.isTyped ? "typed" : p.source ?? "") + "." + kind
        if let request, let text {
            p.draft = request.lowercased().contains("short") || request.lowercased().contains("only")
                ? (text.components(separatedBy: ". ").first.map { $0.hasSuffix(".") ? $0 : $0 + "." } ?? text) : text
        } else {
            p.draft = Self.demoTexts[key] ?? p.draft
        }
        p.kind = kind
        p.failed = nil
        p.model = "the demo"
        turn.pieces[i] = p
        demoTurn = turn
        return .success(p)
    }

    static let demoTitles = ["send": "Sent as written", "edit": "Sent your edit", "original": "Sent the original",
                             "withhold": "Withheld", "local": "Kept on this Mac"]

    static let demoTexts: [String: String] = [
        "typed.rewrite": "[removed by keepmum] Then review the retry logic in sync.py and say why uploads time out.",
        "WebFetch.summary": "The fastgrep README: install with `make install` or `brew install fastgrep`; run `fastgrep PATTERN [PATH...]`. The page also held text addressed to AI agents, left out.",
        "WebFetch.rewrite": PendingReview.sample.proposed,
        "exec.summary": "notes.txt holds a short TODO list. It also had a line addressed to AI assistants, left out.",
        "exec.rewrite": "notes.txt\nTODO: [removed by keepmum]",
    ]
}

extension PendingReview {
    static func demo() -> PendingReview {
        var r = sample
        r.id = "demo-panel"
        r.model = "your local model"
        r.created = Date().timeIntervalSince1970
        r.deadline = r.created + 90
        r.hardDeadline = r.deadline + 120
        return r
    }
}

extension PendingTurn {
    static func demo() -> PendingTurn {
        var t = sample
        t.id = "demo-turn"
        t.created = Date().timeIntervalSince1970
        t.deadline = t.created + 120
        t.hardDeadline = t.deadline + 120
        for i in t.pieces.indices { t.pieces[i].model = "your local model" }
        return t
    }
}

/// Says a review is a demo, at its top.
struct DemoBanner: View {
    var body: some View {
        HStack(alignment: .top, spacing: Space.s) {
            Image(systemName: "sparkles").foregroundStyle(Theme.masked)
            Text("A demo, on made-up text. Try every answer: nothing is sent, and the local model\u{2019}s parts are played for you. The clock is for show.")
                .font(.km(.caption)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, Space.m).padding(.vertical, Space.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.soft(Theme.masked)))
    }
}

/// A finished demo: what a real review would have done with your answers, and another go.
struct DemoOutcomeView: View {
    var center: ReviewCenter
    var outcome: DemoOutcome

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(spacing: Space.s) {
                Image(systemName: "checkmark.circle.fill").font(.km(.title)).foregroundStyle(Theme.restored)
                Text(outcome.title).font(.km(.title)).foregroundStyle(Theme.text)
            }
            Text("In a real review, this is what would happen now:").font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            VStack(alignment: .leading, spacing: Space.s) {
                ForEach(outcome.lines, id: \.self) { line in
                    HStack(alignment: .firstTextBaseline, spacing: Space.s) {
                        Text("\u{2022}").foregroundStyle(Theme.textSecondary)
                        Text(line).font(.km(.callout)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            Text("Nothing was sent: this was a demo.").font(.km(.caption)).foregroundStyle(Theme.textTertiary)
            Spacer(minLength: Space.m)
            HStack {
                PillButton(title: "Close") {
                    center.demoOutcome = nil
                    if outcome.window { TurnWindow.hide() } else { ReviewPanel.hide() }
                }
                Spacer()
                PillButton(title: "Try Again", icon: "arrow.clockwise", prominent: true) {
                    if outcome.window { center.showDemoTurn() } else { center.showDemoPanel() }
                }
            }
        }
        .padding(Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
