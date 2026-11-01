"""Cok dil: katalog kapsami, yer tutucu uyumu, dil secimi (settings.json / sistem dili)."""
import ast
import json
import os
import re

from common import ROOT, SCR  # noqa: F401

import tm_i18n
import tm_lang

SOURCES = ["taskmgr_gui.py", "tm_procfs.py", "tm_services.py", "tm_profiles.py", "tm_alerts.py", "tm_history.py"]
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text))


def source_msgids():
    ids = {}
    for name in SOURCES:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                ids.setdefault(node.args[0].value, f"{name}:{node.lineno}")
    return ids


# ---- katalog yapisi
assert tm_lang.ORDER == ("en", "de", "fr")
for key, vals in tm_lang.CATALOG.items():
    assert isinstance(vals, tuple) and len(vals) == len(tm_lang.ORDER), f"uc ceviri olmali: {key!r}"
    for lang, text in zip(tm_lang.ORDER, vals):
        assert text.strip(), f"bos ceviri ({lang}): {key!r}"
        assert placeholders(text) == placeholders(key), f"yer tutucular farkli ({lang}): {key!r} -> {text!r}"
        assert text.count("<span") == key.count("<span") and text.count("<b>") == key.count("<b>"), \
            f"isaretleme farkli ({lang}): {key!r}"
        assert text.count("\n") == key.count("\n"), f"satir sonu sayisi farkli ({lang}): {key!r}"

# ---- kodda _() ile sarilan her Turkce metin katalogda var (her dilde ayni olanlar haric)
ids = source_msgids()
assert len(ids) > 400, len(ids)
untranslated = sorted(k for k in ids if k not in tm_lang.CATALOG and re.search(r"[^\W\d_]{3,}", PLACEHOLDER.sub("", k)))
# yalniz yer tutucu / ozel ad olanlar (CPU, Docker, "{n} {k}" ...) her dilde aynidir
SAME_EVERYWHERE = {"CPU", "PID", "RAM", "Port", "Git", "Docker", "Docker: ", "Docker: –", "CPU %", "RAM %", "Dil / Language"}
untranslated = [k for k in untranslated if k not in SAME_EVERYWHERE]
assert not untranslated, "katalogda olmayan metinler:\n" + "\n".join(f"  {ids[k]}: {k!r}" for k in untranslated)
# _(degisken) ile cevrilenler: olay turleri (log_event) ve dosya sahibi turleri (tm_procfs.find_owners)
DYNAMIC = {"açık dosya", "çalışma dizini", "bellekte (mmap)", "bellek", "takılma", "disk", "temizlik"}
stale = sorted(k for k in tm_lang.CATALOG if k not in ids and k not in DYNAMIC)
assert not stale, f"kodda kullanilmayan katalog girdileri: {stale}"

# ---- dil secimi
assert tm_i18n.set_language("tr") == "tr" and tm_i18n._("Sonlandır") == "Sonlandır"
assert tm_i18n.set_language("en") == "en" and tm_i18n._("Sonlandır") == "End"
assert tm_i18n.set_language("de") == "de" and tm_i18n._("Sonlandır") == "Beenden"
assert tm_i18n.set_language("fr") == "fr" and tm_i18n._("Sonlandır") == "Terminer"
assert tm_i18n._("katalogda olmayan metin") == "katalogda olmayan metin"
assert tm_i18n._("{n} klasör silinsin mi? ({size})").format(n=2, size="1 GB") == "Supprimer 2 dossier(s) ? (1 GB)"
assert tm_i18n.set_language("xx") == tm_i18n.system_language(), "gecersiz kod sistem diline duser"

for env, want in [({"LANGUAGE": "de_DE.UTF-8:en"}, "de"), ({"LANG": "fr_CA.UTF-8"}, "fr"), ({"LANG": "tr_TR.UTF-8"}, "tr"),
                  ({"LANG": "ja_JP.UTF-8"}, "en"), ({"LC_ALL": "en_US.UTF-8", "LANG": "tr_TR.UTF-8"}, "en"), ({}, "en")]:
    saved = {k: os.environ.pop(k, None) for k in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")}
    os.environ.update(env)
    try:
        assert tm_i18n.system_language() == want, (env, tm_i18n.system_language())
        assert tm_i18n.set_language("auto") == want
    finally:
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})

# ---- settings.json "language"
cfg = os.path.join(os.environ["XDG_CONFIG_HOME"], "tskmngr")
os.makedirs(cfg, exist_ok=True)
path = os.path.join(cfg, "settings.json")
for value, want in [("de", "de"), ("auto", "auto"), ("zz", "auto"), (5, "auto")]:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"language": value}, f)
    assert tm_i18n._settings_language() == want, (value, tm_i18n._settings_language())
with open(path, "w", encoding="utf-8") as f:
    f.write("{bozuk")
assert tm_i18n._settings_language() == "auto"
os.remove(path)
assert tm_i18n._settings_language() == "auto"

print("OK: i18n")
