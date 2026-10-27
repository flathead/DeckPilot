/** Catalogs are embedded by tools/build.py for Decky's single-file frontend. */
const translations = __DECKPILOT_MESSAGES__;
const fallbackError = 'Не удалось выполнить действие. Проверьте связь с ПК и повторите попытку.';

export function translate(language, message, values={}) {
  const selected = translations[language] || translations.ru;
  const value = selected[message] ?? translations.ru[message] ?? message;
  return String(value).replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g,
    (match, key) => Object.hasOwn(values, key) ? String(values[key]) : match);
}

export function translateError(language, error) {
  const message = String(error?.message || error || '');
  if (Object.hasOwn(translations.ru, message)) return translate(language, message);
  const english = Object.entries(translations.en).find(([, value])=>value===message);
  if (english) return translate(language, english[0]);
  const wakePrefix = 'Для WoL нужен корректный MAC. ';
  if (message.startsWith(wakePrefix))
    return translate(language,wakePrefix) + translateError(language,message.slice(wakePrefix.length));
  const shutdown = /^Не удалось подтвердить выключение \(код SSH: ([0-9]+)\)$/.exec(message);
  if (shutdown)
    return translate(language,'Не удалось подтвердить выключение (код SSH: %s)').replace('%s',shutdown[1]);
  // External controller and OS errors have no translation contract. Keep the UI
  // in the selected language rather than displaying an untranslated exception.
  return translate(language, fallbackError);
}
