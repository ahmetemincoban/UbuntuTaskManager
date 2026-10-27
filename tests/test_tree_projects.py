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


def all_pids(model, it=None):
    out = []
    c = model.iter_children(it)
    while c:
        out.append((model.get_value(c, m.COL_PID), model.get_value(c, m.COL_PPID)))
        out += all_pids(model, c)
        c = model.iter_next(c)
    return out

# test sockets
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(); port = srv.getsockname()[1]
sh = subprocess.Popen(["sh", "-c", "sleep 41339; true"])
time.sleep(0.3)

tm = m.TaskManager()
win = Gtk.OffscreenWindow(); win.set_size_request(1180, 720); win.add(tm.root); win.show_all()
m.spin(2.5)

# --- port arama
tm.search.set_text(f":{port}"); m.spin(0.4)
vis = [p for p, _ in all_pids(tm.sort)]
print("port", port, "->", vis, "(beklenen", [os.getpid()], ")")
assert vis == [os.getpid()]
tm.search.set_text(f"port {port}"); m.spin(0.3)
assert [p for p, _ in all_pids(tm.sort)] == [os.getpid()]
row = [r for r in tm.store if r[m.COL_PID] == os.getpid()][0]
print("Portlar sutunu:", repr(row[m.COL_PORTS]))
assert str(port) in row[m.COL_PORTS]
# docker portu (pid yok) -> aciklama
tm.search.set_text(":3306"); m.spin(1.2)
print("aciklama:", tm.flash_text)
tm.search.set_text(":59999"); m.spin(1.2)
print("aciklama2:", tm.flash_text)
tm.search.set_text(""); m.spin(0.3)

# --- agac modu
tm.btn_tree.set_active(True); m.spin(1.0)
tm.refresh_processes(); m.spin(0.3)
pairs = all_pids(tm.store)
live = {p for p, _ in pairs}
top = [r[m.COL_PID] for r in tm.store]
print("agac: toplam", len(pairs), "kok", len(top))
assert len(pairs) == len(tm.iters)
# sleep, sh'nin cocugu olmali
sleep = [p for p in m.psutil.process_iter(["cmdline"]) if p.info["cmdline"] == ["sleep", "41339"]][0]
sp = tm.store.get_path(tm.iters[sleep.pid]); shp = tm.store.get_path(tm.iters[sh.pid])
print("sleep yolu", sp.to_string(), "sh yolu", shp.to_string())
assert sp.to_string().startswith(shp.to_string() + ":")
# filtre: sleep ara -> sh de (ata) gorunmeli
tm.search.set_text("sleep 41339"); m.spin(0.4)
v = [p for p, _ in all_pids(tm.sort)]
print("agac filtre:", sh.pid in v, sleep.pid in v)
assert sh.pid in v and sleep.pid in v
tm.search.set_text(""); m.spin(0.2)
# agac sonlandirma
sel = tm.tree.get_selection(); sel.unselect_all()
tm.tree.expand_all()
def find(model, pid, it=None):
    c = model.iter_children(it)
    while c:
        if model.get_value(c, m.COL_PID) == pid: return model.get_path(c)
        r = find(model, pid, c)
        if r: return r
        c = model.iter_next(c)
sel.select_path(find(tm.sort, sh.pid))
assert [s[0] for s in tm.selected()] == [sh.pid]
tm.confirm = lambda *a: True
tm.end_tasks(signal.SIGKILL, tree=True); m.spin(1.8)
sh.wait()
tm.refresh_processes(); m.spin(0.3)
pairs = all_pids(tm.store)
assert len(pairs) == len(tm.iters)
assert sh.pid not in tm.iters and sleep.pid not in tm.iters
print("agac sonlandirma OK, kalan sleep:", len([p for p in m.psutil.process_iter(['cmdline']) if p.info['cmdline']==['sleep','41339']]))

# agac -> duz gecis
tm.btn_tree.set_active(False); m.spin(0.8)
assert all(tm.store.iter_n_children(r.iter) == 0 for r in tm.store)
print("duz mod OK, satir", len(tm.store))

# --- ayarlar
tm.root.set_current_page(m.TAB_DOCKER); m.spin(0.2)
tm.btn_tree.set_active(True)
tm.pcols.cols["ports"].set_visible(False)
tm.pcols.cols["name"].set_fixed_width(333)
tm.sort.set_sort_column_id(m.COL_RSS, Gtk.SortType.DESCENDING)
m.spin(0.5)
tm.save_settings(win)
print(open(m.settings_path()).read()[:600])
d = json.load(open(m.settings_path()))
assert d["tree_mode"] and d["tab"] == m.TAB_DOCKER and d["proc_sort"] == ["mem", "desc"] and d["proc_columns"]["ports"]["v"] is False
tm2 = m.TaskManager()
assert tm2.tree_mode and tm2.pcols.cols["ports"].get_visible() is False
print("ayarlar geri yuklendi: genislik", tm2.pcols.cols["name"].get_fixed_width(), tm2.sort.get_sort_column_id())
assert tm2.pcols.cols["name"].get_fixed_width() == 333
print("TEST TAMAM")
