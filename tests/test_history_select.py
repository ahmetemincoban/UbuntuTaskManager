"""Gecmis grafiginde fareyle aralik secme: tablolar ve grafik yalniz secilen aralia daralir."""
import time
from types import SimpleNamespace

from common import SCR, load_app  # noqa: F401

m = load_app()
Gtk, Gdk = m.Gtk, m.Gdk

tm = m.TaskManager()
tm.notify = lambda *a, **k: None
win = Gtk.OffscreenWindow(); win.set_size_request(1200, 760); win.add(tm.root); win.show_all()
m.spin(2)

# 10 dakikalik sahte veri: 20..30. ornekler arasinda takilma (projA), digerlerinde projB
h = tm.history; now = time.time()
for i in range(60):
    ts = now - 600 + i * 10
    stall = 20 <= i < 30
    h.add_sample(ts, (80 if stall else 10, 50, 0, 60 if stall else 2, 0, 0, 1, 0, 0),
                 [(111, "dotnet", "projA" if stall else "projB", 90.0, 1 << 30, 0, 0)])
h.flush()
tm.root.set_current_page(m.TAB_HISTORY); m.spin(1.5)
tm.hist_range.set_active(1); m.spin(1)  # Son 1 saat
g = tm.hist_graph
assert g.data and g.data["rows"] and tm.hist_sel is None
assert not tm.hist_clear.get_visible()
all_stalls = len(tm.hist_stalls)
assert all_stalls == 1
assert {"projA", "projB"} <= {r[0] for r in tm.hist_projs}  # uygulamanin kendi kaydettigi gercek surecler de olabilir

L, W = g.L, g.size[0]
pw = W - L - g.R


def x_of(ts):
    return L + (ts - g.data["since"]) / (g.data["until"] - g.data["since"]) * pw


def press(x, button=1, typ=Gdk.EventType.BUTTON_PRESS):
    g.on_press(None, SimpleNamespace(x=x, button=button, type=typ))


def move(x):
    g.on_motion(None, SimpleNamespace(x=x))


def release(x, button=1):
    g.on_release(None, SimpleNamespace(x=x, button=button))


# tiklama (cok kisa surukleme) secim yapmaz
press(300); release(302); m.spin(0.5)
assert tm.hist_sel is None

# takilmadan ONCEki aralik: yalniz projB gorunmeli
t0, t1 = now - 590, now - 410
press(x_of(t0)); move(x_of((t0 + t1) / 2)); m.spin(0.3)
assert g.drag is not None  # surukleme suruyor
move(x_of(t1)); release(x_of(t1)); m.spin(1.5)
assert tm.hist_sel is not None and abs(tm.hist_sel[0] - t0) < 15 and abs(tm.hist_sel[1] - t1) < 15, tm.hist_sel
assert tm.hist_clear.get_visible()
names = {r[0] for r in tm.hist_projs}
assert "projB" in names and "projA" not in names, names
assert len(tm.hist_stalls) == 0
assert g.data["since"] == tm.hist_sel[0] and g.data["until"] == tm.hist_sel[1]
assert "Seçili aralık" in tm.hist_hint.get_label()
print("select OK:", tm.hist_hint.get_label())

# sagdan sola surukleme ve kenar disina tasma da calisir; takilmayi kapsayan aralik
tm.clear_hist_select(); m.spin(1)
assert tm.hist_sel is None and not tm.hist_clear.get_visible() and len(tm.hist_stalls) == 1
press(x_of(now - 300)); release(W + 500); m.spin(1.5)  # saga tasan surukleme plot kenarina kirpilir
assert tm.hist_sel and tm.hist_sel[1] <= int(g.data["until"]) + 1
press(x_of(now - 300) + 80); release(x_of(now - 300)); m.spin(1.5)  # ayni grafikte, soldan saga degilse de
assert tm.hist_sel is not None

# sag tik ve cift tik secimi kaldirir
press(100, button=3); m.spin(1)
assert tm.hist_sel is None
press(x_of(now - 500)); release(x_of(now - 300)); m.spin(1.5); assert tm.hist_sel
press(100, typ=Gdk.EventType._2BUTTON_PRESS); m.spin(1)
assert tm.hist_sel is None

# hazir aralik secilince grafik secimi gecersiz olur
press(x_of(now - 500)); release(x_of(now - 300)); m.spin(1.5); assert tm.hist_sel
tm.hist_range.set_active(2); m.spin(1)
assert tm.hist_sel is None
print("TEST history select OK")
