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

base = tempfile.mkdtemp(dir=os.environ["SCR"])
proj = os.path.join(base, "demoproj"); os.makedirs(os.path.join(proj, ".git")); os.makedirs(os.path.join(proj, "bin"))
plain = os.path.join(base, "nogit"); os.makedirs(plain)
data = os.path.join(proj, "bin", "data.txt"); open(data, "w").write("x")

# 7) dosya sahibi
holder = subprocess.Popen([sys.executable, "-c", f"import time; f=open({data!r}); time.sleep(300)"], cwd=proj)
inproj = subprocess.Popen(["sleep", "41341"], cwd=proj)
inproj2 = subprocess.Popen(["sleep", "41342"], cwd=os.path.join(proj, "bin"))
outside = subprocess.Popen(["sleep", "41343"], cwd=plain)
time.sleep(0.6)
found, denied = m.find_owners(data)
print("dosya sahibi:", [(p, k) for p, _n, k, _x in found], "okunamayan:", denied)
assert [p for p, *_ in found] == [holder.pid] and "açık dosya" in found[0][2]
found, _ = m.find_owners(proj)
pids = {p: k for p, _n, k, _x in found}
print("klasor sahibi:", pids)
assert {inproj.pid, inproj2.pid, holder.pid} <= set(pids) and outside.pid not in pids
assert "çalışma dizini" in pids[inproj.pid]
# mmap: python kendi libini
libs = [l.split()[-1] for l in open(f"/proc/{holder.pid}/maps") if ".so" in l and l.split()[-1].startswith("/")]
found, _ = m.find_owners(libs[0]); print("mmap:", libs[0], "->", holder.pid in [p for p, *_ in found])
assert holder.pid in [p for p, *_ in found]

# 9) proje kokleri
assert m.project_root(proj) == proj and m.project_root(os.path.join(proj, "bin")) == proj
assert m.project_root(plain) == "" and m.project_root(m.HOME) == "" and m.project_root("") == ""
open(os.path.join(plain, "x.csproj"), "w").write("")
m._root_cache.clear(); assert m.project_root(plain) == plain
os.remove(os.path.join(plain, "x.csproj")); m._root_cache.clear()

tm = m.TaskManager()
win = Gtk.OffscreenWindow(); win.set_size_request(1180, 720); win.add(tm.root); win.show_all(); m.spin(2.5)
tm.refresh_processes()
row = [r for r in tm.pstore if r[m.P_ROOT] == proj]
print("proje satiri:", [(r[m.P_NAME], r[m.P_COUNT], r[m.P_PATH]) for r in row])
assert row and row[0][m.P_COUNT] == 3
tm.search.set_text("proje:" + proj); m.spin(0.4)
vis = sorted(r[m.COL_PID] for r in tm.sort)
print("proje filtresi:", vis); assert vis == sorted([holder.pid, inproj.pid, inproj2.pid])
tm.search.set_text("proje:demoproj"); m.spin(0.3)
print("ad ile:", tm.proj_filter, sorted(r[m.COL_PID] for r in tm.sort)[:6]); assert sorted(r[m.COL_PID] for r in tm.sort) == vis
tm.search.set_text(""); m.spin(0.2)
# proje sonlandir
tm.confirm = lambda *a: True
sel = tm.ptree.get_selection(); sel.unselect_all()
for r in tm.psort:
    if r[m.P_ROOT] == proj: sel.select_path(r.path)
tm.project_action(signal.SIGKILL); m.spin(1.5)
for p in (holder, inproj, inproj2): p.wait(timeout=3)
outside.poll(); print("sonlandirma:", holder.returncode, inproj.returncode, inproj2.returncode, "| disarida kalan canli:", outside.returncode is None)
assert outside.returncode is None
tm.refresh_processes(); m.spin(0.3)
assert proj not in tm.proj_iters
# proje disi satiri korunur
sel.unselect_all()
for r in tm.psort:
    if r[m.P_ROOT] == "": sel.select_path(r.path)
called = []
tm.confirm = lambda *a: called.append(1) or True
tm.project_action(signal.SIGTERM)
print("proje disi:", tm.flash_text, "| onay istendi mi:", bool(called)); assert not called
outside.kill(); outside.wait()

# 8) takili surec
procs = [{"pid": 1, "status": "disk-sleep"}, {"pid": 2, "status": "sleeping"}]
tm.dsince.clear()
tm.update_stuck(procs, now=100.0); assert tm.stuck == set()
tm.update_stuck(procs, now=100.0 + m.STUCK_SECS - 1); assert tm.stuck == set()
tm.update_stuck(procs, now=100.0 + m.STUCK_SECS + 0.1); assert tm.stuck == {1}
tm.update_stuck([{"pid": 1, "status": "sleeping"}], now=200.0); assert tm.stuck == set() and not tm.dsince
# takili filtresi + durum cubugu: gercek bir satir icin zorla
mypid = os.getpid(); tm.stuck = {mypid}
tm.flash_until = 0; tm.search.set_text("takılı"); m.spin(0.3)
vis = [r[m.COL_PID] for r in tm.sort]; print("takili filtresi:", vis); assert vis == [mypid]
tm.update_status(); print("durum cubugu:", tm.status.get_text())
assert "takılı" in tm.status.get_text()
tm.stuck = set(); tm.search.set_text(""); m.spin(0.2)

# bulucu penceresi (arayuz)
tm.open_owner_finder(); m.spin(0.3)
entry, store, info, btn_go = tm.finder.test_parts
h2 = subprocess.Popen([sys.executable, "-c", f"import time; f=open({data!r}); time.sleep(300)"]); time.sleep(0.5)
entry.set_text(data); btn_go.clicked(); m.spin(2.5)
print("bulucu:", [tuple(r) for r in store], "|", info.get_text())
assert [r[0] for r in store] == [h2.pid]
h2.kill(); h2.wait(); tm.finder.destroy(); m.spin(0.2); assert tm.finder is None
print("TEST TAMAM")
