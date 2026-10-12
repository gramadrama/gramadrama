import AppKit
import Carbon.HIToolbox
import SwiftUI
import GramaCore
import GramaBackend

/// Wynik poprawki jako pole (PANEL.md, „Treść” i „Edycja”): sam poprawiony tekst, a to, co poprawka zmieniła, na tle
/// szałwii (`marks`). Poza edycją tylko do odczytu: klik w tekst włącza edycję z kursorem w tym miejscu, pisanie
/// (litera, Backspace) — edycję z tym klawiszem, a ↩ i ⎋ działają jak poza edycją. W edycji to samo pole przyjmuje
/// tekst, więc nic nie skacze; szałwia pokazuje wtedy, co tekst zmienia względem oryginału. Pole sprawdza GramaDrama,
/// tak jak tekst w innych aplikacjach (`EditorChecker`: słownik na Macu od razu, LanguageTool po chwili bez pisania),
/// i rysuje falę pod błędami — systemowe sprawdzanie pisowni jest wyłączone, żeby nie było dwóch rodzajów podkreśleń.
/// Bez autokorekty i zamiany cudzysłowów czy myślników: pole niczego nie zmienia samo. ↩ wstawia wynik (`onSubmit`),
/// ⇧↩ i ⌥↩ dają nową linię, ⎋ kończy edycję (`onEscape`) — panel zamyka dopiero następne ⎋. Wysokość z treści,
/// najwyżej `maxHeight`, dalej przewijanie.
struct ResultField: NSViewRepresentable {
    /// Poza edycją wynik, w edycji — `draft`.
    let text: String
    let editing: Bool
    /// Na tle szałwii (UTF-16 w `text`).
    let marks: [NSRange]
    let issues: [LiveIssue]
    let proxy: PanelEditorProxy
    var maxHeight: CGFloat = 220
    let onChange: (String) -> Void
    let onSelect: (NSRange) -> Void
    /// Miejsce w wyniku (nil — koniec) i klawisz, który zaczął edycję.
    let onEdit: (_ offset: Int?, _ event: NSEvent?) -> Void
    let onSubmit: () -> Void
    let onEscape: () -> Void

    func makeNSView(context: Context) -> NSScrollView {
        // TextKit 1: tło zmian (atrybuty tymczasowe) i położenie słów pod falę liczy `NSLayoutManager`.
        let textView = ResultTextView(usingTextLayoutManager: false)
        textView.delegate = context.coordinator
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.isContinuousSpellCheckingEnabled = false
        textView.isGrammarCheckingEnabled = false
        textView.isAutomaticSpellingCorrectionEnabled = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.isAutomaticLinkDetectionEnabled = false
        textView.font = .systemFont(ofSize: NSFont.systemFontSize)
        textView.textColor = NSColor(Brand.ink)
        textView.insertionPointColor = NSColor(Brand.lavender)
        textView.drawsBackground = false
        textView.textContainerInset = .zero
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.string = text
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = textView
        proxy.textView = textView
        update(textView)
        // Klawisze do pola od razu po wyniku — pierwsza litera zaczyna edycję.
        DispatchQueue.main.async {
            if !(textView.window?.firstResponder is NSTextView) { textView.window?.makeFirstResponder(textView) }
        }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let textView = scroll.documentView as? ResultTextView else { return }
        update(textView)
    }

    private func update(_ textView: ResultTextView) {
        textView.actions = (onEdit, onSubmit, onEscape)
        let started = editing && !textView.isEditable
        let ended = !editing && textView.isEditable
        if textView.string != text { textView.string = text }
        textView.isEditable = editing
        textView.marks = marks
        textView.issues = editing ? issues : []
        if ended {
            // Następna edycja zaczyna się od nowego wyniku — ⌘Z nie wraca do tekstu sprzed niej.
            textView.undoManager?.removeAllActions(withTarget: textView.textStorage as Any)
            textView.setSelectedRange(NSRange(location: 0, length: 0))
        }
        guard started else { return }
        // Kursor tam, gdzie kliknięto (albo na końcu), i klawisz, który włączył edycję.
        let proxy = proxy
        DispatchQueue.main.async {
            textView.window?.makeFirstResponder(textView)
            let length = (textView.string as NSString).length
            textView.setSelectedRange(NSRange(location: min(proxy.pendingCaret ?? length, length), length: 0))
            if let event = proxy.pendingEvent { textView.keyDown(with: event) }
            proxy.pendingCaret = nil
            proxy.pendingEvent = nil
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: NSScrollView, context: Context) -> CGSize? {
        guard let textView = nsView.documentView as? NSTextView, let layoutManager = textView.layoutManager,
              let container = textView.textContainer else { return nil }
        let width = proposal.width ?? 468
        container.containerSize = NSSize(width: width, height: .greatestFiniteMagnitude)
        layoutManager.ensureLayout(for: container)
        let line = ceil(layoutManager.defaultLineHeight(for: textView.font ?? .systemFont(ofSize: NSFont.systemFontSize)))
        let height = ceil(layoutManager.usedRect(for: container).height)
        return CGSize(width: width, height: min(max(height, line), maxHeight))
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ResultField

        init(_ parent: ResultField) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView, textView.isEditable else { return }
            parent.onChange(textView.string)
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView, textView.isEditable else { return }
            parent.onSelect(textView.selectedRange())
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.insertNewline(_:)):
                // ⇧↩ — nowa linia jak w Slacku; samo ↩ wstawia wynik.
                if NSApp.currentEvent?.modifierFlags.contains(.shift) == true { return false }
                parent.onSubmit()
                return true
            case #selector(NSResponder.cancelOperation(_:)):
                parent.onEscape()
                return true
            default:
                return false
            }
        }
    }
}

/// Pole wyniku: zmiany na tle szałwii, w edycji fala pod błędami (cegła, jak podkreślenia podczas pisania —
/// `LiveUnderlines`). Poza edycją klik i pisanie zaczynają edycję zamiast zaznaczać tekst.
final class ResultTextView: NSTextView {
    var actions: (edit: (Int?, NSEvent?) -> Void, submit: () -> Void, escape: () -> Void) = ({ _, _ in }, {}, {})

    /// Tło szałwii jako atrybut tymczasowy: nie trafia do tekstu, do cofania ani do tego, co się pisze.
    var marks: [NSRange] = [] {
        didSet {
            guard let layoutManager else { return }
            let length = (string as NSString).length
            layoutManager.removeTemporaryAttribute(.backgroundColor, forCharacterRange: NSRange(location: 0, length: length))
            for mark in marks where mark.length > 0 && NSMaxRange(mark) <= length {
                layoutManager.addTemporaryAttribute(.backgroundColor, value: NSColor(Brand.sageBackground), forCharacterRange: mark)
            }
        }
    }

    var issues: [LiveIssue] = [] {
        didSet { if issues.map(\.range) != oldValue.map(\.range) { needsDisplay = true } }
    }

    override func mouseDown(with event: NSEvent) {
        guard isEditable else {
            return actions.edit(characterIndexForInsertion(at: convert(event.locationInWindow, from: nil)), nil)
        }
        super.mouseDown(with: event)
    }

    override func keyDown(with event: NSEvent) {
        guard !isEditable else { return super.keyDown(with: event) }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let plain = flags.isDisjoint(with: [.shift, .option, .command, .control])
        let typing = flags.isDisjoint(with: [.command, .control])
        switch Int(event.keyCode) {
        case kVK_Return where plain, kVK_ANSI_KeypadEnter where plain:
            actions.submit()
        case kVK_Escape:
            actions.escape()
        case kVK_Delete where typing, kVK_ForwardDelete where typing:
            actions.edit(nil, event)
        default:
            // Litera, cyfra, znak (także z ⌥ i ⇧, np. ą) — edycja na końcu wyniku z tym znakiem.
            let typed = event.characters ?? ""
            if typing, !typed.isEmpty,
               typed.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) && $0.value < 0xF700 }) {
                actions.edit(nil, event)
            } else {
                super.keyDown(with: event)
            }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let layoutManager, let textContainer, !issues.isEmpty else { return }
        let length = (string as NSString).length
        NSColor(Brand.wave).setStroke()
        for issue in issues where issue.range.length > 0 && NSMaxRange(issue.range) <= length {
            let glyphs = layoutManager.glyphRange(forCharacterRange: issue.range, actualCharacterRange: nil)
            layoutManager.enumerateEnclosingRects(forGlyphRange: glyphs, withinSelectedGlyphRange: NSRange(location: NSNotFound, length: 0),
                                                  in: textContainer) { rect, _ in
                let line = rect.offsetBy(dx: self.textContainerOrigin.x, dy: self.textContainerOrigin.y)
                // Widok jest odwrócony: dół linii to maxY, fala tuż nad nim.
                let path = NSBezierPath()
                let y = line.maxY - 1.5, step: CGFloat = 2.5
                var x = line.minX
                path.move(to: NSPoint(x: x, y: y))
                var up = true
                while x < line.maxX {
                    x = min(x + step, line.maxX)
                    path.line(to: NSPoint(x: x, y: up ? y - 1.6 : y))
                    up.toggle()
                }
                path.lineWidth = 1.4
                path.lineCapStyle = .round
                path.lineJoinStyle = .round
                path.stroke()
            }
        }
    }
}

/// Dostęp do pola edycji spoza SwiftUI: podmiana słowa (z cofaniem ⌘Z) i kursor.
@MainActor
final class PanelEditorProxy {
    weak var textView: NSTextView?
    /// Miejsce kursora i klawisz, od których zaczyna się edycja (klik albo pisanie w podglądzie).
    var pendingCaret: Int?
    var pendingEvent: NSEvent?

    /// Wybrana poprawka zamiast słowa — przez pole tekstowe, więc ⌘Z ją cofa, a kursor staje za nią.
    func replace(_ range: NSRange, with text: String) {
        guard let textView, NSMaxRange(range) <= (textView.string as NSString).length else { return }
        textView.window?.makeFirstResponder(textView)
        textView.insertText(text, replacementRange: range)
    }
}

/// Sprawdzanie tekstu w polu edycji tym samym silnikiem co podczas pisania (`LiveChecker`): warstwa 0 (słownik macOS
/// w wybranych językach i lista słów bez polskich liter) od razu, LanguageTool po chwili bez pisania, jeśli jest
/// włączony. Bez modelu w tle — wynik w polu pochodzi już od modelu. „Ignoruj” działa do końca edycji.
@MainActor
final class EditorChecker: ObservableObject {
    @Published private(set) var issues: [LiveIssue] = []
    /// Sprawdzanie jeszcze trwa (do wyniku LanguageTool): pod polem „Sprawdzam…” zamiast „Bez uwag.”.
    @Published private(set) var checking = false
    private var text = ""
    private var task: Task<Void, Never>?
    private var ignored = Set<String>()

    /// Nowy tekst pola: znane błędy przesuwają się od razu (licznik nie skacze), potem sprawdzanie od nowa.
    func update(_ newText: String, languages: TextLanguages) {
        guard newText != text else { return }
        issues = text.isEmpty ? [] : LiveText.shift(issues, from: text, to: newText)
        text = newText
        task?.cancel()
        checking = true
        task = Task { [weak self] in
            // Krótka pauza: przy szybkim pisaniu sprawdzamy dopiero po ostatnim znaku.
            try? await Task.sleep(nanoseconds: 150_000_000)
            guard !Task.isCancelled else { return }
            let live = LiveText(newText, languages: languages)
            let quick = await LiveChecker.shared.quick(live)
            guard !Task.isCancelled else { return }
            self?.publish(quick)
            guard Settings.shared.liveLanguageToolEnabled else { self?.checking = false; return }
            try? await Task.sleep(nanoseconds: 800_000_000)
            guard !Task.isCancelled else { return }
            let grammar = await LiveChecker.shared.languageTool(live)
            guard !Task.isCancelled else { return }
            if let grammar { self?.publish(LiveText.merge(grammar, quick)) }
            self?.checking = false
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        issues = []
        checking = false
        text = ""
        ignored = []
    }

    func ignore(_ issue: LiveIssue) {
        ignored.insert(Self.key(issue))
        issues.removeAll { Self.key($0) == Self.key(issue) }
    }

    /// Słowo trafiło do słownika — znika z błędów pisowni od razu (następne sprawdzenie już go nie zgłosi).
    func learned(_ word: String) {
        issues.removeAll { $0.word == word && $0.isSpelling }
    }

    /// Błąd pod kursorem (także tuż za słowem).
    func issue(at caret: NSRange) -> LiveIssue? {
        issues.first { NSLocationInRange(caret.location, $0.range) || caret.location == NSMaxRange($0.range) }
    }

    private func publish(_ found: [LiveIssue]) {
        issues = found.filter { !ignored.contains(Self.key($0)) }
    }

    private static func key(_ issue: LiveIssue) -> String { "\(issue.word.lowercased())|\(issue.rule)" }
}
