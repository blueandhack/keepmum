import AppKit
import SwiftUI

// The Protection map: a request's round trip drawn once, with each setting on the part of the flow
// it acts on. Requests out run along the top (your agents, the two checks before a request leaves,
// masking, the line where it leaves this Mac, the providers) and replies back along the bottom (the
// commands check, restore, your agents). A check's hits branch off by its mode: nowhere (Off), a
// dashed line to Logs while the request still leaves (Warn), a red line that stops on this Mac's
// side (Block), or a green line to your local model, and from there back to the agent or, with a
// hand-back, into masking (Local). The bands are the Overview's last hour; the cards are the
// settings themselves, and the one selected shows its full settings under the map.
//
// The columns spread across the width the page gives the diagram (`MapLayout.width`, never under
// `minWidth`: a narrower window shows it scaled down); the height is fixed. The bands' split between
// agents and between providers comes from the traffic.

/// A part of the map with settings of its own.
enum MapPart: String, CaseIterable {
    case agents, injection, harm, masking, local, commands

    var title: String {
        switch self {
        case .agents: "Your coding agents"
        case .injection: "Prompt injection"
        case .harm: "Harmful requests"
        case .masking: "Masking"
        case .local: "Your local model"
        case .commands: "Risky commands"
        }
    }
}

/// A made-up request sent along the map, routed by the settings as they are. Nothing is sent.
enum MapSample: String, CaseIterable, Identifiable {
    case injection, harm, secret, command
    var id: String { rawValue }
    var title: String {
        switch self {
        case .injection: "Planted instruction"
        case .harm: "Harmful request"
        case .secret: "API key in a prompt"
        case .command: "curl | sh in a reply"
        }
    }
}

/// The settings the map draws, read from the engine and the proxy.
struct MapSettings {
    var injection = "off"
    var harm = "off"
    var commands = "off"
    /// The injection check is on and its model is missing (an earlier keepmum's is here): it checks nothing.
    var paused = false
    var outdated = false
    var observe = false
    var handback = "stay"
    var injectionLevel: String?
    /// The share of real tool results the injection check flags at its level.
    var injectionRate: Double?
    var harmLevel: String?
    var harmCaught: Double?
    var harmSafe: Double?
    var harmModel = "small"
    var localServers = 0

    init() {}

    @MainActor init(engine: Engine, store: StatsStore) {
        observe = store.snapshot.observe == true
        guard let g = engine.status?.guard else { return }
        let inj = g.injection
        injection = inj.mode
        paused = inj.mode != "off" && !inj.installed
        outdated = inj.outdated == true
        if let t = inj.threshold, let levels = inj.levels,
           let (name, level) = levels.first(where: { abs($0.value.threshold - t) < 0.0005 }) {
            injectionLevel = name
            injectionRate = level.sessions ?? level.ordinary
        }
        harm = g.harm.mode
        harmModel = g.harm.model
        if let name = g.harm.sensitivity, let level = g.harm.levels?[name] {
            harmLevel = name
            harmCaught = level.caught
            harmSafe = level.safe
        }
        commands = g.commands?.mode ?? "off"
        handback = engine.localHandback?.handback ?? g.local?.handback ?? "stay"
        localServers = engine.localServers?.servers.count ?? g.local?.servers.count ?? 0
    }

    /// The injection check's mode as it acts: paused checks nothing.
    var injectionActs: String { paused ? "off" : injection }
    var localUsed: Bool { injectionActs == "local" || harm == "local" }
}

/// Where everything sits, and the bands' split from the traffic.
struct MapLayout {
    /// The narrowest the columns go; the page scales the diagram down below it.
    static let minWidth: CGFloat = 860
    static let height: CGFloat = 636

    struct Bar {
        var id: String
        var name: String
        var requests: Int
        var color: Color
        var rect: CGRect
        /// Where its band meets the column it joins: a span of y.
        var join: ClosedRange<CGFloat>
        /// The share of its band drawn in a signal color: masked (providers out) or put back (agents back).
        var marked: Double = 0
        var marked2 = 0
    }

    var agentsOut: [Bar] = []
    var providersOut: [Bar] = []
    var providersBack: [Bar] = []
    var agentsBack: [Bar] = []
    var empty = true
    var masked = 0
    var restored = 0
    /// Hits in the window, by result and check: flagged (warned), refused, local.
    var hits: [String: [String: Int]] = [:]
    /// The diagram's width: the columns between the agents and the providers spread across it.
    var width: CGFloat = MapLayout.minWidth

    // The parts that stay put: the agents' column and the lanes' heights.
    static let agentX: CGFloat = 112, bundleX: CGFloat = 196, bar: CGFloat = 6
    static let thickness: CGFloat = 84
    static let bundle: ClosedRange<CGFloat> = 112...196
    static let toolStrand: ClosedRange<CGFloat> = 68...132
    static let typedStrand: ClosedRange<CGFloat> = 194...214
    static let maskIn: ClosedRange<CGFloat> = 104...188
    static let backIn: ClosedRange<CGFloat> = 436...520

    static let logY: CGFloat = 276, refuseY: CGFloat = 300
    static let cmdLogY: CGFloat = 584, cmdReplaceY: CGFloat = 608

    // The parts that move with the width: what a wider diagram gains goes to the gaps between them.
    private var extra: CGFloat { max(0, width - Self.minWidth) }
    var providerX: CGFloat { width - 144 }
    /// The edge of this Mac.
    var edge: CGFloat { providerX - 76 }
    var terminalX: CGFloat { edge - 80 }
    var gateX: CGFloat { 236 + extra * 0.22 }
    var gateRight: CGFloat { gateX + 184 }
    var maskX: CGFloat { gateRight + 22 + extra * 0.18 }
    var maskRight: CGFloat { maskX + 122 }
    /// Where a hit's line runs down, between the injection card and masking.
    var branchX: CGFloat { (gateRight + maskX) / 2 }
    var harmX: CGFloat { gateX + 92 }
    var injectionCard: CGRect { CGRect(x: gateX, y: 58, width: 184, height: 84) }
    var harmCard: CGRect { CGRect(x: gateX, y: 162, width: 184, height: 84) }
    var maskCard: CGRect { CGRect(x: maskX, y: 92, width: 122, height: 108) }
    var localCard: CGRect { CGRect(x: gateX, y: 310, width: 184, height: 74) }
    var restoreCard: CGRect { CGRect(x: gateX + 46, y: 428, width: 96, height: 100) }
    var commandsCard: CGRect { CGRect(x: edge - 200, y: 428, width: 180, height: 100) }
    var commandsX: CGFloat { commandsCard.minX + 90 }

    init(flows: Flows, width: CGFloat = MapLayout.minWidth) {
        self.width = max(width, Self.minWidth)
        hits = flows.checks ?? [:]
        let unsent: Set<String> = ["blocked", "refused", "failed", "local"]
        var clients = flows.clients.filter { $0.sent > 0 }.sorted { $0.sent > $1.sent }
        if clients.count > 4 {
            let rest = clients[3...].reduce(0) { $0 + $1.sent }
            clients = Array(clients[..<3]) + [.init(id: "other", name: "Others", sent: rest)]
        }
        var sent: [String: Int] = [:], maskedBy: [String: Int] = [:], names: [String: String] = [:]
        for o in flows.out where !unsent.contains(o.kind) {
            guard let p = o.provider else { continue }
            sent[p, default: 0] += o.n
            if o.kind == "masked" || o.kind == "observed" { maskedBy[p, default: 0] += o.n }
        }
        for p in flows.providers { names[p.id] = p.name }
        masked = maskedBy.values.reduce(0, +)
        empty = clients.isEmpty
        let providers = sent.sorted { $0.value > $1.value }.prefix(3).map { (id: $0.key, n: $0.value) }

        if empty {
            agentsOut = [Bar(id: "none", name: "No requests yet", requests: 0, color: Theme.neutral,
                             rect: CGRect(x: Self.agentX, y: 70, width: Self.bar, height: Self.thickness), join: Self.bundle)]
            providersOut = [Bar(id: "none", name: "Your provider", requests: 0, color: Theme.neutral,
                                rect: CGRect(x: providerX, y: 72, width: Self.bar, height: Self.thickness), join: Self.maskIn)]
            providersBack = [Bar(id: "none", name: "Your provider", requests: 0, color: Theme.neutral,
                                 rect: CGRect(x: providerX, y: 420, width: Self.bar, height: Self.thickness), join: Self.backIn)]
            agentsBack = [Bar(id: "none", name: "", requests: 0, color: Theme.neutral,
                              rect: CGRect(x: Self.agentX, y: 430, width: Self.bar, height: Self.thickness), join: Self.backIn)]
            return
        }

        let total = Double(clients.reduce(0) { $0 + $1.sent })
        let heights = Self.split(clients.map { Double($0.sent) / total })
        var y: CGFloat = 70, join = Self.bundle.lowerBound
        for (c, h) in zip(clients, heights) {
            agentsOut.append(Bar(id: c.id, name: c.name, requests: c.sent, color: Theme.client(c.id),
                                 rect: CGRect(x: Self.agentX, y: y, width: Self.bar, height: h), join: join...(join + h)))
            y += max(h, 10) + 14
            join += h
        }

        let ptotal = Double(max(1, providers.reduce(0) { $0 + $1.n }))
        let pheights = Self.split(providers.map { Double($0.n) / ptotal })
        var py: CGFloat = 72, pj = Self.maskIn.lowerBound, by: CGFloat = 420, bj = Self.backIn.lowerBound
        for (p, h) in zip(providers, pheights) {
            let color = p.id == "anthropic" ? Theme.client("claude-code") : p.id == "openai" ? Theme.client("codex")
                : p.id == "google" ? Theme.client("gemini") : Theme.neutral
            let name = names[p.id] ?? p.id.capitalized
            providersOut.append(Bar(id: p.id, name: name, requests: p.n, color: color,
                                    rect: CGRect(x: providerX, y: py, width: Self.bar, height: h), join: pj...(pj + h),
                                    marked: Double(maskedBy[p.id] ?? 0) / Double(max(1, p.n)), marked2: maskedBy[p.id] ?? 0))
            let replies = flows.providers.first { $0.id == p.id }?.replies ?? p.n
            providersBack.append(Bar(id: p.id, name: name, requests: replies, color: color,
                                     rect: CGRect(x: providerX, y: by, width: Self.bar, height: h), join: bj...(bj + h)))
            py += max(h, 10) + 16; pj += h
            by += max(h, 10) + 16; bj += h
        }

        var ay: CGFloat = 430, aj = Self.backIn.lowerBound
        let named = Set(clients.map(\.id))
        for (c, h) in zip(clients, heights) {
            let back = flows.back.filter { $0.client == c.id || (c.id == "other" && !named.contains($0.client)) }
            let n = back.reduce(0) { $0 + $1.n }, put = back.filter(\.restored).reduce(0) { $0 + $1.n }
            let values = back.filter(\.restored).reduce(0) { $0 + $1.values }
            restored += values
            agentsBack.append(Bar(id: c.id, name: c.name, requests: n, color: Theme.client(c.id),
                                  rect: CGRect(x: Self.agentX, y: ay, width: Self.bar, height: h), join: aj...(aj + h),
                                  marked: Double(put) / Double(max(1, n)), marked2: values))
            ay += max(h, 10) + 14
            aj += h
        }
    }

    /// Shares of the band's thickness, each at least 4 points.
    static func split(_ shares: [Double]) -> [CGFloat] {
        guard !shares.isEmpty else { return [] }
        let floor: CGFloat = 4
        let spare = max(0, thickness - floor * CGFloat(shares.count))
        return shares.map { floor + spare * CGFloat($0) }
    }

    func hitCount(_ result: String, _ check: String) -> Int { hits[result]?[check] ?? 0 }

    // The lines a hit takes.
    /// Out of the injection card's right side and down the gap beside masking.
    var injectionOut: Route { Route(gateRight, 128).curve(branchX, 180, branchX, 128, branchX, 150) }
    func injection(to y: CGFloat) -> Route { injectionOut.downRight(y, to: terminalX - 4) }
    var injectionLocal: Route {
        var out = injectionOut.line(branchX, 338)
        out.path.addQuadCurve(to: CGPoint(x: branchX - 8, y: 346), control: CGPoint(x: branchX, y: 346))
        out.path.addLine(to: CGPoint(x: gateRight, y: 346))
        return out
    }
    func harm(to y: CGFloat) -> Route { Route(harmX, 246).downRight(y, to: terminalX - 4) }
    var harmLocal: Route { Route(harmX, 246).line(harmX, 310) }
    var handback: Route { Route(gateRight, 364).curve(maskX + 60, 202, gateRight + 50, 364, maskX + 60, 300) }
    func answer(to y: CGFloat) -> Route { Route(gateX, 346).curve(118, y, gateX - 56, 346, gateX - 66, y) }
    func commands(to y: CGFloat) -> Route { Route(commandsX, 528).downRight(y, to: terminalX - 4) }

    /// A ribbon between two spans, its edges curving like the Overview's bands.
    static func ribbon(_ x0: CGFloat, _ a: ClosedRange<CGFloat>, _ x1: CGFloat, _ b: ClosedRange<CGFloat>) -> Path {
        var p = Path()
        let m = (x0 + x1) / 2
        p.move(to: CGPoint(x: x0, y: a.lowerBound))
        p.addCurve(to: CGPoint(x: x1, y: b.lowerBound), control1: CGPoint(x: m, y: a.lowerBound), control2: CGPoint(x: m, y: b.lowerBound))
        p.addLine(to: CGPoint(x: x1, y: b.upperBound))
        p.addCurve(to: CGPoint(x: x0, y: a.upperBound), control1: CGPoint(x: m, y: b.upperBound), control2: CGPoint(x: m, y: a.upperBound))
        p.closeSubpath()
        return p
    }

    /// The lower `share` of a span.
    static func lower(_ r: ClosedRange<CGFloat>, _ share: Double) -> ClosedRange<CGFloat> {
        (r.upperBound - (r.upperBound - r.lowerBound) * share)...r.upperBound
    }
    static func upper(_ r: ClosedRange<CGFloat>, _ share: Double) -> ClosedRange<CGFloat> {
        r.lowerBound...(r.lowerBound + (r.upperBound - r.lowerBound) * share)
    }
}

/// Paths drawn with a few steps: a flowing curve, a line, a rounded corner.
struct Route {
    var path = Path()
    init(_ x: CGFloat, _ y: CGFloat) { path.move(to: CGPoint(x: x, y: y)) }

    /// A curve whose ends leave and arrive level, like a band.
    func flow(_ x: CGFloat, _ y: CGFloat) -> Route {
        var r = self
        let c = path.currentPoint ?? .zero, m = (c.x + x) / 2
        r.path.addCurve(to: CGPoint(x: x, y: y), control1: CGPoint(x: m, y: c.y), control2: CGPoint(x: m, y: y))
        return r
    }
    func line(_ x: CGFloat, _ y: CGFloat) -> Route {
        var r = self
        r.path.addLine(to: CGPoint(x: x, y: y))
        return r
    }
    /// Down, then turning right into `y`, by a corner of radius 10.
    func downRight(_ y: CGFloat, to x: CGFloat) -> Route {
        var r = self
        let c = path.currentPoint ?? .zero
        r.path.addLine(to: CGPoint(x: c.x, y: y - 8))
        r.path.addQuadCurve(to: CGPoint(x: c.x + 8, y: y), control: CGPoint(x: c.x, y: y))
        r.path.addLine(to: CGPoint(x: x, y: y))
        return r
    }
    func curve(_ x: CGFloat, _ y: CGFloat, _ c1x: CGFloat, _ c1y: CGFloat, _ c2x: CGFloat, _ c2y: CGFloat) -> Route {
        var r = self
        r.path.addCurve(to: CGPoint(x: x, y: y), control1: CGPoint(x: c1x, y: c1y), control2: CGPoint(x: c2x, y: c2y))
        return r
    }
}

/// A dot travelling a route: when it sets off, how long it takes, and its color along the way.
struct MapDot {
    var path: Path
    var delay: Double = 0
    var duration: Double = 3.4
    /// Colors from a fraction of the way on.
    var colors: [(Double, Color)]
    var size: CGFloat = 11

    func color(at u: Double) -> Color { colors.last { u >= $0.0 }?.1 ?? colors.first?.1 ?? Theme.textSecondary }
}

/// What a sample does with the settings as they are: its dots, its route, and what happened.
struct SampleRun {
    var route: Path
    var dots: [MapDot]
    var title: String
    var story: String
    var length: Double { dots.map { $0.delay + $0.duration }.max() ?? 0 }

    // swiftlint:disable:next cyclomatic_complexity function_body_length
    init(_ sample: MapSample, _ s: MapSettings, _ layout: MapLayout) {
        let agent = layout.agentsOut.first, provider = layout.providersOut.first
        let ay = agent.map { $0.rect.midY } ?? 112, py = provider.map { $0.rect.midY } ?? 114
        let backY = layout.agentsBack.first.map { $0.rect.midY } ?? 459
        let providerBackY = layout.providersBack.first.map { $0.rect.midY } ?? 449
        let agentName = agent?.name ?? "Your agent", providerName = provider?.name ?? "the provider"
        let grey = Theme.textSecondary
        let l = layout
        let toolIn = Route(118, ay).flow(199, 141).flow(l.gateX, 100).line(l.gateX + 144, 100)
        let toolOn = toolIn.line(l.gateRight, 100).flow(l.maskX, 133).line(l.maskRight, 133).flow(l.providerX, py)
        let typedIn = Route(118, ay + 6).flow(199, 186).flow(l.gateX, 204).line(l.harmX, 204)
        let typedOn = typedIn.line(l.gateRight, 204).flow(l.maskX, 178).line(l.maskRight, 178).flow(l.providerX, py)
        let answer = l.answer(to: backY)
        func then(_ a: Route, _ b: Route) -> Path { var p = a.path; p.addPath(b.path); return p }

        switch sample {
        case .injection:
            title = "\(agentName) fetched a page with an instruction planted for the agent"
            let toCard = toolIn.line(l.gateRight, 128)
            switch s.injectionActs {
            case "warn":
                route = toolOn.path
                dots = [MapDot(path: toolOn.path, colors: [(0, grey)]),
                        MapDot(path: l.injection(to: MapLayout.logY).path, delay: 2.4, duration: 1.4, colors: [(0, Theme.warning)])]
                story = "Warn: the request went to \(providerName) at once. The injection check read it afterwards, flagged the page, and logged the hit in Logs."
            case "block":
                route = then(toCard, l.injection(to: MapLayout.refuseY))
                dots = [MapDot(path: route, colors: [(0, grey), (0.48, Theme.blocked)])]
                story = "Block: the injection check read the page before the request left, flagged it and refused the request. Nothing reached \(providerName)."
            case "local":
                let local = then(toCard, l.injectionLocal)
                if s.handback == "answer" {
                    var p = local
                    p.addLine(to: CGPoint(x: l.gateX, y: 346))
                    p.addPath(answer.path)
                    let next = l.handback.line(l.maskX + 60, 133).line(l.maskRight, 133).flow(l.providerX, py)
                    var whole = p
                    whole.addPath(next.path)
                    route = whole
                    dots = [MapDot(path: p, duration: 4, colors: [(0, grey), (0.5, Theme.restored)]),
                            MapDot(path: next.path, delay: 4.2, duration: 2.4, colors: [(0, Theme.restored)])]
                    story = "Local answer, then the cloud: the check flagged the page before it left, and your local model answered that turn on this Mac. The next turn goes to \(providerName) with the page withheld and the local answer in its place."
                } else if s.handback == "stay" {
                    var p = local
                    p.addLine(to: CGPoint(x: l.gateX, y: 346))
                    p.addPath(answer.path)
                    route = p
                    dots = [MapDot(path: p, duration: 4, colors: [(0, grey), (0.5, Theme.restored)])]
                    story = "Local: the check flagged the page before it left. Your local model answered that turn on this Mac, and the conversation stays local."
                } else {
                    var p = local
                    p.addLine(to: CGPoint(x: l.gateRight, y: 364))
                    p.addPath(l.handback.line(l.maskX + 60, 133).line(l.maskRight, 133).flow(l.providerX, py).path)
                    route = p
                    dots = [MapDot(path: p, duration: 4.4, colors: [(0, grey), (0.42, Theme.restored)])]
                    let what = ["summary": "summarised the page, and the summary went to \(providerName) in its place",
                                "rewrite": "took out what the page said to the agent, and the rest went to \(providerName)",
                                "withhold": "was not asked: a notice took the page's place, and the turn went to \(providerName)"][s.handback] ?? ""
                    story = "Local, then the cloud: the check flagged the page before it left. Your local model \(what). The page itself never left this Mac."
                }
            default:
                route = toolOn.path
                dots = [MapDot(path: toolOn.path, colors: [(0, grey)])]
                story = s.paused ? "The injection check is paused until its model is updated, so the page went to \(providerName) unchecked."
                    : "The injection check is off, so the page went to \(providerName) unchecked. Masking still ran."
            }
        case .harm:
            title = "You asked for something a policy may not allow"
            switch s.harm {
            case "warn":
                route = typedOn.path
                dots = [MapDot(path: typedOn.path, colors: [(0, grey)]),
                        MapDot(path: l.harm(to: MapLayout.logY).path, delay: 2.4, duration: 1.3, colors: [(0, Theme.warning)])]
                story = "Warn: your request went to \(providerName) at once. The harm check read it afterwards and logged the hit, with its categories."
            case "block":
                route = then(typedIn, l.harm(to: MapLayout.refuseY))
                dots = [MapDot(path: route, colors: [(0, grey), (0.45, Theme.blocked)])]
                story = "Block: the harm check read what you typed before it left and refused it, with the model's categories. Nothing reached \(providerName)."
            case "local":
                var p = then(typedIn, l.harmLocal.line(l.harmX, 346).line(l.gateX, 346))
                p.addPath(answer.path)
                route = p
                dots = [MapDot(path: p, duration: 4, colors: [(0, grey), (0.45, Theme.restored)])]
                story = "Local: your local model answered on this Mac. A harm hit always stays local: handing it back would send it to the provider the check kept it from."
            default:
                route = typedOn.path
                dots = [MapDot(path: typedOn.path, colors: [(0, grey)])]
                story = "The harm check is off, so your request went to \(providerName) as you typed it."
            }
        case .secret:
            title = "You pasted an API key into your prompt"
            route = typedOn.path
            let back = Route(l.providerX, providerBackY).flow(l.commandsCard.maxX, 444).line(l.restoreCard.minX, 444).flow(118, backY - 8)
            let found = s.observe ? Theme.warning : Theme.masked
            dots = [MapDot(path: typedOn.path, colors: [(0, grey), (0.6, found)]),
                    MapDot(path: back.path, delay: 3.5, duration: 2.8, colors: [(0, found), (s.observe ? 2 : 0.62, Theme.restored)])]
            story = s.observe ? "Observe only: masking found the key and logged it, but the request went to \(providerName) with the real key in it."
                : "Masking swapped the key for a placeholder before the request left. The reply used the placeholder, and Restore put the real key back before \(agentName) saw it."
        case .command:
            title = "A reply asked the agent to run curl \u{2026} | sh"
            let arrive = Route(l.providerX, providerBackY).flow(l.commandsCard.maxX, 465).line(l.commandsX, 465)
            switch s.commands {
            case "block":
                route = then(arrive, l.commands(to: MapLayout.cmdReplaceY))
                dots = [MapDot(path: route, duration: 3, colors: [(0, grey), (0.4, Theme.blocked)]),
                        MapDot(path: Route(l.commandsCard.minX, 488).line(l.restoreCard.minX, 488).flow(118, backY + 10).path, delay: 1.5, duration: 2, colors: [(0, Theme.blocked)], size: 8)]
                story = "Block: the commands check replaced it with a command that tells the agent why it was stopped, and fails. Nothing ran."
            default:
                let on = arrive.line(l.restoreCard.minX, 465).flow(118, backY)
                route = on.path
                dots = [MapDot(path: on.path, duration: 3, colors: [(0, grey)])]
                if s.commands == "warn" {
                    dots.append(MapDot(path: l.commands(to: MapLayout.cmdLogY).path, delay: 1.5, duration: 1.3, colors: [(0, Theme.warning)]))
                }
                story = s.commands == "warn" ? "Warn: the commands check logged it as it arrived, then handed it to the agent, which may still ask you before it runs."
                    : "The commands check is off, so the agent got the command as written."
            }
        }
    }
}

// MARK: - The page

struct ProtectionMapPage: View {
    var store: StatsStore
    var engine: Engine
    @State private var selected: MapPart
    @State private var sample: MapSample?
    @State private var started = Date()
    @Environment(\.isSnapshot) private var isSnapshot

    /// `selected` and `sample`: what it opens on (the snapshot renderer shows each).
    init(store: StatsStore, engine: Engine, selected: MapPart = .injection, sample: MapSample? = nil) {
        self.store = store
        self.engine = engine
        _selected = State(initialValue: selected)
        _sample = State(initialValue: sample)
    }

    /// From this width the selected part's settings sit beside the map, in a column this wide.
    private static let sideBySide: CGFloat = 1400
    private static let sideWidth: CGFloat = 540
    /// The widest the diagram spreads: past this, rows get hard to follow across.
    private static let widest: CGFloat = 1360

    var body: some View {
        let settings = MapSettings(engine: engine, store: store)
        VStack(alignment: .leading, spacing: Space.l) {
            PageHeader(title: "Protection map", subtitle: "Every check where it acts on a request, and where a hit goes. Click a part to change it.")
            GeometryReader { geo in
                let side = geo.size.width >= Self.sideBySide
                let mapWidth = side ? geo.size.width - Self.sideWidth - Space.xl : geo.size.width
                // The diagram's own width: the card's, less its padding, up to the widest it spreads.
                let inner = min(mapWidth - 2 * Space.m, Self.widest)
                let layout = MapLayout(flows: store.flows, width: inner)
                let run = sample.map { SampleRun($0, settings, layout) }
                let page = content(settings, layout, run, side: side, mapWidth: mapWidth, inner: inner)
                if isSnapshot {
                    page
                } else {
                    ScrollView { page.padding(.bottom, Space.xl) }.scrollIndicators(.automatic)
                }
            }
        }
        .task { await engine.loadLocalHandback() }
    }

    @ViewBuilder private func content(_ settings: MapSettings, _ layout: MapLayout, _ run: SampleRun?,
                                      side: Bool, mapWidth: CGFloat, inner: CGFloat) -> some View {
        let map = VStack(alignment: .leading, spacing: Space.l) {
            diagram(settings, layout, run, inner: inner)
            narration(run)
            legend
        }
        let panel = VStack(alignment: .leading, spacing: Space.xl) { selectedSettings(settings) }.id(selected)
        if side {
            HStack(alignment: .top, spacing: Space.xl) {
                map.frame(width: mapWidth)
                panel.frame(width: Self.sideWidth, alignment: .topLeading)
            }
        } else {
            VStack(alignment: .leading, spacing: Space.l) {
                map.frame(width: mapWidth)
                panel.frame(maxWidth: 780, alignment: .topLeading)
            }
        }
    }

    /// The diagram on its card: laid out for the width it has, or scaled down when that is under the narrowest.
    private func diagram(_ settings: MapSettings, _ layout: MapLayout, _ run: SampleRun?, inner: CGFloat) -> some View {
        let scale = min(1, max(0.5, inner / MapLayout.minWidth))
        return MapDiagram(engine: engine, settings: settings, layout: layout, selected: $selected, run: run, started: started)
            .scaleEffect(scale, anchor: .topLeading)
            .frame(width: layout.width * scale, height: MapLayout.height * scale, alignment: .topLeading)
            .padding(Space.m)
            .frame(maxWidth: .infinity)
            .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
    }

    private var samples: some View {
        ChipFlow(spacing: Space.xs) {
            Text("Send a sample:").font(.km(.caption)).foregroundStyle(Theme.textSecondary).padding(.vertical, Space.xs + 1)
            ForEach(MapSample.allCases) { s in
                let on = sample == s
                Button {
                    sample = s
                    started = Date()
                } label: {
                    Label(s.title, systemImage: "play.fill")
                        .font(.km(.caption).weight(.medium)).lineLimit(1).fixedSize()
                        .foregroundStyle(on ? Theme.onAccent : Theme.text)
                        .padding(.horizontal, Space.s + Space.xxs).padding(.vertical, Space.xs + 1)
                        .background(Capsule().fill(on ? Theme.accentFill : Theme.fill))
                }
                .buttonStyle(.plain)
                .help("Sends a made-up request along the map, routed by your settings. Nothing leaves this Mac.")
            }
        }
    }

    private func narration(_ run: SampleRun?) -> some View {
        VStack(alignment: .leading, spacing: Space.m) {
            samples
            Hairline()
            story(run)
        }
        .padding(Space.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
    }

    private func story(_ run: SampleRun?) -> some View {
        HStack(alignment: .top, spacing: Space.m) {
            Image(systemName: "play.fill").font(.km(.caption)).foregroundStyle(Theme.accent)
                .frame(width: 22, height: 22).background(Circle().fill(Theme.accentSoft))
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(run?.title ?? "Send a sample to watch a request take its route")
                    .font(.km(.headline)).foregroundStyle(Theme.text)
                Text(run?.story ?? "Each follows the settings on the map, and nothing leaves this Mac. Change a mode and send it again: the route changes with it.")
                    .font(.km(.callout)).foregroundStyle(Theme.textSecondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }

    private var legend: some View {
        ChipFlow(spacing: Space.l) {
            key(Theme.masked, "Masked before it left")
            key(Theme.restored, "Put back, or answered on this Mac")
            key(Theme.warning, "Sent, then logged", dashed: true)
            key(Theme.blocked, "Refused or replaced")
            Text("A hit's line is thicker the more its level flags").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
        }
        .padding(.horizontal, Space.xs)
    }

    private func key(_ color: Color, _ text: String, dashed: Bool = false) -> some View {
        HStack(spacing: Space.xs + 2) {
            Path { p in p.move(to: CGPoint(x: 0, y: 3)); p.addLine(to: CGPoint(x: 14, y: 3)) }
                .stroke(color, style: StrokeStyle(lineWidth: dashed ? 2 : 5, lineCap: dashed ? .butt : .round, dash: dashed ? [4, 3] : []))
                .frame(width: 14, height: 6)
            Text(text).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
        }
    }

    @ViewBuilder private func selectedSettings(_ settings: MapSettings) -> some View {
        switch selected {
        case .injection: ChecksSection(engine: engine, only: "injection")
        case .harm: ChecksSection(engine: engine, only: "harm")
        case .commands: ChecksSection(engine: engine, only: "commands")
        case .masking:
            MaskingSection(engine: engine, observing: store.snapshot.observe == true, modelError: store.snapshot.detection.modelError)
        case .local:
            if !settings.localUsed {
                Banner(tone: .info, icon: "cpu", title: "Local mode is off.",
                       message: "Your local model answers once Prompt injection or Harmful requests is set to Local.") { EmptyView() }
            }
            LocalServersSection(engine: engine)
            LocalHandbackSection(engine: engine)
        case .agents: SetupSection(engine: engine, part: .agents)
        }
    }
}

// MARK: - The diagram

struct MapDiagram: View {
    var engine: Engine
    var settings: MapSettings
    var layout: MapLayout
    @Binding var selected: MapPart
    var run: SampleRun?
    var started: Date
    @State private var hovered: MapPart?
    @Environment(\.isSnapshot) private var isSnapshot

    private var s: MapSettings { settings }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Canvas { context, _ in drawBands(&context) }
            hitLines
            moving
            labels
            cards
        }
        .frame(width: layout.width, height: MapLayout.height, alignment: .topLeading)
        .animation(Motion.quick, value: s.injection)
        .animation(Motion.quick, value: s.harm)
        .animation(Motion.quick, value: s.commands)
        .animation(Motion.quick, value: s.handback)
    }

    // MARK: Bands

    private func drawBands(_ ctx: inout GraphicsContext) {
        let band = GraphicsContext.Shading.color(Theme.neutral.opacity(0.28))
        // The edge of this Mac, and the line between the lanes.
        ctx.stroke(Path { p in p.move(to: CGPoint(x: layout.edge, y: 10)); p.addLine(to: CGPoint(x: layout.edge, y: 630)) },
                   with: .color(Theme.textTertiary), style: StrokeStyle(lineWidth: 1, dash: [3, 4]))
        ctx.stroke(Path { p in p.move(to: CGPoint(x: 0, y: 390)); p.addLine(to: CGPoint(x: layout.commandsCard.maxX, y: 390)) },
                   with: .color(Theme.separator), lineWidth: 1)

        // Requests out.
        for a in layout.agentsOut {
            ctx.fill(MapLayout.ribbon(MapLayout.agentX + MapLayout.bar, a.rect.minY...a.rect.maxY, MapLayout.bundleX, a.join),
                     with: .color(a.color.opacity(0.32)))
        }
        ctx.fill(MapLayout.ribbon(202, 112...176, layout.gateX, MapLayout.toolStrand), with: band)
        ctx.fill(MapLayout.ribbon(202, 176...196, layout.gateX, MapLayout.typedStrand), with: band)
        ctx.fill(MapLayout.ribbon(layout.gateRight, MapLayout.toolStrand, layout.maskX, 104...168), with: band)
        ctx.fill(MapLayout.ribbon(layout.gateRight, MapLayout.typedStrand, layout.maskX, 168...188), with: band)
        let maskColor = s.observe ? Theme.warning : Theme.masked
        for p in layout.providersOut {
            let target = p.rect.minY...p.rect.maxY
            ctx.fill(MapLayout.ribbon(layout.maskRight, MapLayout.upper(p.join, 1 - p.marked), layout.providerX, MapLayout.upper(target, 1 - p.marked)), with: band)
            if p.marked > 0 {
                ctx.fill(MapLayout.ribbon(layout.maskRight, MapLayout.lower(p.join, p.marked), layout.providerX, MapLayout.lower(target, p.marked)),
                         with: .color(maskColor.opacity(0.4)))
            }
        }

        // Replies back.
        for p in layout.providersBack {
            ctx.fill(MapLayout.ribbon(layout.providerX, p.rect.minY...p.rect.maxY, layout.commandsCard.maxX, p.join), with: band)
        }
        ctx.fill(MapLayout.ribbon(layout.commandsCard.minX, MapLayout.backIn, layout.restoreCard.maxX, MapLayout.backIn), with: band)
        for a in layout.agentsBack {
            let target = a.rect.minY...a.rect.maxY
            ctx.fill(MapLayout.ribbon(layout.restoreCard.minX, MapLayout.lower(a.join, 1 - a.marked), MapLayout.agentX + MapLayout.bar, MapLayout.lower(target, 1 - a.marked)),
                     with: .color(a.color.opacity(0.26)))
            if a.marked > 0 {
                ctx.fill(MapLayout.ribbon(layout.restoreCard.minX, MapLayout.upper(a.join, a.marked), MapLayout.agentX + MapLayout.bar, MapLayout.upper(target, a.marked)),
                         with: .color(Theme.restored.opacity(0.42)))
            }
        }

        // Bars.
        for b in layout.agentsOut + layout.agentsBack + layout.providersOut + layout.providersBack {
            ctx.fill(Path(roundedRect: b.rect, cornerRadius: 2), with: .color(b.color))
        }
        ctx.fill(Path(roundedRect: CGRect(x: MapLayout.bundleX, y: 112, width: MapLayout.bar, height: 84), cornerRadius: 2), with: .color(Theme.text))
    }

    // MARK: Hit lines

    /// A hit's line is thicker the more its level flags: High 2.6% of real tool results, Lowest 0.4%.
    private var injectionWidth: CGFloat { 1 + CGFloat(s.injectionRate ?? 0.015) * 90 }
    private var harmWidth: CGFloat { 1 + CGFloat(s.harmSafe ?? 0.047) * 30 }

    @ViewBuilder private var hitLines: some View {
        let inj = s.injectionActs
        line(layout.injection(to: MapLayout.logY), Theme.warning, injectionWidth, dashed: true, on: inj == "warn")
        line(layout.injection(to: MapLayout.refuseY), Theme.blocked, injectionWidth, on: inj == "block")
        line(layout.injectionLocal, Theme.restored, injectionWidth, on: inj == "local")
        line(layout.harm(to: MapLayout.logY), Theme.warning, harmWidth, dashed: true, on: s.harm == "warn")
        line(layout.harm(to: MapLayout.refuseY), Theme.blocked, harmWidth, on: s.harm == "block")
        line(layout.harmLocal, Theme.restored, harmWidth, on: s.harm == "local")
        // The hand-back is for the injection check's hits: drawn faint while that check is not Local.
        line(layout.handback, inj == "local" ? Theme.restored : Theme.textTertiary, 2, dotted: true, on: s.handback != "stay")
        line(layout.answer(to: layout.agentsBack.first.map { $0.rect.midY } ?? 459), Theme.restored, 2,
             on: answersLocally)
        line(layout.commands(to: MapLayout.cmdLogY), Theme.warning, 2, dashed: true, on: s.commands == "warn")
        line(layout.commands(to: MapLayout.cmdReplaceY), Theme.blocked, 2, on: s.commands == "block")
        terminal(MapLayout.logY, Theme.warning, on: inj == "warn" || s.harm == "warn")
        terminal(MapLayout.refuseY, Theme.blocked, on: inj == "block" || s.harm == "block")
        terminal(MapLayout.cmdLogY, Theme.warning, on: s.commands == "warn", hidden: true)
        terminal(MapLayout.cmdReplaceY, Theme.blocked, on: s.commands == "block", hidden: true)
    }

    /// Your local model answers the agent itself: a harm hit in Local mode, or an injection hit that
    /// stays local or is answered before the hand-back ("Local answer, then cloud"). A summary, a
    /// rewrite or a notice goes to the cloud, which answers instead.
    private var answersLocally: Bool {
        (s.injectionActs == "local" && ["stay", "answer"].contains(s.handback)) || s.harm == "local"
    }

    private var answerHelp: String {
        let injection = s.injectionActs == "local" && ["stay", "answer"].contains(s.handback)
        switch (injection, s.harm == "local") {
        case (true, true): return "Your local model answers flagged requests and flagged tool results itself, back to the agent."
        case (true, false): return "Your local model answers a turn with a flagged tool result itself, back to the agent."
        default: return "Your local model answers a flagged request itself, back to the agent: a harm hit always stays on this Mac."
        }
    }

    private func line(_ r: Route, _ color: Color, _ width: CGFloat, dashed: Bool = false, dotted: Bool = false, on: Bool) -> some View {
        r.path.stroke(color, style: StrokeStyle(lineWidth: width, lineCap: dotted ? .round : .butt, lineJoin: .round,
                                                dash: dashed ? [5, 4] : dotted ? [0.5, 5] : []))
            .opacity(on ? 1 : 0)
            .allowsHitTesting(false)
    }

    private func terminal(_ y: CGFloat, _ color: Color, on: Bool, hidden: Bool = false) -> some View {
        Circle().fill(color).frame(width: 8, height: 8)
            .offset(x: layout.terminalX - 4, y: y - 4)
            .opacity(on ? 1 : hidden ? 0 : 0.28)
            .allowsHitTesting(false)
    }

    // MARK: Moving dots

    /// Requests drifting along the bands, and a sample along its route.
    private var moving: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: isSnapshot)) { tl in
            Canvas { ctx, _ in
                let t = tl.date.timeIntervalSinceReferenceDate
                if !layout.empty {
                    for (i, r) in ambient.enumerated() {
                        let period = 5.0
                        let u = ((t + Double(i) * 1.37).truncatingRemainder(dividingBy: period)) / period
                        let fade = min(1, min(u, 1 - u) * 12)
                        dot(&ctx, r, u, Theme.textTertiary.opacity(0.9 * fade), 6)
                    }
                }
                guard let run else { return }
                let elapsed = isSnapshot ? run.length * 0.55 : tl.date.timeIntervalSince(started)
                ctx.stroke(run.route, with: .color(Theme.accent.opacity(0.85)),
                           style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [2, 6], dashPhase: CGFloat(-t * 26)))
                for d in run.dots where elapsed >= d.delay {
                    let u = min(1, (elapsed - d.delay) / d.duration)
                    let eased = FlowGeometry.ease(u)
                    dot(&ctx, d.path, eased, d.color(at: eased), d.size, ring: true)
                }
            }
        }
        .allowsHitTesting(false)
    }

    private var ambient: [Path] {
        let a = layout.agentsOut, p = layout.providersOut
        let ay = a.first.map { $0.rect.midY } ?? 112, py = p.first.map { $0.rect.midY } ?? 114
        let ay2 = a.dropFirst().first.map { $0.rect.midY } ?? ay, py2 = p.dropFirst().first.map { $0.rect.midY } ?? py
        let backY = layout.agentsBack.first.map { $0.rect.midY } ?? 459
        let pby = layout.providersBack.first.map { $0.rect.midY } ?? 449
        let l = layout
        return [
            Route(118, ay).flow(199, 141).flow(l.gateX, 100).line(l.gateRight, 100).flow(l.maskX, 133).line(l.maskRight, 133).flow(l.providerX, py).path,
            Route(118, ay2).flow(199, 186).flow(l.gateX, 204).line(l.gateRight, 204).flow(l.maskX, 178).line(l.maskRight, 178).flow(l.providerX, py2).path,
            Route(l.providerX, pby).flow(l.commandsCard.maxX, 465).line(l.restoreCard.minX, 465).flow(118, backY).path,
            Route(118, ay).flow(199, 130).flow(l.gateX, 116).line(l.gateRight, 116).flow(l.maskX, 150).line(l.maskRight, 150).flow(l.providerX, py).path,
            Route(l.providerX, pby).flow(l.commandsCard.maxX, 480).line(l.restoreCard.minX, 480).flow(118, backY + 4).path,
        ]
    }

    private func dot(_ ctx: inout GraphicsContext, _ path: Path, _ u: Double, _ color: Color, _ size: CGFloat, ring: Bool = false) {
        guard u > 0, let p = path.trimmedPath(from: 0, to: u).currentPoint else { return }
        let r = CGRect(x: p.x - size / 2, y: p.y - size / 2, width: size, height: size)
        if ring { ctx.fill(Path(ellipseIn: r.insetBy(dx: -2, dy: -2)), with: .color(Theme.canvas)) }
        ctx.fill(Path(ellipseIn: r), with: .color(color))
    }

    // MARK: Labels

    @ViewBuilder private var labels: some View {
        eyebrow("YOUR AGENTS \u{2192}", 0, 14)
        eyebrow("CHECKED BEFORE IT LEAVES", layout.gateX, 14)
        eyebrow("MASKING", layout.maskX, 14)
        eyebrow("THIS MAC", layout.edge - 80, 14, width: 72, align: .trailing)
        eyebrow("INTERNET", layout.edge + 8, 14)
        eyebrow("\u{2190} REPLIES BACK", 0, 400)
        eyebrow("RESTORE", layout.restoreCard.minX, 400)
        eyebrow("CHECKED ON ARRIVAL", layout.commandsCard.minX, 400)
        caption("tool results", 150, 86, width: 80, align: .center)
        caption("what you typed", 146, 214, width: 88, align: .center)

        ForEach(Array(layout.agentsOut.enumerated()), id: \.offset) { _, a in
            (Text(a.name).fontWeight(.semibold).foregroundStyle(Theme.text)
             + Text(a.requests > 0 ? "  \(a.requests.formatted())" : "").foregroundStyle(Theme.textSecondary))
                .font(.km(.callout)).lineLimit(1)
                .frame(width: 104, alignment: .trailing)
                .offset(x: 0, y: a.rect.midY - 8)
        }
        let agentsBottom = (layout.agentsOut.last?.rect.maxY ?? 154) + 12
        Button { selected = .agents } label: {
            RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                .strokeBorder(Theme.accent, lineWidth: selected == .agents ? 2 : 0)
                .contentShape(Rectangle())
                .frame(width: 124, height: agentsBottom - 58)
        }
        .buttonStyle(.plain)
        .offset(x: -6, y: 58)
        .help("Your coding agents: which go through keepmum")
        .accessibilityLabel("Your coding agents")

        ForEach(Array(layout.agentsBack.enumerated()), id: \.offset) { _, a in
            if a.marked2 > 0 {
                caption("\(a.marked2.formatted()) put back", 0, a.rect.midY - 8, width: 104, align: .trailing)
            }
        }
        ForEach(Array(layout.providersOut.enumerated()), id: \.offset) { _, p in
            twoLine(p.name, p.requests > 0 ? "\(p.requests.formatted()) sent" : "", layout.providerX + 16, p.rect.midY - 15)
        }
        ForEach(Array(layout.providersBack.enumerated()), id: \.offset) { _, p in
            twoLine(p.name, p.requests > 0 ? "\(p.requests.formatted()) replies" : "", layout.providerX + 16, p.rect.midY - 15)
        }

        let warned = layout.hitCount("flagged", "injection") + layout.hitCount("flagged", "harm")
        let refused = layout.hitCount("refused", "injection") + layout.hitCount("refused", "harm")
        let inj = s.injectionActs
        let logOn = inj == "warn" || s.harm == "warn", refuseOn = inj == "block" || s.harm == "block"
        terminalLabel("Logged" + (logOn && warned > 0 ? " \(warned)" : ""), Theme.warning, MapLayout.logY, on: logOn)
        terminalLabel("Refused" + (refuseOn && refused > 0 ? " \(refused)" : ""), Theme.blocked, MapLayout.refuseY, on: refuseOn)
        terminalLabel("Logged", Theme.warning, MapLayout.cmdLogY, on: s.commands == "warn", hidden: true)
        terminalLabel("Replaced", Theme.blocked, MapLayout.cmdReplaceY, on: s.commands == "block", hidden: true)
        let hb = ["answer": "local answer, then the cloud", "summary": "local summary to the cloud",
                  "rewrite": "local rewrite to the cloud", "withhold": "a notice to the cloud"][s.handback] ?? ""
        VStack(alignment: .leading, spacing: 0) {
            Text(hb).foregroundStyle(inj == "local" ? Theme.restored : Theme.textSecondary)
            if inj != "local" { Text("injection hits only").foregroundStyle(Theme.textTertiary) }
        }
        .font(.km(.caption)).frame(width: 120, alignment: .leading).offset(x: layout.maskX + 70, y: 222)
        .opacity(s.handback != "stay" ? 1 : 0)
        .help(inj == "local" ? "After a local turn of the injection check, this goes to the cloud in the flagged result's place"
              : "The hand-back follows a local turn of the injection check, which is not set to Local. A harm hit always stays on this Mac.")
        Text("local answer back to the agent").font(.km(.caption)).foregroundStyle(Theme.restored).multilineTextAlignment(.trailing)
            .frame(width: 100, alignment: .trailing).offset(x: layout.gateX - 110, y: 312)
            .opacity(answersLocally ? 1 : 0)
            .help(answerHelp)
    }

    private func eyebrow(_ text: String, _ x: CGFloat, _ y: CGFloat, width: CGFloat? = nil, align: Alignment = .leading) -> some View {
        Text(text).font(.km(.label)).tracking(0.8).foregroundStyle(Theme.textSecondary)
            .frame(width: width, alignment: align).offset(x: x, y: y)
    }

    private func caption(_ text: String, _ x: CGFloat, _ y: CGFloat, width: CGFloat, align: Alignment = .leading) -> some View {
        Text(text).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            .frame(width: width, alignment: align).offset(x: x, y: y)
    }

    private func twoLine(_ title: String, _ detail: String, _ x: CGFloat, _ y: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title).font(.km(.callout).weight(.semibold)).foregroundStyle(Theme.text)
            if !detail.isEmpty { Text(detail).font(.km(.caption)).foregroundStyle(Theme.textSecondary) }
        }
        .frame(width: 124, alignment: .leading).offset(x: x, y: y)
    }

    private func terminalLabel(_ text: String, _ color: Color, _ y: CGFloat, on: Bool, hidden: Bool = false) -> some View {
        Text(text).font(.km(.caption).weight(.semibold)).foregroundStyle(color)
            .offset(x: layout.terminalX + 8, y: y - 8)
            .opacity(on ? 1 : hidden ? 0 : 0.35)
    }

    // MARK: Cards

    @ViewBuilder private var cards: some View {
        gate(.injection, icon: "syringe", mode: s.injection, options: Self.four, rect: layout.injectionCard,
             caption: injectionCaption, paused: s.paused) { await engine.setCheck("injection", mode: $0) }
        gate(.harm, icon: "exclamationmark.bubble", mode: s.harm, options: Self.four, rect: layout.harmCard,
             caption: harmCaption) { await engine.setCheck("harm", mode: $0) }
        maskingCard
        localCard
        restoreCard
        gate(.commands, icon: "terminal", mode: s.commands, options: Self.three, rect: layout.commandsCard,
             caption: s.commands == "off" ? "Off: commands reach the agent unchecked" : "Rules: curl | sh, rm -rf ~, keys sent out\u{2026}") {
            await engine.setCheck("commands", mode: $0)
        }
    }

    private static let four = [("off", "Off"), ("warn", "Warn"), ("block", "Block"), ("local", "Local")]
    private static let three = [("off", "Off"), ("warn", "Warn"), ("block", "Block")]

    private func level(_ name: String?) -> String { name.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? "Custom" }

    private var injectionCaption: String {
        if s.paused { return "Paused until its model updates" }
        if s.injection == "off" { return "Off: tool results pass unchecked" }
        guard let r = s.injectionRate, r > 0 else { return level(s.injectionLevel) }
        return "\(level(s.injectionLevel)) · flags about 1 in \(Int((1 / r).rounded()))"
    }

    private var harmCaption: String {
        if s.harm == "off" { return "Off: what you type passes unchecked" }
        let model = ["small": "Small", "0.6b": "0.6B", "4b": "4B"][s.harmModel] ?? s.harmModel
        guard let c = s.harmCaught else { return model }
        return "\(level(s.harmLevel)) · catches \(Int((c * 100).rounded()))% · \(model)"
    }

    private func modeColor(_ mode: String) -> Color {
        switch mode {
        case "warn": Theme.warning
        case "block": Theme.blocked
        case "local": Theme.restored
        default: Theme.textTertiary
        }
    }

    private func ring(_ part: MapPart) -> some View {
        RoundedRectangle(cornerRadius: Radius.m, style: .continuous)
            .strokeBorder(selected == part ? Theme.accent : hovered == part ? Theme.accent.opacity(0.45) : Theme.separator,
                          lineWidth: selected == part ? 2 : hovered == part ? 1.5 : 1)
    }

    /// Anywhere on a card selects it, its controls included, with a pointing hand over it.
    private func selectable(_ part: MapPart) -> some ViewModifier { SelectableCard(part: part, selected: $selected, hovered: $hovered) }

    private func cardBackground() -> some View {
        RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.raised)
            .shadow(color: .black.opacity(0.06), radius: 2, y: 1)
    }

    private func title(_ part: MapPart, icon: String, dot: Color? = nil) -> some View {
        Button { selected = part } label: {
            HStack(spacing: Space.xs + 2) {
                Image(systemName: icon).font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: 14)
                Text(part.title).font(.km(.headline)).foregroundStyle(Theme.text).lineLimit(1)
                Spacer(minLength: 0)
                if let dot { Circle().fill(dot).frame(width: 7, height: 7) }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Settings for \(part.title), under the map")
    }

    private func gate(_ part: MapPart, icon: String, mode: String, options: [(String, String)], rect: CGRect, caption: String,
                      paused: Bool = false, set: @escaping (String) async -> Void) -> some View {
        VStack(alignment: .leading, spacing: Space.xs + 1) {
            title(part, icon: icon, dot: paused ? Theme.warning : modeColor(mode))
            if paused {
                Button {
                    selected = part
                    Task { await engine.setUpCheck("injection") }
                } label: {
                    Text(engine.checkDownload?.what == "injection"
                         ? "Updating \(Int(((engine.checkDownload?.progress ?? 0) * 100).rounded()))%" : "Update (164 MB)")
                        .font(.km(.caption).weight(.semibold)).foregroundStyle(Theme.onAccent)
                        .frame(maxWidth: .infinity).padding(.vertical, Space.xs)
                        .background(RoundedRectangle(cornerRadius: Radius.s - 1).fill(Theme.warning))
                }
                .buttonStyle(.plain)
                .disabled(!engine.isBundled || engine.busy)
            } else if isSnapshot {
                Segmented(options: options, selection: .constant(mode), compact: true)
            } else {
                Segmented(options: options, selection: Binding(get: { mode }, set: { new in
                    if new != mode { Task { await set(new) } }
                }), compact: true)
                .disabled(!engine.isBundled || engine.busy)
            }
            Text(caption).font(.km(.caption)).foregroundStyle(paused ? Theme.warning : Theme.textSecondary).lineLimit(1)
        }
        .padding(Space.s)
        .frame(width: rect.width, height: rect.height, alignment: .topLeading)
        .background(cardBackground())
        .overlay(paused ? AnyView(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).strokeBorder(Theme.warning, lineWidth: 2))
                        : AnyView(ring(part)))
        .opacity(mode == "off" && !paused ? 0.8 : 1)
        .modifier(selectable(part))
        .offset(x: rect.minX, y: rect.minY)
    }

    private var maskingCard: some View {
        let rect = layout.maskCard
        return VStack(alignment: .leading, spacing: Space.xs + 1) {
            title(.masking, icon: "eye.slash")
            if isSnapshot {
                Segmented(options: [(false, "Mask"), (true, "Observe")], selection: .constant(s.observe), compact: true)
            } else {
                Segmented(options: [(false, "Mask"), (true, "Observe")], selection: Binding(get: { s.observe }, set: { on in
                    if on != s.observe { Task { await engine.setObserve(on) } }
                }), compact: true)
                .disabled(!engine.isBundled || engine.busy)
            }
            Text(s.observe ? "Sent as is" : layout.masked > 0 ? "\(layout.masked.formatted()) masked" : "Placeholders out")
                .font(.km(.caption).weight(.medium)).foregroundStyle(s.observe ? Theme.warning : Theme.masked).lineLimit(1)
        }
        .padding(Space.s)
        .frame(width: rect.width, height: rect.height, alignment: .topLeading)
        .background(cardBackground())
        .overlay(ring(.masking))
        .modifier(selectable(.masking))
        .offset(x: rect.minX, y: rect.minY)
    }

    private var localCard: some View {
        let rect = layout.localCard
        let answered = layout.hitCount("local", "injection") + layout.hitCount("local", "harm")
        let servers = "\(s.localServers) server\(s.localServers == 1 ? "" : "s")"
        let detail = !s.localUsed ? "Not used: no check is Local"
            : s.localServers == 0 ? "No server yet: a hit is refused"
            : s.harm == "local" && s.injectionActs != "local" ? "\(servers) · harm hits stay here"
            : servers + (answered > 0 ? " · \(answered) answered" : "")
        return VStack(alignment: .leading, spacing: Space.xxs) {
            title(.local, icon: "cpu")
            Text(detail).font(.km(.caption)).lineLimit(1)
                .foregroundStyle(s.localUsed && s.localServers == 0 ? Theme.warning : Theme.textSecondary)
            handbackMenu.padding(.top, Space.xxs)
        }
        .padding(Space.s)
        .frame(width: rect.width, height: rect.height, alignment: .topLeading)
        .background(cardBackground())
        .overlay(ring(.local))
        .opacity(s.localUsed ? 1 : 0.6)
        .modifier(selectable(.local))
        .offset(x: rect.minX, y: rect.minY)
    }

    /// What follows a local turn of the injection check (stay, or back to the cloud), on the card.
    @ViewBuilder private var handbackMenu: some View {
        let current = LocalHandbackSection.choices.first { $0.id == s.handback } ?? LocalHandbackSection.choices[0]
        let short = ["stay": "stay local", "answer": "answer, then cloud", "summary": "summary, then cloud",
                     "rewrite": "rewrite, then cloud", "withhold": "withhold, then cloud"][current.id] ?? current.label
        let label = HStack(spacing: Space.xxs) {
            Text("Then:").foregroundStyle(Theme.textSecondary)
            Text(short).foregroundStyle(Theme.text).lineLimit(1)
            Image(systemName: "chevron.down").foregroundStyle(Theme.textSecondary)
        }
        .font(.km(.caption))
        if isSnapshot {
            label
        } else {
            Menu {
                Section("After a local turn of the injection check") {
                    ForEach(LocalHandbackSection.choices, id: \.id) { c in
                        Button(c.label + (c.id == s.handback ? " \u{2713}" : "")) {
                            selected = .local
                            Task { await engine.setLocalHandback(c.id) }
                        }
                    }
                }
            } label: { label }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .disabled(!engine.isBundled || engine.busy)
            .help(current.detail + " For the injection check: a harm hit always stays on this Mac.")
        }
    }

    private var restoreCard: some View {
        let rect = layout.restoreCard
        return VStack(alignment: .leading, spacing: Space.xs) {
            HStack(spacing: Space.xs + 2) {
                Image(systemName: "arrow.uturn.backward").font(.km(.caption)).foregroundStyle(Theme.restored)
                Text("Restore").font(.km(.headline)).foregroundStyle(Theme.text)
            }
            Text("Puts the real values back in replies").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if layout.restored > 0 {
                Text("\(layout.restored.formatted()) put back").font(.km(.caption).weight(.medium)).foregroundStyle(Theme.restored)
            }
        }
        .padding(Space.s)
        .frame(width: rect.width, height: rect.height, alignment: .topLeading)
        .background(cardBackground())
        .overlay(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).strokeBorder(Theme.separator, lineWidth: 1))
        .offset(x: rect.minX, y: rect.minY)
    }
}

/// A card on the map that selects its part when clicked anywhere, its controls included (they still
/// work: the click does both), and shows a pointing hand while the pointer is over it.
private struct SelectableCard: ViewModifier {
    var part: MapPart
    @Binding var selected: MapPart
    @Binding var hovered: MapPart?

    func body(content: Content) -> some View {
        content
            .contentShape(RoundedRectangle(cornerRadius: Radius.m, style: .continuous))
            .simultaneousGesture(TapGesture().onEnded { selected = part })
            .onHover { inside in
                if inside {
                    hovered = part
                    NSCursor.pointingHand.push()
                } else {
                    if hovered == part { hovered = nil }
                    NSCursor.pop()
                }
            }
            .accessibilityAddTraits(selected == part ? [.isSelected] : [])
    }
}

/// On a settings pane: the same settings, drawn where they act on the Protection map.
struct MapLink: View {
    var engine: Engine
    var text: String

    var body: some View {
        HStack(spacing: Space.m) {
            Image(systemName: Page.map.icon).foregroundStyle(Theme.accent)
            Text(text).font(.km(.callout)).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Space.s)
            PillButton(title: "Open the map", icon: "chevron.right") { engine.pageRequested = .map }
        }
        .padding(Space.m)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.accentSoft))
    }
}
