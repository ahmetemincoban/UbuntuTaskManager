import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

from common import SCR, load_app  # noqa: F401

m = load_app()
Gtk = m.Gtk

tm = m.TaskManager()
tm.notify = lambda *a, **k: None
win = Gtk.OffscreenWindow(); win.set_size_request(1200, 760); win.add(tm.root); win.show_all()
m.spin(3)
for t in range(8):
    tm.root.set_current_page(t); m.spin(2.5)
    print("tab", t, "ok")
tm.root.set_current_page(m.TAB_PERFORMANCE)
tm.stack.set_visible_child_name("psi"); m.spin(1.5)
tm.open_settings(); m.spin(1)
print(tm.hist_info_label.get_text())

def find_combo(w):
    if isinstance(w, Gtk.ComboBoxText) and w.get_active_id() in ("auto", "tr", "en", "de", "fr"):
        return w
    for c in (w.get_children() if isinstance(w, Gtk.Container) else []):
        r = find_combo(c)
        if r:
            return r

restarts = []
tm.restart_app = lambda: restarts.append(1) and False  # gercekten yeniden baslatma
lang_combo = find_combo(tm.settings_win)
assert lang_combo is not None, "dil kutusu bulunamadi"
lang_combo.set_active_id("fr"); m.spin(0.8)
assert restarts == [1], "dil secilince uygulama kendini yeniden baslatmali"
assert m.load_settings().get("language") == "fr"
tm.settings_win.destroy(); m.spin(0.3)
tm.save_settings(); print("saved")
tm.set_pref("language", "de")  # dil secimi diske yazilmali (PREFS disinda oldugu icin bir kez kayboluyordu)
assert m.load_settings().get("language") == "de"
tm.save_settings()
assert m.load_settings().get("language") == "de", "baska bir kayit dil secimini silmemeli"
tm.set_pref("language", "gecersiz")
assert m.load_settings().get("language") == "auto"
tm.history.flush(); print("span", tm.history.span())
