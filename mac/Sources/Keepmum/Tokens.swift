import AppKit
import SwiftUI

// Design tokens. Every color, text style, space and corner radius in the app comes from here;
// views never use a raw size, hex value or opacity of their own.
//
// The look is native macOS: neutral grays like the system's, a translucent sidebar, grouped
// sections without borders. Color carries meaning only: coral, keepmum's own, marks what is
// selected or pressable; the signal colors mark what happened to a request.

extension Color {
    /// A color that follows the light or dark appearance it is drawn in.
    init(light: NSColor, dark: NSColor) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        })
    }

    init(light: UInt32, dark: UInt32, alpha: CGFloat = 1) {
        self.init(light: NSColor(hex: light, alpha: alpha), dark: NSColor(hex: dark, alpha: alpha))
    }
}

extension NSColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }
}

/// Colors. Text colors meet 4.5:1 on `canvas` and `surface` in both appearances, except
/// `textTertiary`, which is for disabled and decorative text only.
enum Theme {
    // Surfaces, back to front.
    static let canvas = Color(light: 0xFFFFFF, dark: 0x1E1E1E)
    /// The sidebar where the window cannot be translucent (snapshots).
    static let sidebar = Color(light: 0xF0F0F2, dark: 0x262628)
    /// A grouped section on the canvas.
    static let surface = Color(light: 0xF4F4F5, dark: 0x2A2A2C)
    /// Above a surface: the selected segment, a chart's label box.
    static let raised = Color(light: 0xFFFFFF, dark: 0x3A3A3C)
    /// Controls at rest: an off switch, a secondary button, a chip.
    static let fill = Color(light: NSColor(white: 0, alpha: 0.06), dark: NSColor(white: 1, alpha: 0.09))
    /// Controls hovered or selected: a sidebar row.
    static let fillStrong = Color(light: NSColor(white: 0, alpha: 0.10), dark: NSColor(white: 1, alpha: 0.14))
    static let separator = Color(light: NSColor(white: 0, alpha: 0.09), dark: NSColor(white: 1, alpha: 0.09))

    // Text.
    static let text = Color(light: 0x1D1D1F, dark: 0xF5F5F7)
    static let textSecondary = Color(light: 0x6E6E73, dark: 0x98989D)
    static let textTertiary = Color(light: 0x8E8E93, dark: 0x6C6C70)

    // The colors below follow the palette chosen in Settings › General (`Palette`), so they are
    // read on each use rather than stored.

    /// The standard palette, or the color-blind safe one.
    private static func pick(_ standard: @autoclosure () -> Color, safe: @autoclosure () -> Color) -> Color {
        Palette.current == .standard ? standard() : safe()
    }

    // Brand: keepmum's coral, a shade darker than the icon's so text and labels on it stay legible.
    // Color-blind safe: blue, since coral and the warning amber look alike without red-green vision.
    static var accent: Color { pick(Color(light: 0xC93F17, dark: 0xFF7A4D), safe: Color(light: 0x0A60D0, dark: 0x5B96FA)) }
    /// Behind white text: a primary button, an on switch.
    static var accentFill: Color { pick(Color(light: 0xC93F17, dark: 0xD44A20), safe: Color(light: 0x0A60D0, dark: 0x2F6FDB)) }
    static var accentSoft: Color {
        pick(Color(light: NSColor(hex: 0xC93F17, alpha: 0.10), dark: NSColor(hex: 0xFF7A4D, alpha: 0.16)),
             safe: Color(light: NSColor(hex: 0x0A60D0, alpha: 0.10), dark: NSColor(hex: 0x5B96FA, alpha: 0.16)))
    }
    static let onAccent = Color.white

    // Signals: what happened to a request. Blocked is crimson, well apart from the coral accent.
    // Color-blind safe: indigo, teal, wine and amber, which stay apart (CIEDE2000 ≥ 12, each pair
    // and the accent against blocked and warning) under simulated protanopia, deuteranopia and
    // tritanopia, and as text meet 4.5:1 like the rest.
    static var masked: Color { pick(Color(light: 0x0B63CE, dark: 0x5AA2FF), safe: Color(light: 0x3A22C4, dark: 0xA496FF)) }
    static var restored: Color { pick(Color(light: 0x16834A, dark: 0x3DD68C), safe: Color(light: 0x1D7779, dark: 0x3FD9D0)) }
    static var blocked: Color { pick(Color(light: 0xC8193C, dark: 0xFF5C74), safe: Color(light: 0x8E0F45, dark: 0xE8798A)) }
    /// Behind white text: the observe banner.
    static var blockedFill: Color { pick(Color(light: 0xC8193C, dark: 0xC42443), safe: Color(light: 0x8E0F45, dark: 0xA3264A)) }
    static var warning: Color { pick(Color(light: 0xA15C00, dark: 0xFFB547), safe: Color(light: 0x9D5D03, dark: 0xF2D22E)) }
    /// Nothing to report: clean requests, replies with nothing to restore.
    static let neutral = Color(light: 0xA6A6AD, dark: 0x5E5E63)

    /// A signal's background: chips, banners, highlighted rows.
    static func soft(_ color: Color) -> Color { color.opacity(0.12) }

    /// Each coding agent's color in the flow diagram: its own brand's hue where it has one.
    /// Color-blind safe: Okabe–Ito's hues, the nearest to each brand's.
    static func client(_ id: String) -> Color {
        let safe = Palette.current == .colorBlind
        return switch id {
        case "claude-code": safe ? Color(light: 0xB84A00, dark: 0xF08A3C) : Color(light: 0xC8775A, dark: 0xD98B6E)
        case "codex": Color(light: 0x5B6270, dark: 0x9AA1AE)
        case "pi": safe ? Color(light: 0x0072B2, dark: 0x56B4E9) : Color(light: 0x6E78E0, dark: 0x8F98F0)
        case "opencode": safe ? Color(light: 0xB89300, dark: 0xF0E442) : Color(light: 0xB7791F, dark: 0xE0A458)
        // The purple of Gemini's spark.
        case "gemini": safe ? Color(light: 0xB4528A, dark: 0xE6A3C8) : Color(light: 0x7E64B8, dark: 0x9F8AD6)
        default: safe ? Color(light: 0x008A65, dark: 0x2FD3A0) : Color(light: 0x2A9D8F, dark: 0x4FC3B5)
        }
    }

    // A categorical series, for charts that compare things rather than signal outcomes.
    static var series: [Color] {
        Palette.current == .standard
            ? [masked, Color(light: 0x7C4DCC, dark: 0xB38CFF), Color(light: 0x0E7C7B, dark: 0x4FD1C5)]
            : [Color(light: 0x0A60D0, dark: 0x5B96FA), Color(light: 0x9D5D03, dark: 0xF2B53A), Color(light: 0xA3306F, dark: 0xE88FC4)]
    }
}

/// The set of signal colors: the standard one, or one whose colors stay apart for each common
/// kind of color blindness (red-green and blue-yellow).
enum Palette: String, CaseIterable, Identifiable {
    case standard, colorBlind
    static let key = "palette"
    var id: String { rawValue }
    var title: String { self == .standard ? "Standard" : "Color-blind safe" }

    static var current: Palette {
        UserDefaults.standard.string(forKey: key).flatMap(Palette.init(rawValue:)) ?? .standard
    }
}

extension View {
    /// On a window's root: redraws it all when the palette changes, since views read `Theme`'s
    /// colors directly rather than observing them. It resets the window's view state, so it is
    /// left off windows holding unsaved text (the skills editor, a review): those take the new
    /// palette when next opened.
    func followsPalette() -> some View { modifier(PaletteRoot()) }
}

private struct PaletteRoot: ViewModifier {
    @AppStorage(Palette.key) private var palette: Palette = .standard
    func body(content: Content) -> some View { content.id(palette) }
}

/// Spacing, on a 4-point grid.
enum Space {
    static let xxs: CGFloat = 2
    static let xs: CGFloat = 4
    static let s: CGFloat = 8
    static let m: CGFloat = 12
    static let l: CGFloat = 16
    static let xl: CGFloat = 24
    static let xxl: CGFloat = 32
}

/// Corner radii. Pills and switches are capsules.
enum Radius {
    /// Fields, chips, segments, sidebar rows.
    static let s: CGFloat = 6
    /// Sections, metric tiles.
    static let m: CGFloat = 10
    /// The menu bar panel's groups.
    static let l: CGFloat = 14
}

/// Text styles: seven sizes, macOS's own 13-point body among them.
enum TypeScale {
    /// The menu bar panel's headline number. 30, rounded.
    case display
    /// A metric tile's number. 22, rounded.
    case metric
    /// A page's title. 20.
    case title
    /// A section's title, a row's name. 13, semibold.
    case headline
    /// Prose, controls. 13.
    case body
    /// Table cells, secondary prose. 12.
    case callout
    /// Notes, column headers, footers. 11.
    case caption
    /// Uppercase eyebrows: sidebar groups, chart columns. 10, semibold, tracked.
    case label
    /// Paths, rule ids, times. 12, monospaced.
    case mono
    /// 11, monospaced.
    case monoSmall
}

extension Font {
    static func km(_ style: TypeScale) -> Font {
        switch style {
        case .display: .system(size: 30, weight: .semibold, design: .rounded)
        case .metric: .system(size: 22, weight: .semibold, design: .rounded)
        case .title: .system(size: 20, weight: .semibold)
        case .headline: .system(size: 13, weight: .semibold)
        case .body: .system(size: 13)
        case .callout: .system(size: 12)
        case .caption: .system(size: 11)
        case .label: .system(size: 10, weight: .semibold)
        case .mono: .system(size: 12, design: .monospaced)
        case .monoSmall: .system(size: 11, design: .monospaced)
        }
    }
}

extension Text {
    /// An uppercase eyebrow over a group of rows.
    func eyebrow() -> some View {
        self.font(.km(.label)).kerning(0.6).textCase(.uppercase).foregroundStyle(Theme.textSecondary)
    }
}

/// Motion: short, and only where something changed.
enum Motion {
    static let quick = Animation.easeOut(duration: 0.15)
    static let settle = Animation.spring(duration: 0.4)
}

// MARK: - Sensitive types

extension Theme {
    static let typeOrder = ["SECRET", "ACCOUNT", "PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "DATE"]

    static func label(_ type: String) -> String {
        switch type {
        case "SECRET": "Secrets"
        case "ACCOUNT": "Accounts"
        case "PERSON": "Names"
        case "EMAIL": "Emails"
        case "PHONE": "Phones"
        case "ADDRESS": "Addresses"
        case "URL": "URLs"
        case "DATE": "Dates"
        default: type.capitalized
        }
    }

    static func icon(_ type: String) -> String {
        switch type {
        case "SECRET": "key.fill"
        case "ACCOUNT": "creditcard.fill"
        case "PERSON": "person.fill"
        case "EMAIL": "envelope.fill"
        case "PHONE": "phone.fill"
        case "ADDRESS": "house.fill"
        case "URL": "link"
        case "DATE": "calendar"
        default: "circle.fill"
        }
    }
}

// MARK: - Numbers

extension Int {
    var compact: String {
        switch self {
        case 1_000_000...: String(format: "%.1fM", Double(self) / 1_000_000)
        case 10_000...: String(format: "%.0fk", Double(self) / 1_000)
        case 1_000...: String(format: "%.1fk", Double(self) / 1_000)
        default: "\(self)"
        }
    }
}

func durationText(_ seconds: Double) -> String {
    let s = Int(seconds)
    if s < 60 { return "\(s)s" }
    if s < 3600 { return "\(s / 60)m" }
    return "\(s / 3600)h \(s % 3600 / 60)m"
}

func percent(_ part: Int, of whole: Int) -> String {
    whole > 0 ? String(format: "%.0f%%", Double(part) / Double(whole) * 100) : "—"
}

extension String {
    /// The first letter capitalized, to start a sentence, except keepmum, which is always lowercase.
    var sentence: String {
        hasPrefix("keepmum") ? self : prefix(1).uppercased() + dropFirst()
    }
}

func milliseconds(_ ms: Double?) -> String {
    ms.map { String(format: "%.0f ms", $0) } ?? "—"
}
