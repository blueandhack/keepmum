import ImageIO
import SwiftUI
import UniformTypeIdentifiers

/// The app tour video: the app's own views, on preview data, over a drawn desktop, with a cursor,
/// captions and a camera that moves in on what is being shown. Frame by frame, as PNGs for ffmpeg:
///
///   Keepmum --tour dir              every frame, dir/00000.png on, at 30 fps
///   Keepmum --tour dir 12.5 31      those moments only, to check them
///   Keepmum --tour-stills dir       keepmum.app's tour: its screens as stills, and its timing (stills(dir:))
///
/// The window is built from the dashboard's own parts (Sidebar and each page), as Dashboard lays
/// them out, so a page can open on a selected row or a sample on its way. What moves (the
/// Overview's pulses, the map's sample, the review's clock) is drawn as of `tourClock`.
@MainActor
enum Tour {
    static let fps = 30.0
    static let duration = 58.0
    /// The frame, in pixels, and the desktop it shows, in points: the desktop is scaled up to fill it.
    static let frame = CGSize(width: 1920, height: 1080)
    static let desk = CGSize(width: 1360, height: 765)
    static var base: CGFloat { frame.width / desk.width }
    static let menuBar: CGFloat = 24
    /// The dashboard window, where it sits on the desktop, and the turn window over it.
    static let window = CGRect(x: 90, y: 44, width: 1180, height: 700)
    static let review = CGRect(x: 160, y: 46, width: 1040, height: 690)
    /// The menu bar panel, under the menu bar icon.
    static let iconX: CGFloat = 1128
    static let panel = CGPoint(x: 1016, y: 30)
    /// How far the map page scrolls to show its sample buttons: down, then back up to follow the sample.
    static let mapScroll: CGFloat = 420
    static let scrollDown = (30.2, 31.2), scrollUp = (32.6, 33.4)
    /// The dashboard's content area, in the window: right of the sidebar and its hairline, inside its padding.
    static let content = CGRect(x: 208 + 1 + Space.xl, y: Space.xl, width: window.width - 209 - 2 * Space.xl,
                                height: window.height - 2 * Space.xl)
    /// When each window is on screen: the dashboard fades in and stays; the turn window and the menu
    /// panel come and go (start, end, fade).
    static let windowIn = (9.0, 9.5)
    static let reviewShown = (41.0, 51.4, 0.4)
    static let menuShown = (4.85, 9.2, 0.25)
    /// The frame's own parts: the preview-data note, the title card going, the closing card coming.
    static let previewShown = (3.2, 52.6, 0.5)
    static let titleOut = (2.6, 3.3)
    static let closingIn = (52.4, 53.2)

    static func run(dir: String, at moments: [Double]) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSApp.appearance = NSAppearance(named: .aqua)
        let tour = Tour.State()
        if moments.isEmpty {
            let frames = Int(duration * fps)
            for n in 0..<frames {
                write(tour.frame(at: Double(n) / fps), to: String(format: "%@/%05d.png", dir, n))
                if n.isMultiple(of: 150) { print("frame \(n)/\(frames)") }
            }
        } else {
            for t in moments { write(tour.frame(at: t), to: String(format: "%@/t%05.1f.png", dir, t)) }
        }
    }

    private static func write(_ image: CGImage, to path: String) {
        guard let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, UTType.png.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(dest, image, nil)
        CGImageDestinationFinalize(dest)
    }

    // MARK: Stills, for keepmum.app

    /// keepmum.app's tour (site/tour.js): the screens the tour shows, as stills, and its timing, as
    /// tour.json; mac/tour-stills.sh makes them WebP and site/tour-data.js. The page draws the
    /// desktop, the camera, the cursor and the captions itself, from the same keys as the video, so
    /// this file stays the source of both. What moves inside a screen (the Overview's pulses, the
    /// map's sample, the review's clock) is a still, or a few in turn, as of the moment each names.
    static func stills(dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSApp.appearance = NSAppearance(named: .aqua)
        let tour = Tour.State()
        var shots: [[String: Any]] = []
        func save(_ image: NSImage, _ name: String) {
            guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fatalError("tour: no still \(name)") }
            write(cg, to: "\(dir)/\(name).png")
        }
        func rendered(_ view: some View, scale: CGFloat) -> NSImage {
            let renderer = ImageRenderer(content: view.environment(\.colorScheme, .light).environment(\.isSnapshot, true))
            renderer.scale = scale
            return renderer.nsImage ?? NSImage()
        }
        /// A still of a window (the dashboard, the map's content area, or the turn window), drawn as
        /// of `at`, on screen from `from` to `to`. The scale is how far the camera moves in on it. A
        /// map is drawn without what moves on it (`quiet`): the page draws that.
        let quiet = TourReport(origin: tour.start) { _, _ in }
        func shot(_ kind: String, _ name: String, at: Double, from: Double, to: Double, scale: CGFloat, map: Bool = false) {
            let report = map ? quiet : nil
            switch kind {
            case "window": save(tour.render(tour.page(at: at).environment(\.tourReport, report), size: window.size, scale: scale, t: at), name)
            case "content": save(tour.render(tour.mapContent(at: at).environment(\.tourReport, report),
                                             size: CGSize(width: content.width, height: content.height + mapScroll), scale: scale, t: at), name)
            default: save(tour.render(tour.turnView(at: at), size: review.size, scale: scale, t: at), name)
            }
            shots.append(["kind": kind, "name": name, "from": from, "to": to, "map": map])
        }
        // The Overview without its pulses: they come with the timing, and the page draws them moving.
        var flow: [String: Any] = [:]
        let report = TourReport(origin: tour.start) { frame, data in
            flow = data.merging(["frame": [frame.minX, frame.minY, frame.width, frame.height]]) { a, _ in a }
        }
        save(tour.render(tour.page(at: 15).environment(\.tourReport, report), size: window.size, scale: 2.5, t: 15), "overview")
        shots.append(["kind": "window", "name": "overview", "from": windowIn.0, "to": 20.5, "flow": true])
        shot("window", "logs", at: 21.5, from: 20.5, to: 22.9, scale: 2.25)
        shot("window", "logs-open", at: 24, from: 22.9, to: 28.9, scale: 2.25)
        shot("content", "map-top", at: 30, from: 28.9, to: 32.2, scale: 2.25, map: true)
        shot("content", "map-sample", at: 33, from: 32.2, to: scrollUp.1, scale: 2.25, map: true)
        for (i, at) in [33.6, 35.2, 36.8, 38.4, 40.0].enumerated() {
            shot("window", "map-\(i + 1)", at: at, from: i == 0 ? 28.9 : at - 0.6, to: i == 4 ? duration : at + 1.0, scale: 2.25, map: true)
        }
        // What moves on the map, from its content area as the sample runs (where the page draws it).
        var map: [String: Any] = [:]
        let mapReport = TourReport(origin: tour.start) { frame, data in
            map = data.merging(["frame": [frame.minX, frame.minY, frame.width, frame.height]]) { a, _ in a }
        }
        _ = tour.render(tour.mapContent(at: 33).environment(\.tourReport, mapReport),
                        size: CGSize(width: content.width, height: content.height + mapScroll), scale: 1, t: 33)
        shot("review", "review-drafting", at: 42.5, from: reviewShown.0, to: 43.6, scale: 2)
        shot("review", "review-summary", at: 44, from: 43.6, to: 44.6, scale: 2)
        shot("review", "review-original", at: 45.4, from: 44.6, to: 46.4, scale: 2)
        shot("review", "review-rewrite", at: 47.5, from: 46.4, to: duration, scale: 2)
        let menu = tour.menuImage(scale: 2)
        save(menu, "menu")
        for on in [0.0, 1.0] {
            let bar = Desktop(t: 0, app: nil, appOn: 0, turn: nil, turnOn: 0, menu: nil, menuOn: on, pointer: .zero, click: nil).menuBar
            save(rendered(bar, scale: 2.5), on > 0 ? "menubar-open" : "menubar")
        }
        save(rendered(AppIcon().frame(width: 1024, height: 1024).scaleEffect(200 / 1024).frame(width: 200, height: 200), scale: 2), "icon")

        let point = { (p: CGPoint) -> [CGFloat] in [p.x, p.y] }
        let rect = { (r: CGRect) -> [CGFloat] in [r.minX, r.minY, r.width, r.height] }
        let timing: [String: Any] = [
            "duration": duration, "desk": [desk.width, desk.height], "frame": [frame.width, frame.height], "menuBar": menuBar,
            "window": rect(window), "review": rect(review), "content": rect(content), "panel": point(panel),
            "menu": [menu.size.width, menu.size.height],
            "cursor": cursor.map { [$0.0, $0.1.x, $0.1.y] },
            "clicks": clicks,
            "camera": camera.map { [$0.0, $0.1.0, $0.1.1.x, $0.1.1.y] },
            "captions": captions.map { [$0.0, $0.1, $0.2] as [Any] },
            "scroll": [[scrollDown.0, 0], [scrollDown.1, mapScroll], [scrollUp.0, mapScroll], [scrollUp.1, 0]],
            "windowIn": [windowIn.0, windowIn.1],
            "reviewShown": [reviewShown.0, reviewShown.1, reviewShown.2],
            "menuShown": [menuShown.0, menuShown.1, menuShown.2],
            "previewShown": [previewShown.0, previewShown.1, previewShown.2],
            "titleOut": [titleOut.0, titleOut.1],
            "closingIn": [closingIn.0, closingIn.1],
            "stills": shots,
            "flow": flow,
            "map": map,
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: timing, options: [.prettyPrinted, .sortedKeys]) else { fatalError("tour: timing") }
        FileManager.default.createFile(atPath: "\(dir)/tour.json", contents: json)
    }

    // MARK: Timing

    static func clamp(_ x: Double) -> Double { min(1, max(0, x)) }
    static func ease(_ x: Double) -> Double { let c = clamp(x); return c * c * (3 - 2 * c) }
    /// 0 before `a`, 1 after `b`, eased between.
    static func p(_ t: Double, _ a: Double, _ b: Double) -> Double { ease((t - a) / (b - a)) }
    /// In over [a, a+f], out over [b-f, b].
    static func shown(_ t: Double, _ a: Double, _ b: Double, fade f: Double = 0.4) -> Double { min(p(t, a, a + f), 1 - p(t, b - f, b)) }

    /// Where a keyframed value is at `t`: held at each key, eased from one to the next.
    static func track<V>(_ keys: [(Double, V)], _ t: Double, _ mix: (V, V, Double) -> V) -> V {
        guard var (t0, v0) = keys.first else { fatalError("no keys") }
        for (t1, v1) in keys.dropFirst() {
            if t < t1 { return mix(v0, v1, ease((t - t0) / max(t1 - t0, 0.001))) }
            (t0, v0) = (t1, v1)
        }
        return v0
    }

    static func mix(_ a: CGPoint, _ b: CGPoint, _ k: Double) -> CGPoint {
        CGPoint(x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k)
    }

    // MARK: The script

    /// A point in the dashboard window, on the desktop.
    static func inWindow(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: window.minX + x, y: window.minY + y) }
    static func inReview(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: review.minX + x, y: review.minY + y) }

    /// Sidebar rows, in the window (Dashboard's layout).
    static let logsRow = inWindow(66, 218)
    static let mapRow = inWindow(90, 305)
    /// The menu panel's Open Dashboard button, and the menu bar icon.
    static let openDashboard = CGPoint(x: panel.x + 88, y: panel.y + 488)
    static let icon = CGPoint(x: iconX, y: menuBar / 2)
    /// A Logs row, the map's sample chip, and the turn window's answer and send buttons.
    static let logsHit = inWindow(640, 352)
    static let sampleChip = inWindow(880, 362)
    static let keepOriginal = inReview(438, 354)
    static let rewrite = inReview(306, 354)
    static let send = inReview(950, 664)

    static let clicks: [Double] = [4.7, 8.8, 20.5, 22.9, 28.9, 32.2, 44.6, 46.4, 50.6]

    static let cursor: [(Double, CGPoint)] = [
        (0, CGPoint(x: 820, y: 470)), (3.4, CGPoint(x: 820, y: 470)), (4.6, icon),
        (7.4, icon), (8.6, openDashboard),
        (10, openDashboard), (11.2, inWindow(900, 520)), (19.4, inWindow(900, 520)), (20.4, logsRow),
        (21.6, logsRow), (22.8, logsHit),
        (27.6, logsHit), (28.8, mapRow),
        (29.8, mapRow), (32.0, sampleChip),
        (33.0, sampleChip), (34.2, inWindow(1060, 640)), (40.6, inWindow(1060, 640)), (43.2, inReview(600, 520)), (44.5, keepOriginal),
        (45.4, keepOriginal), (46.3, rewrite),
        (48.6, rewrite), (50.5, send), (58, send),
    ]

    /// The camera: how far in, and the desktop point at the frame's center.
    static let camera: [(Double, (CGFloat, CGPoint))] = [
        (0, (1, center)), (3.4, (1, center)), (4.4, (1.15, CGPoint(x: 1100, y: 320))),
        (8.0, (1.15, CGPoint(x: 1100, y: 320))), (9.2, (1, center)),
        (11.0, (1, center)), (12.6, (1.5, inWindow(760, 470))),
        (18.8, (1.5, inWindow(760, 470))), (20.0, (1.15, center)),
        (21.6, (1.25, inWindow(690, 330))), (23.0, (1.3, inWindow(690, 300))),
        (27.6, (1.3, inWindow(690, 300))), (28.6, (1, center)),
        (33.2, (1, center)), (34.4, (1.3, inWindow(690, 330))),
        (40.4, (1.3, inWindow(690, 330))), (41.6, (1, center)),
        (42.6, (1, center)), (44.0, (1.2, inReview(560, 380))),
        (48.4, (1.2, inReview(560, 400))), (49.6, (1, center)),
        (52.0, (1, center)),
    ]
    static var center: CGPoint { CGPoint(x: desk.width / 2, y: desk.height / 2) }

    static let captions: [(Double, Double, String)] = [
        (3.4, 8.8, "keepmum runs in your menu bar: its background service, and the agents it protects."),
        (9.8, 14.6, "The Overview: every request out and every reply back, live."),
        (14.6, 20.2, "Blue went out masked: the provider got a placeholder, never the value."),
        (20.6, 28.2, "Logs: what the checks flagged, and every masked value, searchable."),
        (29.2, 33.6, "The Protection map: every check, where it acts on a request."),
        (33.6, 40.8, "Send a made-up sample, and watch the route your settings give it."),
        (41.4, 46.2, "When a check flags part of a turn, you look at it before it goes."),
        (46.2, 50.3, "Your local model writes what goes in its place. Then send the turn, or keep it on your Mac."),
    ]

    // MARK: Data and frames

    @MainActor final class State {
        let store = StatsStore()
        let engine = Engine.preview()
        let mapEngine = Engine.preview(bothLocal: true)
        let start = Date()
        var hit: String?
        var menuCache: (CGFloat, NSImage)?

        init() {
            store.sampleMode = true
            var feed = SampleFeed()
            store.snapshot = feed.next()
            // Preview data: the video says so itself, and the dashboard's own banner is left out.
            store.connection = .live
            store.injectFlows(SampleFeed.flows(window: 3600))
            store.injectTools(ToolServer.samples)
            // The jailbreak you typed: the turn the review at the end is about.
            hit = (store.snapshot.guardHits.first { $0.check == "harm" } ?? store.snapshot.guardHits.first)?.id
            store.injectOpen([("masked", "claude-code", 5.0), ("clean", "codex", 3.0)].map { kind, client, age in
                Pulse(kind: .out(kind), client: client, provider: client == "codex" ? "openai" : "anthropic",
                      born: start.addingTimeInterval(9 - age))
            })
            // A steady stream from before the window opens: each request, then its reply.
            var pulses: [Pulse] = []
            let kinds = ["masked", "clean", "masked", "clean", "masked", "blocked", "clean", "masked", "clean", "clean"]
            let clients = ["claude-code", "claude-code", "codex", "claude-code", "pi", "claude-code", "codex"]
            for k in 0..<70 {
                let kind = kinds[k % kinds.count], client = clients[k % clients.count]
                let provider = client == "codex" ? "openai" : "anthropic"
                let born = start.addingTimeInterval(6 + Double(k) * 0.42 + Double((k * 7) % 5) * 0.06)
                pulses.append(Pulse(kind: .out(kind), client: client, provider: provider, born: born))
                if kind != "blocked" {
                    pulses.append(Pulse(kind: .back(restored: kind == "masked", result: kind), client: client, provider: provider, born: born))
                }
            }
            store.pulses = pulses
        }

        func clock(_ t: Double) -> Date { start.addingTimeInterval(t) }

        /// The dashboard as Dashboard draws it, on a given page's view.
        func dashboard(_ page: Page, _ content: some View) -> some View {
            HStack(spacing: 0) {
                Sidebar(store: store, place: .page(page)).frame(width: 208)
                Hairline(vertical: true)
                content
                    .frame(maxWidth: .infinity, minHeight: 0, maxHeight: .infinity, alignment: .topLeading)
                    .clipped()
                    .padding(Space.xl)
            }
            .followsPalette()
            .background(Theme.canvas)
            .coordinateSpace(name: TourReport.space)
        }

        func page(at t: Double) -> AnyView {
            if t < 20.5 { return AnyView(dashboard(.overview, OverviewPage(store: store))) }
            if t < 28.9 {
                return AnyView(dashboard(.logs, LogsPage(store: store, selectedHit: t >= 22.9 ? hit : nil, layout: .together)))
            }
            // Down to the sample buttons, a click, and back up to follow it along the map.
            let scroll = Tour.mapScroll * CGFloat(Tour.p(t, scrollDown.0, scrollDown.1) * (1 - Tour.p(t, scrollUp.0, scrollUp.1)))
            let sample: MapSample? = t >= 32.2 ? .jailbreak : nil
            let map = ProtectionMapPage(store: store, engine: mapEngine, sample: sample, started: clock(32.2))
                .frame(height: 1400, alignment: .top).offset(y: -scroll)
            return AnyView(dashboard(.map, map))
        }

        /// The map page alone, as the content area shows it unscrolled, and tall enough to scroll
        /// through: keepmum.app's tour scrolls it itself.
        func mapContent(at t: Double) -> some View {
            ProtectionMapPage(store: store, engine: mapEngine, sample: t >= 32.2 ? .jailbreak : nil, started: clock(32.2))
                .frame(height: 1400, alignment: .top)
                .frame(width: Tour.content.width, height: Tour.content.height + Tour.mapScroll, alignment: .top)
                .clipped()
                .followsPalette()
                .background(Theme.canvas)
                .coordinateSpace(name: TourReport.space)
        }

        /// The turn window: you keep the original of one piece for a moment, then rewrite it.
        func turnView(at t: Double) -> some View {
            TurnReviewView(center: .shared, turn: turn(at: t), choices: t >= 44.6 && t < 46.4 ? ["1": "original"] : [:])
                .background(Theme.canvas)
        }

        func menuImage(scale: CGFloat) -> NSImage {
            if menuCache?.0 != scale {
                let renderer = ImageRenderer(content: MenuPanel(store: store, engine: engine)
                    .environment(\.colorScheme, .light).environment(\.isSnapshot, true))
                renderer.scale = scale
                menuCache = (scale, renderer.nsImage ?? NSImage())
            }
            return menuCache?.1 ?? NSImage()
        }

        func turn(at t: Double) -> PendingTurn {
            var turn = PendingTurn.sample
            turn.created = clock(41).timeIntervalSince1970
            turn.deadline = turn.created + 120
            // The last piece's summary is written while you look.
            if t >= 43.6 {
                turn.pieces[2].drafting = false
                turn.pieces[2].kind = "summary"
                turn.pieces[2].draft = "notes.txt, with a TODO. The file also held text addressed to AI assistants, left out."
            }
            return turn
        }

        func render(_ view: some View, size: CGSize, scale: CGFloat, t: Double) -> NSImage {
            let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height)
                .environment(\.colorScheme, .light).environment(\.isSnapshot, true).environment(\.tourClock, clock(t)))
            renderer.scale = scale
            return renderer.nsImage ?? NSImage()
        }

        func frame(at t: Double) -> CGImage {
            let (zoom, focus) = Tour.track(camera, t) { a, b, k in (a.0 + (b.0 - a.0) * k, Tour.mix(a.1, b.1, k)) }
            // Each window is drawn at the density it shows at, so text stays sharp as the camera moves in.
            let scale = max(1.5, (base * zoom * 4).rounded(.up) / 4)
            let windowOn = p(t, windowIn.0, windowIn.1)
            let reviewOn = shown(t, reviewShown.0, reviewShown.1, fade: reviewShown.2)
            let menuOn = shown(t, menuShown.0, menuShown.1, fade: menuShown.2)
            let app = windowOn > 0 ? render(page(at: t), size: Tour.window.size, scale: scale, t: t) : nil
            let turnWindow = reviewOn > 0 ? render(turnView(at: t), size: Tour.review.size, scale: scale, t: t) : nil
            let menu = menuOn > 0 ? menuImage(scale: scale) : nil
            let pointer = Tour.track(cursor, t) { a, b, k in Tour.mix(a, b, k) }
            let click = clicks.last { $0 <= t }.map { t - $0 }

            let scene = Desktop(t: t, app: app, appOn: windowOn, turn: turnWindow, turnOn: reviewOn, menu: menu, menuOn: menuOn,
                                pointer: pointer, click: click)
            let z = base * zoom
            // Keep the camera on the desktop: never past its edges.
            let half = CGSize(width: Tour.frame.width / 2 / z, height: Tour.frame.height / 2 / z)
            let fx = min(max(focus.x, half.width), desk.width - half.width)
            let fy = min(max(focus.y, half.height), desk.height - half.height)
            let composed = ZStack(alignment: .topLeading) {
                scene
                    .frame(width: desk.width, height: desk.height)
                    .scaleEffect(z, anchor: .topLeading)
                    .offset(x: Tour.frame.width / 2 - fx * z, y: Tour.frame.height / 2 - fy * z)
                Overlay(t: t)
            }
            .frame(width: Tour.frame.width, height: Tour.frame.height, alignment: .topLeading)
            .clipped()
            .environment(\.colorScheme, .light)
            let renderer = ImageRenderer(content: composed)
            renderer.scale = 1
            renderer.proposedSize = ProposedViewSize(Tour.frame)
            guard let image = renderer.cgImage else { fatalError("tour: no frame at \(t)") }
            return image
        }
    }

    // MARK: The desktop

    struct Desktop: View {
        var t: Double
        var app: NSImage?
        var appOn: Double
        var turn: NSImage?
        var turnOn: Double
        var menu: NSImage?
        var menuOn: Double
        var pointer: CGPoint
        var click: Double?

        var body: some View {
            ZStack(alignment: .topLeading) {
                LinearGradient(colors: [Color(red: 0.97, green: 0.89, blue: 0.85), Color(red: 0.89, green: 0.91, blue: 0.96)],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                if let app {
                    windowed(app, Tour.window, sidebar: true)
                        .opacity(appOn).scaleEffect(0.97 + 0.03 * appOn, anchor: .center)
                }
                if let turn {
                    windowed(turn, Tour.review, sidebar: false)
                        .opacity(turnOn).scaleEffect(0.97 + 0.03 * turnOn, anchor: .center)
                }
                menuBar
                if let menu {
                    Image(nsImage: menu).resizable().interpolation(.high)
                        .frame(width: menu.size.width, height: menu.size.height)
                        .background(Color.white.opacity(0.96))
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.black.opacity(0.1)))
                        .shadow(color: .black.opacity(0.18), radius: 20, y: 8)
                        .offset(x: Tour.panel.x, y: Tour.panel.y - 6 * (1 - menuOn))
                        .opacity(menuOn)
                }
                Pointer(click: click).offset(x: pointer.x, y: pointer.y)
            }
            .frame(width: Tour.desk.width, height: Tour.desk.height, alignment: .topLeading)
        }

        /// A window: the image on rounded corners, a hairline, a shadow, and its three buttons.
        func windowed(_ image: NSImage, _ rect: CGRect, sidebar: Bool) -> some View {
            Image(nsImage: image).resizable().interpolation(.high)
                .frame(width: rect.width, height: rect.height)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay(alignment: .topLeading) {
                    if sidebar {
                        HStack(spacing: 8) {
                            ForEach([0xFF5F57, 0xFEBC2E, 0x28C840], id: \.self) { hex in
                                Circle().fill(Color(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                                                    blue: Double(hex & 0xFF) / 255))
                                    .overlay(Circle().strokeBorder(.black.opacity(0.12), lineWidth: 0.5))
                                    .frame(width: 12, height: 12)
                            }
                        }
                        .padding(.leading, 18).padding(.top, 18)
                    }
                }
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.black.opacity(0.14), lineWidth: 0.75))
                .shadow(color: .black.opacity(0.22), radius: 30, y: 14)
                .offset(x: rect.minX, y: rect.minY)
        }

        var menuBar: some View {
            HStack(spacing: 18) {
                Image(systemName: "apple.logo").font(.system(size: 14, weight: .medium))
                Spacer()
                Image(nsImage: Brand.menuBarActive).renderingMode(.template)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: 4).fill(.black.opacity(menuOn > 0 ? 0.12 : 0)))
                Image(systemName: "wifi").font(.system(size: 13, weight: .medium))
                Image(systemName: "battery.75percent").font(.system(size: 15))
                Text("Tue Nov 17  9:41").font(.system(size: 13, weight: .medium))
            }
            .foregroundStyle(Color(white: 0.1))
            .padding(.horizontal, 18)
            .frame(width: Tour.desk.width, height: Tour.menuBar)
            .background(.white.opacity(0.55))
        }
    }

    /// The arrow, pressed for a moment on a click, with a ring that spreads from it.
    struct Pointer: View {
        var click: Double?

        var body: some View {
            let k = click.map { Tour.clamp($0 / 0.45) } ?? 1
            let pressed = (click ?? 1) < 0.16
            ZStack(alignment: .topLeading) {
                Circle().stroke(Theme.accent.opacity(0.55 * (1 - k)), lineWidth: 2.5)
                    .frame(width: 10 + 34 * k, height: 10 + 34 * k)
                    .offset(x: -(5 + 17 * k), y: -(5 + 17 * k))
                Arrow().fill(.black).overlay(Arrow().stroke(.white, lineWidth: 1.4))
                    .frame(width: 13, height: 20)
                    .shadow(color: .black.opacity(0.3), radius: 2, y: 1)
                    .scaleEffect(pressed ? 0.88 : 1, anchor: .topLeading)
            }
        }
    }

    struct Arrow: Shape {
        func path(in r: CGRect) -> Path {
            let u = r.height / 20
            return Path { p in
                p.move(to: CGPoint(x: 0, y: 0))
                p.addLine(to: CGPoint(x: 0, y: 16.5 * u))
                p.addLine(to: CGPoint(x: 4 * u, y: 12.8 * u))
                p.addLine(to: CGPoint(x: 6.8 * u, y: 19 * u))
                p.addLine(to: CGPoint(x: 9.4 * u, y: 17.9 * u))
                p.addLine(to: CGPoint(x: 6.7 * u, y: 11.9 * u))
                p.addLine(to: CGPoint(x: 12 * u, y: 11.9 * u))
                p.closeSubpath()
            }
        }
    }

    // MARK: Title, captions, close

    /// What sits on the frame itself, not on the desktop: the title, captions, and the close.
    struct Overlay: View {
        var t: Double

        var body: some View {
            ZStack {
                ForEach(Array(Tour.captions.enumerated()), id: \.offset) { _, c in
                    let on = Tour.shown(t, c.0, c.1, fade: 0.35)
                    if on > 0 {
                        Text(c.2).font(.system(size: 34, weight: .semibold)).foregroundStyle(Theme.text)
                            .padding(.horizontal, 30).padding(.vertical, 16)
                            .background(Capsule().fill(.white.opacity(0.94)).shadow(color: .black.opacity(0.14), radius: 18, y: 6))
                            .opacity(on).offset(y: 8 * (1 - on))
                            .frame(maxHeight: .infinity, alignment: .bottom).padding(.bottom, 54)
                    }
                }
                Text("Preview data: made up, nothing real")
                    .font(.system(size: 18, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 14).padding(.vertical, 7)
                    .background(Capsule().fill(.white.opacity(0.8)))
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
                    .padding(24)
                    .opacity(Tour.shown(t, previewShown.0, previewShown.1, fade: previewShown.2))
                card(title: "keepmum on your Mac", lines: ["A tour of the app"]).opacity(1 - Tour.p(t, titleOut.0, titleOut.1))
                card(title: "Keep mum.", lines: ["Free and open source, for macOS", "keepmum.app"]).opacity(Tour.p(t, closingIn.0, closingIn.1))
            }
            .frame(width: Tour.frame.width, height: Tour.frame.height)
        }

        func card(title: String, lines: [String]) -> some View {
            VStack(spacing: 26) {
                AppIcon().frame(width: 1024, height: 1024).scaleEffect(200 / 1024).frame(width: 200, height: 200)
                Text(title).font(.system(size: 76, weight: .bold)).foregroundStyle(Theme.text)
                ForEach(lines, id: \.self) { line in
                    Text(line).font(.system(size: 34, weight: line.contains(".app") ? .semibold : .regular))
                        .foregroundStyle(line.contains(".app") ? Theme.accent : Theme.textSecondary)
                }
            }
            .frame(width: Tour.frame.width, height: Tour.frame.height)
            .background(Color(red: 0.957, green: 0.957, blue: 0.961))
        }
    }
}
