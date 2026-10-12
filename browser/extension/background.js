// Service worker wtyczki GramaDrama (ta sama na Macu i na Linuksie). Trzyma połączenie z aplikacją (native messaging →
// host: GramaBrowserHost na Macu, gramadrama-browser-host na Linuksie → gniazdo aplikacji albo silnika) i przekazuje jej
// prośby do pola w aktywnej karcie. Sam niczego nie decyduje: aplikacja
// wybiera fragment, liczy poprawki i sprawdza wynik; tu tylko wybór karty i ramki z fokusem.

importScripts('i18n.js');
const HOST = 'pl.gramadrama.browser';
let uiLanguage = GramaUI.language(navigator.language);
let correctionShortcut = null;
let port = null;
/// Kolejna próba połączenia po rozłączeniu: 1 s, 2 s, 4 s… do 30 s; od nowa po odpowiedzi aplikacji.
let retryDelay = 1000;
let retryTimer = null;
/// Czy aplikacja odpowiada (status od hosta) i lista wyłączonych stron — trzyma ją aplikacja, tu tylko kopia.
let appConnected = false;
let disabledSites = new Set();

/// Host strony jak w ustawieniach aplikacji: małe litery, bez „www.”. Tylko strony http(s).
function siteOf(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return parsed.hostname.toLowerCase().replace(/^www\./, '').replace(/\.$/, '') || null;
  } catch (error) {
    return null;
  }
}

/// Ikona w każdej karcie: lawendowa — GramaDrama działa na tej stronie; szara z „off” — strona wyłączona;
/// szara z różowym „!” (błąd aplikacji, branding/BRAND.md) — aplikacja nie działa (wtedy nic nie działa nigdzie).
function iconPath(active) {
  const variant = active ? '' : '-off';
  return { 16: `icons/icon16${variant}.png`, 32: `icons/icon32${variant}.png` };
}

function tabState(tab) {
  const site = siteOf(tab.url || '');
  if (!appConnected) return { active: false, badge: '!', color: '#9E3446', title: GramaUI.tr('title_disconnected', uiLanguage) };
  if (site && disabledSites.has(site)) return { active: false, badge: 'off', color: '#6B6870', title: GramaUI.tr('title_disabled', uiLanguage, { site }) };
  return { active: true, badge: '', color: '#6B6870', title: site ? GramaUI.tr('title_enabled', uiLanguage, { site }) : 'GramaDrama' };
}

async function updateIcons() {
  await GramaUI.ready;
  // Domyślny wygląd (nowe karty) i osobno każda otwarta karta.
  chrome.action.setIcon({ path: iconPath(appConnected) }).catch(() => {});
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    const state = tabState(tab);
    chrome.action.setIcon({ tabId: tab.id, path: iconPath(state.active) }).catch(() => {});
    chrome.action.setBadgeText({ tabId: tab.id, text: state.badge }).catch(() => {});
    chrome.action.setBadgeBackgroundColor({ tabId: tab.id, color: state.color }).catch(() => {});
    chrome.action.setTitle({ tabId: tab.id, title: state.title }).catch(() => {});
  }
}
chrome.tabs.onUpdated.addListener((tabId, change) => { if (change.url || change.status === 'complete') updateIcons(); });
chrome.tabs.onActivated.addListener(() => updateIcons());

function showStatus(connected) {
  appConnected = connected;
  updateIcons();
}

function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (error) {
    port = null;
    return;
  }
  port.onMessage.addListener(onAppMessage);
  // Brak hosta (aplikacja niezainstalowana) albo koniec przeglądarki — ponowna próba z alarmu.
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    port = null;
    showStatus(false);
    // Host kończy się, gdy traci aplikację (np. po instalacji nowej wersji) — nowy host od razu, z przerwą rosnącą.
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 30000);
  });
  port.postMessage({ type: 'hello', version: chrome.runtime.getManifest().version });
}

function onAppMessage(message) {
  if (message && message.type === 'preferences') {
    uiLanguage = GramaUI.language(message.ui_language);
    if (typeof message.correction_shortcut === 'string' && message.correction_shortcut.length <= 60)
      correctionShortcut = message.correction_shortcut;
    updateIcons();
    return;
  }
  if (message && message.type === 'status') {
    if (message.app === true) retryDelay = 1000;
    showStatus(message.app === true);
    return;
  }
  if (message && message.type === 'sites' && Array.isArray(message.disabled)) {
    disabledSites = new Set(message.disabled);
    updateIcons();
    return;
  }
  if (!message || typeof message.op !== 'string' || typeof message.id !== 'string') return;
  const received = performance.now();
  handle(message, received).then((reply) => {
    if (port) port.postMessage({ v: 1, id: message.id, ...reply });
  });
}

/// Karta i ramka dla prośby: z tokenu pola (podmiana tam, skąd tekst pobrano) albo aktywna karta i ramka z fokusem.
async function findTarget(client) {
  if (typeof client === 'string') {
    const [tab, frame] = client.split(':').map(Number);
    if (!Number.isInteger(tab) || !Number.isInteger(frame)) return null;
    const info = await chrome.tabs.get(tab).catch(() => null);
    return info ? { tabId: tab, frameId: frame, site: siteOf(info.url || '') } : null;
  }
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || tab.id === undefined) return null;
  // Ramka z fokusem: dokument ma fokus, a aktywny element nie jest ramką (wtedy fokus jest głębiej).
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    func: () => document.hasFocus() && !(document.activeElement instanceof HTMLIFrameElement)
      && !(document.activeElement instanceof HTMLFrameElement),
  });
  const focused = results.find((result) => result.result === true);
  return focused ? { tabId: tab.id, frameId: focused.frameId, site: siteOf(tab.url || '') } : null;
}

async function handle(request, received) {
  const writes = request.op === 'replace' || request.op === 'insert';
  let target;
  try {
    target = await findTarget(request.client);
  } catch (error) {
    target = null;
  }
  if (!target) return { ok: false, reason: request.client ? 'client_changed' : 'no_client', attempted: false };
  // Czas spędzony tutaj odejmujemy od terminu — pole dostaje, ile zostało.
  const forwarded = { ...request };
  if (typeof request.expiresInMs === 'number') {
    forwarded.expiresInMs = Math.max(0, request.expiresInMs - (performance.now() - received));
  }
  try {
    const reply = await chrome.tabs.sendMessage(target.tabId, { gramadrama: forwarded, tabId: target.tabId, frameId: target.frameId },
      { frameId: target.frameId });
    // Host karty (nie ramki): po nim aplikacja sprawdza, czy strona jest wyłączona. Powiększenie karty (1 = 100%):
    // przeliczenie pikseli strony (CSS) na pikseli okna, którego aplikacja potrzebuje do położenia słów.
    if (reply) {
      const zoom = await chrome.tabs.getZoom(target.tabId).catch(() => undefined);
      return { ...reply, site: target.site || undefined, zoom };
    }
  } catch (error) {
    // Strona bez naszego skryptu (strona przeglądarki, karta sprzed instalacji) albo zamknięta ramka.
  }
  // Prośba zmieniająca tekst mogła zadziałać, zanim ramka zniknęła — wtedy „nie wiadomo”, nie odmowa.
  return writes ? { ok: false, reason: 'extension_error' } : { ok: false, reason: 'no_client', attempted: false };
}

/// Po instalacji albo aktualizacji: skrypt także w kartach, które były już otwarte.
async function injectIntoOpenTabs() {
  const tabs = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
  for (const tab of tabs) {
    chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ['content.js'] }).catch(() => {});
  }
}

// Okienko wtyczki (popup.html): stan dla aktywnej karty i przełącznik strony — decyzję zapisuje aplikacja.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.popup || sender.id !== chrome.runtime.id || sender.tab) return false;
  (async () => {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    const site = tab ? siteOf(tab.url || '') : null;
    if (message.popup === 'toggle' && site && port && appConnected) {
      port.postMessage({ type: 'setSite', site, disabled: message.disabled === true });
      // Od razu w kopii — aplikacja i tak odeśle aktualną listę.
      if (message.disabled === true) disabledSites.add(site); else disabledSites.delete(site);
      updateIcons();
    }
    sendResponse({ connected: Boolean(port) && appConnected, site, disabled: site !== null && disabledSites.has(site),
      ui_language: uiLanguage, correction_shortcut: correctionShortcut });
  })();
  return true;
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => {
  connect();
  injectIntoOpenTabs();
});
// Połączenie z hostem utrzymuje service worker przy życiu; alarm wznawia je po zamknięciu aplikacji czy hosta.
chrome.alarms.create('gramadrama-connect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
connect();
