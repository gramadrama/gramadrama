import AppKit
import ApplicationServices
import SwiftUI
import GramaCore
import GramaBackend

/// Położenie słów na ekranie z Accessibility API (etap 3d).
///
/// Pola natywne i Outlook (WebKit) podają `AXBoundsForRange` na samym polu. Slack (Chromium) zwraca tam
/// pusty prostokąt, ale poprawne wartości daje każdy element `AXStaticText` wewnątrz pola — wtedy
/// mapujemy zakres pola na te elementy (ich teksty leżą w wartości pola po kolei).
/// Wywołania AX — tylko na kolejce AX (`AXQueue`); pamięć elementów tekstu jest używana tylko tam.
enum TextGeometry {
    /// Elementy tekstu wewnątrz pola z ich położeniem w wartości pola (UTF-16).
    private struct Piece { let element: AXUIElement; let start: Int; let length: Int }
    /// Najdłuższy czas liczenia pozycji w jednym przejściu (kolejka AX obsługuje też ⌥⌘G).
    static let budget: TimeInterval = 0.8
    nonisolated(unsafe) private static var cachedPieces: (field: AXUIElement, value: String, pieces: [Piece])?

    /// Prostokąty słów z zakresów (współrzędne AX: od lewego górnego rogu głównego ekranu), po jednym na słowo,
    /// żeby błąd łamiący się między liniami nie dał jednego wielkiego prostokąta.
    static func wordRects(for ranges: [NSRange], in field: AXUIElement, value: String, fieldFrame: CGRect) -> [[CGRect]] {
        let ns = value as NSString
        // Zawieszona aplikacja odpowiada po limicie czasu na każde wywołanie — po budżecie reszta bez podkreśleń.
        let deadline = Date().addingTimeInterval(budget)
        let direct = usesFieldBounds(field, value: value, fieldFrame: fieldFrame)
        guard Date() < deadline else { return ranges.map { _ in [] } }
        func rects(_ pieces: [Piece]) -> [[CGRect]] {
            ranges.map { range in
                words(in: range, of: ns).compactMap { word -> CGRect? in
                    guard Date() < deadline else { return nil }
                    if direct { return valid(bounds(field, word), fieldFrame: fieldFrame) }
                    guard let piece = pieces.first(where: { $0.start <= word.location && NSMaxRange(word) <= $0.start + $0.length }) else { return nil }
                    return valid(bounds(piece.element, NSRange(location: word.location - piece.start, length: word.length)), fieldFrame: fieldFrame)
                }
            }
        }
        guard !direct else { return rects([]) }
        let result = rects(pieces(of: field, value: value, deadline: deadline))
        // Chromium potrafi podmienić elementy tekstu przy tym samym tekście (np. po wysłaniu i ponownym wpisaniu) —
        // pamięć wskazuje wtedy martwe elementy i podkreślenia znikają. Brak jakichkolwiek pozycji = czytamy od nowa.
        guard result.allSatisfy(\.isEmpty), !ranges.isEmpty, cachedPieces != nil, Date() < deadline else { return result }
        cachedPieces = nil
        return rects(pieces(of: field, value: value, deadline: deadline))
    }

    /// Słowa (ciągi bez białych znaków) w zakresie — zakres z samych białych znaków daje nic.
    static func words(in range: NSRange, of text: NSString) -> [NSRange] {
        var result: [NSRange] = []
        var start: Int?
        let end = min(NSMaxRange(range), text.length)
        var i = range.location
        while i <= end {
            let isSpace = i == end || (UnicodeScalar(text.character(at: i)).map(CharacterSet.whitespacesAndNewlines.contains) ?? false)
            if isSpace, let s = start { result.append(NSRange(location: s, length: i - s)); start = nil }
            if !isSpace, start == nil { start = i }
            i += 1
        }
        return result
    }

    /// Czy pole samo podaje sensowne pozycje: próbujemy pierwszego słowa. Slack zwraca pusty prostokąt
    /// albo prostokąt całego pola.
    private static func usesFieldBounds(_ field: AXUIElement, value: String, fieldFrame: CGRect) -> Bool {
        let ns = value as NSString
        guard let first = words(in: NSRange(location: 0, length: ns.length), of: ns).first,
              let rect = valid(bounds(field, first), fieldFrame: fieldFrame) else { return false }
        return !(rect.width >= fieldFrame.width * 0.9 && ns.length > first.length * 2)
    }

    private static func valid(_ rect: CGRect?, fieldFrame: CGRect) -> CGRect? {
        guard let rect, rect.width > 0, rect.height > 4, rect.height < 80,
              fieldFrame.insetBy(dx: -30, dy: -30).intersects(rect) else { return nil }
        return rect
    }

    static func bounds(_ element: AXUIElement, _ range: NSRange) -> CGRect? {
        var cf = CFRange(location: range.location, length: range.length)
        guard let value = AXValueCreate(.cfRange, &cf) else { return nil }
        var out: CFTypeRef?
        guard AXUIElementCopyParameterizedAttributeValue(element, kAXBoundsForRangeParameterizedAttribute as CFString, value, &out) == .success,
              let out, CFGetTypeID(out) == AXValueGetTypeID() else { return nil }
        var rect = CGRect.zero
        return AXValueGetValue(out as! AXValue, .cgRect, &rect) ? rect : nil
    }

    private static func pieces(of field: AXUIElement, value: String, deadline: Date) -> [Piece] {
        if let cached = cachedPieces, CFEqual(cached.field, field), cached.value == value { return cached.pieces }
        let ns = value as NSString
        var pieces: [Piece] = []
        var cursor = 0, visited = 0
        func walk(_ element: AXUIElement, _ depth: Int) {
            visited += 1
            guard visited < 600, Date() < deadline else { return }
            if TextBridge.string(element, kAXRoleAttribute) == "AXStaticText" {
                if let text = TextBridge.string(element, kAXValueAttribute), !text.isEmpty, cursor <= ns.length {
                    let found = ns.range(of: text, range: NSRange(location: cursor, length: ns.length - cursor))
                    if found.location != NSNotFound {
                        pieces.append(Piece(element: element, start: found.location, length: found.length))
                        cursor = NSMaxRange(found)
                    }
                }
                return
            }
            var children: CFTypeRef?
            guard depth < 10, AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children) == .success,
                  let list = children as? [AXUIElement] else { return }
            list.forEach { walk($0, depth + 1) }
        }
        walk(field, 0)
        // Przejście przerwane budżetem jest niepełne — nie zapamiętujemy go.
        if Date() < deadline { cachedPieces = (field, value, pieces) }
        return pieces
    }
}

// MARK: - Nakładka z podkreśleniami

/// Podkreślenie jednego słowa (współrzędne Cocoa) i błąd, do którego należy.
struct UnderlineMark {
    let rect: NSRect
    let issue: LiveIssue

    enum Kind { case spelling, grammar, style }
    var kind: Kind {
        if issue.source == .bielik { return .style }
        // Tryb „tylko model”: kolor według rodzaju zmiany (literówka, polskie znaki — jak pisownia).
        if issue.source == .model { return issue.rule == "MODEL_SPELLING" ? .spelling : .grammar }
        return issue.isSpelling || issue.rule == "PL_DIACRITICS" ? .spelling : .grammar
    }
}

/// Przezroczyste okno nad polem z falistymi podkreśleniami. Przepuszcza kliknięcia (pisanie i zaznaczanie
/// działają jak zwykle); najechanie myszą na podkreślone słowo pokazuje kartę z sugestiami do kliknięcia.
@MainActor
final class LiveUnderlines {
    var onApply: (LiveIssue, String) -> Void = { _, _ in }
    var onIgnore: (LiveIssue) -> Void = { _ in }
    var onLearn: (LiveIssue) -> Void = { _ in }
    var onTurnOff: (LiveIssue) -> Void = { _ in }

    private var window: NSWindow?
    private let canvas = UnderlineCanvas()
    private var marks: [UnderlineMark] = []
    private var mouseMonitor: Any?
    private var clickMonitor: Any?
    private lazy var card: IssueCard = IssueCard(
        onApply: { [weak self] issue, suggestion in self?.card.hide(); self?.onApply(issue, suggestion) },
        onIgnore: { [weak self] issue in self?.card.hide(); self?.onIgnore(issue) },
        onLearn: { [weak self] issue in self?.card.hide(); self?.onLearn(issue) },
        onTurnOff: { [weak self] issue in self?.card.hide(); self?.onTurnOff(issue) })
    /// Słowo, nad którym otwarto kartę — obszar słowa, przerwy i karty nie zamyka jej przy przejściu myszą.
    private var cardWord: NSRect?

    func show(_ marks: [UnderlineMark], clip: NSRect) {
        self.marks = marks
        guard !marks.isEmpty else { hide(); return }
        let frame = clip.insetBy(dx: -2, dy: -6)
        let window = self.window ?? makeWindow()
        if window.frame != frame { window.setFrame(frame, display: false) }
        canvas.frame = NSRect(origin: .zero, size: frame.size)
        canvas.marks = marks.map { mark in
            UnderlineCanvas.Line(rect: mark.rect.offsetBy(dx: -frame.minX, dy: -frame.minY), kind: mark.kind)
        }
        canvas.needsDisplay = true
        if !window.isVisible { window.orderFrontRegardless() }
        startMouseMonitor()
    }

    func hide() {
        marks = []
        window?.orderOut(nil)
        card.hide()
        cardWord = nil
        if let mouseMonitor { NSEvent.removeMonitor(mouseMonitor) }
        if let clickMonitor { NSEvent.removeMonitor(clickMonitor) }
        mouseMonitor = nil
        clickMonitor = nil
    }

    private func makeWindow() -> NSWindow {
        let window = NSWindow(contentRect: .zero, styleMask: .borderless, backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.level = .floating
        window.ignoresMouseEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle, .stationary]
        window.contentView = canvas
        self.window = window
        return window
    }

    /// Globalne zdarzenia myszy przychodzą tylko z innych aplikacji — gdy mysz jest nad naszą kartą, nic
    /// nie przychodzi i karta zostaje otwarta.
    private func startMouseMonitor() {
        guard mouseMonitor == nil else { return }
        mouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) { [weak self] _ in
            MainActor.assumeIsolated { self?.mouseMoved(NSEvent.mouseLocation) }
        }
        // Kliknięcie gdziekolwiek indziej (np. dalsze pisanie w polu) zamyka kartę.
        clickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] _ in
            MainActor.assumeIsolated { self?.closeCard() }
        }
    }

    private func closeCard() {
        card.hide()
        cardWord = nil
    }

    private func mouseMoved(_ location: NSPoint) {
        if let word = cardWord, let cardFrame = card.frame, word.union(cardFrame).insetBy(dx: -4, dy: -4).contains(location) { return }
        if let mark = marks.first(where: { $0.rect.insetBy(dx: -1, dy: -3).contains(location) }) {
            card.show(mark.issue, below: mark.rect)
            cardWord = mark.rect
        } else {
            closeCard()
        }
    }
}

private final class UnderlineCanvas: NSView {
    struct Line { let rect: NSRect; let kind: UnderlineMark.Kind }
    var marks: [Line] = []

    override var isFlipped: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        for mark in marks {
            // Cegła dla każdego rodzaju (BRAND.md): pisownię, gramatykę i styl rozróżnia komunikat na karcie,
            // a kształt fali odróżnia to, co wykryło sprawdzanie, od poprawek w panelu.
            NSColor(Brand.wave).setStroke()
            // Falka tuż pod linią bazową słowa (dolna krawędź prostokąta to dół linii tekstu).
            let path = NSBezierPath()
            let y = mark.rect.minY + 1.5, step: CGFloat = 2.5
            var x = mark.rect.minX
            path.move(to: NSPoint(x: x, y: y))
            var up = true
            while x < mark.rect.maxX {
                x = min(x + step, mark.rect.maxX)
                path.line(to: NSPoint(x: x, y: up ? y + 1.6 : y))
                up.toggle()
            }
            path.lineWidth = 1.4
            path.lineCapStyle = .round
            path.lineJoinStyle = .round
            path.stroke()
        }
    }
}

// MARK: - Karta z sugestią

@MainActor
private final class IssueCard {
    private var panel: NSPanel?
    private var shown: LiveIssue?
    private let onApply: (LiveIssue, String) -> Void
    private let onIgnore: (LiveIssue) -> Void
    private let onLearn: (LiveIssue) -> Void
    private let onTurnOff: (LiveIssue) -> Void

    init(onApply: @escaping (LiveIssue, String) -> Void, onIgnore: @escaping (LiveIssue) -> Void,
         onLearn: @escaping (LiveIssue) -> Void, onTurnOff: @escaping (LiveIssue) -> Void) {
        self.onApply = onApply
        self.onIgnore = onIgnore
        self.onLearn = onLearn
        self.onTurnOff = onTurnOff
    }

    var frame: NSRect? { panel?.isVisible == true ? panel?.frame : nil }

    func show(_ issue: LiveIssue, below word: NSRect) {
        if shown == issue, panel?.isVisible == true { return }
        shown = issue
        let view = IssueCardView(issue: issue,
                                 apply: { [weak self] in self?.onApply(issue, $0) },
                                 ignore: { [weak self] in self?.onIgnore(issue) },
                                 learn: issue.isSpelling || issue.rule == "PL_DIACRITICS" ? { [weak self] in self?.onLearn(issue) } : nil,
                                 turnOff: issue.canTurnOff ? { [weak self] in self?.onTurnOff(issue) } : nil)
        let hosting = ClickThroughHostingView(rootView: view)
        hosting.layoutSubtreeIfNeeded()
        let size = hosting.fittingSize
        let panel = self.panel ?? {
            let panel = CardPanel(contentRect: .zero, styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.level = .floating
            panel.hidesOnDeactivate = false
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
            self.panel = panel
            return panel
        }()
        panel.contentView = hosting
        var origin = NSPoint(x: word.minX - 8, y: word.minY - size.height - 4)
        if let visible = (NSScreen.screens.first { $0.frame.intersects(word) } ?? NSScreen.main)?.visibleFrame {
            if origin.y < visible.minY { origin.y = word.maxY + 4 }
            origin.x = min(max(origin.x, visible.minX + 4), visible.maxX - size.width - 4)
        }
        panel.setFrame(NSRect(origin: origin, size: size), display: true)
        panel.orderFrontRegardless()
    }

    func hide() {
        shown = nil
        panel?.orderOut(nil)
    }
}

/// Karta nie aktywuje GramaDramy (fokus i kursor zostają w Slacku), a przyciski reagują na pierwsze kliknięcie.
private final class CardPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class ClickThroughHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

struct IssueCardView: View {
    let issue: LiveIssue
    let apply: (String) -> Void
    let ignore: () -> Void
    let learn: (() -> Void)?
    /// „Wyłącz regułę” — dla reguły LanguageTool o gramatyce, interpunkcji albo stylu.
    var turnOff: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(tr(issue.message)).font(.caption).foregroundStyle(Brand.secondaryInk).fixedSize(horizontal: false, vertical: true)
            if issue.suggestions.isEmpty {
                Text(issue.word).font(.callout.weight(.semibold)).waveUnderline()
            } else {
                HStack(spacing: 6) {
                    ForEach(issue.suggestions.prefix(3), id: \.self) { suggestion in
                        Button { apply(suggestion) } label: { Text(suggestion) }
                            .buttonStyle(SuggestionButtonStyle(first: suggestion == issue.suggestions.first))
                    }
                }
            }
            HStack(spacing: 12) {
                Button(tr("Ignoruj"), action: ignore)
                if let learn { Button(tr("Dodaj do słownika"), action: learn) }
                if let turnOff { Button(tr("Wyłącz regułę"), action: turnOff) }
                Spacer(minLength: 8)
                // Warstwa modelu nazywa się w kodzie „bielik”, ale na karcie stoi model, który naprawdę działa.
                Text(issue.source == .bielik || issue.source == .model ? Settings.shared.modelDisplayName : tr(issue.source.rawValue))
                    .foregroundStyle(Brand.secondaryInk.opacity(0.8))
            }
            .buttonStyle(.plain)
            .font(.caption)
            .foregroundStyle(Brand.secondaryInk)
        }
        .padding(10)
        .frame(minWidth: 220, maxWidth: 320, alignment: .leading)
        .fixedSize()
        .foregroundStyle(Brand.ink)
        .background(Brand.surface.opacity(0.98), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Brand.line))
    }
}
