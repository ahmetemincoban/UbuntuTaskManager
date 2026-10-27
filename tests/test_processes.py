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




def visible_pids(tm):
    return [r[m.COL_PID] for r in tm.sort]


def select_pids(tm, pids):
    sel = tm.tree.get_selection()
    sel.unselect_all()
    for row in tm.sort:
        if row[m.COL_PID] in pids:
            sel.select_path(row.path)


procs = [subprocess.Popen(["sleep", "31337"]) for _ in range(2)]
solo = subprocess.Popen(["sleep", "31338"])
pids = {p.pid for p in procs}

tm = m.TaskManager()
win = Gtk.OffscreenWindow()
win.set_size_request(1100, 700)
win.add(tm.root)
win.show_all()
m.spin(2.5)

# 1) arama filtresi PID uzerinden: tek PID yazinca tam o surec gorunmeli
first = procs[0].pid
tm.search.set_text(str(first))
m.spin(0.3)
vis = visible_pids(tm)
print("PID filtresi", first, "->", vis)
assert first in vis and procs[1].pid not in vis

# 2) komut filtresi: iki sleep de gorunmeli
tm.search.set_text("sleep 31337")
m.spin(0.3)
vis = visible_pids(tm)
print("komut filtresi ->", len(vis), "satir; iki sleep da var mi:", pids <= set(vis))
assert pids <= set(vis)

# 3) dotnet filtresi
tm.search.set_text("")
tm.btn_dev.set_active(True)
m.spin(0.3)
print("dotnet filtresi ->", tm.sort.iter_n_children(None), "/", len(tm.last_procs), "satir; sleep gizli mi:",
      not (pids & set(visible_pids(tm))))
assert not (pids & set(visible_pids(tm)))
tm.btn_dev.set_active(False)
m.spin(0.2)

# 4) iki sleep'i sec -> onay (otomatik evet) -> SIGTERM
select_pids(tm, pids)
sel = tm.selected()
print("secili:", [(p, n) for p, n, _c, _w in sel])
assert {s[0] for s in sel} == pids
asked = {}
tm.confirm = lambda title, detail, ok: asked.update(title=title, detail=detail) or True
tm.end_tasks(signal.SIGTERM)
m.spin(2.0)
print("onay basligi:", asked["title"])
for p in procs:
    p.poll()
print("returncode:", [p.returncode for p in procs], "(beklenen [-15, -15])")
assert all(p.returncode == -signal.SIGTERM for p in procs)
tm.refresh_processes()
print("listede kalan:", pids & set(visible_pids(tm)), "(beklenen bos)")
assert not (pids & set(visible_pids(tm)))
print("alt cubuk:", tm.status.get_text())

# 5) SIGKILL + surec agaci: sh -> sleep
sh = subprocess.Popen(["sh", "-c", "sleep 31339; true"])
time.sleep(0.3)
m.spin(2.2)
select_pids(tm, {sh.pid})
assert tm.selected(), "sh listede yok"
tm.end_tasks(signal.SIGKILL, tree=True)
m.spin(1.5)
sh.poll()
sh.wait()
kids = [p for p in m.psutil.process_iter(["cmdline"]) if p.info["cmdline"] == ["sleep", "31339"]]
print("sh returncode:", sh.returncode, "| kalan alt sleep sayisi:", len(kids), "(beklenen -9 ve 0)")
assert sh.returncode == -9 and not kids

# 6) temizleme hedefleri (oldurmeden) ve korumali PID
targets = tm.cleanup_targets()
print("temizleme hedefi:", len(targets)); assert isinstance(targets, list)
print("kendi PID'ini oldurme denemesi:", m.kill_pids([os.getpid()], signal.SIGTERM, False))

solo.kill()
solo.wait()
print("TEST TAMAM")
