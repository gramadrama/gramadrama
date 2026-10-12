// Projekt panelu (wspólny z wersją na Linuksa): branding/PANEL.md — zmiana tutaj = zmiana tam.
import SwiftUI
import GramaCore
import GramaBackend

struct PanelView: View {
    @ObservedObject var session: CorrectionSession
    let onApply: (String) -> Void
    let onCopy: (String) -> Void
    let onClose: () -> Void
    @State private var editorProxy = PanelEditorProxy()
    @State private var editorSelection = NSRange(location: 0, length: 0)
    @StateObject private var editorChecker = EditorChecker()
    /// Ostatnie słowo dodane do słownika z pola edycji — potwierdzenie pod polem.
    @State private var learnedWord: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            modePicker
            content
            footer
        }
        .padding(16)
        .frame(width: 520)
        // Wysokość z treści, nie z okna — NSHostingView dopasowuje wtedy okno do treści
        // (inaczej ScrollView ściska się do zera, gdy po streamingu dochodzi lista zmian).
        .fixedSize(horizontal: false, vertical: true)
        // Pole edycji sprawdza GramaDrama: od wejścia w edycję i po każdej zmianie tekstu.
        .onChange(of: session.editing) { _, editing in
            if editing { editorChecker.update(session.draft, languages: session.languages) } else { editorChecker.stop() }
        }
        .onChange(of: session.draft) { _, draft in
            if session.editing { editorChecker.update(draft, languages: session.languages) }
        }
        .foregroundStyle(Brand.ink)
        .background(Brand.surface.opacity(0.98), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(Brand.line))
    }

    private var header: some View {
        HStack(spacing: 8) {
            // Pełne po wyniku, konturowe w trakcie (PANEL.md, „Nagłówek”).
            MasksLogo(filled: session.phase == .done).frame(width: 20, height: 20)
            Text("GramaDrama").font(.headline)
            Spacer()
            if session.mode == .translate { directionButton }
            statusBadge
            closeButton
        }
    }

    /// Anuluj: × dla myszy, ⎋ z klawiatury — stopka zostaje dla akcji na tekście.
    private var closeButton: some View {
        Button(action: onClose) {
            Image(systemName: "xmark").font(.system(size: 12, weight: .semibold)).frame(width: 16, height: 16)
        }
        .buttonStyle(CloseButtonStyle())
        // W trakcie edycji ⎋ kończy edycję (pole tekstowe), a nie zamyka panelu z niezapisanym tekstem.
        .keyboardShortcut(session.editing ? nil : .cancelAction)
        .help(tr("Anuluj (⎋)"))
        .accessibilityLabel(tr("Anuluj"))
    }

    @ViewBuilder private var statusBadge: some View {
        switch session.phase {
        case .checking where session.applyPending, .streaming where session.applyPending:
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text(tr("Zastosuję po zakończeniu")) }
                .font(.caption).foregroundStyle(Brand.secondaryInk)
        case .checking:
            Label(tr("Czytam scenariusz…"), systemImage: "text.magnifyingglass").labelStyle(.titleAndIcon)
                .font(.caption).foregroundStyle(Brand.secondaryInk)
        case .streaming:
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text(session.mode == .translate ? tr("Tłumaczę…") : tr("Poprawiam…")) }
                .font(.caption).foregroundStyle(Brand.secondaryInk)
        case .done:
            if session.editing {
                Label(tr("Edycja wyniku"), systemImage: "pencil").labelStyle(.titleAndIcon)
                    .font(.caption).foregroundStyle(Brand.secondaryInk)
            } else if session.mode == .translate {
                EmptyView()
            } else if session.hasChanges {
                Text(UI.count(session.changes.count, ("poprawka", "poprawki", "poprawek"), ("change", "changes")))
                    .font(.caption.bold()).foregroundStyle(Brand.sage).padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Brand.sageBackground, in: Capsule())
            } else {
                Text(tr("Zero dramatu")).font(.caption.bold()).foregroundStyle(Brand.sage)
            }
        case .failed:
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Brand.rose)
        }
    }

    /// Aktywny tryb jest neutralny (pogrubiony na subtelnym tle): lawenda zostaje dla akcji na tekście.
    private var modePicker: some View {
        HStack(spacing: 4) {
            ForEach(Mode.allCases) { mode in
                Button(mode.title) { session.mode = mode }
                    .buttonStyle(ModeButtonStyle(selected: session.mode == mode))
                    .accessibilityAddTraits(session.mode == mode ? .isSelected : [])
            }
        }
        // Inny tryb liczy wynik od nowa — w trakcie edycji skasowałby zmiany.
        .disabled(session.editing)
        .background(modeShortcuts.disabled(session.editing))
    }

    /// Kierunek tłumaczenia; ⌘D odwraca.
    private var directionButton: some View {
        Button {
            session.reverseDirection()
        } label: {
            Label(session.directionLabel, systemImage: "arrow.left.arrow.right")
        }
        .buttonStyle(BrandButtonStyle(compact: true))
        .keyboardShortcut("d", modifiers: .command)
        .disabled(session.editing)
        .help(tr("Zmień kierunek tłumaczenia (⌘D)"))
    }

    /// ⌘1…⌘6 przełącza tryby.
    private var modeShortcuts: some View {
        ForEach(Array(Mode.allCases.enumerated()), id: \.offset) { index, mode in
            Button("") { session.mode = mode }
                .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")), modifiers: .command)
                .opacity(0)
        }
    }

    @ViewBuilder private var content: some View {
        switch session.phase {
        case .failed(let message):
            Text(message).foregroundStyle(Brand.secondaryInk).frame(maxWidth: .infinity, alignment: .leading)
        case .done:
            if session.mode == .translate {
                // Oryginał mały i przygaszony, tłumaczenie jako główna treść (także w trakcie edycji).
                Text(session.original).font(.callout).foregroundStyle(Brand.secondaryInk).lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Rectangle().fill(Brand.line).frame(height: 1)
            }
            resultField
            if session.editing {
                // Dwie linie wysokości, cokolwiek pokazuje — panel nie zmienia wysokości przy ruchu kursora.
                Group {
                    if let issue = editorChecker.issue(at: editorSelection) { issueBar(issue) } else { checkStatus }
                }
                .frame(height: 44, alignment: .topLeading)
            } else {
                if session.changesOpen, session.canShowChanges {
                    changesList
                }
                if let note = session.note {
                    Label(note, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(Brand.secondaryInk)
                }
            }
        default:
            ScrollView {
                Text(session.corrected.isEmpty ? session.original : session.corrected)
                    .foregroundStyle(Brand.secondaryInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 220)
        }
    }

    private var changesList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(session.changes.prefix(30)) { change in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if change.from.isEmpty && change.to.isEmpty {
                            Text("␣").foregroundStyle(Brand.sage).bold()
                        }
                        if !change.from.isEmpty {
                            Text(change.from).strikethrough().foregroundStyle(Brand.secondaryInk)
                        }
                        if !change.from.isEmpty && !change.to.isEmpty {
                            Image(systemName: "arrow.right").font(.caption2).foregroundStyle(Brand.secondaryInk)
                        }
                        if !change.to.isEmpty {
                            Text(change.to).foregroundStyle(Brand.sage).bold()
                        }
                        Text("· " + tr(change.reason)).font(.caption).foregroundStyle(Brand.secondaryInk).lineLimit(2)
                        Spacer(minLength: 4)
                        changeActions(change)
                    }
                    .font(.callout)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxHeight: 130)
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(Brand.subtle, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var footer: some View {
        HStack {
            Text(footerInfo).font(.caption2).foregroundStyle(Brand.secondaryInk).lineLimit(1)
            Spacer()
            if session.canShowChanges { changesToggle }
            // W trakcie edycji ↩ obsługuje pole tekstowe (wstawia; ⇧↩ — nowa linia), więc przyciski nie mają ↩.
            let returnKey: KeyboardShortcut? = session.editing ? nil : .defaultAction
            if nothingToApply {
                // „Zero dramatu” albo błąd — ↩ po prostu zamyka panel (także dla tekstu tylko do odczytu).
                copyButton(primary: false)
                if session.captured.canApply { Button(tr("Zastąp")) {}.buttonStyle(BrandButtonStyle(primary: true)).disabled(true) }
                Button("", action: onClose).keyboardShortcut(returnKey).opacity(0).frame(width: 0)
            } else if !session.captured.canApply {
                // Tekst tylko do odczytu albo z załącznikami — ↩ kopiuje zamiast wklejać, więc Kopiuj jest główny.
                copyButton(primary: true).keyboardShortcut(returnKey)
            } else {
                copyButton(primary: false).keyboardShortcut("c", modifiers: [.command, .shift])
                Button(tr("Zastąp")) {
                    if session.phase == .done {
                        session.finishEditing()
                        onApply(session.corrected)
                    } else {
                        session.applyPending = true
                    }
                }
                .buttonStyle(BrandButtonStyle(primary: true))
                .keyboardShortcut(returnKey)
                .disabled(session.applyPending)
            }
        }
        .background(editShortcut)
    }

    /// „Pokaż N poprawek ▾” / „Ukryj zmiany ▴” (⌘I): cichy, bo tylko pokazuje więcej.
    private var changesToggle: some View {
        Button(session.changesOpen ? tr("Ukryj zmiany ▴")
               : tr("Pokaż {count} ▾", ["count": UI.count(session.changes.count, ("poprawkę", "poprawki", "poprawek"),
                                                           ("change", "changes"))])) {
            session.toggleChanges()
        }
        .buttonStyle(QuietButtonStyle())
        .keyboardShortcut("i", modifiers: .command)
        .help(tr("Lista zmian (⌘I)"))
    }

    /// ⌘E zaczyna edycję wyniku albo ją kończy. Przycisku nie ma: pole edytuje się też kliknięciem albo pisaniem.
    private var editShortcut: some View {
        Button("") {
            guard session.phase == .done else { return }
            learnedWord = nil
            session.toggleEditing()
        }
        .keyboardShortcut("e", modifiers: .command)
        .opacity(0)
        .accessibilityHidden(true)
    }

    /// Wynik do wstawienia: w trakcie edycji — tekst z pola.
    private var pending: String { session.editing ? session.draft : session.corrected }

    private var nothingToApply: Bool {
        switch session.phase {
        case .done: pending == session.original || pending.isBlank
        case .failed: true
        default: false
        }
    }

    private func copyButton(primary: Bool) -> some View {
        Button(tr("Kopiuj")) {
            session.finishEditing()
            onCopy(session.corrected)
        }
        .buttonStyle(BrandButtonStyle(primary: primary))
        .disabled(session.phase != .done || nothingToApply)
    }

    // MARK: Edycja wyniku

    /// Wynik jako pole (PANEL.md, „Treść”): nad nim, czym jest, a ramka w trakcie edycji jest lawendowa. Klik w tekst
    /// albo pisanie włącza edycję w tym miejscu (PANEL.md, „Edycja”).
    private var resultField: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(session.captured.canApply ? tr("Wynik — możesz go poprawić, zanim wstawisz")
                  : tr("Wynik — możesz go poprawić, zanim skopiujesz"), systemImage: "pencil")
                .labelStyle(.titleAndIcon)
                .font(.caption).foregroundStyle(Brand.secondaryInk)
            ResultField(text: session.editing ? session.draft : session.corrected, editing: session.editing,
                        marks: session.marks, issues: editorChecker.issues, proxy: editorProxy,
                        onChange: { session.draft = $0 }, onSelect: { editorSelection = $0 },
                        onEdit: beginEditing, onSubmit: submitFromEditor,
                        onEscape: { if session.editing { session.finishEditing() } else { onClose() } })
                .padding(.horizontal, 10).padding(.vertical, 8)
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(session.editing ? Brand.lavender : Brand.line))
        }
    }

    /// Edycja od miejsca w wyniku (nil — od końca) i z klawiszem, który ją zaczął.
    private func beginEditing(offset: Int?, event: NSEvent?) {
        guard session.phase == .done, !session.editing else { return }
        editorProxy.pendingCaret = offset
        editorProxy.pendingEvent = event
        learnedWord = nil
        session.startEditing()
        EventLog.write("panel_edit", ["action": event == nil ? "click" : "type", "mode": session.mode.rawValue])
    }

    /// Bez błędu pod kursorem: ile znalazło sprawdzanie, „Sprawdzam…” do wyniku LanguageTool, albo słowo dodane do słownika.
    private var checkStatus: some View {
        let count = editorChecker.issues.count
        let text = learnedWord.map { tr("Dodano „{word}” do słownika.", ["word": $0]) }
            ?? (count > 0 ? tr("Do sprawdzenia: {count} — kliknij podkreślone słowo, żeby zobaczyć poprawki.", ["count": count])
                : editorChecker.checking ? tr("Sprawdzam…") : tr("Bez uwag."))
        return Text(text).font(.caption).foregroundStyle(learnedWord == nil && count > 0 ? Brand.brick : Brand.secondaryInk)
            .lineLimit(1)
    }

    /// Błąd pod kursorem: słowo z falą, poprawki do kliknięcia, komunikat, Ignoruj i Dodaj do słownika (jak karta
    /// podkreślenia w innych aplikacjach).
    private func issueBar(_ issue: LiveIssue) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(issue.word).font(.callout).waveUnderline()
                if !issue.suggestions.isEmpty {
                    Image(systemName: "arrow.right").font(.caption2).foregroundStyle(Brand.secondaryInk)
                    ForEach(Array(issue.suggestions.prefix(3).enumerated()), id: \.offset) { index, suggestion in
                        Button(suggestion) {
                            editorProxy.replace(issue.range, with: suggestion)
                            EventLog.write("panel_edit", ["action": "suggestion", "mode": session.mode.rawValue])
                        }
                        .buttonStyle(SuggestionButtonStyle(first: index == 0))
                    }
                }
            }
            HStack(spacing: 12) {
                Text(tr(issue.message)).foregroundStyle(Brand.secondaryInk).lineLimit(1)
                Spacer(minLength: 8)
                Button(tr("Ignoruj")) { editorChecker.ignore(issue) }
                if issue.isSpelling || issue.rule == "PL_DIACRITICS" {
                    Button(tr("Dodaj do słownika")) { learn(issue.word) }
                }
            }
            .buttonStyle(.plain)
            .font(.caption)
            .foregroundStyle(Brand.secondaryInk)
        }
    }

    /// Słowo do słownika macOS — tego samego, którego używa sprawdzanie podczas pisania.
    private func learn(_ word: String) {
        NSSpellChecker.shared.learnWord(word)
        editorChecker.learned(word)
        learnedWord = word
        EventLog.write("panel_edit", ["action": "learn_word", "mode": session.mode.rawValue])
    }

    /// ↩ w polu: koniec edycji i to, co ↩ robi poza nią — Zastąp, Kopiuj (tekst tylko do odczytu) albo zamknięcie
    /// panelu, gdy wynik nie różni się już od oryginału.
    private func submitFromEditor() {
        session.finishEditing()
        guard session.hasChanges else { return onClose() }
        if session.captured.canApply { onApply(session.corrected) } else { onCopy(session.corrected) }
    }

    /// „Cofnij” przy zmianie: tylko ona wraca do oryginału. Przy zmienionym słowie także „Zostaw i dodaj do słownika”.
    private func changeActions(_ change: Change) -> some View {
        HStack(spacing: 2) {
            if change.isWordChange {
                Button { session.revert(change, learn: true) } label: {
                    Image(systemName: "character.book.closed").font(.caption)
                }
                .help(tr("Zostaw „{word}” i dodaj do słownika", ["word": change.from]))
                .accessibilityLabel(tr("Zostaw „{word}” i dodaj do słownika", ["word": change.from]))
            }
            Button { session.revert(change, learn: false) } label: {
                Image(systemName: "arrow.uturn.backward").font(.caption)
            }
            .help(change.from.isEmpty ? tr("Cofnij tę zmianę") : tr("Cofnij: zostaw „{word}”", ["word": change.from]))
            .accessibilityLabel(change.from.isEmpty ? tr("Cofnij tę zmianę") : tr("Cofnij: zostaw „{word}”", ["word": change.from]))
        }
        .buttonStyle(CloseButtonStyle())
    }

    private var footerInfo: String {
        // W trakcie edycji klawisze pola: ↩ to Zastąp (albo Kopiuj) tuż obok.
        if session.editing { return tr("⇧↩ nowa linia · ⎋ kończy edycję") }
        // PANEL.md: model · kierunek (tłumaczenie) · czas „2,9 s” · zakres.
        var parts = [Settings.shared.modelDisplayName]
        if session.usesModelOnly { parts.append(tr("tylko model")) }
        if session.mode == .translate { parts.append(session.directionLabel) }
        if session.phase == .done {
            parts.append(UI.seconds(session.elapsed))
        }
        parts.append(session.captured.scope.title)
        if !session.captured.canApply { parts.append(tr("tylko do odczytu — ↩ kopiuje")) }
        return parts.joined(separator: " · ")
    }
}

/// Zakładka trybu: aktywna pogrubiona na subtelnym tle, pozostałe przygaszone.
private struct ModeButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        ModeButton(configuration: configuration, selected: selected)
    }

    private struct ModeButton: View {
        let configuration: ButtonStyleConfiguration
        let selected: Bool
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.callout.weight(selected ? .bold : .regular))
                .foregroundStyle(selected || hovering ? Brand.ink : Brand.secondaryInk)
                .lineLimit(1)
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity)
                .background(selected ? Brand.subtle : configuration.isPressed ? Brand.pressed : hovering ? Brand.subtle.opacity(0.6) : .clear,
                            in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
        }
    }
}

/// Cichy przycisk w stopce (lista zmian): przygaszony tekst, na najechanie tło jak w trybach.
private struct QuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        StyledButton(configuration: configuration)
    }

    private struct StyledButton: View {
        let configuration: ButtonStyleConfiguration
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.caption.bold())
                .foregroundStyle(hovering ? Brand.ink : Brand.secondaryInk)
                .lineLimit(1)
                .padding(.horizontal, 8).padding(.vertical, 2)
                .background(configuration.isPressed ? Brand.pressed : hovering ? Brand.subtle : .clear,
                            in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
        }
    }
}

/// × w nagłówku: przygaszony, na najechanie tło jak w trybach.
private struct CloseButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        StyledButton(configuration: configuration)
    }

    private struct StyledButton: View {
        let configuration: ButtonStyleConfiguration
        @State private var hovering = false

        var body: some View {
            configuration.label
                .foregroundStyle(hovering ? Brand.ink : Brand.secondaryInk)
                .padding(4)
                .background(configuration.isPressed ? Brand.pressed : hovering ? Brand.subtle : .clear,
                            in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
        }
    }
}
