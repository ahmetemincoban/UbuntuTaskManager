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
notes = []
tm.notify = lambda *a, **k: notes.append(a)
win = Gtk.OffscreenWindow(); win.set_size_request(1200, 760); win.add(tm.root); win.show_all()
m.spin(2)

# --- ayarlar kalicilik
assert tm.pref("log_days") == 14
tm.set_pref("log_days", 3); tm.set_pref("log_days", 9999)
assert tm.pref("log_days") == 365
tm.set_pref("log_days", 3)
saved = json.load(open(os.path.join(os.environ["XDG_CONFIG_HOME"], "tskmngr", "settings.json")))
assert saved["log_days"] == 3 and saved["version"] == 2, saved
print("pref persisted OK")

# --- gecmis: sahte veri + prune
h = tm.history; now = time.time()
for i in range(60):
    ts = now - 600 + i * 10
    psi = 60 if 20 <= i < 30 else 2
    h.add_sample(ts, (80 if psi > 50 else 10, 50, 0, psi, 0, 0, 1, 0, 0),
                 [(111, "dotnet", "projA", 90.0, 1 << 30, 0, 0)] if i % 6 == 0 or psi > 50 else [])
h.add_sample(now - 5 * 86400, (5, 5, 0, 0, 0, 0, 0, 0, 0), [(1, "old", "", 1.0, 1, 0, 0)])
h.add_event(now - 5 * 86400, "kill", "old event")
h.flush()
tm.root.set_current_page(m.TAB_HISTORY); m.spin(1.5)
tm.refresh_history(); m.spin(1)
print("stall rows:", len(tm.hist_stalls), "offs:", len(tm.hist_offs), "events:", len(tm.hist_events))
assert len(tm.hist_stalls) >= 1 and len(tm.hist_offs) >= 1
tm.hist_range.set_active(5); m.spin(1)  # Tumu
assert len(tm.hist_events) >= 1
# log_days degisimi hemen silmez
assert h.span()[2] > 60
res = {}
tm.prune_history(3, lambda n: res.update(n=n)); m.spin(1.5)
print("pruned", res)
assert res.get("n", 0) >= 2
tm.refresh_history(); m.spin(1)
assert not any(r[2] == "old event" for r in tm.hist_events)

# --- ayarlar penceresi
tm.open_settings(); m.spin(0.5)
w = tm.settings_win.test_parts
w["log_days"].set_value(21); assert tm.pref("log_days") == 21
w["log_enabled"].set_active(False); assert tm.pref("log_enabled") is False
w["log_enabled"].set_active(True)
tm.settings_win.destroy(); m.spin(0.3)

# --- git sutunlari
tm.root.set_current_page(m.TAB_PROJECTS); m.spin(1)
tm.update_projects(True); m.spin(3)
print("proj rows:", [(r[m.P_NAME], r[m.P_BRANCH], r[m.P_DIRTY]) for r in tm.pstore][:5])

# --- servisler
tm.root.set_current_page(m.TAB_SERVICES); m.spin(4)
print("services rows:", len(tm.sstore), tm.svc_status.get_text())
assert len(tm.sstore) > 0

# --- temizlik: git'te izlenen klasor korumasi
d = tempfile.mkdtemp(dir=os.environ["SCR"])
subprocess.run(["git", "init", "-q", d], check=True)
os.makedirs(d + "/bin"); open(d + "/bin/a.txt", "w").write("x")
subprocess.run(["git", "-C", d, "add", "-f", "bin/a.txt"], check=True)
os.makedirs(d + "/obj"); open(d + "/obj/b.txt", "w").write("x")
print("tracked_dirs:", m.tracked_dirs([d + "/bin", d + "/obj"]))
assert m.tracked_dirs([d + "/bin", d + "/obj"]) == {d + "/bin"}
print("git_status:", m.git_status(d))

# --- gizle/goster
tm.set_hidden(True); m.spin(2); assert tm.hidden
tm.set_hidden(False); m.spin(1); assert not tm.hidden

# --- priv helper
from tm_procfs import PrivHelper, helper_source
ph = PrivHelper(cmd=[sys.executable, "-I", "-c", helper_source()])
print("uid", ph.start()); print("stats", len(ph.request("stats"))); ph.stop()
print("T5 OK")
