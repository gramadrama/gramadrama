import SwiftUI
import GramaCore
import GramaBackend

/// Wiersz „Klawiatura GramaDrama” (źródło „Polski (GramaDrama)”): stan i jedyny przycisk, którego teraz trzeba —
/// w Aplikacjach › Integracje i w Pierwszych krokach na Starcie. Z wybraną klawiaturą ⌥⌘G czyta i podmienia tekst bez
/// Dostępności i schowka (aplikacje macOS, Mail, nowy Outlook), a dyktowanie wstawia przez nią prawie wszędzie
/// (`ImePolicy`); bez niej oba idą przez Dostępność i schowek.
struct ImeKeyboardRow: View {
    @ObservedObject var model: MainWindowModel
    /// Pierwsze kroki: po co ta klawiatura, przed stanem; i komunikat włączania zamiast stanu, póki jest.
    var explained = false

    var body: some View {
        if let ime = model.ime {
            SettingsRow(mark: .status(Self.ok(ime)), title: tr("Klawiatura GramaDrama"), subtitle: subtitle(ime)) {
                button(ime)
            }
        } else {
            SettingsRow(mark: .status(nil), title: tr("Klawiatura GramaDrama"), subtitle: tr("Sprawdzam…"))
        }
    }

    private func subtitle(_ ime: ImeSetup.Snapshot) -> String {
        guard explained else { return Self.description(ime) }
        let why = tr("Zalecana: {shortcut} i dyktowanie bez schowka; pisze w Twoim obecnym układzie.",
                     ["shortcut": Settings.shared.correctionShortcut.label])
        return why + " " + (model.imeMessage ?? Self.description(ime))
    }

    static func ok(_ ime: ImeSetup.Snapshot) -> Bool? {
        guard ime.installed, ime.signed else { return ime.installed ? false : nil }
        if !ime.enabled || !ime.selected { return nil }
        return ime.hello != nil
    }

    static func description(_ ime: ImeSetup.Snapshot) -> String {
        guard ime.installed else {
            return tr("Nie jest zainstalowana — instaluje ją ./scripts/build-app.sh z certyfikatem (./scripts/create-signing-cert.sh).")
        }
        guard ime.signed else { return tr("Ta kopia aplikacji nie jest podpisana, więc klawiatura z nią nie współpracuje.") }
        guard ime.enabled else { return tr("Wyłączona. „Włącz…” poprosi o zgodę w Ustawieniach systemowych.") }
        guard ime.selected else { return tr("Włączona, ale piszesz teraz w układzie {layout}.", ["layout": ime.current]) }
        guard let hello = ime.hello else { return tr("Wybrana, ale nie odpowiada — kliknij w dowolne pole tekstowe.") }
        return tr("Działa od {duration}.", ["duration": WindowText.duration(hello.uptime ?? 0)])
    }

    @ViewBuilder private func button(_ ime: ImeSetup.Snapshot) -> some View {
        if ime.installed, ime.signed {
            if !ime.enabled {
                Button(model.imeBusy ? tr("Czekam na zgodę…") : tr("Włącz…")) { model.enableIME() }.disabled(model.imeBusy)
            } else {
                Button(ime.selected ? tr("Przełącz na {layout}", ["layout": ime.fallbackName]) : tr("Przełącz na GramaDramę")) {
                    model.toggleKeyboard()
                }
            }
        }
    }
}
