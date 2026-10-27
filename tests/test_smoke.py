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
tm.settings_win.destroy(); m.spin(0.3)
tm.save_settings(); print("saved")
tm.history.flush(); print("span", tm.history.span())
