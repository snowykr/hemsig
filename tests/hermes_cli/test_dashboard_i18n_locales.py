from pathlib import Path


def test_dashboard_i18n_registers_all_supported_locales():
    root = Path(__file__).resolve().parents[2]
    context = (root / "web" / "src" / "i18n" / "context.tsx").read_text(encoding="utf-8")
    types = (root / "web" / "src" / "i18n" / "types.ts").read_text(encoding="utf-8")

    for locale in ("en", "zh", "fr", "uk", "tr"):
        assert f'"{locale}"' in types
        assert f'"{locale}"' in context


def test_dashboard_i18n_locale_files_exist_for_all_supported_locales():
    root = Path(__file__).resolve().parents[2] / "web" / "src" / "i18n"

    for locale in ("en", "zh", "fr", "uk", "tr"):
        assert (root / f"{locale}.ts").exists()
