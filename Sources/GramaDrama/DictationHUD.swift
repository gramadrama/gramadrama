import AppKit
import SwiftUI
import GramaCore
import GramaBackend

/// Pastylka na dole ekranu podczas dyktowania: „Słucham…” z poziomem dźwięku i podglądem rozpoznanego tekstu,
/// potem „Rozpoznaję…” i „Porządkuję…”. Nie zabiera fokusu ani klawiszy.
@MainActor
final class DictationHUD {
    private var panel: NSPanel?
    /// Okno na największą pastylkę (z podglądem) i jej cień; pastylka stoi przy dolnej krawędzi, reszta jest przezroczysta.
    static let margin: CGFloat = 16
    static let size = NSSize(width: DictationHUDView.expandedWidth + 2 * margin, height: 150)

    func show(controller: DictationController) {
        let panel = self.panel ?? makePanel(controller: controller)
        self.panel = panel
        // Ekran z kursorem myszy — zwykle ten, na którym się pisze.
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        let visible = screen?.visibleFrame ?? .zero
        panel.setFrameOrigin(NSPoint(x: visible.midX - Self.size.width / 2, y: visible.minY + 48 - Self.margin))
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    private func makePanel(controller: DictationController) -> NSPanel {
        let hosting = NSHostingView(rootView: DictationHUDView(controller: controller))
        hosting.frame = NSRect(origin: .zero, size: Self.size)
        let panel = NonKeyPanel(contentRect: hosting.frame, styleMask: [.nonactivatingPanel, .borderless],
                                backing: .buffered, defer: false)
        panel.contentView = hosting
        panel.isOpaque = false
        panel.backgroundColor = .clear
        // Cień rysuje SwiftUI — cień okna nie nadąża za zmianą rozmiaru pastylki.
        panel.hasShadow = false
        panel.level = .statusBar
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        return panel
    }
}

struct DictationHUDView: View {
    @ObservedObject var controller: DictationController
    static let width: CGFloat = 250
    static let expandedWidth: CGFloat = 440
    /// Ok. trzech linii — starszy tekst ucinamy z przodu, żeby było widać to, co właśnie powiedziałeś.
    static let previewCharacters = 150

    var body: some View {
        let expanded = !controller.preview.isEmpty
        VStack(alignment: .leading, spacing: 0) {
            statusRow
            if expanded {
                previewText
                    .padding(.bottom, 12)
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, 16)
        .frame(width: expanded ? Self.expandedWidth : Self.width, alignment: .leading)
        .foregroundStyle(Brand.ink)
        .background(Brand.surface.opacity(0.98), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Brand.line))
        .shadow(color: .black.opacity(0.25), radius: 8, y: 2)
        .animation(.easeOut(duration: 0.15), value: expanded)
        .padding(DictationHUD.margin)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
    }

    private var previewText: some View {
        let tail = controller.preview.tail(maxCharacters: Self.previewCharacters)
        let separator = tail.confirmed.isEmpty || tail.pending.isEmpty ? "" : " "
        // Pewne zwykłym kolorem, końcówka (jeszcze może się zmienić) szarzej.
        return (Text(tail.truncated ? "…" : "") + Text(tail.confirmed + separator)
                + Text(tail.pending).foregroundStyle(.secondary))
            .font(.callout)
            .lineLimit(4)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusRow: some View {
        HStack(spacing: 10) {
            MasksLogo(filled: controller.phase != .listening).frame(width: 18, height: 18)
            switch controller.phase {
            case .listening:
                LevelBars(level: controller.level)
                Text(tr("Słucham…")).font(.callout).fixedSize()
                Spacer(minLength: 0)
                if !controller.preview.isEmpty {
                    Text(DictationPreviewText.wordCountLabel(controller.preview.wordCount))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                Text(controller.language.shortLabel)
                    .font(.caption.weight(.semibold))
                    // Wyróżniony język inny niż główny (zwykle po ⇧).
                    .foregroundStyle(controller.language.textLanguage.map { $0 != Settings.shared.textLanguages.primary } == true
                                     ? Brand.brick : Brand.secondaryInk)
                TimelineView(.periodic(from: controller.startedAt, by: 1)) { context in
                    let seconds = max(0, Int(context.date.timeIntervalSince(controller.startedAt)))
                    Text(String(format: "%d:%02d", seconds / 60, seconds % 60))
                        .font(.callout.monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                }
            case .recognizing, .cleaning:
                ProgressView().controlSize(.small)
                Text(controller.phase == .recognizing ? tr("Rozpoznaję…") : tr("Porządkuję…")).font(.callout).fixedSize()
                Spacer(minLength: 0)
            case .idle:
                Spacer(minLength: 0)
            }
        }
        .frame(height: 40)
    }
}

private struct LevelBars: View {
    let level: Float
    private static let weights: [CGFloat] = [0.45, 0.75, 1, 0.75, 0.45]

    var body: some View {
        HStack(spacing: 3) {
            ForEach(Self.weights.indices, id: \.self) { index in
                Capsule()
                    .fill(Brand.brick)
                    .frame(width: 3, height: 4 + 14 * CGFloat(level) * Self.weights[index])
            }
        }
        .frame(height: 18)
        .animation(.easeOut(duration: 0.08), value: level)
    }
}
