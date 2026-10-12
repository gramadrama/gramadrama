// Okienko wtyczki: stan połączenia z aplikacją i przełącznik strony. Listę wyłączonych stron trzyma aplikacja
// (background.js przekazuje zmianę i trzyma kopię listy).
const status = document.getElementById('status');
const siteRow = document.getElementById('site-row');
const noSite = document.getElementById('no-site');
const siteLabel = document.getElementById('site');
const enabled = document.getElementById('enabled');
// Ta sama wtyczka jest na Macu i na Linuksie: skrót poprawiania jest inny.
const defaultShortcut = /Mac/.test(navigator.userAgent) ? '⌥⌘G' : 'Ctrl+Alt+G';

function render(state) {
  const language = GramaUI.language(state.ui_language || navigator.language);
  document.documentElement.lang = language;
  const text = key => GramaUI.tr(key, language);
  status.textContent = text(state.connected ? 'connected' : 'disconnected');
  document.getElementById('check-site').textContent = text('check_site');
  document.getElementById('site-note').textContent = GramaUI.tr('site_note', language,
    { shortcut: state.correction_shortcut || defaultShortcut });
  noSite.textContent = text('no_site');
  status.className = `status ${state.connected ? 'ok' : 'off'}`;
  siteRow.hidden = !state.site;
  noSite.hidden = Boolean(state.site);
  siteLabel.textContent = state.site || '';
  enabled.checked = !state.disabled;
  // Bez aplikacji nie ma gdzie zapisać zmiany.
  enabled.disabled = !state.connected;
}

GramaUI.ready.then(() => chrome.runtime.sendMessage({ popup: 'state' })).then(render);
enabled.addEventListener('change', () => {
  chrome.runtime.sendMessage({ popup: 'toggle', disabled: !enabled.checked }).then(render);
});
