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

import os, sys, time, tempfile
import tm_history as th, tm_procfs as tp, tm_services as ts

d = tempfile.mkdtemp()
h = th.History(os.path.join(d, "h.db"))
now = time.time()
# 20 gun onceden bugune sistem satirlari
for day in range(20, -1, -1):
    t = now - day * 86400
    h.add_sample(t, (50, 60, 1, 5, 5, 5, 1, 0, 0), [(1, "old", "p", 10, 1000, 0, 0)])
    h.add_event(t, "x", "ev")
h.flush()
print("span", h.span(), "size", h.size())
n = h.prune(14, now)
print("pruned rows", n)
assert n == 6 * 3, n
sp = h.span(); assert sp[2] == 15
# stall
base = now - 600
for i in range(10):
    h.add_sample(base + i * 10, (95, 70, 1, 60, 5, 5, 8, 0, 0), [(7, "dotnet", "Proj", 300, 5e9, 0, 0)])
rec = th.Recorder(h, 25)
assert rec.step(now, (10, 10, 0, 0, 0, 0, 0, 0, 0), lambda: [{"pid": 1, "name": "a", "project": "", "cpu": 5, "mem": 100}])
assert not rec.step(now + 2, (10,) * 9, lambda: [])
h.flush()
st = h.stalls(now - 3600, now, 25)
print("stalls", st)
assert len(st) == 1 and st[0]["cause"] == "cpu" and st[0]["culprits"][0][0] == "dotnet"
print("off", h.offenders(now - 3600, now)[:2])
print("series", h.series(now - 3600, now)[0], len(h.series(now - 3600, now)[1]))
print("events", len(h.events(0, now + 10)))
h.clear(); assert h.span() is None
h.close()

# yardimci: pkexec olmadan
ph = tp.PrivHelper(cmd=[sys.executable, "-I", "-c", tp.helper_source()])
print("uid", ph.start())
st = ph.request("stats"); print("stats pids", len(st), st.get(os.getpid()))
print("sockets", len(ph.request("sockets")))
r = ph.request("owners", path="/tmp"); print("owners", len(r[0]), r[1])
try: ph.request("bogus")
except tp.HelperError as e: print("err ok", e)
ph.stop(); assert not ph.alive
try: ph.request("ping")
except tp.HelperError as e: print("dead ok")

# servisler
u, err = ts.list_user_services(); print("services", len(u), err, u[:2])
print("autostart", [(a["file"], a["enabled"]) for a in ts.list_autostart()])
os.environ["XDG_CONFIG_HOME"] = d
ts.AUTOSTART_USER = os.path.join(d, "autostart")
a = ts.list_autostart()
if a:
    f = a[0]["file"]
    assert ts.set_autostart(f, False) is None
    assert [x for x in ts.list_autostart() if x["file"] == f][0]["enabled"] is False
    assert ts.set_autostart(f, True) is None
    assert [x for x in ts.list_autostart() if x["file"] == f][0]["enabled"] is True
assert ts.write_self_autostart("tskmngr --background", True) is None and ts.self_autostart_enabled()
assert ts.write_self_autostart("", False) is None and not ts.self_autostart_enabled()
print("TAMAM")
