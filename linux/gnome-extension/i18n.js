// UI strings only. Text, suggestions and model explanations never enter this catalogue.
import Gio from 'gi://Gio';

let english = {};
try {
    const file = Gio.File.new_for_uri(import.meta.url).get_parent().get_child('locales').get_child('en.json');
    const [ok, bytes] = file.load_contents(null);
    if (ok) english = JSON.parse(new TextDecoder().decode(bytes));
} catch (error) {
    console.error(`GramaDrama: cannot load the English UI catalogue: ${error.message}`);
}

export function uiLanguage(value) {
    return value === 'pl' || value === undefined || value === null ? 'pl' : 'en';
}

export function tr(source, language = 'pl', values = {}) {
    const template = uiLanguage(language) === 'pl' ? source : english[source] ?? source;
    return template.replace(/\{([A-Za-z_][A-Za-z_0-9]*)\}/g,
        (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match);
}
