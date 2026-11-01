import Charts
import SwiftUI

struct MaskedTimeline: View {
    var minutes: [Snapshot.Minute]

    var body: some View {
        // Only the minutes inside the axis: Swift Charts does not clip marks outside its domain.
        let start = Date().addingTimeInterval(-3600)
        let recent = minutes.filter { $0.date >= start }
        Chart {
            ForEach(recent) { m in
                BarMark(x: .value("Minute", m.date, unit: .minute), y: .value("Masked", m.masked.values.reduce(0, +)))
                    .foregroundStyle(Theme.masked.opacity(0.8))
                    .cornerRadius(Radius.s - Space.xxs)
                if m.blocked > 0 {
                    PointMark(x: .value("Minute", m.date, unit: .minute), y: .value("Blocked", 0))
                        .symbol(.circle).symbolSize(36).foregroundStyle(Theme.blocked)
                }
            }
        }
        .chartXScale(domain: start...Date().addingTimeInterval(420))
        .chartPlotStyle { $0.clipped() }
        .chartXAxis { AxisMarks(values: .stride(by: .minute, count: 20)) { _ in
            AxisGridLine().foregroundStyle(Theme.separator)
            AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(Theme.textSecondary)
        } }
        .chartYAxis { AxisMarks(position: .leading) { _ in
            AxisGridLine().foregroundStyle(Theme.separator)
            AxisValueLabel().foregroundStyle(Theme.textSecondary)
        } }
    }
}

struct LatencyChart: View {
    var events: [Snapshot.Event]
    static let budget = 150.0

    var body: some View {
        let points = Array(events.compactMap { e in e.scanMs.map { (e.date, $0) } }.suffix(120))
        Chart {
            ForEach(Array(points.enumerated()), id: \.offset) { _, pt in
                AreaMark(x: .value("Time", pt.0), y: .value("ms", pt.1))
                    .foregroundStyle(Theme.soft(Theme.masked)).interpolationMethod(.monotone)
                LineMark(x: .value("Time", pt.0), y: .value("ms", pt.1))
                    .foregroundStyle(Theme.masked).interpolationMethod(.monotone)
                    .lineStyle(StrokeStyle(lineWidth: 1.6))
            }
            RuleMark(y: .value("Budget", Self.budget))
                .foregroundStyle(Theme.warning)
                .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .annotation(position: .top, alignment: .trailing) {
                    Text("budget \(Int(Self.budget)) ms").font(.km(.label)).foregroundStyle(Theme.textSecondary)
                }
        }
        .chartYScale(domain: 0...max(Self.budget * 1.3, points.map(\.1).max() ?? 0))
        .chartPlotStyle { $0.clipped() }
        .chartXAxis { AxisMarks { _ in
            AxisGridLine().foregroundStyle(Theme.separator)
            AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(Theme.textSecondary)
        } }
        .chartYAxis { AxisMarks(position: .leading) { _ in
            AxisGridLine().foregroundStyle(Theme.separator)
            AxisValueLabel().foregroundStyle(Theme.textSecondary)
        } }
    }
}

/// How scan times are distributed, in 25 ms buckets; buckets over the budget are amber.
struct LatencyHistogram: View {
    var events: [Snapshot.Event]

    var body: some View {
        let times = events.compactMap(\.scanMs)
        let buckets: [(label: String, count: Int, over: Bool)] = stride(from: 0, to: 250, by: 25).map { lo in
            let last = lo >= 225
            let n = times.filter { $0 >= Double(lo) && (last || $0 < Double(lo + 25)) }.count
            return (last ? "225+" : "\(lo)", n, lo >= 150)
        }
        Chart(buckets, id: \.label) { b in
            BarMark(x: .value("ms", b.label), y: .value("Requests", b.count))
                .foregroundStyle(b.over ? Theme.warning.opacity(0.8) : Theme.masked.opacity(0.8))
                .cornerRadius(Radius.s - Space.xxs)
        }
        .chartXAxis { AxisMarks { _ in AxisValueLabel().foregroundStyle(Theme.textSecondary) } }
        .chartYAxis { AxisMarks(position: .leading) { _ in
            AxisGridLine().foregroundStyle(Theme.separator)
            AxisValueLabel().foregroundStyle(Theme.textSecondary)
        } }
    }
}
