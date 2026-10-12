// Paleta i logo marki (branding/BRAND.md), wspólne z wersją na Linuksa — zmiana tutaj = zmiana tam.
import SwiftUI

/// Każdy kolor ma jedno znaczenie, na ciepłych neutralnych tłach; wersja ciemna to jaśniejszy odcień
/// tego samego koloru (BRAND.md, „Kolory”).
enum Brand {
    /// Akcje aplikacji: przyciski, sugestie do kliknięcia, wybrana zakładka.
    static let lavender = adaptive(0x66579E, dark: 0xBCB0EC)
    static let lavenderHover = adaptive(0x574A88, dark: 0xDCD5F2)
    /// Wciśnięty główny przycisk (`action-primary-pressed`).
    static let lavenderPressed = adaptive(0x4B3F77, dark: 0xA99CE0)
    /// Tło drugorzędnej sugestii (lawenda w tle).
    static let lavenderTint = adaptive(0xEEEAF7, dark: 0x353047)
    static let lavenderTintHover = adaptive(0xDCD5F2, dark: 0x423B5C)
    /// Tekst na lawendzie.
    static let onLavender = adaptive(0xFFFEFC, dark: 0x181B1B)

    /// To, co wykryło sprawdzanie: tekst i licznik (fala pod słowem to `wave`).
    static let brick = adaptive(0xAD4E2C, dark: 0xEBA27F)
    /// Fala pod słowem — w każdej aplikacji i obu motywach ta sama (jak na Linuksie).
    static let wave = rgb(0xD07A52)
    /// Plakietka bieżącego sprawdzania nad dowolną aplikacją: biały na niej 5,4:1.
    static let badge = rgb(0xAD4E2C)
    static let badgeHover = rgb(0x9A4426)

    /// Poprawiony tekst, „działa”, „Zero dramatu”.
    static let sage = adaptive(0x536B60, dark: 0xADC8B5)
    static let sageBackground = adaptive(0xE9F0E9, dark: 0x34483C)

    /// Błąd aplikacji (np. serwer nie odpowiada).
    static let rose = adaptive(0x9E3446, dark: 0xF2A7AE)
    /// Tło zdania nieudanej korekty (`error-tint`, to samo w obu motywach).
    static let roseTint = Color(nsColor: nsColor(0xC4566A).withAlphaComponent(0.16))

    static let ink = adaptive(0x343337, dark: 0xF1EEE8)
    static let secondaryInk = adaptive(0x6B6870, dark: 0xB9BDB8)
    static let disabledInk = adaptive(0x8E8992, dark: 0x7A847C)
    static let surface = adaptive(0xFFFEFC, dark: 0x242827)
    /// Subtelne tło: lista zmian, aktywny tryb, najechanie, żeton usuniętej interpunkcji.
    static let subtle = adaptive(0xF0EDE7, dark: 0x303631)
    static let pressed = adaptive(0xE7E3DC, dark: 0x3A413C)
    static let line = adaptive(0xDDD8D0, dark: 0x505852)
    /// Logo w panelu, pigułce i komunikacie: same maski, neutralne.
    static let logo = adaptive(0x4D4B4F, dark: 0xB9BDB8)

    private static func rgb(_ hex: UInt32) -> Color { Color(nsColor: nsColor(hex)) }

    private static func nsColor(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    private static func adaptive(_ light: UInt32, dark: UInt32) -> Color {
        let light = nsColor(light), dark = nsColor(dark)
        return Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        })
    }
}

/// Logo: dwie maski — wesoła z przodu, smutna (lekko przygaszona) za nią; oczy i usta wycięte.
/// Kształt z `linux/icon.svg` (współrzędne 64×64, z niego robi też ikonę `scripts/make-icon.swift`).
struct MasksLogo: View {
    /// Pełne po wyniku, konturowe w trakcie pracy.
    var filled = true
    var color = Brand.logo

    var body: some View {
        Canvas { context, size in
            let scale = min(size.width, size.height) / 64
            let transform = CGAffineTransform(translationX: (size.width - 64 * scale) / 2, y: (size.height - 64 * scale) / 2)
                .scaledBy(x: scale, y: scale)
            for mask in [MaskPaths.sad, MaskPaths.happy] {
                let path = mask.applying(transform)
                if filled {
                    context.fill(path, with: .color(color), style: FillStyle(eoFill: true))
                } else {
                    context.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: 3.2 * scale, lineJoin: .round))
                }
            }
        }
        .accessibilityHidden(true)
    }
}

enum MaskPaths {
    static let sad = path("M35.6 20.1 C36.8 17.2 39.2 16.4 42.2 17.2 L56.8 21.5 C60.4 22.6 62 25.2 61 29 L57.6 42.7 C55.4 51.2 49.4 54.8 42.3 52.7 C38.8 51.7 35.6 49.7 33.4 46.6 C37.8 42.2 39.3 36.9 37.6 30.2 Z M40.4 27.9 C41.6426 27.9 42.65 28.9074 42.65 30.15 C42.65 31.3926 41.6426 32.4 40.4 32.4 C39.1574 32.4 38.15 31.3926 38.15 30.15 C38.15 28.9074 39.1574 27.9 40.4 27.9 Z M50.4 30.7 C51.6426 30.7 52.65 31.7074 52.65 32.95 C52.65 34.1926 51.6426 35.2 50.4 35.2 C49.1574 35.2 48.15 34.1926 48.15 32.95 C48.15 31.7074 49.1574 30.7 50.4 30.7 Z M38.3 40.7 C40.8 35.2 48.2 36 51.1 42.6 C46.9 40.2 42.7 39.7 39 41.2 C38.4 41.4 38.1 41.2 38.3 40.7 Z")
    static let happy = path("M25.3 12.7 C29.2 11.7 31.3 13.7 32.1 17 L35.7 31.3 C37.8 39.3 34.8 45.4 27.3 47.6 C20 49.8 12.4 47.7 10.1 40.1 L4.6 23.2 C3.3 19 4.8 17.2 8.4 16.2 Z M12.4 29.1 C11.6 27 13 25.3 14.6 25 C16.5 24.6 18.4 26.3 18.2 27.7 C18.1 28.3 17.5 28 16.6 27.9 C15.2 27.5 14.2 28.2 13.3 29 C12.9 29.4 12.6 29.5 12.4 29.1 Z M22.5 26.3 C21.7 24.2 23.1 22.5 24.7 22.2 C26.6 21.8 28.5 23.5 28.3 24.9 C28.2 25.5 27.6 25.2 26.7 25.1 C25.3 24.7 24.3 25.4 23.4 26.2 C23 26.6 22.7 26.7 22.5 26.3 Z M15.4 34.3 C19.7 34.7 24.8 33.5 29 30.7 C29.7 30.2 30.1 30.6 30 31.4 C29.6 36.3 26.9 39.1 23.6 39.7 C20.4 40.3 16.9 38.7 14.9 35.4 C14.5 34.7 14.7 34.2 15.4 34.3 Z")

    /// Ścieżka SVG z bezwzględnymi M, L, C i Z — tylko tyle mają maski.
    static func path(_ svg: String) -> Path {
        var path = Path()
        var command = Character("M")
        var numbers: [CGFloat] = []
        func flush() {
            switch command {
            case "M" where numbers.count == 2: path.move(to: CGPoint(x: numbers[0], y: numbers[1]))
            case "L" where numbers.count == 2: path.addLine(to: CGPoint(x: numbers[0], y: numbers[1]))
            case "C" where numbers.count == 6:
                path.addCurve(to: CGPoint(x: numbers[4], y: numbers[5]), control1: CGPoint(x: numbers[0], y: numbers[1]),
                              control2: CGPoint(x: numbers[2], y: numbers[3]))
            case "Z": path.closeSubpath()
            default: return
            }
            numbers = []
        }
        for var token in svg.split(separator: " ") {
            // Litera polecenia stoi przy pierwszej liczbie („M35.6”).
            if let letter = token.first, letter.isLetter {
                command = letter
                numbers = []
                if letter == "Z" { flush() }
                token = token.dropFirst()
            }
            guard let value = Double(token) else { continue }
            numbers.append(CGFloat(value))
            flush()
        }
        return path
    }
}

/// Fala pod słowem, które wykryło sprawdzanie — jak w podkreśleniach (`LiveUnderlines`).
struct WaveLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let step: CGFloat = 2.5
        var x = rect.minX
        var up = false
        path.move(to: CGPoint(x: x, y: rect.maxY))
        while x < rect.maxX {
            x = min(x + step, rect.maxX)
            up.toggle()
            path.addLine(to: CGPoint(x: x, y: up ? rect.minY : rect.maxY))
        }
        return path
    }
}

extension View {
    /// Słowo z falą pod spodem (lista plakietki, karta podkreślenia).
    func waveUnderline() -> some View {
        padding(.bottom, 3).overlay(alignment: .bottom) {
            WaveLine().stroke(Brand.wave, style: StrokeStyle(lineWidth: 1.4, lineCap: .round, lineJoin: .round))
                .frame(height: 1.6)
        }
    }
}

/// Przyciski panelu: drugorzędny z obrysem, główny w lawendzie (kolor akcji aplikacji).
struct BrandButtonStyle: ButtonStyle {
    var primary = false
    var compact = false

    func makeBody(configuration: Configuration) -> some View {
        BrandButton(configuration: configuration, primary: primary, compact: compact)
    }

    private struct BrandButton: View {
        let configuration: ButtonStyleConfiguration
        let primary: Bool
        let compact: Bool
        @Environment(\.isEnabled) private var isEnabled
        @State private var hovering = false

        var body: some View {
            let shape = RoundedRectangle(cornerRadius: compact ? 6 : 8, style: .continuous)
            configuration.label
                .font(compact ? .caption.bold() : .callout.bold())
                .padding(.horizontal, compact ? 10 : 16).padding(.vertical, compact ? 2 : 6)
                .foregroundStyle(foreground)
                .background(background, in: shape)
                .overlay(shape.strokeBorder(primary ? .clear : isEnabled ? Brand.line : Brand.pressed))
                .contentShape(shape)
                .onHover { hovering = $0 }
        }

        private var foreground: Color {
            guard isEnabled else { return Brand.disabledInk }
            return primary ? Brand.onLavender : Brand.ink
        }

        private var background: Color {
            if primary {
                guard isEnabled else { return Brand.subtle }
                return configuration.isPressed ? Brand.lavenderPressed : hovering ? Brand.lavenderHover : Brand.lavender
            }
            guard isEnabled else { return .clear }
            return configuration.isPressed ? Brand.pressed : hovering ? Brand.subtle : .clear
        }
    }
}

/// Sugestia to akcja (karta podkreślenia, lista plakietki): pierwsza w lawendzie, kolejne na lawendowym tle.
struct SuggestionButtonStyle: ButtonStyle {
    let first: Bool

    func makeBody(configuration: Configuration) -> some View {
        Suggestion(configuration: configuration, first: first)
    }

    private struct Suggestion: View {
        let configuration: ButtonStyleConfiguration
        let first: Bool
        @State private var hovering = false

        var body: some View {
            let active = hovering || configuration.isPressed
            configuration.label
                .font(.callout.bold())
                .foregroundStyle(first ? Brand.onLavender : Brand.lavender)
                .padding(.horizontal, 8).padding(.vertical, 2)
                .background(first ? (active ? Brand.lavenderHover : Brand.lavender) : (active ? Brand.lavenderTintHover : Brand.lavenderTint),
                            in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
        }
    }
}
