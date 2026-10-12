import SwiftUI
import GramaCore
import GramaBackend

/// Zakładki okna. `rawValue` trafia do UserDefaults (`mainWindowTab`) — okno pamięta ostatnią zakładkę,
/// a aplikacja może otworzyć konkretną (np. Usługi po zalogowaniu). `connection` to dziś „Usługi”;
/// nazwa przypadku zostaje, bo jest zapisana w ustawieniach.
enum MainWindowTab: String, CaseIterable, Identifiable {
    case start, writing, dictation, apps, connection, privacy

    static let defaultsKey = "mainWindowTab"
    var id: String { rawValue }

    var title: String {
        switch self {
        case .start: tr("Start")
        case .writing: tr("Pisanie")
        case .dictation: tr("Dyktowanie")
        case .apps: tr("Aplikacje")
        case .connection: tr("Usługi")
        case .privacy: tr("Prywatność")
        }
    }

    var symbol: String {
        switch self {
        case .start: "theatermasks"
        case .writing: "text.cursor"
        case .dictation: "mic"
        case .apps: "square.grid.2x2"
        case .connection: "server.rack"
        case .privacy: "lock.shield"
        }
    }

    /// Następne otwarcie okna (albo otwarte okno) pokaże tę zakładkę.
    static func select(_ tab: MainWindowTab) { UserDefaults.standard.set(tab.rawValue, forKey: defaultsKey) }
}

/// Okno aplikacji (odpowiednik okna na Linuksie). Każda zakładka zawiera tylko swoje ustawienia:
/// Pisanie — sprawdzanie tekstu, Dyktowanie — dyktowanie, Usługi — serwery i modele (także rozpoznawanie mowy
/// i dostęp zdalny), Aplikacje — ustawienia per aplikacja i integracje, Start — stan, uprawnienia i skróty.
struct MainWindowView: View {
    @StateObject private var model = MainWindowModel()
    @ObservedObject private var appState = AppState.shared
    @ObservedObject private var settings = Settings.shared
    @ObservedObject private var dictation = DictationSettings.shared
    @AppStorage(MainWindowTab.defaultsKey) private var tab: MainWindowTab = .start
    @ObservedObject private var highlight = WindowHighlight.shared

    /// Wysokość okna (podgląd `--preview-window` renderuje każdą zakładkę w całości).
    var height: CGFloat = 800
    /// Podgląd: ta zakładka zamiast zapamiętanej.
    var previewTab: MainWindowTab?

    var body: some View {
        let current = previewTab ?? tab
        VStack(spacing: 0) {
            tabBar(current)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 24) {
                        switch current {
                        case .start: StartTab(model: model)
                        case .writing: WritingTab()
                        case .dictation: DictationTab(model: model)
                        case .apps: AppsTab(model: model)
                        case .connection: ServicesTab(model: model)
                        case .privacy: PrivacyTab()
                        }
                    }
                    .padding(28)
                    // Inny język interfejsu (idzie za głównym językiem tekstu): zakładka od nowa, już w nowym języku.
                    .id(UI.language)
                }
                // Wskazana grupa (WindowHighlight) na widok — po przełączeniu zakładki, gdy jej treść już jest.
                .onChange(of: highlight.request) { _, _ in scrollToHighlight(proxy) }
                .onAppear { scrollToHighlight(proxy) }
            }
        }
        .frame(width: 580, height: height)
        .tint(Brand.lavender)
        .background(Color(nsColor: .windowBackgroundColor))
        .toolbar {
            ToolbarItem {
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .help(tr("Odśwież stan"))
            }
        }
        .task { await model.refresh() }
        // Po powrocie z Ustawień systemowych (zatwierdzenie rzeczy otwieranej przy logowaniu, uprawnienia).
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.refreshSystem()
            Task { model.ime = await ImeSetup.snapshot() }
        }
        .onReceive(NotificationCenter.default.publisher(for: EventLog.didWrite)) { note in
            model.handleEvent(note.userInfo as? [String: Any] ?? [:])
        }
        .onChange(of: dictation.speechBackend) { _, _ in Task { await model.checkSpeechServer() } }
        // Stany usług to teksty z chwili sprawdzenia — po zmianie języka interfejsu sprawdzamy od nowa.
        .onChange(of: UILanguage(primary: settings.textLanguages.primary)) { _, _ in Task { await model.refresh() } }
    }

    private func scrollToHighlight(_ proxy: ScrollViewProxy) {
        guard let target = highlight.target else { return }
        DispatchQueue.main.async {
            withAnimation(.easeInOut(duration: 0.25)) { proxy.scrollTo(target.rawValue, anchor: .top) }
        }
    }

    /// Pasek zakładek jak w ustawieniach macOS: ikona nad nazwą, wybrana na lawendowym tle. Kropka w cegle
    /// przy zakładce, w której coś wymaga uwagi.
    private func tabBar(_ current: MainWindowTab) -> some View {
        let attention = Set(model.problems.map(\.tab))
        return HStack(spacing: 2) {
            ForEach(MainWindowTab.allCases) { item in
                Button { tab = item } label: {
                    VStack(spacing: 3) {
                        Image(systemName: item.symbol).font(.system(size: 17, weight: .medium)).frame(height: 20)
                        Text(item.title).font(.caption)
                    }
                    .frame(width: 84, height: 48)
                    .foregroundStyle(item == current ? Color.primary : Color.secondary)
                    .background(item == current ? Brand.lavender.opacity(0.22) : .clear, in: RoundedRectangle(cornerRadius: 8))
                    .overlay(alignment: .topTrailing) {
                        if attention.contains(item) {
                            Circle().fill(Brand.brick).frame(width: 7, height: 7).offset(x: -18, y: 6)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(item.title)
            }
        }
        .padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 6)
        .frame(maxWidth: .infinity)
    }
}
