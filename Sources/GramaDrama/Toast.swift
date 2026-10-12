import AppKit
import SwiftUI

/// Panel, który nigdy nie zabiera fokusu ani klawiszy — aplikacja docelowa pisze dalej.
final class NonKeyPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Krótki komunikat w stylu panelu (PANEL.md: „odmowy pokazuje ten sam panel jako krótki komunikat bez
/// przycisków; znika sam po kilku sekundach i nie przechwytuje klawiszy”). Zastępuje modalny NSAlert.
@MainActor
final class Toast {
    static let shared = Toast()
    private var panel: NSPanel?
    private var hideTask: Task<Void, Never>?

    func show(_ message: String, near anchor: NSRect? = nil, duration: TimeInterval = 3) {
        hideTask?.cancel()
        panel?.orderOut(nil)

        let hosting = NSHostingView(rootView: ToastView(message: message))
        hosting.layoutSubtreeIfNeeded()
        let size = hosting.fittingSize
        let panel = NonKeyPanel(contentRect: NSRect(origin: .zero, size: size),
                                styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
        panel.contentView = hosting
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.setFrameOrigin(origin(for: size, anchor: anchor))
        panel.orderFrontRegardless()
        self.panel = panel

        hideTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.panel?.orderOut(nil)
            self?.panel = nil
        }
    }

    /// Pod polem (albo nad nim), a bez pola — przy kursorze myszy.
    private func origin(for size: NSSize, anchor: NSRect?) -> NSPoint {
        let mouse = NSEvent.mouseLocation
        let reference = anchor ?? NSRect(origin: mouse, size: .zero)
        let visible = (NSScreen.screens.first { $0.frame.contains(reference.origin) } ?? NSScreen.main)?.visibleFrame ?? .zero
        var x = anchor.map { $0.minX } ?? mouse.x - size.width / 2
        var y = reference.minY - size.height - 8
        if y < visible.minY { y = reference.maxY + 8 }
        x = min(max(x, visible.minX + 8), visible.maxX - size.width - 8)
        y = min(max(y, visible.minY + 8), visible.maxY - size.height - 8)
        return NSPoint(x: x, y: y)
    }
}

private struct ToastView: View {
    let message: String

    var body: some View {
        HStack(spacing: 10) {
            MasksLogo().frame(width: 20, height: 20)
            Text(message).font(.callout).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: 420, alignment: .leading)
        .fixedSize()
        .foregroundStyle(Brand.ink)
        .background(Brand.surface.opacity(0.98), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Brand.line))
    }
}
