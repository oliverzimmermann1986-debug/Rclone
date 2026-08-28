import ActivityKit
import SwiftUI
import WidgetKit
import UIKit

private struct ProtectionEntry: TimelineEntry {
    let date: Date
    let snapshot: ProtectionWidgetSnapshot?
}

private struct ProtectionProvider: TimelineProvider {
    func placeholder(in context: Context) -> ProtectionEntry {
        ProtectionEntry(
            date: Date(),
            snapshot: ProtectionWidgetSnapshot(
                score: 92, state: "ready", hostname: "Backup", generatedAt: Date().timeIntervalSince1970,
                activePaths: 2, totalPaths: 2, quarantines: 0
            )
        )
    }

    func getSnapshot(in context: Context, completion: @escaping (ProtectionEntry) -> Void) {
        completion(ProtectionEntry(date: Date(), snapshot: ProtectionWidgetSnapshot.load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<ProtectionEntry>) -> Void) {
        let entry = ProtectionEntry(date: Date(), snapshot: ProtectionWidgetSnapshot.load())
        completion(Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(30 * 60))))
    }
}

private struct ProtectionWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: ProtectionEntry

    var body: some View {
        if let snapshot = entry.snapshot {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: snapshot.quarantines > 0 ? "hand.raised.fill" : "checkmark.shield.fill")
                        .foregroundStyle(snapshot.quarantines > 0 ? .red : color(snapshot.score))
                    Text(snapshot.hostname).font(.caption.weight(.semibold)).lineLimit(1)
                    Spacer()
                    Text("\(snapshot.score)").font(.title2.monospacedDigit().bold())
                }
                ProgressView(value: Double(snapshot.score), total: 100).tint(color(snapshot.score))
                Text(snapshot.quarantines > 0 ? "\(snapshot.quarantines) Sicherheitsstopp(s)" : "\(snapshot.activePaths)/\(snapshot.totalPaths) Datenwege aktiv")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                if family != .systemSmall {
                    Text("Stand \(Date(timeIntervalSince1970: snapshot.generatedAt), style: .relative)")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
            .widgetURL(URL(string: "rclonesync://recovery"))
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Image(systemName: "lifepreserver").foregroundStyle(.green)
                Text("Recovery Center öffnen").font(.headline)
                Text("Noch kein Schutzstatus auf diesem iPhone.").font(.caption).foregroundStyle(.secondary)
            }
            .widgetURL(URL(string: "rclonesync://recovery"))
        }
    }

    private func color(_ score: Int) -> Color { score >= 85 ? .green : score >= 60 ? .orange : .red }
}

struct RcloneProtectionWidget: Widget {
    let kind = "RcloneProtectionWidget"
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: ProtectionProvider()) { entry in
            ProtectionWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Schutzstatus")
        .description("Zeigt den letzten verifizierten Schutzstatus und Sicherheitsstopps.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct RcloneProtectionLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ProtectionActivityAttributes.self) { context in
            HStack(spacing: 12) {
                Image(systemName: context.state.error == nil ? "arrow.triangle.2.circlepath" : "exclamationmark.triangle.fill")
                    .foregroundStyle(context.state.error == nil ? .green : .red)
                VStack(alignment: .leading, spacing: 3) {
                    Text(context.state.pair).font(.headline)
                    Text(context.state.error ?? context.state.transferred ?? context.state.status)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if let percent = context.state.percent {
                    Text("\(Int(percent)) %").font(.headline.monospacedDigit())
                }
            }
            .padding()
            .activityBackgroundTint(Color(.secondarySystemBackground))
            .activitySystemActionForegroundColor(.green)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) { Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.green) }
                DynamicIslandExpandedRegion(.center) { Text(context.state.pair).lineLimit(1) }
                DynamicIslandExpandedRegion(.trailing) {
                    if let percent = context.state.percent { Text("\(Int(percent)) %").monospacedDigit() }
                }
                DynamicIslandExpandedRegion(.bottom) { Text(context.state.transferred ?? context.state.status).font(.caption) }
            } compactLeading: {
                Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.green)
            } compactTrailing: {
                if let percent = context.state.percent { Text("\(Int(percent))").monospacedDigit() }
            } minimal: {
                Image(systemName: "arrow.triangle.2.circlepath").foregroundStyle(.green)
            }
        }
    }
}

@main
struct RcloneProtectionWidgetBundle: WidgetBundle {
    var body: some Widget {
        RcloneProtectionWidget()
        RcloneProtectionLiveActivity()
    }
}
