// UI text only. The application sends its selected interface language; field text is untouched.
globalThis.GramaUI = (() => {
  const catalogues = {};
  const language = value => String(value || '').toLowerCase().split(/[-_]/)[0] === 'pl' ? 'pl' : 'en';
  const ready = Promise.all(['pl', 'en'].map(async code => {
    const response = await fetch(chrome.runtime.getURL(`locales/${code}.json`));
    catalogues[code] = await response.json();
  }));
  function tr(key, locale, values = {}) {
    const template = catalogues[language(locale)]?.[key] || catalogues.en?.[key] || key;
    return template.replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(values, name) ? String(values[name]) : match);
  }
  return { ready, language, tr };
})();
