import { Button } from "@nous-research/ui/ui/components/button";
import { Typography } from "@/components/NouiTypography";
import { SUPPORTED_LOCALES, useI18n } from "@/i18n/context";
import type { Locale } from "@/i18n/types";

const LOCALE_META: Record<Locale, { flag: string; label: string }> = {
  en: { flag: "🇬🇧", label: "EN" },
  zh: { flag: "🇨🇳", label: "中文" },
  fr: { flag: "🇫🇷", label: "FR" },
  uk: { flag: "🇺🇦", label: "UK" },
  tr: { flag: "🇹🇷", label: "TR" },
};

/**
 * Compact language toggle — shows a clickable flag that switches between
 * English and Chinese.  Persists choice to localStorage.
 */
export function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();

  const currentIndex = SUPPORTED_LOCALES.indexOf(locale);
  const nextLocale = SUPPORTED_LOCALES[(currentIndex + 1) % SUPPORTED_LOCALES.length];
  const currentMeta = LOCALE_META[locale];
  const nextMeta = LOCALE_META[nextLocale];

  const toggle = () => setLocale(nextLocale);

  return (
    <Button
      ghost
      onClick={toggle}
      title={`${t.language.switchTo}: ${nextMeta.label}`}
      aria-label={`${t.language.switchTo}: ${nextMeta.label}`}
      className="px-2 py-1 normal-case tracking-normal font-normal text-xs text-muted-foreground hover:text-foreground"
    >
      <span className="inline-flex items-center gap-1.5">
        <span className="text-base leading-none">{currentMeta.flag}</span>

        <Typography
          mondwest
          className="hidden sm:inline tracking-wide uppercase text-[0.65rem]"
        >
          {currentMeta.label}
        </Typography>
      </span>
    </Button>
  );
}
