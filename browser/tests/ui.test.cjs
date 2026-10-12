const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const extension = path.resolve(__dirname, '../extension');

function environment() {
  const noop = () => {};
  const listeners = {};
  const event = key => ({ addListener: listener => { listeners[key] = listener; } });
  const port = { onMessage: event('native'), onDisconnect: event('disconnect'), postMessage: noop };
  const chrome = {
    runtime: { getURL: name => path.join(extension, name), getManifest: () => ({ version: '1' }),
      connectNative: () => port, onMessage: event('popup'), onStartup: event('startup'), onInstalled: event('installed'), id: 'example' },
    action: Object.fromEntries(['setIcon', 'setBadgeText', 'setBadgeBackgroundColor', 'setTitle'].map(name => [name, async () => {}])),
    tabs: { query: async () => [{ id: 1, url: 'https://example.com/' }], onUpdated: event('updated'), onActivated: event('activated') },
    alarms: { create: noop, onAlarm: event('alarm') },
  };
  const context = vm.createContext({ chrome, navigator: { language: 'pl', userAgent: 'Linux' }, URL,
    setTimeout, clearTimeout, performance, fetch: async url => ({ json: async () => JSON.parse(fs.readFileSync(url, 'utf8')) }) });
  context.importScripts = name => vm.runInContext(fs.readFileSync(path.join(extension, name), 'utf8'), context);
  return { context, listeners, chrome };
}

test('browser UI follows application preferences and preserves custom shortcut', async () => {
  const { context, listeners } = environment();
  vm.runInContext(fs.readFileSync(path.join(extension, 'background.js'), 'utf8'), context);
  await context.GramaUI.ready;
  assert.equal(context.GramaUI.tr('connected', 'de'), 'Connected to the app');
  listeners.native({ type: 'status', app: true });
  listeners.native({ type: 'preferences', ui_language: 'en', correction_shortcut: 'Ctrl+Shift+K' });
  const state = await new Promise(resolve => listeners.popup({ popup: 'state' }, { id: 'example' }, resolve));
  assert.equal(state.ui_language, 'en');
  assert.equal(state.correction_shortcut, 'Ctrl+Shift+K');
  assert.equal(vm.runInContext("tabState({url: 'https://example.com'}).title", context), 'GramaDrama — enabled on example.com');
  listeners.native({ type: 'preferences', ui_language: 'pl', correction_shortcut: 'Ctrl+Alt+G' });
  assert.equal(vm.runInContext("tabState({url: 'https://example.com'}).title", context), 'GramaDrama — działa na example.com');
});

test('popup translates all visible text without interpreting site or shortcut as HTML', async () => {
  const { context, chrome } = environment();
  context.importScripts('i18n.js');
  await context.GramaUI.ready;
  const elements = {};
  const element = id => elements[id] ||= { textContent: '', addEventListener() {} };
  context.document = { getElementById: element, documentElement: {} };
  chrome.runtime.sendMessage = async () => ({ connected: true, site: 'example.com', ui_language: 'en', correction_shortcut: 'Ctrl+Shift+K' });
  vm.runInContext(fs.readFileSync(path.join(extension, 'popup.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.status.textContent, 'Connected to the app');
  assert.equal(elements['check-site'].textContent, 'Check text on this site');
  assert.match(elements['site-note'].textContent, /Ctrl\+Shift\+K/);
  assert.equal(context.document.documentElement.lang, 'en');
  vm.runInContext("render({connected: false, site: '<example>', ui_language: 'pl'})", context);
  assert.equal(elements.status.textContent, 'Aplikacja GramaDrama nie działa');
  assert.equal(elements.site.textContent, '<example>');
  assert.equal(elements.enabled.disabled, true);
  assert.equal(context.document.documentElement.lang, 'pl');
});

test('browser catalogues contain the same keys and interpolation parameters', () => {
  const en = JSON.parse(fs.readFileSync(path.join(extension, 'locales/en.json')));
  const pl = JSON.parse(fs.readFileSync(path.join(extension, 'locales/pl.json')));
  assert.deepEqual(Object.keys(en).sort(), Object.keys(pl).sort());
  for (const key of Object.keys(en))
    assert.deepEqual(en[key].match(/\{\w+\}/g), pl[key].match(/\{\w+\}/g));
});
