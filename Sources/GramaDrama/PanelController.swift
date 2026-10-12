import AppKit
import SwiftUI
import GramaCore

/// Panel nie aktywuje GramaDramy — aplikacja docelowa zachowuje fokus i zaznaczenie.
final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class PanelController {
    private var panel: FloatingPanel?
    private var session: CorrectionSession?
    private var observers: [NSObjectProtocol] = []
    /// Pole tekstowe, przy którym stoi panel (nil = panel przy kursorze myszy).
    private var placementAnchor: NSRect?
    /// Panel pod polem rośnie w dół (stoi górna krawędź), panel nad polem rośnie w górę (stoi dolna).
    private var growsDownward = true
    private var pinnedEdge: CGFloat = 0
    /// Użytkownik przesunął panel myszką — od tej pory nie przestawiamy go sami.
    private var userMoved = false


    var isOpen: Bool { panel != nil }

    func show(_ captured: CapturedText, mode: Mode) {
        close()
        let session = CorrectionSession(captured: captured, mode: mode)
        self.session = session

        session.onReadyToApply = { [weak self] text in self?.apply(text, captured: captured) }
        let view = PanelView(session: session,
                             onApply: { [weak self] text in self?.apply(text, captured: captured) },
                             onCopy: { [weak self] text in
                                 TextBridge.setClipboard(text)
                                 self?.close()
                                 Toast.shared.show(tr("Skopiowano do schowka."), near: captured.anchor)
                             },
                             onClose: { [weak self] in self?.close() },
                             onProblem: { [weak self] action in
                                 self?.close()
                                 AppDelegate.perform(action)
                             })
        let hosting = NSHostingView(rootView: view)
        hosting.layoutSubtreeIfNeeded()

        let panel = FloatingPanel(contentRect: NSRect(origin: .zero, size: hosting.fittingSize),
                                  styleMask: [.nonactivatingPanel, .borderless],
                                  backing: .buffered, defer: false)
        panel.contentView = hosting
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let placement = placement(for: hosting.fittingSize, anchor: captured.anchor)
        placementAnchor = placement.anchor
        growsDownward = placement.growsDownward
        panel.setFrameOrigin(placement.origin)
        pinnedEdge = growsDownward ? panel.frame.maxY : panel.frame.minY
        userMoved = false
        panel.makeKeyAndOrderFront(nil)
        self.panel = panel

        let center = NotificationCenter.default
        observers = [
            center.addObserver(forName: NSWindow.didResignKeyNotification, object: panel, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.close() }
            },
            // Okno rośnie samo (NSHostingView dopasowuje je do treści), trzymając górną krawędź.
            // Tu przestawiamy je tak, żeby nie zasłaniało pola i nie wychodziło poza ekran.
            center.addObserver(forName: NSWindow.didResizeNotification, object: panel, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.repositionAfterResize() }
            },
            center.addObserver(forName: NSWindow.didMoveNotification, object: panel, queue: .main) { [weak self] _ in
                // Ruch z wciśniętym przyciskiem myszy to przeciąganie przez użytkownika, nie nasz setFrameOrigin.
                MainActor.assumeIsolated {
                    if NSEvent.pressedMouseButtons & 1 != 0 { self?.userMoved = true }
                }
            },
        ]
        session.run()
    }

    func close() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        session?.cancel()
        session = nil
        panel?.orderOut(nil)
        panel = nil
    }

    // MARK: Diagnostyka (--preview)

    var debugPhase: CorrectionSession.Phase? { session?.phase }
    /// Zamiast wklejać — tylko dla --preview (wklejenie trafiłoby do aplikacji na wierzchu).
    var debugApplyOverride: ((String) -> Void)?
    func debugPressApplyDuringWork() { session?.applyPending = true }

    /// Zapisuje zrzut zawartości panelu do PNG i zwraca opis geometrii.
    func debugSnapshot(to path: String) -> String {
        guard let panel, let view = panel.contentView else { return "panel: brak (zamknięty)" }
        if let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
        }
        return "panel.frame=\(panel.frame) view.bounds=\(view.bounds) fitting=\(view.fittingSize) visible=\(panel.isVisible) key=\(panel.isKeyWindow)"
    }

    // MARK: Prywatne

    private func apply(_ text: String, captured: CapturedText) {
        close()
        if let debugApplyOverride {
            debugApplyOverride(text)
            return
        }
        Task {
            switch await TextBridge.replace(captured, with: text) {
            case .pasted: break
            case .copiedToClipboard:
                Toast.shared.show(tr("Poprawka jest w schowku — wklej ją ⌘V."), near: captured.anchor, duration: 4)
            case .busy:
                Toast.shared.show(tr("Poprzednia operacja jeszcze trwa — poprawka jest w schowku, wklej ją ⌘V."),
                                  near: captured.anchor, duration: 4)
            case .fieldChanged:
                Toast.shared.show(tr("Tekst w polu zmienił się od sprawdzenia — poprawka jest w schowku (⌘V)."),
                                  near: captured.anchor, duration: 4)
            case .pasteNotLanded:
                Toast.shared.show(tr("Aplikacja nie przyjęła wklejenia — poprawiany fragment jest zaznaczony, a poprawka w schowku (⌘V)."),
                                  near: captured.anchor, duration: 5)
            case .uncertain:
                Toast.shared.show(tr("Pole zmieniło się w trakcie wklejania — sprawdź tekst (⌘Z cofa zmianę)."),
                                  near: captured.anchor, duration: 5)
            case .badResult:
                Toast.shared.show(tr("Wynik nie nadaje się do wklejenia — nic nie zmieniłem."), near: captured.anchor, duration: 4)
            case .lineBreaksChanged:
                Toast.shared.show(tr("Poprawka zmienia podział na linie — jest w schowku, wklej ją ⌘V w miejsce zaznaczenia."),
                                  near: captured.anchor, duration: 5)
            }
        }
    }

    private func repositionAfterResize() {
        guard let panel, !userMoved else { return }
        let height = panel.frame.height
        let visible = (panel.screen ?? NSScreen.main)?.visibleFrame ?? panel.frame
        let minY = visible.minY + 8, maxY = visible.maxY - 8

        // Nie mieści się po wybranej stronie pola — przenosimy na drugą, jeśli tam jest miejsce.
        if let anchor = placementAnchor {
            if growsDownward, pinnedEdge - height < minY, anchor.maxY + 8 + height <= maxY {
                growsDownward = false
                pinnedEdge = anchor.maxY + 8
            } else if !growsDownward, pinnedEdge + height > maxY, anchor.minY - 8 - height >= minY {
                growsDownward = true
                pinnedEdge = anchor.minY - 8
            }
        }
        var y = growsDownward ? pinnedEdge - height : pinnedEdge
        y = min(max(y, minY), maxY - height)
        if abs(panel.frame.minY - y) > 0.5 {
            panel.setFrameOrigin(NSPoint(x: panel.frame.minX, y: y))
        }
    }

    /// Pod polem tekstowym (albo nad nim, gdy brak miejsca); w ostateczności przy kursorze myszy.
    private func placement(for size: NSSize, anchor: NSRect?) -> (origin: NSPoint, growsDownward: Bool, anchor: NSRect?) {
        let mouse = NSEvent.mouseLocation
        let reference = anchor ?? NSRect(origin: mouse, size: .zero)
        let screen = NSScreen.screens.first { $0.frame.contains(reference.origin) } ?? NSScreen.main
        let visible = screen?.visibleFrame ?? .zero

        var usedAnchor = anchor
        var x = anchor.map { $0.minX } ?? mouse.x - size.width / 2
        var y = reference.minY - size.height - 8
        var growsDownward = true
        if y < visible.minY {
            y = reference.maxY + 8
            growsDownward = false
        }
        // Duże pola (np. treść maila) zajmują pół ekranu — wtedy przy myszy.
        if y + size.height > visible.maxY || (anchor?.height ?? 0) > visible.height * 0.5 {
            x = mouse.x - size.width / 2
            y = mouse.y - size.height - 16
            growsDownward = true
            usedAnchor = nil
        }
        x = min(max(x, visible.minX + 8), visible.maxX - size.width - 8)
        y = min(max(y, visible.minY + 8), visible.maxY - size.height - 8)
        return (NSPoint(x: x, y: y), growsDownward, usedAnchor)
    }
}
