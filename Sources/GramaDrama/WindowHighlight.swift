import SwiftUI

/// Obwódka wokół grupy, w której naprawisz przyczynę (`point_to` na Linuksie): przycisk nieudanej korekty
/// w panelu, pigułka stanu i Pierwsze kroki otwierają zakładkę i obrysowują grupę przewiniętą na widok. Obwódka
/// znika po sześciu sekundach albo przy następnym wskazaniu.
@MainActor
final class WindowHighlight: ObservableObject {
    static let shared = WindowHighlight()

    enum Target: String, CaseIterable {
        case languageModel, textLanguages, dictionary, speechRecognition, remoteAccess

        var tab: MainWindowTab { self == .textLanguages || self == .dictionary ? .writing : .connection }
    }

    @Published private(set) var target: Target?
    /// Każde wskazanie, także tej samej grupy — okno przewija ją wtedy na widok od nowa.
    @Published private(set) var request = 0
    private var clearing: Task<Void, Never>?

    func point(to target: Target) {
        MainWindowTab.select(target.tab)
        clearing?.cancel()
        self.target = target
        request += 1
        clearing = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard !Task.isCancelled else { return }
            self?.target = nil
        }
    }
}
