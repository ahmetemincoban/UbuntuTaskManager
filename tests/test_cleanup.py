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

tmp = tempfile.mkdtemp(dir=os.environ["SCR"])

# --- sahte proje: .NET bin/obj + node_modules
proj = os.path.join(tmp, "proj"); os.makedirs(proj)
open(os.path.join(proj, "A.csproj"), "w").write("<Project/>")
for d in ("bin/Debug", "obj", "src"):
    os.makedirs(os.path.join(proj, d))
open(os.path.join(proj, "bin/Debug/x.dll"), "wb").write(b"0" * 300000)
web = os.path.join(tmp, "web"); os.makedirs(os.path.join(web, "node_modules/a"))
open(os.path.join(web, "package.json"), "w").write("{}")
open(os.path.join(web, "node_modules/a/i.js"), "w").write("x" * 50000)
nopkg = os.path.join(tmp, "lone/node_modules"); os.makedirs(nopkg)  # package.json yok -> listelenmemeli
plain = os.path.join(tmp, "plain/bin"); os.makedirs(plain)           # csproj yok -> listelenmemeli

import tm_profiles
act = tm_profiles.Active(["dotnet", "angular"])
found = m.scan_clean([tmp], act)
paths = sorted(p for p, _a, _b in found)
print("bulunan:", [os.path.relpath(p, tmp) for p in paths])
assert paths == sorted([proj + "/bin", proj + "/obj", web + "/node_modules"])
sizes = m.dir_sizes(paths)
assert sizes[proj + "/bin"] > 300000 and sizes[web + "/node_modules"] > 50000
# guvenlik
assert m.safe_delete(proj + "/src", [tmp], act)[0] is False
assert m.safe_delete("/etc", [tmp], act)[0] is False
other = tempfile.mkdtemp(dir=os.environ["SCR"]); os.makedirs(other + "/bin")
assert m.safe_delete(other + "/bin", [tmp], act)[0] is False
os.symlink(web + "/node_modules", tmp + "/bin"); assert m.safe_delete(tmp + "/bin", [tmp], act)[0] is False
# kullanimdaki klasor
holder = subprocess.Popen(["sleep", "31341"], cwd=proj + "/obj")
time.sleep(0.2)
b = m.busy_dirs(paths); print("kullanimda:", b); assert list(b) == [proj + "/obj"]

# --- uygulama
procs_listener = subprocess.Popen([sys.executable, "-c",
    "import socket,time;s=socket.socket();s.bind(('127.0.0.1',45871));s.listen();time.sleep(60)"])
time.sleep(0.5)
tm = m.TaskManager()
tm.apply_profiles(["dotnet", "angular"], save=False)
tm.clean_roots = [tmp]
win = Gtk.OffscreenWindow(); win.set_size_request(1200, 720); win.add(tm.root); win.show_all()
m.spin(2.5)

# PSS
m.spin(5)
mine = [r for r in tm.last_procs if r["uid"] == m.ME]
with_pss = [r for r in mine if r["pss"]]
print("PSS dolu surec:", len(with_pss), "/", len(mine)); assert len(with_pss) > len(mine) * 0.8

# portlar sekmesi
tm.root.set_current_page(m.TAB_PORTS); m.spin(2)
rows = {(r[m.N_PORT], r[m.N_PID]): r for r in tm.nstore}
row = [r for r in tm.nstore if r[m.N_PORT] == 45871]
print("port satiri:", [(r[m.N_PID], r[m.N_NAME], r[m.N_ADDR]) for r in row])
assert row and row[0][m.N_PID] == procs_listener.pid and row[0][m.N_ADDR] == "yerel"
tm.ports_search.set_text("45871"); m.spin(0.4)
assert tm.nsort.iter_n_children(None) == 1
asked = {}
tm.confirm = lambda t, d, ok: asked.update(t=t, d=d) or True
tm.ntree.get_selection().select_all()
tm.ports_end(signal.SIGTERM); m.spin(1.5)
procs_listener.poll(); print("dinleyici:", procs_listener.returncode, asked["t"]); assert procs_listener.returncode == -15
m.spin(2.5)
assert not [r for r in tm.nstore if r[m.N_PORT] == 45871], "port satiri kalkmadi"
tm.ports_search.set_text("")

# dondur / devam
tm.root.set_current_page(m.TAB_PROCESSES); m.spin(1.5)
tm.search.set_text(str(holder.pid)); m.spin(0.5)
tm.tree.get_selection().select_all()
assert tm.selected() and tm.selected()[0][0] == holder.pid
tm.freeze(signal.SIGSTOP); m.spin(2.5)
st = m.psutil.Process(holder.pid).status(); print("durum:", st); assert st == "stopped"
r = [r for r in tm.sort if r[m.COL_PID] == holder.pid][0]
assert r[m.COL_STATUS] == "stopped"
tm.freeze(signal.SIGCONT); m.spin(2.5)
assert m.psutil.Process(holder.pid).status() != "stopped"

# nice
tm.tree.get_selection().select_all()
tm.set_nice(10); m.spin(2.5)
print("nice:", m.psutil.Process(holder.pid).nice()); assert m.psutil.Process(holder.pid).nice() == 10
r = [r for r in tm.sort if r[m.COL_PID] == holder.pid][0]; assert r[m.COL_NICE] == 10
# nice dusurme: yetki hatasi mesaji
tm.set_nice(0); print("nice0 mesaji:", tm.status_msg if hasattr(tm, 'status_msg') else '')

# affinity
tm.choose_affinity = lambda cur: [0, 1] if m.NCPU > 1 else [0]
tm.set_affinity()
print("affinity:", m.psutil.Process(holder.pid).cpu_affinity()); assert m.psutil.Process(holder.pid).cpu_affinity() == ([0, 1] if m.NCPU > 1 else [0])

# ayrintilar
d = m.process_details(holder.pid)
print(d["general"][:400]); assert "sleep" in d["general"] and "nice" in d["general"]
tm.show_selected_details(); m.spin(0.5)
assert holder.pid in tm.details
buf = tm.details[holder.pid].test_views["general"].get_buffer()
txt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False); assert "31341" in txt
tm.details[holder.pid].destroy(); m.spin(0.2); assert holder.pid not in tm.details
dsrv = m.process_details(procs_listener.pid) if False else None
tm.search.set_text("")

# temizlik sekmesi
tm.root.set_current_page(m.TAB_CLEAN)
for _ in range(40):
    m.spin(0.5)
    if not tm.clean_busy and len(tm.cstore): break
print("temizlik satirlari:", [(r[m.C_KIND], r[m.C_SIZE]) for r in tm.cstore])
assert len(tm.cstore) == 3
print("durum:", tm.clean_status.get_text()); print("docker:", tm.docker_df_label.get_text())
tm.ctree.get_selection().select_all(); m.spin(0.2)
tm.clean_delete(); m.spin(2.5)
assert os.path.isdir(proj + "/obj"), "kullanimdaki klasor silindi!"
assert not os.path.exists(proj + "/bin") and not os.path.exists(web + "/node_modules")
assert os.path.exists(web + "/package.json") and os.path.exists(proj + "/A.csproj")
print("silme sonrasi:", tm.clean_status.get_text())
assert len(tm.cstore) == 1

# ayarlar
tm.save_settings()
cfg = json.load(open(os.path.join(os.environ["XDG_CONFIG_HOME"], "tskmngr/settings.json")))
for k in ("ports_columns", "ports_sort", "clean_columns", "clean_sort", "clean_roots"): assert k in cfg, k
holder.kill(); holder.wait()
print("TEST TAMAM")
