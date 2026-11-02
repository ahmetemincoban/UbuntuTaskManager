"""Guvenlik sertlestirmeleri: ozel izinler, Exec= tirnaklama, git fsmonitor kapali, bozuk yardimci cevabi."""
import os
import stat
import sys
import tempfile
import threading

from common import SCR, load_app  # noqa: F401

import tm_history as th
import tm_procfs as tp
import tm_services as ts

m = load_app()

# Exec= satiri: bosluklu / ozel karakterli yollar tirnaklanir, duz yollar oldugu gibi kalir
assert ts.desktop_exec("/usr/bin/python3", "/opt/tm/taskmgr_gui.py", "--background") == \
    "/usr/bin/python3 /opt/tm/taskmgr_gui.py --background"
assert ts.desktop_exec("/usr/bin/python3", "/home/a b/tm.py", "--background") == \
    '/usr/bin/python3 "/home/a b/tm.py" --background'
assert ts.desktop_exec("/x/100%/tm.py") == '"/x/100%%/tm.py"'
assert ts.desktop_exec("/x/$HOME/`id`.py") == '"/x/\\$HOME/\\`id\\`.py"'

# gecmis veritabani ve klasoru yalniz sahibine acik
d = os.path.join(tempfile.mkdtemp(), "yeni")
h = th.History(os.path.join(d, "h.db"))
h.flush()
assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, oct(os.stat(d).st_mode)
assert stat.S_IMODE(os.stat(os.path.join(d, "h.db")).st_mode) == 0o600
h.close()

# ayar dosyasi yalniz sahibine acik
m.write_settings({"a": 1})
sp = m.settings_path()
assert stat.S_IMODE(os.stat(sp).st_mode) == 0o600, oct(os.stat(sp).st_mode)
assert stat.S_IMODE(os.stat(os.path.dirname(sp)).st_mode) == 0o700

# git cagrilari depo yapilandirmasindaki core.fsmonitor komutunu calistirmaz
repo = tempfile.mkdtemp()
marker = os.path.join(repo, "pwned")
os.system(f"git init -q {repo} && git -C {repo} config core.fsmonitor 'touch {marker}; echo'")
m.git_status(repo)
m.git_ignored(os.path.join(repo, "x"))
assert not os.path.exists(marker), "core.fsmonitor calisti"

# yardimci bozuk (JSON olmayan) cevap verirse HelperError, cokme degil
helper = tp.PrivHelper(cmd=[sys.executable, "-c", "import sys\nfor l in sys.stdin: print('not json'); sys.stdout.flush()"])
try:
    helper.start()
    raise AssertionError("HelperError bekleniyordu")
except tp.HelperError:
    pass
assert not helper.alive

print("OK")
