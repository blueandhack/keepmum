import SwiftUI

enum FlowGeometry {
    /// Points per second along every band, so a pulse keeps one speed from bar to bar.
    static let speed: CGFloat = 520
    /// The pause at the provider between a request landing and its reply setting off.
    static let turnaround: TimeInterval = 0.25
    /// How long a pulse is kept: a poll's stagger, a request, the turnaround, its reply, and its ring.
    static let horizon: TimeInterval = 6
    /// Results that never reach a provider.
    static let unsent: Set<String> = ["blocked", "refused", "failed", "local"]
    /// Results with no reply: all of those but "local", whose answer comes back from the local model.
    static let unanswered: Set<String> = ["blocked", "refused", "failed"]

    /// How long a route of this length takes, kept within bounds for very short and very long ones.
    static func duration(_ length: CGFloat) -> TimeInterval { min(max(Double(length / speed), 0.9), 1.8) }

    /// Progress along a route at time fraction u: slower at both ends, never stopped
    /// (0.4 of the average speed there, 1.6 midway).
    static func ease(_ u: Double) -> Double { u - easing * sin(2 * .pi * u) / (2 * .pi) }
    /// The speed at u, from 0.25 at the ends to 1 midway: how long the comet's tail is.
    static func pace(_ u: Double) -> Double { (1 - easing * cos(2 * .pi * u)) / (1 + easing) }
    /// The time fraction at which progress reaches x: when a pulse passes a bar.
    static func uneased(_ x: Double) -> Double {
        var lo = 0.0, hi = 1.0
        for _ in 0..<22 {
            let m = (lo + hi) / 2
            if ease(m) < x { lo = m } else { hi = m }
        }
        return (lo + hi) / 2
    }
    private static let easing = 0.6
}

/// How a request is drawn on the live flow: a dot, or a dot with a tail. Settings › General › Display.
enum FlowPulseStyle: String, CaseIterable, Identifiable {
    case dot, comet
    static let key = "flowPulseStyle"
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
}

/// The live flow as two Sankey diagrams on shared columns.
///
///   Requests out:   clients ─▶ keepmum scans ─▶ clean / masked / handed back / … / local ┆─▶ providers
///   Replies back:   clients ◀─ keepmum restores (values put back / nothing to) ◀─ local model ┆◀─ providers
///
/// Local mode has a route of each kind. A request answered on this Mac ends at its result, under the
/// local model, whose answer comes back from there, on this side of the line. A request handed back
/// (a flagged result replaced by the local model's answer, summary or rewrite, or a notice) goes on
/// to its provider like any other.
///
/// Band width is the number of requests in the window. A request sets off as it leaves this Mac,
/// waits at its provider (a breathing dot) for as long as the provider takes, and its reply sets
/// off when the reply arrives. With an engine that has no live feed, requests are drawn after the
/// fact instead: out, then back. A pulse takes each band's color, so it visibly changes at
/// keepmum; as a comet, its tail shows the change too.
struct FlowBandsView: View {
    var flows: Flows
    var pulses: [Pulse]
    /// Requests out at a provider now: the pulses that took them there.
    var open: [Pulse] = []
    var live: Bool
    /// The pointer, while it is over the diagram: what is under it is highlighted.
    @State private var hover: CGPoint?
    @AppStorage(FlowPulseStyle.key) private var style: FlowPulseStyle = .comet

    var body: some View {
        // Requests that only wait at a provider breathe slowly: a low frame rate is enough for them.
        TimelineView(.animation(minimumInterval: pulses.isEmpty ? 1 / 20 : 1 / 60, paused: !live || (pulses.isEmpty && open.isEmpty))) { tl in
            Canvas { context, size in
                FlowBands(flows: flows, size: size, open: open).draw(&context, now: tl.date, pulses: pulses, style: style, hover: hover)
            }
        }
        .onContinuousHover { phase in
            switch phase {
            case .active(let p): hover = p
            case .ended: hover = nil
            }
        }
    }
}

/// What the pointer is over: a band, or a bar with every band through it.
struct FlowFocus {
    /// Highlighted links per diagram: 0 out, 1 back. Empty: nothing is highlighted.
    var links: [Int: Set<Int>] = [:]
    var nodes: [Int: Set<String>] = [:]
    var label: String?
    var at: CGPoint = .zero
    var isEmpty: Bool { links.isEmpty && nodes.isEmpty }
}

// MARK: - Layout

/// A Sankey layout: nodes in columns, links between them, band thickness from the values.
struct BandLayout {
    struct Node {
        var id: String
        var col: Int
        var color: Color
        var rect: CGRect = .zero
        /// Kept at the bottom, under every other column's nodes, so no band between them crosses it.
        var under = false
        /// The height its label needs: a thin node takes this much of its column, gap included, with the
        /// bar in the middle, so the label beside it never has to move off it to clear the next one.
        var slot: CGFloat = 0
    }
    struct Link {
        var from: String
        var to: String
        var value: Int
        var color: Color
        var t: CGFloat = 0     // thickness
        var y0: CGFloat = 0    // top of the band at its source
        var y1: CGFloat = 0    // top of the band at its target
        var x0: CGFloat = 0    // the band's ends: the side of each bar that faces the other
        var x1: CGFloat = 0
        /// Distance along the centre line at evenly spaced Bézier steps, from 0 to the length.
        var lengths: [CGFloat] = []

        var length: CGFloat { lengths.last ?? 0 }

        /// A point on the centre line by Bézier parameter, f from 0 (source) to 1 (target).
        func point(_ f: CGFloat) -> CGPoint {
            let xm = (x0 + x1) / 2, a = y0 + t / 2, b = y1 + t / 2
            let u = 1 - f
            let x = u * u * u * x0 + 3 * u * u * f * xm + 3 * u * f * f * xm + f * f * f * x1
            let y = u * u * u * a + 3 * u * u * f * a + 3 * u * f * f * b + f * f * f * b
            return CGPoint(x: x, y: y)
        }

        /// The point `d` along the centre line by distance: the same speed on every part of the curve.
        func point(at d: CGFloat) -> CGPoint {
            let n = lengths.count - 1
            guard n > 0, d > 0 else { return point(0) }
            guard d < length else { return point(1) }
            var lo = 0, hi = n
            while hi - lo > 1 {
                let m = (lo + hi) / 2
                if lengths[m] <= d { lo = m } else { hi = m }
            }
            let span = lengths[hi] - lengths[lo]
            return point((CGFloat(lo) + (span > 0 ? (d - lengths[lo]) / span : 0)) / CGFloat(n))
        }

        mutating func measure(steps n: Int = 32) {
            var total: CGFloat = 0, last = point(0)
            lengths = [0]
            for k in 1...n {
                let p = point(CGFloat(k) / CGFloat(n))
                total += hypot(p.x - last.x, p.y - last.y)
                lengths.append(total)
                last = p
            }
        }
    }

    var nodes: [String: Node] = [:]
    var links: [Link] = []

    static let bar: CGFloat = 8

    init(nodes order: [Node], links: [Link], xs: [CGFloat], top: CGFloat, height: CGFloat, gap: CGFloat = 12) {
        var inSum = [String: Int](), outSum = [String: Int]()
        for l in links where l.value > 0 {
            outSum[l.from, default: 0] += l.value
            inSum[l.to, default: 0] += l.value
        }
        let present = order.filter { max(inSum[$0.id] ?? 0, outSum[$0.id] ?? 0) > 0 }
        let byCol = Dictionary(grouping: present, by: \.col)
        let live = links.filter { $0.value > 0 }
        func thick(_ v: Int, _ scale: CGFloat) -> CGFloat { v > 0 ? max(CGFloat(v) * scale, 1.5) : 0 }
        /// A node's bar at a scale: as tall as the bands on its fuller side.
        func bar(_ id: String, _ scale: CGFloat) -> CGFloat {
            max(live.filter { $0.to == id }.reduce(0) { $0 + thick($1.value, scale) },
                live.filter { $0.from == id }.reduce(0) { $0 + thick($1.value, scale) }, 2)
        }
        /// The height a node takes in its column, the gap after it included.
        func block(_ n: Node, _ h: CGFloat) -> CGFloat { max(h + gap, n.slot) }
        let underNodes = present.filter(\.under)
        /// Whether every column fits the height at this scale: its nodes, then any kept under them.
        func fits(_ scale: CGFloat) -> Bool {
            let under = underNodes.map { bar($0.id, scale) }.max().map { $0 + gap } ?? 0
            return byCol.values.allSatisfy { col in
                let main = col.filter { !$0.under }
                return main.reduce(-gap) { $0 + block($1, bar($1.id, scale)) } + under <= height + 0.5
            }
        }
        // One scale for both sides of every node: the largest at which the fullest column fits, its
        // labels' room included. Without labels' room it would be total over height; with it, less.
        var hi: CGFloat = 0
        for (_, col) in byCol {
            let total = col.reduce(0) { $0 + max(inSum[$1.id] ?? 0, outSum[$1.id] ?? 0) }
            if total > 0 { hi = max(hi, height / CGFloat(total)) }
        }
        var lo: CGFloat = 0
        if fits(hi) { lo = hi } else {
            for _ in 0..<40 {
                let m = (lo + hi) / 2
                if fits(m) { lo = m } else { hi = m }
            }
        }
        let scale = lo

        var links = live
        for i in links.indices { links[i].t = thick(links[i].value, scale) }
        var nodes = [String: Node]()
        for (c, col) in byCol {
            var y = top
            for var n in col {
                let h = bar(n.id, scale)
                if n.under {
                    n.rect = CGRect(x: xs[c], y: top + height - h, width: Self.bar, height: h)
                } else {
                    let b = block(n, h)
                    // A thin node sits in the middle of its slot, where its label is.
                    n.rect = CGRect(x: xs[c], y: y + max(0, (b - gap - h) / 2), width: Self.bar, height: h)
                    y += b
                }
                nodes[n.id] = n
            }
        }
        // Stack each node's bands in the order of the nodes at their other end: fewest crossings.
        for (id, node) in nodes {
            var y = node.rect.minY
            for i in links.indices.filter({ links[$0].from == id }).sorted(by: { nodes[links[$0].to]!.rect.minY < nodes[links[$1].to]!.rect.minY }) {
                links[i].y0 = y
                y += links[i].t
            }
            y = node.rect.minY
            for i in links.indices.filter({ links[$0].to == id }).sorted(by: { nodes[links[$0].from]!.rect.minY < nodes[links[$1].from]!.rect.minY }) {
                links[i].y1 = y
                y += links[i].t
            }
        }
        for i in links.indices {
            let s = nodes[links[i].from]!.rect, d = nodes[links[i].to]!.rect
            (links[i].x0, links[i].x1) = d.minX > s.minX ? (s.maxX, d.minX) : (s.minX, d.maxX)
            links[i].measure()
        }
        self.nodes = nodes
        self.links = links
    }

    func band(_ l: Link) -> Path {
        let x0 = l.x0, x1 = l.x1
        let xm = (x0 + x1) / 2
        var p = Path()
        p.move(to: CGPoint(x: x0, y: l.y0))
        p.addCurve(to: CGPoint(x: x1, y: l.y1), control1: CGPoint(x: xm, y: l.y0), control2: CGPoint(x: xm, y: l.y1))
        p.addLine(to: CGPoint(x: x1, y: l.y1 + l.t))
        p.addCurve(to: CGPoint(x: x0, y: l.y0 + l.t), control1: CGPoint(x: xm, y: l.y1 + l.t), control2: CGPoint(x: xm, y: l.y0 + l.t))
        p.closeSubpath()
        return p
    }

    func link(_ from: String, _ to: String) -> Link? { links.first { $0.from == from && $0.to == to } }
}

/// A pulse's way across one diagram: its bands end to end, measured by length.
struct FlowRoute {
    let layout: BandLayout
    let links: [BandLayout.Link]
    /// How far along the route each band begins.
    let starts: [CGFloat]
    let length: CGFloat

    init(layout: BandLayout, links: [BandLayout.Link]) {
        self.layout = layout
        self.links = links
        var starts: [CGFloat] = [], d: CGFloat = 0
        for l in links {
            starts.append(d)
            d += l.length
        }
        self.starts = starts
        length = d
    }

    /// The point `s` along the route, and the band it is on.
    func at(_ s: CGFloat) -> (point: CGPoint, link: BandLayout.Link) {
        let s = min(max(s, 0), length)
        let i = starts.lastIndex { $0 <= s } ?? 0
        return (links[i].point(at: s - starts[i]), links[i])
    }
}

// MARK: - The two diagrams

private struct FlowBands {
    let flows: Flows
    let size: CGSize
    let open: [Pulse]
    let out: BandLayout
    let back: BandLayout
    let xs: [CGFloat]
    let outTop: CGFloat, backTop: CGFloat, dashX: CGFloat

    /// What keepmum did with each request: sent (clean, masked, observed, flagged by a check) or not
    /// (failed upstream, blocked by keepmum, refused by a check).
    static let results = ["clean", "masked", "observed", "handed", "flagged", "failed", "blocked", "refused", "local"]
    /// The local model, in the replies' Result column: on this Mac.
    static let localModel = "m:local"
    /// Results that are usually thin: their labels take one line each, so several fit below the others.
    static let oneLine: Set = ["handed", "flagged", "failed", "blocked", "refused", "local"]
    /// The room a label takes in its column: a title, or a title over a detail.
    static func slot(_ oneLine: Bool) -> CGFloat { oneLine ? 18 : 34 }

    init(flows: Flows, size: CGSize, open: [Pulse] = []) {
        self.flows = flows
        self.size = size
        self.open = open
        let left: CGFloat = 132, right: CGFloat = size.width - 132
        let span = right - left
        xs = [left, left + span * 0.30, left + span * 0.58, right]
        dashX = (xs[2] + xs[3]) / 2 + 20
        let header: CGFloat = 26, gapBetween: CGFloat = 30
        let half = (size.height - 2 * header - gapBetween) / 2
        outTop = header
        backTop = header + half + gapBetween + header

        let clients = flows.clients.map(\.id)
        var providers = flows.providers.map(\.id)
        for o in flows.out { if let p = o.provider, !providers.contains(p) { providers.append(p) } }

        // Requests out.
        let two = Self.slot(false)
        var nodes = clients.map { BandLayout.Node(id: "c:" + $0, col: 0, color: Self.clientColor($0), slot: two) }
        nodes.append(.init(id: "keepmum", col: 1, color: Theme.text))
        nodes += Self.results.map { BandLayout.Node(id: "r:" + $0, col: 2, color: Self.resultColor($0), slot: Self.slot(Self.oneLine.contains($0))) }
        nodes += providers.map { BandLayout.Node(id: "p:" + $0, col: 3, color: Self.providerColor($0), slot: two) }
        var links: [BandLayout.Link] = []
        for c in flows.clients {
            links.append(.init(from: "c:" + c.id, to: "keepmum", value: c.sent, color: Self.clientColor(c.id)))
        }
        for r in Self.results {
            let n = flows.out.filter { $0.kind == r }.reduce(0) { $0 + $1.n }
            links.append(.init(from: "keepmum", to: "r:" + r, value: n, color: Self.resultColor(r)))
            for p in providers {
                let m = flows.out.filter { $0.kind == r && $0.provider == p }.reduce(0) { $0 + $1.n }
                links.append(.init(from: "r:" + r, to: "p:" + p, value: m, color: Self.resultColor(r)))
            }
        }
        out = BandLayout(nodes: nodes, links: links, xs: xs, top: outTop, height: half)

        // Replies back, right to left: providers, restore, clients.
        var bnodes = providers.map { BandLayout.Node(id: "p:" + $0, col: 3, color: Self.providerColor($0), slot: two) }
        bnodes.append(BandLayout.Node(id: Self.localModel, col: 2, color: Theme.restored, under: true))
        bnodes += [BandLayout.Node(id: "restored", col: 1, color: Theme.restored, slot: two),
                   BandLayout.Node(id: "plain", col: 1, color: Self.resultColor("clean"), slot: two)]
        bnodes += clients.map { BandLayout.Node(id: "c:" + $0, col: 0, color: Self.clientColor($0), slot: two) }
        var blinks: [BandLayout.Link] = []
        for p in providers {
            for (node, r) in [("restored", true), ("plain", false)] {
                let n = flows.back.filter { $0.provider == p && $0.restored == r }.reduce(0) { $0 + $1.n }
                blinks.append(.init(from: "p:" + p, to: node, value: n, color: r ? Theme.restored : Self.resultColor("clean")))
            }
        }
        // The local model's answers: real values never left this Mac, so there is nothing to put back.
        blinks.append(.init(from: Self.localModel, to: "plain", value: flows.localReplies.reduce(0) { $0 + $1.n },
                            color: Theme.restored))
        for c in clients {
            for (node, r) in [("restored", true), ("plain", false)] {
                let n = flows.back.filter { $0.client == c && $0.restored == r }.reduce(0) { $0 + $1.n }
                    + (r ? 0 : flows.localReplies.filter { $0.client == c }.reduce(0) { $0 + $1.n })
                blinks.append(.init(from: node, to: "c:" + c, value: n, color: r ? Theme.restored : Self.resultColor("clean")))
            }
        }
        back = BandLayout(nodes: bnodes, links: blinks, xs: xs, top: backTop, height: half)
    }

    // MARK: Colors

    static func clientColor(_ id: String) -> Color { Theme.client(id) }

    static func providerColor(_ id: String) -> Color {
        switch id {
        case "anthropic": clientColor("claude-code")
        case "openai": clientColor("codex")
        case "google": clientColor("gemini")
        default: clientColor("other")
        }
    }

    static func resultColor(_ kind: String) -> Color {
        switch kind {
        case "masked": Theme.masked
        case "observed", "flagged": Theme.warning
        case "blocked", "refused": Theme.blocked
        case "local", "handed": Theme.restored
        case "failed": Theme.textSecondary
        default: Theme.neutral
        }
    }

    // MARK: Drawing

    // MARK: Hover

    /// Bars are thin, so they take hits a few points wide; bands take hits by their shape.
    func focus(at p: CGPoint) -> FlowFocus {
        let layouts = [out, back]
        for (k, layout) in layouts.enumerated() {
            for (id, n) in layout.nodes where n.rect.insetBy(dx: -6, dy: -2).contains(p) {
                return nodeFocus(id, primary: k, at: p)
            }
        }
        for (k, layout) in layouts.enumerated() {
            // Last drawn is on top: test in reverse paint order.
            for i in layout.links.indices.reversed() where layout.band(layout.links[i]).contains(p) {
                let l = layout.links[i]
                return FlowFocus(links: [k: [i]], nodes: [k: [l.from, l.to]],
                                 label: "\(name(l.from)) → \(name(l.to)) · \(l.value.compact)", at: p)
            }
        }
        return FlowFocus()
    }

    /// A bar: the bands into and out of it. A client or provider is lit in both diagrams, with
    /// the bands its traffic takes through keepmum (from the per-client totals).
    private func nodeFocus(_ id: String, primary: Int, at p: CGPoint) -> FlowFocus {
        var f = FlowFocus(at: p)
        let route = id.hasPrefix("c:") || id.hasPrefix("p:")
        let key = String(id.dropFirst(2))
        for (k, layout) in [out, back].enumerated() where k == primary || route {
            guard layout.nodes[id] != nil else { continue }
            var nodes: Set<String> = [id]
            if route { nodes.formUnion(k == 0 ? outRoute(id, key) : backRoute(id, key)) }
            var links = Set<Int>()
            for (i, l) in layout.links.enumerated() {
                let touches = l.from == id || l.to == id
                // Another client's or provider's own band never lights up with this one.
                let foreign = route && [l.from, l.to].contains { $0 != id && ($0.hasPrefix("c:") || $0.hasPrefix("p:")) && !touches
                    && (($0.hasPrefix("c:") && id.hasPrefix("c:")) || ($0.hasPrefix("p:") && id.hasPrefix("p:"))) }
                if touches || (route && !foreign && nodes.contains(l.from) && nodes.contains(l.to)) {
                    links.insert(i)
                    nodes.formUnion([l.from, l.to])
                }
            }
            f.links[k] = links
            f.nodes[k] = nodes
        }
        let (n, unit) = totals(id)
        f.label = "\(name(id)) · \(n.compact) \(unit)"
        return f
    }

    /// Out: the client or provider, keepmum, the results it has traffic in, and the far end.
    private func outRoute(_ id: String, _ key: String) -> Set<String> {
        let mine = flows.out.filter { $0.n > 0 && (id.hasPrefix("c:") ? $0.client == key : $0.provider == key) }
        var nodes: Set<String> = ["keepmum"]
        for o in mine {
            nodes.insert("r:" + o.kind)
            nodes.insert("c:" + o.client)
            if let p = o.provider { nodes.insert("p:" + p) }
        }
        return nodes
    }

    /// Back: the provider, restored or not, and the clients.
    private func backRoute(_ id: String, _ key: String) -> Set<String> {
        let mine = flows.back.filter { $0.n > 0 && (id.hasPrefix("c:") ? $0.client == key : $0.provider == key) }
        var nodes = Set<String>()
        for b in mine {
            nodes.formUnion([b.restored ? "restored" : "plain", "c:" + b.client, "p:" + b.provider])
        }
        if id.hasPrefix("c:"), flows.localReplies.contains(where: { $0.client == key && $0.n > 0 }) {
            nodes.formUnion([Self.localModel, "plain", id])
        }
        return nodes
    }

    private var localTotal: Int { flows.localReplies.reduce(0) { $0 + $1.n } }

    private func totals(_ id: String) -> (Int, String) {
        let key = String(id.dropFirst(2))
        if id.hasPrefix("c:") { return (flows.clients.first { $0.id == key }?.sent ?? 0, "requests") }
        if id.hasPrefix("p:") { return (flows.providers.first { $0.id == key }?.replies ?? 0, "requests") }
        if id.hasPrefix("r:") { return (flows.out.filter { $0.kind == key }.reduce(0) { $0 + $1.n }, "requests") }
        if id == "restored" { return (flows.back.filter(\.restored).reduce(0) { $0 + $1.n }, "replies") }
        if id == "plain" { return (flows.back.filter { !$0.restored }.reduce(0) { $0 + $1.n } + localTotal, "replies") }
        if id == Self.localModel { return (localTotal, "answers") }
        return (flows.clients.reduce(0) { $0 + $1.sent }, "scanned")
    }

    private func name(_ id: String) -> String {
        let key = String(id.dropFirst(2))
        if id.hasPrefix("c:") { return flows.clients.first { $0.id == key }?.name ?? key }
        if id.hasPrefix("p:") { return flows.providers.first { $0.id == key }?.name ?? key }
        if id.hasPrefix("r:") {
            return ["refused": "Refused by a check", "local": "Answered on this Mac", "handed": "Handed back"][key]
                ?? key.prefix(1).uppercased() + key.dropFirst()
        }
        return ["keepmum": "keepmum", "restored": "Real values put back", "plain": "Nothing to put back",
                Self.localModel: "Your local model"][id] ?? id
    }

    func draw(_ ctx: inout GraphicsContext, now: Date, pulses: [Pulse], style: FlowPulseStyle = .comet, hover: CGPoint? = nil) {
        if flows.clients.isEmpty {
            ctx.draw(Text("No requests in this window yet").font(.km(.body)).foregroundStyle(Theme.textSecondary),
                     at: CGPoint(x: size.width / 2, y: size.height / 2))
            return
        }
        // The line between this Mac and the internet runs through both diagrams.
        var dash = Path()
        dash.move(to: CGPoint(x: dashX, y: outTop - 8))
        dash.addLine(to: CGPoint(x: dashX, y: size.height))
        ctx.stroke(dash, with: .color(Theme.textSecondary.opacity(0.5)), style: StrokeStyle(lineWidth: 1, dash: [4, 5]))
        var rule = Path()
        rule.move(to: CGPoint(x: 0, y: backTop - 44))
        rule.addLine(to: CGPoint(x: size.width, y: backTop - 44))
        ctx.stroke(rule, with: .color(Theme.separator), lineWidth: 1)

        let focus = hover.map { self.focus(at: $0) } ?? FlowFocus()
        for (k, layout) in [out, back].enumerated() {
            for (i, l) in layout.links.enumerated() {
                let lit = focus.links[k]?.contains(i) ?? false
                ctx.fill(layout.band(l), with: .color(l.color.opacity(focus.isEmpty ? 0.28 : lit ? 0.55 : 0.08)))
            }
            for (id, n) in layout.nodes {
                let lit = focus.isEmpty || (focus.nodes[k]?.contains(id) ?? false)
                ctx.fill(Path(roundedRect: n.rect, cornerRadius: Radius.s / 2), with: .color(n.color.opacity(lit ? 1 : 0.3)))
            }
        }
        headers(&ctx)
        labels(&ctx)
        drawWaiting(&ctx, now: now)
        drawPulses(&ctx, now: now, pulses: pulses, style: style)
        if let label = focus.label { tooltip(&ctx, label, at: focus.at) }
    }

    private func headers(_ ctx: inout GraphicsContext) {
        func h(_ s: String, _ x: CGFloat, _ y: CGFloat, _ anchor: UnitPoint) {
            ctx.draw(Text(s.uppercased()).font(.km(.label)).kerning(0.4).foregroundStyle(Theme.textSecondary),
                     at: CGPoint(x: x, y: y), anchor: anchor)
        }
        for (top, a, b, c, d) in [(outTop, "Requests out →", "keepmum scans", "Result", "leaves this Mac"),
                                  (backTop, "← Replies back", "keepmum restores", "", "arrives on this Mac")] {
            let y = top - 14
            h(a, xs[0] - 10, y, .trailing)
            h(b, xs[1] + 4, y, .center)
            if !c.isEmpty { h(c, xs[2], y, .leading) }
            h(d, dashX + 8, y, .leading)
        }
        if flows.unresolved > 0 {
            // In the replies' empty Result column, clear of "arrives on this Mac"; shorter if it must be.
            let n = flows.unresolved
            let room = dashX - 8 - xs[2]
            let note = [n == 1 ? "1 placeholder could not be restored" : "\(n) placeholders could not be restored",
                        "\(n) placeholder\(n == 1 ? "" : "s") not restored", "\(n) not restored"]
                .map { ctx.resolve(Text($0).font(.km(.caption).weight(.semibold)).foregroundStyle(Theme.blocked)) }
            let text = note.first { $0.measure(in: size).width <= room } ?? note[2]
            ctx.draw(text, at: CGPoint(x: xs[2], y: backTop - 14), anchor: .leading)
        }
    }

    private func labels(_ ctx: inout GraphicsContext) {
        let pname = Dictionary(uniqueKeysWithValues: flows.providers.map { ($0.id, $0.name) })
        func sum(_ xs: [Int]) -> Int { xs.reduce(0, +) }

        // (node rect, title, detail, title color) per column, then placed without overlaps.
        typealias Tag = (CGRect, String, String, Color, Bool)  // the last: title and detail on one line
        var leftOut: [Tag] = [], leftBack: [Tag] = [], mid: [Tag] = [], rightOut: [Tag] = [], rightBack: [Tag] = [], restore: [Tag] = []
        for c in flows.clients {
            if let n = out.nodes["c:" + c.id] { leftOut.append((n.rect, c.name, c.sent.compact, Theme.text, false)) }
            if let n = back.nodes["c:" + c.id] {
                let got = sum(flows.back.filter { $0.client == c.id }.map(\.n)) + sum(flows.localReplies.filter { $0.client == c.id }.map(\.n))
                let values = sum(flows.back.filter { $0.client == c.id }.map(\.values))
                leftBack.append((n.rect, c.name, values > 0 ? "\(got.compact) · \(values.compact) restored" : got.compact, Theme.text, false))
            }
        }
        // The checks' results say which check: "3 injection, 1 harm".
        func byCheck(_ r: String, _ otherwise: String) -> String {
            let n = flows.checks?[r] ?? [:]
            let parts = ["injection", "harm", "commands"].compactMap { c in
                n[c].map { "\($0.compact) \(c == "commands" ? ($0 == 1 ? "command" : "commands") : c)" }
            }
            return parts.isEmpty ? otherwise : parts.joined(separator: ", ")
        }
        // A hand-back says what replaced the flagged result, as the checks say which check: "2 summaries, 1 answer".
        func byStrategy() -> String {
            let n = flows.handback ?? [:]
            let parts = [("summary", "summary", "summaries"), ("rewrite", "rewrite", "rewrites"), ("withhold", "notice", "notices"),
                         ("answer", "answer", "answers")].compactMap { s, one, many in n[s].map { "\($0.compact) \($0 == 1 ? one : many)" } }
            return parts.isEmpty ? "result replaced" : parts.joined(separator: ", ")
        }
        let detail = ["clean": "sent as is", "masked": "as placeholders", "observed": "sent unmasked", "failed": "upstream failed",
                      "blocked": "never sent", "flagged": byCheck("flagged", "sent with a warning"),
                      "refused": byCheck("refused", "never sent"), "local": byCheck("local", "by your local model"),
                      "handed": byStrategy()]
        let title = ["clean": "Clean", "masked": "Masked", "observed": "Observed", "failed": "Failed", "blocked": "Blocked",
                     "flagged": "Flagged", "refused": "Refused by a check", "local": "Answered on this Mac", "handed": "Handed back"]
        let tint: [String: Color] = ["masked": Theme.masked, "blocked": Theme.blocked, "refused": Theme.blocked, "local": Theme.restored,
                                     "handed": Theme.restored, "observed": Self.resultColor("observed"),
                                     "flagged": Self.resultColor("flagged")]
        for r in Self.results {
            guard let n = out.nodes["r:" + r] else { continue }
            let v = sum(flows.out.filter { $0.kind == r }.map(\.n))
            mid.append((n.rect, "\(title[r]!) \(v.compact)", detail[r]!, tint[r] ?? Theme.text, Self.oneLine.contains(r)))
        }
        for (id, n) in out.nodes where id.hasPrefix("p:") {
            let p = String(id.dropFirst(2))
            let waiting = open.filter { $0.provider == p }.count
            rightOut.append((n.rect, pname[p] ?? p, "\(sum(flows.out.filter { $0.provider == p }.map(\.n)).compact) sent"
                             + (waiting > 0 ? " · \(waiting) open" : ""), Theme.text, false))
        }
        for (id, n) in back.nodes where id.hasPrefix("p:") {
            let p = String(id.dropFirst(2))
            rightBack.append((n.rect, pname[p] ?? p, "\(sum(flows.back.filter { $0.provider == p }.map(\.n)).compact) replies", Theme.text, false))
        }
        if let n = back.nodes["restored"] {
            let replies = sum(flows.back.filter(\.restored).map(\.n)), values = sum(flows.back.map(\.values))
            restore.append((n.rect, "Real values put back", "\(replies.compact) replies · \(values.compact) values", Theme.restored, false))
        }
        if let n = back.nodes["plain"] {
            restore.append((n.rect, "Nothing to put back", "\((sum(flows.back.filter { !$0.restored }.map(\.n)) + localTotal).compact) replies",
                            Theme.text, false))
        }
        var local: [Tag] = []
        if let n = back.nodes[Self.localModel] {
            local.append((n.rect, "Your local model", "\(localTotal.compact) \(localTotal == 1 ? "answer" : "answers")", Theme.restored, false))
        }

        /// Top down, each label beside its bar (the layout leaves each the room, `slot`), or below the
        /// one before; with `floor`, the last no lower than it, the ones before moved up to make room
        /// (only in a diagram too short for its labels).
        func place(_ items: [Tag], x: CGFloat, anchor: UnitPoint, floor: CGFloat? = nil) {
            let items = items.sorted(by: { $0.0.midY < $1.0.midY })
            func height(_ inline: Bool) -> CGFloat { inline ? 18 : 30 }
            var ys: [CGFloat] = []
            var next = -CGFloat.infinity  // the first y below the label before
            for (rect, _, _, _, inline) in items {
                ys.append(max(rect.midY - (inline ? 0 : 8), next))
                next = ys[ys.count - 1] + height(inline)
            }
            if var last = floor {
                for i in ys.indices.reversed() {
                    ys[i] = min(ys[i], last)
                    if i > 0 { last = ys[i] - height(items[i - 1].4) }
                }
            }
            for ((_, t, d, color, inline), y) in zip(items, ys) {
                let title = Text(t).font(.km(.callout).weight(.semibold)).foregroundStyle(color)
                if inline {
                    let line = Text("\(title)\(Text("  " + d).font(.km(.caption)).foregroundStyle(Theme.textSecondary))")
                    ctx.draw(line, at: CGPoint(x: x, y: y), anchor: anchor)
                } else {
                    ctx.draw(title, at: CGPoint(x: x, y: y), anchor: anchor)
                    ctx.draw(Text(d).font(.km(.caption)).foregroundStyle(Theme.textSecondary), at: CGPoint(x: x, y: y + 14), anchor: anchor)
                }
            }
        }
        place(leftOut, x: xs[0] - 10, anchor: .trailing)
        place(leftBack, x: xs[0] - 10, anchor: .trailing)
        // Above the rule between the two diagrams.
        place(mid, x: xs[2] + BandLayout.bar + 8, anchor: .leading, floor: backTop - 44 - 14)
        place(restore, x: xs[1] + BandLayout.bar + 8, anchor: .leading)
        place(local, x: xs[2] + BandLayout.bar + 8, anchor: .leading)
        place(rightOut, x: xs[3] + BandLayout.bar + 8, anchor: .leading)
        place(rightBack, x: xs[3] + BandLayout.bar + 8, anchor: .leading)
    }

    private func tooltip(_ ctx: inout GraphicsContext, _ text: String, at p: CGPoint) {
        let t = ctx.resolve(Text(text).font(.km(.callout).weight(.semibold)).foregroundStyle(Theme.text))
        let sz = t.measure(in: CGSize(width: 400, height: 40))
        var box = CGRect(x: p.x + 14, y: p.y - sz.height - 18, width: sz.width + 20, height: sz.height + 12)
        if box.maxX > size.width { box.origin.x = p.x - box.width - 14 }
        if box.minY < 0 { box.origin.y = p.y + 16 }
        let shape = Path(roundedRect: box, cornerRadius: Radius.s)
        ctx.fill(shape, with: .color(Theme.raised))
        ctx.stroke(shape, with: .color(Theme.separator), lineWidth: 1)
        ctx.draw(t, at: CGPoint(x: box.midX, y: box.midY))
    }

    /// The bands a pulse crosses, end to end; nil when one of them has no traffic in the window.
    private func route(_ kind: Pulse.Kind, client: String, provider: String) -> FlowRoute? {
        let (layout, keys): (BandLayout, [(String, String)]) = switch kind {
        case .out(let r):
            (out, [("c:" + client, "keepmum"), ("keepmum", "r:" + r)] + (FlowGeometry.unsent.contains(r) ? [] : [("r:" + r, "p:" + provider)]))
        case .back(_, "local"):  // the local model's answer: from this Mac, nothing to put back
            (back, [(Self.localModel, "plain"), ("plain", "c:" + client)])
        case .back(let restored, _):
            (back, [("p:" + provider, restored ? "restored" : "plain"), (restored ? "restored" : "plain", "c:" + client)])
        }
        let links = keys.compactMap { layout.link($0.0, $0.1) }
        guard links.count == keys.count, !links.isEmpty else { return nil }
        return FlowRoute(layout: layout, links: links)
    }

    private func drawPulses(_ ctx: inout GraphicsContext, now: Date, pulses: [Pulse], style: FlowPulseStyle) {
        for pulse in pulses {
            guard let route = route(pulse.kind, client: pulse.client, provider: pulse.provider) else { continue }
            let duration = FlowGeometry.duration(route.length)
            var start = pulse.born
            var stopped = false
            switch pulse.kind {
            case .out(let r):
                stopped = r == "blocked" || r == "refused"
            case .back(_, let result):
                // A reply shares its request's born and sets off once that request has reached the provider.
                let request = self.route(.out(result), client: pulse.client, provider: pulse.provider)?.length ?? route.length
                start = start.addingTimeInterval(FlowGeometry.duration(request) + FlowGeometry.turnaround)
                // A reply seen arriving sets off then, however long its request waited at the provider.
                if let leaves = pulse.leaves { start = max(start, leaves) }
            }
            let elapsed = now.timeIntervalSince(start), u = elapsed / duration
            guard u >= 0 else { continue }
            ticks(&ctx, route, u: u, duration: duration, stopped: stopped)
            guard u < 1 else { continue }
            let s = CGFloat(FlowGeometry.ease(u)) * route.length
            let (p, link) = route.at(s)
            // In over its first tenth of a second; it shrinks into the bar it lands on.
            let alpha = min(1, elapsed / 0.12) * min(1, (1 - u) / 0.04)
            let size: CGFloat = u > 0.9 ? 0.55 + 0.45 * CGFloat((1 - u) / 0.1) : 1
            if style == .comet { tail(&ctx, route, at: s, u: u, alpha: alpha, size: size) }
            let halo = (style == .comet ? 8 : 7) * size, dot = 3.5 * size
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - halo, y: p.y - halo, width: 2 * halo, height: 2 * halo)),
                     with: .color(link.color.opacity(0.18 * alpha)))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - dot, y: p.y - dot, width: 2 * dot, height: 2 * dot)),
                     with: .color(link.color.opacity(alpha)))
        }
    }

    /// Requests out at a provider now: once each has landed, a dot breathes at the provider's end of
    /// its band until the reply comes back, the later ones queued along the band behind it.
    private func drawWaiting(_ ctx: inout GraphicsContext, now: Date) {
        var queued: [String: Int] = [:]
        for pulse in open.sorted(by: { $0.born < $1.born }) {
            guard let route = route(pulse.kind, client: pulse.client, provider: pulse.provider),
                  let last = route.links.last else { continue }
            let age = now.timeIntervalSince(pulse.born) - FlowGeometry.duration(route.length)
            guard age >= 0 else { continue }  // still on its way: the pulse itself is drawn
            let key = last.from + "→" + last.to
            let i = queued[key, default: 0]
            queued[key] = i + 1
            guard i < 6 else { continue }
            let end = last.point(1), p = CGPoint(x: end.x - 6 - CGFloat(i) * 9, y: end.y)
            let breath = 0.5 - 0.5 * cos(2 * .pi * age / 1.6), a = min(1, age / 0.2)
            let halo = 5 + 3 * CGFloat(breath)
            // A clean request's band is the neutral grey, which a dot on it would vanish into.
            let color = if case .out("clean") = pulse.kind { Theme.textSecondary } else { last.color }
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - halo, y: p.y - halo, width: 2 * halo, height: 2 * halo)),
                     with: .color(color.opacity(0.22 * a)))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 3, y: p.y - 3, width: 6, height: 6)),
                     with: .color(color.opacity((0.55 + 0.45 * breath) * a)))
        }
    }

    /// The comet's tail: longer while the pulse is fast, in the color of each band it lies on, so a
    /// masked request's tail turns from its client's color to masked blue at keepmum.
    private func tail(_ ctx: inout GraphicsContext, _ route: FlowRoute, at s: CGFloat, u: Double, alpha: Double, size: CGFloat) {
        let length = (14 + 64 * CGFloat(FlowGeometry.pace(u))) * size, steps = 12
        var (last, on) = route.at(s)
        for j in 1...steps {
            let d = s - length * CGFloat(j) / CGFloat(steps)
            guard d >= 0 else { break }
            let (p, link) = route.at(d)
            // A band can leave a bar higher or lower than the one before it arrives: the tail breaks
            // at the bar and carries on from the earlier band's end, rather than streak across it.
            if link.from != on.from || link.to != on.to {
                last = link.point(1)
                on = link
            }
            let q = 1 - CGFloat(j - 1) / CGFloat(steps)
            var piece = Path()
            piece.move(to: last)
            piece.addLine(to: p)
            ctx.stroke(piece, with: .color(link.color.opacity(alpha * pow(Double(q), 1.3) * 0.95)),
                       style: StrokeStyle(lineWidth: 0.6 + 5.4 * q * size, lineCap: .round))
            last = p
        }
    }

    /// Each bar a pulse passes flashes; a ring spreads where it lands, red where a request was stopped.
    private func ticks(_ ctx: inout GraphicsContext, _ route: FlowRoute, u: Double, duration: TimeInterval, stopped: Bool) {
        let flash = 0.45, ring = 0.6
        for k in route.links.indices.dropFirst() {
            let age = (u - FlowGeometry.uneased(Double(route.starts[k] / route.length))) * duration
            guard age >= 0, age < flash, let bar = route.layout.nodes[route.links[k].from]?.rect else { continue }
            let q = CGFloat(age / flash), e = 2 + 5 * q
            ctx.fill(Path(roundedRect: bar.insetBy(dx: -e, dy: -e), cornerRadius: 3 + e),
                     with: .color(route.links[k].color.opacity(0.28 * Double(1 - q))))
        }
        let age = (u - 1) * duration
        guard age >= 0, age < ring, let last = route.links.last, let node = route.layout.nodes[last.to] else { return }
        let q = CGFloat(age / ring), end = last.point(1), color = stopped ? Theme.blocked : node.color
        for r in stopped ? [4 + 14 * q.squareRoot(), 2 + 7 * q.squareRoot()] : [4 + 14 * q.squareRoot()] {
            ctx.stroke(Path(ellipseIn: CGRect(x: end.x - r, y: end.y - r, width: 2 * r, height: 2 * r)),
                       with: .color(color.opacity(0.75 * Double(1 - q))), lineWidth: 1.6)
        }
    }
}

/// A coding agent's color, as the flow diagrams draw it.
func clientColor(_ id: String) -> Color { FlowBands.clientColor(id) }
