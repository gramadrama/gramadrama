import SwiftUI
import GramaCore
import GramaBackend

/// Pisanie › Słownik i reguły (`build_dictionary` na Linuksie): własne słowa, słowa dodane „Dodaj do słownika” (najnowsze
/// pierwsze, każde z koszem) i reguły LanguageTool wyłączone kartą, każda z „Włącz”. Słownik jest jeden dla sprawdzania,
/// korekty i dyktowania.
struct DictionarySection: View {
    @ObservedObject private var settings = Settings.shared
    /// Ile ostatnich słów pokazuje lista (`MAX_WORDS_SHOWN`).
    private static let shownWords = 100

    var body: some View {
        let learned = settings.learnedLatestFirst
        let rules = settings.rulesOff
        SettingsSection(title: tr("Słownik i reguły"),
                        note: tr("Twoich słów sprawdzanie nie podkreśla, korekta zostawia ich pisownię, a dyktowanie lepiej je rozpoznaje."),
                        ring: .dictionary) {
            SettingsCard {
                VStack(alignment: .leading, spacing: 6) {
                    Text(tr("Własne słowa: imiona, nazwy i żargon, po przecinku"))
                    TextField(tr("np. GramaDrama, staging, code review"), text: $settings.ownWordsText, axis: .vertical)
                        .lineLimit(1...4)
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
                Divider()
                DisclosureGroup {
                    VStack(spacing: 0) {
                        ForEach(learned.prefix(Self.shownWords), id: \.self) { word in
                            HStack {
                                Text(word)
                                Spacer()
                                Button { UserWords.forget(word) } label: { Image(systemName: "trash") }
                                    .buttonStyle(.borderless)
                                    .help(tr("Usuń ze słownika"))
                                    .accessibilityLabel(tr("Usuń ze słownika: {value}", ["value": word]))
                            }
                            .padding(.vertical, 4)
                        }
                        if learned.count > Self.shownWords {
                            Text(tr("Starszych słów: {count}", ["count": learned.count - Self.shownWords]))
                                .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
                        }
                    }
                    .padding(.top, 4)
                } label: {
                    label(tr("Dodane do słownika"), icon: "character.book.closed",
                          subtitle: learned.isEmpty ? tr("Brak. Słowo dodasz kartą pod podkreślonym słowem albo w panelu korekty.")
                                                    : tr("Słów: {count}. Wspólne z aplikacjami macOS, które używają słownika systemu.",
                                                         ["count": learned.count]))
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
                Divider()
                DisclosureGroup {
                    VStack(spacing: 0) {
                        ForEach(rules, id: \.id) { rule in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(rule.name.isEmpty ? rule.id : rule.name)
                                    if !rule.name.isEmpty { Text(rule.id).font(.caption).foregroundStyle(.secondary) }
                                }
                                Spacer()
                                Button(tr("Włącz")) { settings.turnOnRule(rule.id) }
                                    .accessibilityLabel(tr("Włącz regułę: {value}", ["value": rule.name.isEmpty ? rule.id : rule.name]))
                            }
                            .padding(.vertical, 4)
                        }
                    }
                    .padding(.top, 4)
                } label: {
                    label(tr("Wyłączone reguły"), icon: "eye.slash",
                          subtitle: rules.isEmpty ? tr("Brak. Regułę LanguageTool wyłączysz kartą pod podkreślonym słowem.")
                                                  : tr("Reguł: {count}. LanguageTool ich nie sprawdza.", ["count": rules.count]))
                }
                .padding(.horizontal, 14).padding(.vertical, 9)
            }
        }
    }

    private func label(_ title: String, icon: String, subtitle: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon).foregroundStyle(.secondary).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(subtitle).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
