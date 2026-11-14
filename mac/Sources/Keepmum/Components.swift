import AppKit
import SwiftUI

// The app's building blocks, drawn from the tokens. Controls are drawn in SwiftUI rather than
// taken from AppKit, so they look the same in both appearances and in snapshots (ImageRenderer
// cannot draw AppKit-backed views).

extension EnvironmentValues {
    /// True while Snapshotter renders: views avoid what ImageRenderer cannot draw (ScrollView).
    @Entry var isSnapshot = false
}

enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var scheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

// MARK: - Layout

struct PageHeader<Trailing: View>: View {
    var title: String
    var subtitle: String
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .center, spacing: Space.l) {
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(title).font(.km(.title)).foregroundStyle(Theme.text)
                Text(subtitle).font(.km(.callout)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            }
            Spacer(minLength: 0)
            trailing
        }
    }
}

extension PageHeader where Trailing == EmptyView {
    init(title: String, subtitle: String) {
        self.init(title: title, subtitle: subtitle) { EmptyView() }
    }
}

/// A grouped section: a title, an optional accessory on the right, and its content, on a
/// borderless surface.
struct Card<Content: View, Accessory: View>: View {
    var title: String
    var subtitle: String?
    @ViewBuilder var accessory: Accessory
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: Space.m) {
            HStack(alignment: .center, spacing: Space.s) {
                VStack(alignment: .leading, spacing: Space.xxs) {
                    Text(title).font(.km(.headline)).foregroundStyle(Theme.text)
                    if let subtitle { Text(subtitle).font(.km(.caption)).foregroundStyle(Theme.textSecondary) }
                }
                Spacer(minLength: Space.s)
                accessory
            }
            content
        }
        .padding(Space.l)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
    }
}

extension Card where Accessory == EmptyView {
    init(title: String, subtitle: String? = nil, @ViewBuilder content: () -> Content) {
        self.init(title: title, subtitle: subtitle, accessory: { EmptyView() }, content: content)
    }
}

/// A settings group, as in System Settings: a title above, rows on a surface, a note below. The
/// note is a sentence or two; `details`, the full explanation, opens under it on request, so a
/// page stays short enough to scan.
struct FormSection<Content: View>: View {
    var title: String
    var footer: String?
    var details: String?
    var content: Content
    @State private var showingDetails = false

    init(title: String, footer: String? = nil, details: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.footer = footer
        self.details = details
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.s) {
            Text(title).font(.km(.headline)).foregroundStyle(Theme.text).padding(.leading, Space.xs)
            VStack(spacing: 0) { content }
                .padding(.horizontal, Space.m)
                .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
            if footer != nil || details != nil {
                VStack(alignment: .leading, spacing: Space.xs) {
                    if let footer {
                        Text(footer).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let details {
                        if showingDetails {
                            Text(details).font(.km(.caption)).foregroundStyle(Theme.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                        Button(showingDetails ? "Show less" : "More about this") {
                            withAnimation(Motion.quick) { showingDetails.toggle() }
                        }
                        .buttonStyle(.plain).font(.km(.caption)).foregroundStyle(Theme.accent)
                        .accessibilityHint("Shows or hides the full explanation of \(title)")
                    }
                }
                .padding(.horizontal, Space.xs)
            }
        }
    }
}

/// When a row happened: its date and time, so rows kept across days read apart ("Nov 13 19:11:12").
struct Stamp: View {
    var date: Date
    static let width: CGFloat = 128

    var body: some View {
        Text(verbatim: date.formatted(.dateTime.month(.abbreviated).day()) + " " + date.formatted(.dateTime.hour().minute().second()))
            .font(.km(.monoSmall)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            .frame(width: Self.width, alignment: .leading)
            .help(date.formatted(date: .complete, time: .standard))
    }
}

/// One row of a FormSection: what it is on the left, its control on the right.
struct FormRow<Control: View>: View {
    var title: String
    var detail: String? = nil
    var detailTone: Color = Theme.textSecondary
    /// The full story behind a short detail, on hover.
    var help: String? = nil
    @ViewBuilder var control: Control

    var body: some View {
        HStack(alignment: .center, spacing: Space.m) {
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text(title).font(.km(.body)).foregroundStyle(Theme.text)
                if let detail {
                    Text(detail).font(.km(.caption)).foregroundStyle(detailTone).fixedSize(horizontal: false, vertical: true)
                }
            }
            .help(help ?? "")
            Spacer(minLength: Space.m)
            control
        }
        .padding(.vertical, Space.s + Space.xxs)
    }
}

struct Hairline: View {
    var vertical = false
    var body: some View {
        Rectangle().fill(Theme.separator).frame(width: vertical ? 1 : nil, height: vertical ? nil : 1)
    }
}

/// A table's column titles, over a hairline.
struct ColumnHeader<Columns: View>: View {
    @ViewBuilder var columns: Columns

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Space.m) { columns }
                .font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
                .padding(.bottom, Space.s - Space.xxs)
            Hairline()
        }
    }
}

/// A table row's vertical rhythm, with the hairline under it.
struct TableRow<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Space.m) { content }.padding(.vertical, Space.s - 1)
            Hairline()
        }
    }
}

/// What to show when a list has nothing in it.
struct EmptyNote: View {
    var text: String
    var body: some View {
        Text(text).font(.km(.callout)).foregroundStyle(Theme.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.top, Space.m)
            .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - Numbers

/// One number with its name and a note: the top row of Overview and Performance.
struct Metric: View {
    var title: String
    var value: String
    var icon: String
    var footnote: String
    /// A signal color when the number is a warning, such as blocked requests.
    var tone: Color? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Label {
                Text(title)
            } icon: {
                Image(systemName: icon).foregroundStyle(tone ?? Theme.textSecondary)
            }
            .font(.km(.caption).weight(.medium)).foregroundStyle(Theme.textSecondary)
            Text(value).font(.km(.metric)).monospacedDigit()
                .foregroundStyle(tone ?? Theme.text).contentTransition(.numericText())
            Text(footnote).font(.km(.caption)).foregroundStyle(Theme.textSecondary).lineLimit(1)
        }
        .padding(Space.m + Space.xxs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(Theme.surface))
    }
}

// MARK: - Controls

/// A capsule button. Prominent is the one primary action in its group; destructive is for
/// actions that turn protection down.
struct PillButton: View {
    var title: String
    var icon: String? = nil
    var prominent = false
    var destructive = false
    /// On a solid signal color, such as the observe banner.
    var onSolid = false
    /// Its action is under way: a spinner in place of the icon, and no second click.
    var loading = false
    var action: () -> Void
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.xs + Space.xxs) {
                if loading {
                    Spinner(size: 11)
                } else if let icon {
                    Image(systemName: icon).font(.km(.caption).weight(.semibold))
                }
                Text(title).font(.km(.callout).weight(.medium)).lineLimit(1)
            }
            .fixedSize()
            .foregroundStyle(foreground)
            .padding(.horizontal, Space.m).padding(.vertical, Space.xs + 1)
            .background(Capsule().fill(background))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(loading)
        // A loading button stays legible: it is busy, not unavailable.
        .opacity(isEnabled || loading ? 1 : 0.4)
        .onHover { hovering = $0 && isEnabled && !loading }
        .animation(Motion.quick, value: hovering)
        .animation(Motion.quick, value: loading)
    }

    private var foreground: Color {
        if prominent || onSolid { return Theme.onAccent }
        return destructive ? Theme.blocked : Theme.text
    }

    private var background: Color {
        if prominent { return hovering ? Theme.accentFill.opacity(0.88) : Theme.accentFill }
        if onSolid { return .white.opacity(hovering ? 0.3 : 0.2) }
        return hovering ? Theme.fillStrong : Theme.fill
    }
}

/// An indeterminate spinner in the current foreground color, drawn in SwiftUI so snapshots show it.
struct Spinner: View {
    var size: CGFloat = 12
    @State private var turning = false

    var body: some View {
        Circle()
            .trim(from: 0, to: 0.72)
            .stroke(style: StrokeStyle(lineWidth: max(1.5, size / 7), lineCap: .round))
            .frame(width: size, height: size)
            .rotationEffect(.degrees(turning ? 360 : 0))
            .animation(.linear(duration: 0.9).repeatForever(autoreverses: false), value: turning)
            .onAppear { turning = true }
            .accessibilityLabel("In progress")
    }
}

/// A small borderless symbol button, such as remove: a 22-point target around an 11-point glyph.
struct IconButton: View {
    var symbol: String
    var help: String
    var action: () -> Void
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.km(.caption).weight(.semibold))
                .foregroundStyle(hovering ? Theme.text : Theme.textSecondary)
                .frame(width: 22, height: 22)
                .background(Circle().fill(hovering ? Theme.fill : .clear))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .opacity(isEnabled ? 1 : 0.4)
        .onHover { hovering = $0 && isEnabled }
        .help(help).accessibilityLabel(help)
    }
}

/// A segmented control.
struct Segmented<T: Hashable>: View {
    var options: [(T, String)]
    @Binding var selection: T
    /// Small, its segments sharing the width it is given: on the Protection map's cards.
    var compact = false
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        HStack(spacing: Space.xxs) {
            ForEach(options, id: \.0) { value, title in
                let on = selection == value
                Button { selection = value } label: {
                    Text(title).font(.km(compact ? .caption : .callout).weight(.medium))
                        .foregroundStyle(on ? Theme.text : Theme.textSecondary)
                        .lineLimit(1)
                        .padding(.horizontal, compact ? Space.xxs : Space.m).padding(.vertical, compact ? Space.xxs + 1 : Space.xs)
                        .frame(maxWidth: compact ? .infinity : nil)
                        .background(RoundedRectangle(cornerRadius: Radius.s - Space.xxs, style: .continuous)
                            .fill(on ? Theme.raised : .clear)
                            .shadow(color: .black.opacity(on ? 0.12 : 0), radius: 1, y: 0.5))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(Space.xxs)
        .background(RoundedRectangle(cornerRadius: Radius.s, style: .continuous).fill(Theme.fill))
        // Full width or nothing: the text beside it wraps instead of the segments truncating.
        .fixedSize(horizontal: !compact, vertical: false)
        .opacity(isEnabled ? 1 : 0.4)
        .animation(Motion.quick, value: selection)
    }
}

/// An on/off switch.
struct Switch: View {
    var isOn: Bool
    var enabled = true
    var action: (Bool) -> Void

    var body: some View {
        Button { action(!isOn) } label: {
            Capsule()
                .fill(isOn ? Theme.accentFill : Theme.fillStrong)
                .frame(width: 34, height: 20)
                .overlay(alignment: isOn ? .trailing : .leading) {
                    Circle().fill(.white).shadow(color: .black.opacity(0.2), radius: 1, y: 0.5).padding(Space.xxs)
                }
                .animation(Motion.quick, value: isOn)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
        .accessibilityValue(isOn ? "on" : "off")
    }
}

/// A text field's stand-in for snapshots, which cannot draw native controls.
struct FieldStandIn: View {
    var text: String
    var mono = false

    var body: some View {
        Text(text).font(mono ? .km(.mono) : .km(.callout)).foregroundStyle(Theme.textTertiary)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, Space.s).frame(height: 22)
            .background(RoundedRectangle(cornerRadius: Radius.s - Space.xxs).fill(Theme.raised))
            .overlay(RoundedRectangle(cornerRadius: Radius.s - Space.xxs).strokeBorder(Theme.separator))
    }
}

// MARK: - Status

/// A short tinted label: "passes through", "yours", "Weakened".
struct Chip: View {
    var text: String
    var tone: Color = Theme.textSecondary

    var body: some View {
        Text(text).font(.km(.caption).weight(.medium)).foregroundStyle(tone)
            .padding(.horizontal, Space.s - Space.xxs).padding(.vertical, Space.xxs)
            .background(Capsule().fill(tone == Theme.textSecondary ? Theme.fill : Theme.soft(tone)))
            .fixedSize()
    }
}

/// How a request ended, as an icon and a word in its signal color.
struct OutcomeLabel: View {
    var kind: Snapshot.Event.Kind

    var body: some View {
        let (label, icon, tone): (String, String, Color) = switch kind {
        case .blocked: ("Blocked", "hand.raised.fill", Theme.blocked)
        case .observed: ("Observed", "eye", Theme.warning)
        case .masked: ("Masked", "eye.slash", Theme.masked)
        case .clean: ("Clean", "checkmark.circle", Theme.textSecondary)
        case .flagged: ("Flagged", "flag.fill", Theme.warning)
        case .local: ("Local", "desktopcomputer", Theme.restored)
        }
        HStack(spacing: Space.xs + 1) {
            Image(systemName: icon).font(.km(.caption))
            Text(label)
        }
        .font(.km(.callout)).foregroundStyle(tone)
    }
}

struct StatusDot: View {
    var connection: Connection
    /// Observe mode overrides "Live": it is the one state where values leave this Mac.
    var observing = false

    var body: some View {
        let (text, color) = info
        HStack(spacing: Space.s - Space.xxs) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(text).font(.km(.caption).weight(.medium)).foregroundStyle(observing ? Theme.blocked : Theme.textSecondary)
        }
    }

    private var info: (String, Color) {
        if observing && connection == .live { return ("Observe only", Theme.blocked) }
        return switch connection {
        case .live: ("Live", Theme.restored)
        case .sample: ("Preview data", Theme.warning)
        case .connecting: ("Connecting", Theme.neutral)
        case .offline: ("Offline", Theme.blocked)
        }
    }
}

/// A full-width notice at the top of the dashboard. Danger is drawn solid: it is for the states
/// where values leave this Mac unmasked, which must never look routine.
struct Banner<Action: View>: View {
    enum Tone { case danger, warning, info }
    var tone: Tone
    var icon: String
    var title: String
    var message: String
    @ViewBuilder var action: Action

    var body: some View {
        HStack(spacing: Space.m) {
            Image(systemName: icon).font(.km(.headline)).foregroundStyle(solid ? Theme.onAccent : color)
            (Text(title).fontWeight(.semibold) + Text("  ") + Text(message))
                .font(.km(.callout)).foregroundStyle(solid ? Theme.onAccent : Theme.text)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Space.s)
            action
        }
        .padding(.horizontal, Space.m + Space.xxs).padding(.vertical, Space.s + Space.xxs)
        .background(RoundedRectangle(cornerRadius: Radius.m, style: .continuous).fill(solid ? Theme.blockedFill : Theme.soft(color)))
    }

    private var solid: Bool { tone == .danger }
    private var color: Color {
        switch tone {
        case .danger: Theme.blocked
        case .warning: Theme.warning
        case .info: Theme.masked
        }
    }
}

// MARK: - Materials

/// The sidebar's translucent material. Snapshots cannot draw it, so they get a flat stand-in.
struct SidebarBackground: View {
    @Environment(\.isSnapshot) private var isSnapshot

    var body: some View {
        if isSnapshot { Theme.sidebar } else { VisualEffect(material: .sidebar) }
    }
}

struct VisualEffect: NSViewRepresentable {
    var material: NSVisualEffectView.Material

    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = material
        v.blendingMode = .behindWindow
        v.state = .followsWindowActiveState
        return v
    }

    func updateNSView(_ v: NSVisualEffectView, context: Context) { v.material = material }
}
