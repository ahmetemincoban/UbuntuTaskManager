"""Cok dil destegi: kaynak metinler Turkce; tm_lang.CATALOG Turkce metni en / de / fr karsiliklarina esler.

Kullanim:   from tm_i18n import _        _("Sonlandır")        _("{n} süreç").format(n=3)
Dil secimi: settings.json "language": "auto" (sistem dili) | "tr" | "en" | "de" | "fr". Degisiklik yeniden
            baslatinca gecerli olur; dil bu modul ilk ice aktarilirken bir kez belirlenir.
Katalogda olmayan metin oldugu gibi (Turkce) gosterilir.
"""
import json
import os

LANGUAGES = [("tr", "Türkçe"), ("en", "English"), ("de", "Deutsch"), ("fr", "Français")]
CODES = [c for c, _n in LANGUAGES]

_lang = "tr"
_table = {}


def _settings_language():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(base, "tskmngr", "settings.json"), encoding="utf-8") as f:
            value = json.load(f).get("language", "auto")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return value if value in CODES or value == "auto" else "auto"


def system_language():
    """LANGUAGE / LC_ALL / LC_MESSAGES / LANG degiskenlerinden desteklenen ilk dil; yoksa 'en'."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for part in os.environ.get(var, "").split(":"):
            code = part.strip().lower().replace("-", "_").split("_")[0].split(".")[0]
            if code in CODES:
                return code
    return "en"


def set_language(choice):
    """'auto' ya da dil kodu; gecerli dil kodunu dondurur."""
    global _lang, _table
    code = system_language() if choice == "auto" or choice not in CODES else choice
    _lang = code
    _table = {}
    if code != "tr":
        import tm_lang
        idx = tm_lang.ORDER.index(code)
        _table = {tr: vals[idx] for tr, vals in tm_lang.CATALOG.items() if vals[idx]}
    return code


def current():
    return _lang


def _(text):
    return _table.get(text, text)


set_language(_settings_language())
