import AppKit
import SwiftUI

// A table whose columns the user sizes and picks, in pages, and the selected row's details beside
// it or in its place. Drawn in SwiftUI, as the rest of the app, so it looks the same in snapshots.

/// One column of a DataTable.
struct DataColumn: Identifiable {
    let id: String
    let title: String
    /// Its width, padding included, until the user drags it. The flexible column takes what the
    /// others leave, never less than `min`.
    var width: CGFloat
    var min: CGFloat = 44
    var alignment: Alignment = .leading
    var flexible = false
    /// The user may hide it (every column but the first).
    var hideable = true
    /// Hidden until the user shows it.
    var hiddenByDefault = false
}

/// What the user changed about a table's columns, kept in UserDefaults under `columns.<key>`.
private struct ColumnLayout: Codable, Equatable {
    var widths: [String: Double] = [:]
    var hidden: [String]?
    var shown: [String] = []
}

/// Opens the selected row's details. Always equal to another: it only sets ListDetail's state,
/// which an older copy sets just the same, so a new one needn't redraw the table.
struct OpenDetailsAction: Equatable {
    let action: () -> Void
    func callAsFunction() { action() }
    static func == (_: Self, _: Self) -> Bool { true }
}

extension EnvironmentValues {
    /// Set by ListDetail while the details take the table's place: opens them for the selected row.
    @Entry var openDetails: OpenDetailsAction? = nil
}

/// A table in pages of as many rows as fit, so it never scrolls up and down: the rows stay put
/// and the details open beside or in place of it (ListDetail).
struct DataTable<Item: Identifiable, Cell: View>: View where Item.ID == String {
    var key: String
    var columns: [DataColumn]
    var items: [Item]
    @Binding var selection: String?
    /// What to say when there are no items.
    var empty: String
    /// Rows a snapshot draws.
    var snapshotRows = 14
    /// Back to the first page when it changes, such as a filter or a search.
    var pageKey = ""
    /// Shown on the last page: loads older items.
    var footer: (() -> Void)? = nil
    @ViewBuilder var cell: (Item, String) -> Cell

    @AppStorage private var stored: String
    /// The column being dragged and its width, saved when the drag ends.
    @State private var drag: (id: String, start: CGFloat, width: CGFloat)?
    @State private var page = 0
    /// Rows per page: as many as fit the height.
    @State private var perPage = 20
    @FocusState private var focused: Bool
    @Environment(\.isSnapshot) private var isSnapshot
    @Environment(\.openDetails) private var openDetails

    static var cellPadding: CGFloat { Space.s - Space.xxs }
    /// Room at the end of the header for the columns menu.
    private static var menuRoom: CGFloat { 28 }
    static var headerHeight: CGFloat { 28 }
    static var pagerHeight: CGFloat { 40 }

    init(key: String, columns: [DataColumn], items: [Item], selection: Binding<String?>, empty: String,
         snapshotRows: Int = 14, pageKey: String = "", footer: (() -> Void)? = nil,
         @ViewBuilder cell: @escaping (Item, String) -> Cell) {
        self.key = key
        self.columns = columns
        self.items = items
        _selection = selection
        self.empty = empty
        self.snapshotRows = snapshotRows
        self.pageKey = pageKey
        self.footer = footer
        self.cell = cell
        _stored = AppStorage(wrappedValue: "", "columns.\(key)")
    }

    /// Rows that fit this height under the header and over the pager; never fewer than 3.
    static func rowsThatFit(_ height: CGFloat) -> Int {
        max(3, Int((height - headerHeight - 1 - pagerHeight) / (dataRowHeight + 1)))
    }

    static func pageCount(_ items: Int, _ perPage: Int) -> Int {
        max(1, (items + max(perPage, 1) - 1) / max(perPage, 1))
    }

    /// The page numbers to offer: all of them up to 7, else the first, the last and those around
    /// the current one, nil where some are left out.
    static func pageNumbers(_ current: Int, _ pages: Int) -> [Int?] {
        guard pages > 7 else { return Array(0..<pages) }
        let near = current < 4 ? Set(0...4) : current > pages - 5 ? Set(pages - 5..<pages) : Set(current - 1...current + 1)
        let shown = (near.union([0, pages - 1])).sorted()
        var out: [Int?] = []
        for n in shown {
            if case let last?? = out.last, n > last + 1 { out.append(nil) }
            out.append(n)
        }
        return out
    }

    private var layout: ColumnLayout {
        guard !isSnapshot, let data = stored.data(using: .utf8),
              let l = try? JSONDecoder().decode(ColumnLayout.self, from: data) else { return ColumnLayout() }
        return l
    }

    private func save(_ l: ColumnLayout) {
        guard let data = try? JSONEncoder().encode(l), let s = String(data: data, encoding: .utf8) else { return }
        stored = l == ColumnLayout() ? "" : s
    }

    private func isHidden(_ c: DataColumn, _ l: ColumnLayout) -> Bool {
        guard c.hideable else { return false }
        if l.hidden?.contains(c.id) == true { return true }
        return c.hiddenByDefault && !l.shown.contains(c.id)
    }

    private func toggle(_ c: DataColumn) {
        var l = layout
        var hidden = Set(l.hidden ?? [])
        var shown = Set(l.shown)
        if isHidden(c, l) {
            hidden.remove(c.id)
            if c.hiddenByDefault { shown.insert(c.id) }
        } else {
            hidden.insert(c.id)
            shown.remove(c.id)
        }
        l.hidden = hidden.sorted()
        l.shown = shown.sorted()
        save(l)
    }

    /// Each shown column's width for this much room: the user's, or the default, the flexible
    /// column taking the rest.
    private func widths(_ visible: [DataColumn], room: CGFloat, _ l: ColumnLayout) -> [String: CGFloat] {
        var out: [String: CGFloat] = [:]
        for c in visible where !c.flexible {
            out[c.id] = drag?.id == c.id ? drag!.width : l.widths[c.id].map { max(CGFloat($0), c.min) } ?? c.width
        }
        let used = out.values.reduce(0, +)
        for c in visible where c.flexible {
            out[c.id] = max(c.min, room - Self.menuRoom - used)
        }
        return out
    }

    var body: some View {
        let l = layout
        let visible = columns.filter { !isHidden($0, l) }
        GeometryReader { geo in
            let w = widths(visible, room: geo.size.width, l)
            let total = max(geo.size.width, visible.reduce(0) { $0 + (w[$1.id] ?? 0) } + Self.menuRoom)
            let fit = isSnapshot ? snapshotRows : Self.rowsThatFit(geo.size.height)
            let pages = Self.pageCount(items.count, fit)
            let p = min(page, pages - 1)
            VStack(spacing: 0) {
                grid(visible, w, total, Array(items.dropFirst(p * fit).prefix(fit)), scrolls: total > geo.size.width + 0.5)
                Spacer(minLength: 0)
                if !items.isEmpty { pager(p, pages, fit) }
            }
            .overlay(alignment: .topTrailing) { columnsMenu(l).frame(height: Self.headerHeight) }
            .onChange(of: fit, initial: true) { _, n in
                perPage = n
                page = min(page, Self.pageCount(items.count, n) - 1)
                follow(selection)
            }
        }
        .clipped()
        .focusable(!isSnapshot)
        .focused($focused)
        .focusEffectDisabled()
        .onKeyPress(.downArrow) { move(1) }
        .onKeyPress(.upArrow) { move(-1) }
        .onKeyPress(.leftArrow) { turn(-1) }
        .onKeyPress(.rightArrow) { turn(1) }
        .onKeyPress(.return) {
            guard selection != nil, let openDetails else { return .ignored }
            openDetails()
            return .handled
        }
        .onKeyPress(.escape) {
            guard selection != nil else { return .ignored }
            selection = nil
            return .handled
        }
        .onChange(of: selection) { _, s in follow(s) }
        .onChange(of: pageKey) { page = 0 }
    }

    /// The header and this page's rows; sideways scrolling when the columns are wider than the table.
    @ViewBuilder private func grid(_ visible: [DataColumn], _ w: [String: CGFloat], _ total: CGFloat,
                                   _ rows: [Item], scrolls: Bool) -> some View {
        let g = VStack(spacing: 0) {
            header(visible, w, total)
            ForEach(rows) { row($0, visible, w) }
            if items.isEmpty { EmptyNote(text: empty).padding(.horizontal, Self.cellPadding) }
        }
        .frame(width: total, alignment: .topLeading)
        if scrolls && !isSnapshot {
            ScrollView(.horizontal) { g }.scrollBounceBehavior(.basedOnSize).fixedSize(horizontal: false, vertical: true)
        } else {
            g
        }
    }

    /// Shows the page with the selected row on it.
    private func follow(_ s: String?) {
        guard let s, let i = items.firstIndex(where: { $0.id == s }) else { return }
        let target = i / max(perPage, 1)
        if target != page { page = target }
    }

    private func move(_ by: Int) -> KeyPress.Result {
        guard !items.isEmpty else { return .ignored }
        let first = min(page * perPage, items.count - 1)
        let i = selection.flatMap { s in items.firstIndex { $0.id == s } }
        let next = i.map { min(max($0 + by, 0), items.count - 1) } ?? (by > 0 ? first : min(first + perPage, items.count) - 1)
        selection = items[next].id
        return .handled
    }

    private func turn(_ by: Int) -> KeyPress.Result {
        let pages = Self.pageCount(items.count, perPage)
        let next = min(max(page + by, 0), pages - 1)
        guard next != page else { return .ignored }
        page = next
        return .handled
    }

    // MARK: Pager

    @ViewBuilder private func pager(_ p: Int, _ pages: Int, _ per: Int) -> some View {
        let start = p * per
        let end = min(start + per, items.count)
        VStack(spacing: 0) {
            HStack(spacing: Space.xs) {
                Text(verbatim: "\((start + 1).formatted())–\(end.formatted()) of \(items.count.formatted())")
                    .font(.km(.caption)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                Spacer(minLength: Space.s)
                if let footer, p == pages - 1 {
                    PillButton(title: "Load older", icon: "arrow.down", action: footer).padding(.trailing, Space.s)
                }
                if pages > 1 {
                    IconButton(symbol: "chevron.left", help: "Previous page (←)") { page = p - 1 }.disabled(p == 0)
                    ForEach(Array(Self.pageNumbers(p, pages).enumerated()), id: \.offset) { _, n in
                        if let n {
                            PageNumber(number: n + 1, current: n == p) { page = n }
                        } else {
                            Text(verbatim: "…").font(.km(.caption)).foregroundStyle(Theme.textTertiary).frame(width: 16)
                        }
                    }
                    IconButton(symbol: "chevron.right", help: "Next page (→)") { page = p + 1 }.disabled(p == pages - 1)
                }
            }
            .padding(.horizontal, Self.cellPadding)
            .frame(maxHeight: .infinity)
        }
        .frame(height: Self.pagerHeight)
    }

    // MARK: Header

    private func header(_ visible: [DataColumn], _ w: [String: CGFloat], _ total: CGFloat) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                ForEach(visible) { c in
                    Text(c.title).lineLimit(1)
                        .frame(width: max((w[c.id] ?? c.width) - 2 * Self.cellPadding, 0), alignment: c.alignment)
                        .padding(.horizontal, Self.cellPadding)
                        .overlay(alignment: .trailing) {
                            if !c.flexible && !isSnapshot {
                                ResizeHandle { t in resize(c, w[c.id] ?? c.width, t) } ended: { endResize() } reset: { reset(c) }
                                    .offset(x: ResizeHandle.width / 2)
                            } else if !c.flexible {
                                Rectangle().fill(Theme.separator).frame(width: 1, height: 12)
                            }
                        }
                }
                Spacer(minLength: 0)
            }
            .font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
            .frame(height: Self.headerHeight)
            Hairline()
        }
        .frame(width: total)
        .background(Theme.surface)
        .contextMenu { columnItems(layout) }
    }

    private func resize(_ c: DataColumn, _ current: CGFloat, _ translation: CGFloat) {
        let start = drag?.id == c.id ? drag!.start : current
        drag = (c.id, start, min(max(start + translation, c.min), 900))
    }

    private func endResize() {
        guard let drag else { return }
        var l = layout
        l.widths[drag.id] = Double(drag.width)
        save(l)
        self.drag = nil
    }

    private func reset(_ c: DataColumn) {
        var l = layout
        l.widths[c.id] = nil
        save(l)
    }

    @ViewBuilder private func columnItems(_ l: ColumnLayout) -> some View {
        Section("Columns") {
            ForEach(columns.filter(\.hideable)) { c in
                Toggle(c.title, isOn: Binding(get: { !isHidden(c, l) }, set: { _ in toggle(c) }))
            }
        }
        Divider()
        Button("Reset Widths and Columns") { save(ColumnLayout()) }
    }

    @ViewBuilder private func columnsMenu(_ l: ColumnLayout) -> some View {
        if isSnapshot {
            Image(systemName: "tablecells").font(.km(.caption)).foregroundStyle(Theme.textSecondary).frame(width: Self.menuRoom)
        } else {
            Menu { columnItems(l) } label: {
                Image(systemName: "tablecells").font(.km(.caption)).foregroundStyle(Theme.textSecondary)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().frame(width: Self.menuRoom)
            .help("Show, hide and reset columns. Drag a column's edge to size it; double-click the edge to reset it.")
            .accessibilityLabel("Columns")
        }
    }

    // MARK: Rows

    private func row(_ item: Item, _ visible: [DataColumn], _ w: [String: CGFloat]) -> some View {
        DataRow(selected: selection == item.id) {
            selection = item.id
            focused = true
            openDetails?()
        } content: {
            ForEach(visible) { c in
                cell(item, c.id)
                    .lineLimit(1)
                    .frame(width: max((w[c.id] ?? c.width) - 2 * Self.cellPadding, 0), alignment: c.alignment)
                    .padding(.horizontal, Self.cellPadding)
                    .clipped()
            }
        }
    }
}

/// A table row's height, its hairline not counted: every row the same, so a page fits exactly.
private let dataRowHeight: CGFloat = 30

/// A row: hover to highlight, click to select and see its details.
private struct DataRow<Content: View>: View {
    var selected: Bool
    var tap: () -> Void
    @ViewBuilder var content: Content
    @State private var hovering = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                content
                Spacer(minLength: 0)
            }
            .frame(height: dataRowHeight)
            .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous)
                .fill(selected ? Theme.accentSoft : hovering ? Theme.fill : .clear))
            .contentShape(Rectangle())
            Hairline()
        }
        .onHover { hovering = $0 }
        .onTapGesture(perform: tap)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityHint("Shows its details")
    }
}

/// A page's number in a table's pager.
private struct PageNumber: View {
    var number: Int
    var current: Bool
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Text(verbatim: "\(number)").font(.km(.caption).weight(current ? .semibold : .regular)).monospacedDigit()
                .foregroundStyle(current ? Theme.accent : Theme.text)
                .frame(minWidth: 24).frame(height: 22).padding(.horizontal, Space.xxs)
                .background(RoundedRectangle(cornerRadius: Radius.s - Space.xxs, style: .continuous)
                    .fill(current ? Theme.accentSoft : hovering ? Theme.fill : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel("Page \(number)")
        .accessibilityAddTraits(current ? .isSelected : [])
    }
}

/// The edge of a column title: drag to size the column, double-click to give it back its width.
private struct ResizeHandle: View {
    static let width: CGFloat = 9
    var changed: (CGFloat) -> Void
    var ended: () -> Void
    var reset: () -> Void
    @State private var hovering = false
    @State private var dragging = false

    var body: some View {
        Rectangle().fill(Color.clear).frame(width: Self.width, height: 20).contentShape(Rectangle())
            .overlay {
                Rectangle().fill(hovering || dragging ? Theme.accent : Theme.separator)
                    .frame(width: hovering || dragging ? 2 : 1, height: hovering || dragging ? 16 : 12)
            }
            .onHover { inside in
                if inside && !hovering { NSCursor.resizeLeftRight.push() }
                if !inside && hovering && !dragging { NSCursor.pop() }
                hovering = inside
            }
            .onDisappear { if hovering || dragging { NSCursor.pop() } }
            .onTapGesture(count: 2, perform: reset)
            .gesture(DragGesture(minimumDistance: 1, coordinateSpace: .global)
                .onChanged { dragging = true; changed($0.translation.width) }
                .onEnded { _ in
                    dragging = false
                    if !hovering { NSCursor.pop() }
                    ended()
                })
            .accessibilityHidden(true)
    }
}

// MARK: - Table and details

/// A table and its selected row's details: beside it when the page is wide enough, else in its
/// place, with a way back. Either way the details have the page's full height, so they rarely
/// scroll, and ↑ ↓ step through the rows without leaving them.
struct ListDetail<List: View, Detail: View>: View {
    /// Where the details' width is kept.
    var key: String
    /// The rows, in the table's order: for "3 of 40" and previous and next.
    var ids: [String]
    @Binding var selection: String?
    /// What the way back says: the table's name.
    var back: String
    /// What to say while nothing is selected.
    var hint: String
    @ViewBuilder var list: List
    @ViewBuilder var detail: Detail
    /// The details' width as the user dragged it; 0: a share of the page.
    @AppStorage private var width: Double
    /// Narrow: the details are showing in the table's place.
    @State private var open = false
    @State private var dragStart: CGFloat?
    @FocusState private var focused: Bool
    @Environment(\.isSnapshot) private var isSnapshot

    /// From this width the details sit beside the table.
    static var wideFrom: CGFloat { 1240 }
    /// The table keeps at least this much beside them.
    private static var listMin: CGFloat { 620 }

    init(key: String, ids: [String], selection: Binding<String?>, back: String, hint: String,
         @ViewBuilder list: () -> List, @ViewBuilder detail: () -> Detail) {
        self.key = key
        self.ids = ids
        _selection = selection
        self.back = back
        self.hint = hint
        self.list = list()
        self.detail = detail()
        _width = AppStorage(wrappedValue: 0, "details.\(key).width")
        _open = State(initialValue: selection.wrappedValue != nil)
    }

    private var index: Int? { selection.flatMap { s in ids.firstIndex(of: s) } }

    private func paneWidth(_ total: CGFloat, _ wanted: CGFloat? = nil) -> CGFloat {
        let w = wanted ?? (width > 0 ? CGFloat(width) : min(max(total * 0.4, 420), 720))
        return min(max(w, 360), total - Self.listMin - Space.m)
    }

    var body: some View {
        GeometryReader { geo in
            if geo.size.width >= Self.wideFrom {
                let w = paneWidth(geo.size.width)
                HStack(spacing: 0) {
                    list.frame(maxWidth: .infinity, maxHeight: .infinity)
                    SplitHandle(vertical: true) { t in
                        let start = dragStart ?? w
                        dragStart = start
                        width = Double(paneWidth(geo.size.width, start - t))
                    } ended: {
                        dragStart = nil
                    } reset: {
                        width = 0
                    }
                    pane(wide: true).frame(width: w)
                }
            } else {
                let showing = open && index != nil
                ZStack {
                    list.opacity(showing ? 0 : 1).allowsHitTesting(!showing).accessibilityHidden(showing)
                    if showing { pane(wide: false) }
                }
                .environment(\.openDetails, OpenDetailsAction { open = true })
            }
        }
        .onChange(of: selection) { _, s in if s == nil { open = false } }
    }

    @ViewBuilder private func pane(wide: Bool) -> some View {
        VStack(spacing: 0) {
            if let i = index {
                toolbar(i, wide: wide)
                Hairline()
                if isSnapshot {
                    detail
                } else {
                    // A new row starts at its top.
                    ScrollView { detail.frame(maxWidth: .infinity, alignment: .topLeading) }.id(selection)
                }
            } else {
                VStack(spacing: Space.s) {
                    Image(systemName: "sidebar.right").font(.km(.title)).foregroundStyle(Theme.textTertiary)
                    Text(hint).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                }
                .font(.km(.callout)).foregroundStyle(Theme.textSecondary)
                .padding(Space.xl)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
        .clipShape(RoundedRectangle(cornerRadius: Radius.m, style: .continuous))
        .focusable(!isSnapshot && index != nil)
        .focused($focused)
        .focusEffectDisabled()
        .onKeyPress(.upArrow) { step(-1) }
        .onKeyPress(.downArrow) { step(1) }
        .onKeyPress(.escape) {
            guard index != nil else { return .ignored }
            if wide { selection = nil } else { open = false }
            return .handled
        }
        .onAppear { if !wide && !isSnapshot { focused = true } }
    }

    private func toolbar(_ i: Int, wide: Bool) -> some View {
        HStack(spacing: Space.xs) {
            if !wide {
                Button { open = false } label: {
                    HStack(spacing: Space.xs) {
                        Image(systemName: "chevron.left").font(.km(.caption).weight(.semibold))
                        Text(back)
                    }
                    .font(.km(.callout).weight(.medium)).foregroundStyle(Theme.accent)
                    .padding(.vertical, Space.xs).contentShape(Rectangle())
                }
                .buttonStyle(.plain).help("Back to the list (Esc)")
            }
            Spacer(minLength: Space.s)
            Text(verbatim: "\((i + 1).formatted()) of \(ids.count.formatted())")
                .font(.km(.caption)).monospacedDigit().foregroundStyle(Theme.textSecondary).padding(.trailing, Space.xs)
            IconButton(symbol: "chevron.up", help: "Previous (↑)") { _ = step(-1) }.disabled(i == 0)
            IconButton(symbol: "chevron.down", help: "Next (↓)") { _ = step(1) }.disabled(i >= ids.count - 1)
            if wide { IconButton(symbol: "xmark", help: "Close (Esc)") { selection = nil } }
        }
        .padding(.horizontal, Space.m)
        .frame(height: 40)
    }

    private func step(_ by: Int) -> KeyPress.Result {
        guard let i = index else { return .ignored }
        let next = i + by
        guard ids.indices.contains(next) else { return .ignored }
        selection = ids[next]
        return .handled
    }
}

/// A bar to drag between two parts of a page: up and down, or (vertical) left and right.
/// Double-click gives back the default.
struct SplitHandle: View {
    var vertical = false
    var changed: (CGFloat) -> Void
    var ended: () -> Void
    var reset: () -> Void = {}
    @State private var hovering = false

    var body: some View {
        Capsule().fill(hovering ? Theme.textSecondary : Theme.fillStrong)
            .frame(width: vertical ? 4 : 36, height: vertical ? 36 : 4)
            .frame(maxWidth: vertical ? nil : .infinity, maxHeight: vertical ? .infinity : nil)
            .frame(width: vertical ? Space.m : nil, height: vertical ? nil : Space.m)
            .contentShape(Rectangle())
            .onHover { inside in
                let cursor: NSCursor = vertical ? .resizeLeftRight : .resizeUpDown
                if inside && !hovering { cursor.push() }
                if !inside && hovering { NSCursor.pop() }
                hovering = inside
            }
            .onDisappear { if hovering { NSCursor.pop() } }
            .onTapGesture(count: 2, perform: reset)
            .gesture(DragGesture(minimumDistance: 1, coordinateSpace: .global)
                .onChanged { changed(vertical ? $0.translation.width : $0.translation.height) }
                .onEnded { _ in ended() })
            .help("Drag to resize; double-click for the default")
            .accessibilityHidden(true)
    }
}

// MARK: - Details content

/// Label-value rows for a details pane, as a two-column grid.
struct DetailGrid<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Space.l, verticalSpacing: Space.s) { content }
    }
}

/// One row of a DetailGrid. Long values wrap; every value can be selected and copied.
struct DetailField: View {
    var key: String
    var value: String
    var mono = false
    var tone: Color = Theme.text

    init(_ key: String, _ value: String, mono: Bool = false, tone: Color = Theme.text) {
        self.key = key
        self.value = value
        self.mono = mono
        self.tone = tone
    }

    var body: some View {
        GridRow {
            Text(key).font(.km(.callout)).foregroundStyle(Theme.textSecondary).gridColumnAlignment(.trailing)
                .fixedSize()
            Text(value).font(mono ? .km(.mono) : .km(.callout)).foregroundStyle(tone).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// A titled group in a details pane.
struct DetailSection<Content: View>: View {
    var title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(title).eyebrow()
            content
        }
    }
}

/// A details pane's two sides: the facts on the left, the text on the right, or one above the
/// other when the pane is narrow.
struct DetailColumns<Left: View, Right: View>: View {
    @ViewBuilder var left: Left
    @ViewBuilder var right: Right
    var body: some View {
        SideBySide {
            VStack(alignment: .leading, spacing: Space.l) { left }
            VStack(alignment: .leading, spacing: Space.l) { right }
        }
    }
}

/// Two views side by side, the first at most 460 wide, from 760 of width; one above the other below it.
private struct SideBySide: Layout {
    static let threshold: CGFloat = 760
    let spacing = Space.xl

    private func widths(_ w: CGFloat) -> (CGFloat, CGFloat)? {
        guard w >= Self.threshold else { return nil }
        let left = min(460, (w - spacing) * 0.48)
        return (left, w - spacing - left)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard subviews.count == 2 else { return .zero }
        let w = proposal.width ?? 900
        if let (l, r) = widths(w) {
            let h = max(subviews[0].sizeThatFits(.init(width: l, height: nil)).height,
                        subviews[1].sizeThatFits(.init(width: r, height: nil)).height)
            return CGSize(width: w, height: h)
        }
        let h = subviews.map { $0.sizeThatFits(.init(width: w, height: nil)).height }.reduce(0, +) + Space.l
        return CGSize(width: w, height: h)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard subviews.count == 2 else { return }
        if let (l, r) = widths(bounds.width) {
            subviews[0].place(at: bounds.origin, proposal: .init(width: l, height: nil))
            subviews[1].place(at: CGPoint(x: bounds.minX + l + spacing, y: bounds.minY), proposal: .init(width: r, height: nil))
        } else {
            let top = subviews[0].sizeThatFits(.init(width: bounds.width, height: nil)).height
            subviews[0].place(at: bounds.origin, proposal: .init(width: bounds.width, height: nil))
            subviews[1].place(at: CGPoint(x: bounds.minX, y: bounds.minY + top + Space.l), proposal: .init(width: bounds.width, height: nil))
        }
    }
}

/// "1 email", "3 emails": a count of a type of value.
func typeCount(_ n: Int, _ type: String) -> String {
    let one = switch type {
    case "SECRET": "secret"
    case "ACCOUNT": "account number"
    case "PERSON": "name"
    case "EMAIL": "email"
    case "PHONE": "phone number"
    case "ADDRESS": "address"
    case "URL": "URL"
    case "DATE": "date"
    default: type.lowercased()
    }
    return n == 1 ? "1 \(one)" : "\(n) \(one == "address" ? "addresses" : one + "s")"
}

/// The full date and time, and how long ago.
func detailTime(_ date: Date) -> String {
    let rel = RelativeDateTimeFormatter()
    rel.unitsStyle = .full
    return date.formatted(.dateTime.weekday(.wide).month(.wide).day().year().hour().minute().second())
        + " (" + rel.localizedString(for: date, relativeTo: Date()) + ")"
}
