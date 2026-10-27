"""Uyarilar: korunan surecler, takilma bildirimi + sonlandirma dugmesi, disk doluluk, swap/zram, proje gecmisi."""
from common import fresh_dir
import os
import signal
import subprocess
import tempfile
import time

import tm_alerts
import tm_history
from common import load_app

m = load_app()
ME = m.ME

# ---- korunan kaliplar
assert tm_alerts.parse_patterns(" LosevApi \n\nlosevapi\n" + "x" * 500) == ["LosevApi", "x" * 200]
assert tm_alerts.clean_patterns("metin") == [] and tm_alerts.clean_patterns([1, None, " a "]) == ["a"]
assert tm_alerts.is_protected(["losevapi"], "dotnet", "dotnet run", "/home/u/LosevApi")
assert not tm_alerts.is_protected([], "dotnet", "x", "") and not tm_alerts.is_protected(["abc"], "x", "y", "z")
assert tm_alerts.is_essential("gnome-shell") and tm_alerts.is_essential("Xorg")
assert tm_alerts.is_essential("python3", "python3 /x/taskmgr_gui.py") and not tm_alerts.is_essential("dotnet")

# ---- suclu secimi
def row(pid, name, cpu=0.0, rss=0, uid=ME, cmd=None):
    return dict(pid=pid, name=name, cmd=cmd or name, uid=uid, cpu=cpu, rss=rss, pss=0, io_r=0, io_w=0)

procs = [row(1, "dotnet", cpu=80, rss=1 << 30), row(2, "chrome", cpu=30, rss=3 << 30), row(3, "gnome-shell", cpu=95),
         row(4, "other", cpu=99, uid=ME + 1), row(5, "idle", cpu=1)]
assert tm_alerts.pick_culprit(procs, "cpu", ME)[0]["pid"] == 1, "gnome-shell ve baska kullanici elenmeli"
assert tm_alerts.pick_culprit(procs, "mem", ME)[0]["pid"] == 2
assert tm_alerts.pick_culprit(procs, "cpu", ME, ["dotnet"])[0]["pid"] == 2, "korunan elenmeli"
assert tm_alerts.pick_culprit(procs, "io", ME) is None, "esigin altindaki suclu gosterilmez"
assert tm_alerts.pick_culprit([row(5, "idle", cpu=1)], "cpu", ME) is None

# ---- swap / zram / disk
z = fresh_dir()
open(os.path.join(z, "mm_stat"), "w").write("4096000 1024000 1100000 0 0 0 0 0\n")
assert tm_alerts.zram_stats(os.path.join(z, "mm_*")) == (4096000, 1024000, 1100000)
assert tm_alerts.zram_stats(os.path.join(z, "yok*")) is None
sw = os.path.join(z, "swaps")
open(sw, "w").write("Filename Type Size Used Priority\n/swap.img file 2048 512 -2\n")
assert tm_alerts.swap_devices(sw) == [("/swap.img", "file", 2048 * 1024, 512 * 1024, -2)]
assert tm_alerts.full_disks(0, ["/"])[0][0] == "/" and tm_alerts.full_disks(101, ["/"]) == []
assert tm_alerts.watched_mounts()[0] == "/"

# ---- uygulama
tm = m.TaskManager()
sent = []
tm.notify = lambda title, body, buttons=(): sent.append((title, body, list(buttons)))
tm.log_event = lambda kind, text: sent.append(("event", kind, text))

# takilma bildirimi: esik altinda sessiz, ust uste STALL_TICKS sn ustunde bildirir, bekleme suresi var
victim = subprocess.Popen(["sleep", "31500"])
tm.last_procs = [row(victim.pid, "dotnet", cpu=85, cmd="sleep 31500")]
tm.proc_project = {victim.pid: "/home/u/LosevApi"}
tm.set_pref("stall_psi", 25)
now = 1000.0
for _ in range(m.STALL_TICKS + 3):
    assert tm.stall_step(now, 10, 0, 0) is False
assert not sent
results = [tm.stall_step(now, 60, 0, 0) for _ in range(m.STALL_TICKS)]
assert results == [False] * (m.STALL_TICKS - 1) + [True], results
title, body, buttons = [s for s in sent if s[0] != "event"][0]
print(title, "|", body, "|", [(b[0], b[1], b[2].get_string()) for b in buttons])
assert "CPU" in title and "dotnet" in body and "LosevApi" in body and "%85" in body
assert len(buttons) == 1 and buttons[0][1] == "app.kill-stall"
target = buttons[0][2].get_string()
assert tm.stall_step(now + 10, 60, 0, 0) is False, "5 dk icinde tekrar bildirmemeli"
tm.set_pref("notify_stall", False)
assert tm.stall_step(now + 1000, 60, 0, 0) is False and tm.stall_step(now + 1000, 60, 0, 0) is False
tm.set_pref("notify_stall", True)

# dugme: yanlis create_time -> dokunma; korunan -> dokunma; dogru -> SIGTERM
sent.clear()
pid, ct = target.split(":")
assert tm.kill_stalled(f"{pid}:{float(ct) + 50}") is False and victim.poll() is None
assert sent[0][0] == "Süreç bulunamadı"
assert tm.kill_stalled("sacma") is False and tm.kill_stalled("1:0") is False
tm.settings["protect"] = ["sleep 31500"]
assert tm.kill_stalled(target) is False and victim.poll() is None
tm.settings["protect"] = []
assert tm.kill_stalled(target) is True
time.sleep(0.4)
assert victim.poll() == -signal.SIGTERM
assert tm.kill_stalled(target) is False, "oldukten sonra ayni hedef tekrar vurulmamali"
assert tm.kill_stalled("1:%s" % m.psutil.Process(1).create_time()) is False, "kok sureç (PID 1) baska kullanicinin"

# suclu yoksa dugme yok
tm.stall_note_t = -1e9
tm.stall_ticks = 0
tm.last_procs = [row(9, "gnome-shell", cpu=99)]
sent.clear()
for _ in range(m.STALL_TICKS):
    tm.stall_step(5000.0, 0, 70, 0)
t2 = [s for s in sent if s[0] != "event"][0]
assert "Bellek" in t2[0] and t2[2] == []

# disk uyarisi: esik 50 -> bildirim + eylem; ayni disk icin bekleme suresi; kapali ise sessiz
sent.clear()
tm.set_pref("notify_disk_pct", 50)
tm.disk_check_t = 0
tm.disk_note_t = {}
real_full = tm_alerts.full_disks
tm_alerts.full_disks = lambda thr, paths=None: [("/", 93.0, 5 << 30)]
try:
    assert tm.disk_step(10000.0) == ["/"]
    n = [s for s in sent if s[0] != "event"][0]
    assert "Disk" in n[0] and "%93" in n[1] and n[2][0][1] == "app.open-tab" and n[2][0][2].get_int32() == m.TAB_CLEAN
    assert tm.disk_step(10000.0 + 61) == [], "6 saat dolmadan tekrar bildirmemeli"
    assert tm.disk_step(10000.0 + m.DISK_NOTE_COOLDOWN + 100) == ["/"]
    tm.set_pref("notify_disk", False)
    tm.disk_check_t = 0
    assert tm.disk_step(10 ** 6) == []
finally:
    tm_alerts.full_disks = real_full

# swap trafigi + zram metni
class SW:
    sin, sout = 1 << 20, 4 << 20
assert tm.swap_traffic(SW) == "–"
time.sleep(0.05)
SW.sin, SW.sout = 3 << 20, 4 << 20
assert "/" in tm.swap_traffic(SW)
assert tm.zram_text() in ("yok", "kullanılmıyor") or "×" in tm.zram_text()

# ayarlar: yeni kontroller ve korunanlar metin kutusu kaydedilir
tm.open_settings()
parts = tm.settings_win.test_parts
for key in ("notify_stall", "notify_disk", "notify_disk_pct", "protect"):
    assert key in parts, key
parts["protect"].get_buffer().set_text("LosevApi\nchrome")
m.spin(1.0)  # yazma gecikmesi (debounce) dolsun
import json
assert json.load(open(m.settings_path()))["protect"] == ["LosevApi", "chrome"]
tm.settings_win.destroy()

# elle sonlandirma onayinda korunan uyarisi
victim2 = subprocess.Popen(["sleep", "31501"])
tm.settings["protect"] = ["31501"]
tm.search.set_text(str(victim2.pid))
win = m.Gtk.OffscreenWindow(); win.add(tm.root); win.show_all()
tm.refresh_processes(); m.spin(0.5)
tm.tree.get_selection().select_all()
asked = {}
tm.confirm = lambda t, d, ok: asked.update(d=d) or False
tm.end_tasks(signal.SIGTERM)
assert "Korunanlar" in asked["d"], asked
victim2.kill(); victim2.wait()

# ---- proje bazli gecmis
h = tm_history.History(os.path.join(fresh_dir(), "h.db"))
base = 1_000_000
for i in range(10):  # 10 sn arayla; Proj ve Other
    ts = base + i * 10
    h.add_sample(ts, (90, 60, 0, 70 if i in (4, 5) else 5, 0, 0, 1, 0, 0),
                 [(1, "dotnet", "Proj", 50.0, 1 << 30, 0, 0), (2, "node", "Proj", 10.0, 1 << 29, 0, 0),
                  (3, "chrome", "", 5.0, 2 << 30, 1 << 20, 0)])
h.flush()
res = h.by_project(base, base + 100, 25)
byname = {r["project"]: r for r in res}
assert set(byname) == {"Proj", ""} and res[0]["project"] == "Proj"
p = byname["Proj"]
assert abs(p["avg_cpu"] - 60.0) < 1e-6 and p["max_cpu"] == 60.0 and p["max_mem"] == (1 << 30) + (1 << 29)
assert p["procs"] == 2 and p["seen"] == 10 and p["stalls"] == 1 and byname[""]["stalls"] == 1
assert h.by_project(base + 5000, base + 6000) == []
h.close()
# bildirim dugmelerinin baglandigi uygulama eylemleri
app = m.App(app_id=m.APP_ID + ".Test")
assert app.lookup_action("kill-stall") is not None and app.lookup_action("open-tab") is not None
print("TEST TAMAM")
