import { createContext, useContext, useState, useCallback, type ReactNode } from "react";
import type { Locale, Translations } from "./types";
import { en } from "./en";
import { fr } from "./fr";
import { tr } from "./tr";
import { uk } from "./uk";
import { zh } from "./zh";

export const SUPPORTED_LOCALES = ["en", "zh", "fr", "uk", "tr"] as const;

const TRANSLATIONS: Record<Locale, Translations> = { en, zh, fr, uk, tr };
const STORAGE_KEY = "hermes-locale";

function getInitialLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED_LOCALES.includes(stored as Locale)) return stored as Locale;
  } catch {
    // SSR or privacy mode
  }
  return "en";
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: Translations;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  setLocale: () => {},
  t: en,
});

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem(STORAGE_KEY, l);
    } catch {
      // ignore
    }
  }, []);

  const value: I18nContextValue = {
    locale,
    setLocale,
    t: TRANSLATIONS[locale],
  };

  return (
    <I18nContext.Provider value={value}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n() {
  return useContext(I18nContext);
}
