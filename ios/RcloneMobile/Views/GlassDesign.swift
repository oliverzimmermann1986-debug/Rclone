import SwiftUI
import UIKit

enum SicherpfadGlassAppearance {
    private static var configured = false

    @MainActor
    static func configure() {
        guard !configured else { return }
        configured = true

        let navAppearance = UINavigationBarAppearance()
        navAppearance.configureWithTransparentBackground()
        navAppearance.backgroundEffect = UIBlurEffect(style: .systemUltraThinMaterial)
        navAppearance.shadowColor = UIColor.white.withAlphaComponent(0.16)
        UINavigationBar.appearance().standardAppearance = navAppearance
        UINavigationBar.appearance().compactAppearance = navAppearance

        let scrollEdgeNav = UINavigationBarAppearance()
        scrollEdgeNav.configureWithTransparentBackground()
        scrollEdgeNav.shadowColor = .clear
        UINavigationBar.appearance().scrollEdgeAppearance = scrollEdgeNav

        let tabAppearance = UITabBarAppearance()
        tabAppearance.configureWithTransparentBackground()
        tabAppearance.backgroundEffect = UIBlurEffect(style: .systemUltraThinMaterial)
        tabAppearance.shadowColor = UIColor.white.withAlphaComponent(0.18)
        UITabBar.appearance().standardAppearance = tabAppearance
        UITabBar.appearance().scrollEdgeAppearance = tabAppearance
    }
}

enum SicherpfadGlassPalette {
    static let emerald = Color(red: 0.16, green: 0.78, blue: 0.56)
    static let teal = Color(red: 0.20, green: 0.68, blue: 0.78)
    static let jade = Color(red: 0.10, green: 0.48, blue: 0.39)
    static let glintDark = Color(red: 0.82, green: 1.00, blue: 0.94)
    static let warmSand = Color(red: 0.90, green: 0.86, blue: 0.69)

    static func canvasBase(for scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color(red: 0.035, green: 0.085, blue: 0.088)
            : Color(red: 0.87, green: 0.935, blue: 0.915)
    }

    static func cardFillGradient(for scheme: ColorScheme, prominent: Bool) -> LinearGradient {
        if scheme == .dark {
            return LinearGradient(
                colors: [
                    Color(red: 0.16, green: 0.31, blue: 0.29).opacity(prominent ? 0.52 : 0.38),
                    Color(red: 0.08, green: 0.19, blue: 0.20).opacity(prominent ? 0.32 : 0.22)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        } else {
            return LinearGradient(
                colors: [
                    Color.white.opacity(prominent ? 0.62 : 0.48),
                    Color(red: 0.92, green: 0.98, blue: 0.96).opacity(prominent ? 0.32 : 0.22)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
    }

    static func borderGradient(for scheme: ColorScheme, accent: Color, prominent: Bool) -> LinearGradient {
        if scheme == .dark {
            return LinearGradient(
                colors: [
                    glintDark.opacity(prominent ? 0.65 : 0.46),
                    accent.opacity(prominent ? 0.52 : 0.32),
                    glintDark.opacity(0.14),
                    accent.opacity(prominent ? 0.28 : 0.16)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        } else {
            return LinearGradient(
                colors: [
                    Color.white.opacity(prominent ? 0.98 : 0.88),
                    accent.opacity(prominent ? 0.42 : 0.24),
                    Color.white.opacity(0.36),
                    Color.white.opacity(prominent ? 0.72 : 0.55)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
    }

    static func rowFillColor(for scheme: ColorScheme, accent: Color?) -> Color {
        if let accent {
            return accent.opacity(scheme == .dark ? 0.18 : 0.12)
        }
        return scheme == .dark
            ? Color(red: 0.12, green: 0.24, blue: 0.23).opacity(0.34)
            : Color.white.opacity(0.46)
    }
}

struct SicherpfadGlassBackdrop: View {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    var accent: Color = SicherpfadGlassPalette.emerald

    var body: some View {
        ZStack {
            if reduceTransparency {
                Color(.systemGroupedBackground)
            } else {
                SicherpfadGlassPalette.canvasBase(for: colorScheme)

                GeometryReader { proxy in
                    let w = max(proxy.size.width, 320)
                    let h = max(proxy.size.height, 640)

                    ZStack {
                        // Top-right vivid teal-cyan glow orb
                        RadialGradient(
                            colors: [
                                SicherpfadGlassPalette.teal.opacity(colorScheme == .dark ? 0.56 : 0.46),
                                SicherpfadGlassPalette.teal.opacity(colorScheme == .dark ? 0.16 : 0.12),
                                .clear
                            ],
                            center: .center,
                            startRadius: 10,
                            endRadius: w * 0.88
                        )
                        .frame(width: w * 1.5, height: w * 1.5)
                        .position(x: w * 0.86, y: h * 0.04)

                        // Upper-left emerald accent orb
                        RadialGradient(
                            colors: [
                                accent.opacity(colorScheme == .dark ? 0.46 : 0.36),
                                SicherpfadGlassPalette.jade.opacity(colorScheme == .dark ? 0.18 : 0.10),
                                .clear
                            ],
                            center: .center,
                            startRadius: 10,
                            endRadius: w * 0.82
                        )
                        .frame(width: w * 1.4, height: w * 1.4)
                        .position(x: w * 0.14, y: h * 0.24)

                        // Bottom-right warm ambient contrast orb
                        RadialGradient(
                            colors: [
                                (colorScheme == .dark ? SicherpfadGlassPalette.jade : SicherpfadGlassPalette.warmSand)
                                    .opacity(colorScheme == .dark ? 0.40 : 0.52),
                                .clear
                            ],
                            center: .center,
                            startRadius: 10,
                            endRadius: w * 0.78
                        )
                        .frame(width: w * 1.35, height: w * 1.35)
                        .position(x: w * 0.90, y: h * 0.86)

                        // Bottom-left deep cyan-emerald glow
                        RadialGradient(
                            colors: [
                                SicherpfadGlassPalette.teal.opacity(colorScheme == .dark ? 0.30 : 0.24),
                                .clear
                            ],
                            center: .center,
                            startRadius: 10,
                            endRadius: w * 0.70
                        )
                        .frame(width: w * 1.2, height: w * 1.2)
                        .position(x: w * 0.10, y: h * 0.78)

                        // Diagonal specular light beam
                        LinearGradient(
                            colors: [
                                .clear,
                                (colorScheme == .dark ? SicherpfadGlassPalette.glintDark : Color.white)
                                    .opacity(colorScheme == .dark ? 0.10 : 0.44),
                                .clear
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    }
                    .frame(width: proxy.size.width, height: proxy.size.height)
                }
            }
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

private struct SicherpfadGlassScreenModifier: ViewModifier {
    var accent: Color

    func body(content: Content) -> some View {
        content
            .scrollContentBackground(.hidden)
            .background {
                SicherpfadGlassBackdrop(accent: accent)
            }
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbarBackground(.ultraThinMaterial, for: .tabBar)
    }
}

private struct SicherpfadGlassCardModifier: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    let cornerRadius: CGFloat
    let accent: Color
    let prominent: Bool

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)

        if reduceTransparency || contrast == .increased {
            content
                .background(Color(.secondarySystemGroupedBackground), in: shape)
                .overlay {
                    shape.stroke(accent.opacity(0.45), lineWidth: 1.25)
                }
        } else {
            content
                .background {
                    ZStack {
                        shape.fill(prominent ? .thinMaterial : .ultraThinMaterial)
                        shape.fill(SicherpfadGlassPalette.cardFillGradient(for: colorScheme, prominent: prominent))
                        if prominent {
                            RadialGradient(
                                colors: [
                                    accent.opacity(colorScheme == .dark ? 0.28 : 0.20),
                                    .clear
                                ],
                                center: .topTrailing,
                                startRadius: 8,
                                endRadius: 240
                            )
                            .clipShape(shape)
                        }
                    }
                }
                .overlay {
                    ZStack {
                        shape.strokeBorder(
                            SicherpfadGlassPalette.borderGradient(for: colorScheme, accent: accent, prominent: prominent),
                            lineWidth: prominent ? 1.4 : 1.15
                        )
                        shape.stroke(
                            LinearGradient(
                                colors: [
                                    (colorScheme == .dark ? SicherpfadGlassPalette.glintDark : Color.white)
                                        .opacity(colorScheme == .dark ? 0.36 : 0.85),
                                    .clear
                                ],
                                startPoint: .top,
                                endPoint: .center
                            ),
                            lineWidth: 0.75
                        )
                        .padding(0.75)
                    }
                }
                .shadow(
                    color: accent.opacity(colorScheme == .dark ? (prominent ? 0.34 : 0.18) : (prominent ? 0.22 : 0.12)),
                    radius: prominent ? 24 : 14,
                    x: 0,
                    y: prominent ? 10 : 6
                )
                .shadow(
                    color: Color.black.opacity(colorScheme == .dark ? 0.45 : 0.10),
                    radius: prominent ? 22 : 12,
                    x: 0,
                    y: prominent ? 12 : 6
                )
        }
    }
}

struct SicherpfadGlassRowBackground: View {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    var accent: Color? = nil

    var body: some View {
        if reduceTransparency {
            if let accent {
                accent.opacity(0.14)
            } else {
                Color(.secondarySystemGroupedBackground)
            }
        } else {
            ZStack {
                Rectangle().fill(.ultraThinMaterial)
                Rectangle().fill(SicherpfadGlassPalette.rowFillColor(for: colorScheme, accent: accent))
            }
        }
    }
}

private struct SicherpfadGlassBadgeModifier: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let color: Color

    func body(content: Content) -> some View {
        if reduceTransparency {
            content
                .background(color.opacity(0.16), in: Capsule())
                .overlay(Capsule().stroke(color.opacity(0.4), lineWidth: 1))
        } else {
            content
                .background {
                    ZStack {
                        Capsule().fill(.ultraThinMaterial)
                        Capsule().fill(color.opacity(colorScheme == .dark ? 0.22 : 0.14))
                    }
                }
                .overlay {
                    Capsule().strokeBorder(
                        LinearGradient(
                            colors: [
                                Color.white.opacity(colorScheme == .dark ? 0.45 : 0.85),
                                color.opacity(colorScheme == .dark ? 0.55 : 0.38)
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ),
                        lineWidth: 1
                    )
                }
                .shadow(color: color.opacity(colorScheme == .dark ? 0.32 : 0.18), radius: 8, x: 0, y: 3)
        }
    }
}

struct SicherpfadGlassProminentButtonStyle: ButtonStyle {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.isEnabled) private var isEnabled
    var accent: Color = SicherpfadGlassPalette.emerald
    var cornerRadius: CGFloat = 16

    func makeBody(configuration: Configuration) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        configuration.label
            .foregroundStyle(.white)
            .background {
                ZStack {
                    shape.fill(
                        LinearGradient(
                            colors: [
                                accent.opacity(isEnabled ? 0.96 : 0.42),
                                SicherpfadGlassPalette.jade.opacity(isEnabled ? 0.94 : 0.38)
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    shape.fill(
                        LinearGradient(
                            colors: [
                                Color.white.opacity(0.32),
                                .clear
                            ],
                            startPoint: .top,
                            endPoint: .center
                        )
                    )
                }
            }
            .overlay {
                shape.strokeBorder(
                    LinearGradient(
                        colors: [
                            Color.white.opacity(0.68),
                            accent.opacity(0.35),
                            Color.white.opacity(0.18)
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ),
                    lineWidth: 1.2
                )
            }
            .shadow(
                color: accent.opacity(isEnabled ? (colorScheme == .dark ? 0.46 : 0.34) : 0),
                radius: configuration.isPressed ? 8 : 16,
                x: 0,
                y: configuration.isPressed ? 3 : 8
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1.0)
            .opacity(configuration.isPressed ? 0.92 : 1.0)
            .animation(.easeOut(duration: 0.14), value: configuration.isPressed)
    }
}

struct SicherpfadGlassSecondaryButtonStyle: ButtonStyle {
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.isEnabled) private var isEnabled
    var accent: Color = SicherpfadGlassPalette.emerald
    var cornerRadius: CGFloat = 16

    func makeBody(configuration: Configuration) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        configuration.label
            .foregroundStyle(isEnabled ? .primary : .secondary)
            .background {
                if reduceTransparency {
                    shape.fill(Color(.secondarySystemGroupedBackground))
                } else {
                    ZStack {
                        shape.fill(.ultraThinMaterial)
                        shape.fill(
                            (colorScheme == .dark ? Color.white.opacity(0.08) : Color.white.opacity(0.45))
                        )
                    }
                }
            }
            .overlay {
                shape.strokeBorder(
                    SicherpfadGlassPalette.borderGradient(for: colorScheme, accent: accent, prominent: false),
                    lineWidth: 1.1
                )
            }
            .shadow(
                color: Color.black.opacity(colorScheme == .dark ? 0.30 : 0.08),
                radius: configuration.isPressed ? 4 : 10,
                x: 0,
                y: configuration.isPressed ? 2 : 5
            )
            .scaleEffect(configuration.isPressed ? 0.985 : 1.0)
            .opacity(isEnabled ? (configuration.isPressed ? 0.88 : 1.0) : 0.55)
            .animation(.easeOut(duration: 0.14), value: configuration.isPressed)
    }
}

extension View {
    func sicherpfadGlassScreen(accent: Color = SicherpfadGlassPalette.emerald) -> some View {
        modifier(SicherpfadGlassScreenModifier(accent: accent))
    }

    func sicherpfadGlassCard(
        cornerRadius: CGFloat = 22,
        accent: Color = SicherpfadGlassPalette.emerald,
        prominent: Bool = false
    ) -> some View {
        modifier(SicherpfadGlassCardModifier(cornerRadius: cornerRadius, accent: accent, prominent: prominent))
    }

    func sicherpfadGlassBadge(color: Color) -> some View {
        modifier(SicherpfadGlassBadgeModifier(color: color))
    }

    func sicherpfadGlassListRow(accent: Color? = nil) -> some View {
        listRowBackground(SicherpfadGlassRowBackground(accent: accent))
    }
}
