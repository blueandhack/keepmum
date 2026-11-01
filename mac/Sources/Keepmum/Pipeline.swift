import SwiftUI

enum FlowGeometry {
    /// How long a pulse takes to cross a diagram, and how long after a request its reply sets off.
    static let lifetime: TimeInterval = 2.4
    static let replyDelay: TimeInterval = 1.2
}

/// The live flow as two Sankey diagrams on shared columns.
///
///   Requests out:   clients ─▶ keepmum scans ─▶ clean / masked / observed / blocked ┆─▶ providers
///   Replies back:   clients ◀─ keepmum restores (values put back / nothing to) ◀─┆── providers
///
/// Band width is the number of requests in the window. Each live request sends a dot along its
/// route: the dot takes each band's color, so it visibly changes at keepmum.
struct FlowBandsView: View {
    var flows: Flows
    var pulses: [Pulse]
    var live: Bool
    /// The pointer, while it is over the diagram: what is under it is highlighted.
    @State var hover: CGPoint?

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: !live || pulses.isEmpty)) { tl in
            Canvas { context, size in
                FlowBands(flows: flows, size: size).draw(&context, now: tl.date, pulses: pulses, hover: hover)
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
    }
    struct Link {
        var from: String
        var to: String
        var value: Int
        var color: Color
        var t: CGFloat = 0     // thickness
        var y0: CGFloat = 0    // top of the band at its source
        var y1: CGFloat = 0    // top of the band at its target
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
        // One scale for both sides of every node: the fullest column fills the height.
        var scale = CGFloat.greatestFiniteMagnitude
        for (_, col) in byCol {
            let total = col.reduce(0) { $0 + max(inSum[$1.id] ?? 0, outSum[$1.id] ?? 0) }
            let room = height - gap * CGFloat(max(col.count - 1, 0))
            if total > 0 { scale = min(scale, room / CGFloat(total)) }
        }
        if scale == .greatestFiniteMagnitude { scale = 0 }
        func thick(_ v: Int) -> CGFloat { v > 0 ? max(CGFloat(v) * scale, 1.5) : 0 }

        var links = links.filter { $0.value > 0 }
        for i in links.indices { links[i].t = thick(links[i].value) }
        var nodes = [String: Node]()
        for (c, col) in byCol {
            var y = top
            for var n in col {
                let h = max(links.filter { $0.to == n.id }.reduce(0) { $0 + $1.t },
                            links.filter { $0.from == n.id }.reduce(0) { $0 + $1.t }, 2)
                n.rect = CGRect(x: xs[c], y: y, width: Self.bar, height: h)
                nodes[n.id] = n
                y += h + gap
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
        self.nodes = nodes
        self.links = links
    }

    /// Band ends: the side of each bar that faces the other.
    func ends(_ l: Link) -> (CGFloat, CGFloat) {
        let s = nodes[l.from]!.rect, d = nodes[l.to]!.rect
        return d.minX > s.minX ? (s.maxX, d.minX) : (s.minX, d.maxX)
    }

    func band(_ l: Link) -> Path {
        let (x0, x1) = ends(l)
        let xm = (x0 + x1) / 2
        var p = Path()
        p.move(to: CGPoint(x: x0, y: l.y0))
        p.addCurve(to: CGPoint(x: x1, y: l.y1), control1: CGPoint(x: xm, y: l.y0), control2: CGPoint(x: xm, y: l.y1))
        p.addLine(to: CGPoint(x: x1, y: l.y1 + l.t))
        p.addCurve(to: CGPoint(x: x0, y: l.y0 + l.t), control1: CGPoint(x: xm, y: l.y1 + l.t), control2: CGPoint(x: xm, y: l.y0 + l.t))
        p.closeSubpath()
        return p
    }

    /// A point on a band's centre line, f from 0 (source) to 1 (target).
    func point(_ l: Link, _ f: CGFloat) -> CGPoint {
        let (x0, x1) = ends(l)
        let xm = (x0 + x1) / 2, a = l.y0 + l.t / 2, b = l.y1 + l.t / 2
        let u = 1 - f
        let x = u * u * u * x0 + 3 * u * u * f * xm + 3 * u * f * f * xm + f * f * f * x1
        let y = u * u * u * a + 3 * u * u * f * a + 3 * u * f * f * b + f * f * f * b
        return CGPoint(x: x, y: y)
    }

    func link(_ from: String, _ to: String) -> Link? { links.first { $0.from == from && $0.to == to } }
}

// MARK: - The two diagrams

private struct FlowBands {
    let flows: Flows
    let size: CGSize
    let out: BandLayout
    let back: BandLayout
    let xs: [CGFloat]
    let outTop: CGFloat, backTop: CGFloat, dashX: CGFloat

    static let results = ["clean", "masked", "observed", "failed", "blocked"]

    init(flows: Flows, size: CGSize) {
        self.flows = flows
        self.size = size
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
        var nodes = clients.map { BandLayout.Node(id: "c:" + $0, col: 0, color: Self.clientColor($0)) }
        nodes.append(.init(id: "keepmum", col: 1, color: Theme.text))
        nodes += Self.results.map { BandLayout.Node(id: "r:" + $0, col: 2, color: Self.resultColor($0)) }
        nodes += providers.map { BandLayout.Node(id: "p:" + $0, col: 3, color: Self.providerColor($0)) }
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
        var bnodes = providers.map { BandLayout.Node(id: "p:" + $0, col: 3, color: Self.providerColor($0)) }
        bnodes += [BandLayout.Node(id: "restored", col: 1, color: Theme.restored),
                   BandLayout.Node(id: "plain", col: 1, color: Self.resultColor("clean"))]
        bnodes += clients.map { BandLayout.Node(id: "c:" + $0, col: 0, color: Self.clientColor($0)) }
        var blinks: [BandLayout.Link] = []
        for p in providers {
            for (node, r) in [("restored", true), ("plain", false)] {
                let n = flows.back.filter { $0.provider == p && $0.restored == r }.reduce(0) { $0 + $1.n }
                blinks.append(.init(from: "p:" + p, to: node, value: n, color: r ? Theme.restored : Self.resultColor("clean")))
            }
        }
        for c in clients {
            for (node, r) in [("restored", true), ("plain", false)] {
                let n = flows.back.filter { $0.client == c && $0.restored == r }.reduce(0) { $0 + $1.n }
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
        default: clientColor("other")
        }
    }

    static func resultColor(_ kind: String) -> Color {
        switch kind {
        case "masked": Theme.masked
        case "observed": Theme.warning
        case "blocked": Theme.blocked
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
        return nodes
    }

    private func totals(_ id: String) -> (Int, String) {
        let key = String(id.dropFirst(2))
        if id.hasPrefix("c:") { return (flows.clients.first { $0.id == key }?.sent ?? 0, "requests") }
        if id.hasPrefix("p:") { return (flows.providers.first { $0.id == key }?.replies ?? 0, "requests") }
        if id.hasPrefix("r:") { return (flows.out.filter { $0.kind == key }.reduce(0) { $0 + $1.n }, "requests") }
        if id == "restored" { return (flows.back.filter(\.restored).reduce(0) { $0 + $1.n }, "replies") }
        if id == "plain" { return (flows.back.filter { !$0.restored }.reduce(0) { $0 + $1.n }, "replies") }
        return (flows.clients.reduce(0) { $0 + $1.sent }, "scanned")
    }

    private func name(_ id: String) -> String {
        let key = String(id.dropFirst(2))
        if id.hasPrefix("c:") { return flows.clients.first { $0.id == key }?.name ?? key }
        if id.hasPrefix("p:") { return flows.providers.first { $0.id == key }?.name ?? key }
        if id.hasPrefix("r:") { return key.prefix(1).uppercased() + key.dropFirst() }
        return ["keepmum": "keepmum", "restored": "Real values put back", "plain": "Nothing to put back"][id] ?? id
    }

    func draw(_ ctx: inout GraphicsContext, now: Date, pulses: [Pulse], hover: CGPoint? = nil) {
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
        drawPulses(&ctx, now: now, pulses: pulses)
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
            ctx.draw(Text("\(flows.unresolved) placeholders could not be restored").font(.km(.caption).weight(.semibold))
                .foregroundStyle(Theme.blocked), at: CGPoint(x: size.width, y: backTop - 14), anchor: .trailing)
        }
    }

    private func labels(_ ctx: inout GraphicsContext) {
        let pname = Dictionary(uniqueKeysWithValues: flows.providers.map { ($0.id, $0.name) })
        func sum(_ xs: [Int]) -> Int { xs.reduce(0, +) }

        // (node rect, title, detail, title color) per column, then placed without overlaps.
        typealias L = (CGRect, String, String, Color)
        var leftOut: [L] = [], leftBack: [L] = [], mid: [L] = [], rightOut: [L] = [], rightBack: [L] = [], restore: [L] = []
        for c in flows.clients {
            if let n = out.nodes["c:" + c.id] { leftOut.append((n.rect, c.name, c.sent.compact, Theme.text)) }
            if let n = back.nodes["c:" + c.id] {
                let got = sum(flows.back.filter { $0.client == c.id }.map(\.n))
                let values = sum(flows.back.filter { $0.client == c.id }.map(\.values))
                leftBack.append((n.rect, c.name, values > 0 ? "\(got.compact) · \(values.compact) restored" : got.compact, Theme.text))
            }
        }
        let detail = ["clean": "sent as is", "masked": "as placeholders", "observed": "sent unmasked", "failed": "upstream failed",
                      "blocked": "never sent"]
        let title = ["clean": "Clean", "masked": "Masked", "observed": "Observed", "failed": "Failed", "blocked": "Blocked"]
        let tint: [String: Color] = ["masked": Theme.masked, "blocked": Theme.blocked, "observed": Self.resultColor("observed")]
        for r in Self.results {
            guard let n = out.nodes["r:" + r] else { continue }
            let v = sum(flows.out.filter { $0.kind == r }.map(\.n))
            mid.append((n.rect, "\(title[r]!) \(v.compact)", detail[r]!, tint[r] ?? Theme.text))
        }
        for (id, n) in out.nodes where id.hasPrefix("p:") {
            let p = String(id.dropFirst(2))
            rightOut.append((n.rect, pname[p] ?? p, "\(sum(flows.out.filter { $0.provider == p }.map(\.n)).compact) sent", Theme.text))
        }
        for (id, n) in back.nodes where id.hasPrefix("p:") {
            let p = String(id.dropFirst(2))
            rightBack.append((n.rect, pname[p] ?? p, "\(sum(flows.back.filter { $0.provider == p }.map(\.n)).compact) replies", Theme.text))
        }
        if let n = back.nodes["restored"] {
            let replies = sum(flows.back.filter(\.restored).map(\.n)), values = sum(flows.back.map(\.values))
            restore.append((n.rect, "Real values put back", "\(replies.compact) replies · \(values.compact) values", Theme.restored))
        }
        if let n = back.nodes["plain"] {
            restore.append((n.rect, "Nothing to put back", "\(sum(flows.back.filter { !$0.restored }.map(\.n)).compact) replies", Theme.text))
        }

        func place(_ items: [L], x: CGFloat, anchor: UnitPoint) {
            var lastY = -CGFloat.infinity
            for (rect, t, d, color) in items.sorted(by: { $0.0.midY < $1.0.midY }) {
                let y = max(rect.midY - 8, lastY + 30)
                lastY = y
                ctx.draw(Text(t).font(.km(.callout).weight(.semibold)).foregroundStyle(color), at: CGPoint(x: x, y: y), anchor: anchor)
                ctx.draw(Text(d).font(.km(.caption)).foregroundStyle(Theme.textSecondary), at: CGPoint(x: x, y: y + 14), anchor: anchor)
            }
        }
        place(leftOut, x: xs[0] - 10, anchor: .trailing)
        place(leftBack, x: xs[0] - 10, anchor: .trailing)
        place(mid, x: xs[2] + BandLayout.bar + 8, anchor: .leading)
        place(restore, x: xs[1] + BandLayout.bar + 8, anchor: .leading)
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

    private func drawPulses(_ ctx: inout GraphicsContext, now: Date, pulses: [Pulse]) {
        for pulse in pulses {
            let age = now.timeIntervalSince(pulse.born)
            guard age >= 0, age < FlowGeometry.lifetime else { continue }
            let (layout, route): (BandLayout, [(String, String)]) = switch pulse.kind {
            case .out(let r):
                (out, [("c:" + pulse.client, "keepmum"), ("keepmum", "r:" + r)] + (r == "blocked" || r == "failed" ? [] : [("r:" + r, "p:" + pulse.provider)]))
            case .back(let restored):
                (back, [("p:" + pulse.provider, restored ? "restored" : "plain"), (restored ? "restored" : "plain", "c:" + pulse.client)])
            }
            let links = route.compactMap { layout.link($0.0, $0.1) }
            guard links.count == route.count, !links.isEmpty else { continue }
            let progress = CGFloat(age / FlowGeometry.lifetime) * CGFloat(links.count)
            let i = min(Int(progress), links.count - 1)
            let p = layout.point(links[i], min(progress - CGFloat(i), 1))
            let fade = min(1, (FlowGeometry.lifetime - age) / 0.3)
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 7, y: p.y - 7, width: 14, height: 14)), with: .color(links[i].color.opacity(0.18 * fade)))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 3.5, y: p.y - 3.5, width: 7, height: 7)), with: .color(links[i].color.opacity(fade)))
        }
    }
}

/// A coding agent's color, as the flow diagrams draw it.
func clientColor(_ id: String) -> Color { FlowBands.clientColor(id) }
