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

tm = m.TaskManager(); notes=[]; tm.notify=lambda *a,**k: notes.append(a); m.spin(1.5)
# auto idle
victim = subprocess.Popen(["sleep","31400"])
row = dict(pid=victim.pid, rss=1<<20, cmd="sleep 31400", idle="test")
tm.cleanup_targets = lambda: [row]
tm.set_pref("auto_idle", True); tm.set_pref("auto_idle_min", 1); tm.set_pref("auto_idle_mem", 0)
tm.auto_idle_step(50); assert victim.poll() is None
tm.idle_since[victim.pid] -= 61
tm.auto_idle_step(50); time.sleep(0.5); print("victim rc", victim.poll()); assert victim.poll() == -15
# mem threshold gate
v2 = subprocess.Popen(["sleep","31401"]); row2=dict(pid=v2.pid,rss=1,cmd="x",idle="test"); tm.cleanup_targets=lambda:[row2]
tm.set_pref("auto_idle_mem", 90); tm.auto_idle_step(50); tm.idle_since[v2.pid]-=3600; tm.auto_idle_step(50)
assert v2.poll() is None; v2.kill()
# temizlik: izlenen klasor
d = tempfile.mkdtemp(dir=os.environ["SCR"]); subprocess.run(["git","init","-q",d],check=True)
os.makedirs(d+"/bin"); open(d+"/bin/a","w").write("x"); subprocess.run(["git","-C",d,"add","-f","bin/a"],check=True)
os.makedirs(d+"/obj"); open(d+"/obj/b","w").write("x")
print("safe_delete tracked:", m.safe_delete(d+"/bin",[os.path.dirname(d)]), "untracked:", m.safe_delete(d+"/obj",[os.path.dirname(d)]))
assert os.path.exists(d+"/bin") and not os.path.exists(d+"/obj")
# kapat -> arka plan
class W:  # delete-event davranisi
    pass
print("bg_run", tm.pref("bg_run"), "hide fn", hasattr(tm,"set_hidden"), hasattr(tm,"close"))
tm.on_priv_toggled if hasattr(tm,"on_priv_toggled") else None
print("T7 OK")
