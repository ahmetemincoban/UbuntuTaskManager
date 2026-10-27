"""Gelistirme profilleri: kurallar, ozel profil dosyasi, git-ignore korumasi, Ayarlar ile secim ve kalicilik."""
from common import fresh_dir  # noqa: F401  (ilk import ortami hazirlar)
import json
import os
import subprocess

import tm_profiles
from common import load_app

m = load_app()

# ---- kurallar
dn = tm_profiles.Active(["dotnet"])
assert dn.is_dev("dotnet run --project X", "dotnet") and not dn.is_dev("node server.js", "node")
assert dn.idle_label("dotnet /x/MSBuild.dll /nodeReuse:true") == "MSBuild node"
assert dn.idle_label("dotnet /x/VBCSCompiler.dll") == "VBCSCompiler"
assert dn.idle_label("dotnet MSBuild.dll -m") is None
assert dn.clean_names == {"bin", "obj"} and not dn.needs_ignore("bin")
assert dn.shutdowns == [(".NET", ["dotnet", "build-server", "shutdown"])]

ng = tm_profiles.Active(["angular", "react"])
assert ng.is_dev("node /x/node_modules/.bin/ng serve", "node")
assert ng.needs_ignore("dist") and ng.needs_ignore("build") and not ng.needs_ignore("node_modules")
assert ng.match_clean("node_modules", {"package.json"}) == "node_modules"
assert ng.match_clean("node_modules", {"readme.md"}) is None
assert tm_profiles.Active([]).proc_rx is None and not tm_profiles.Active([]).is_dev("dotnet", "dotnet")
assert tm_profiles.Active(["yok-boyle-profil"]).ids == []

# ---- ozel profil dosyasi (bozuk girdiler yok sayilir)
cfg = tm_profiles.custom_path()
os.makedirs(os.path.dirname(cfg), exist_ok=True)
json.dump({
    "godot": {"name": "Godot", "desc": "oyun", "proc": "godot", "idle": [["Sunucu", "godot.*--headless"]],
              "clean": [[".godot", ["project.godot"], "Godot önbelleği", False], ["../etc", [], "kotu"]],
              "shutdown": ["echo", "kapat"]},
    "bozuk": {"proc": "x"},
    "dotnet": {"name": "Ezilmemeli"},
    "regexli": {"name": "R", "proc": "(["},
}, open(cfg, "w"))
profs = tm_profiles.load_profiles()
assert "godot" in profs and profs["godot"]["custom"] and "bozuk" not in profs
assert profs["dotnet"]["name"] == ".NET", "yerlesik profil ezilmemeli"
assert profs["regexli"]["proc"] == r"(?!x)x", "gecersiz regex yok sayilmali"
assert [c[0] for c in profs["godot"]["clean"]] == [".godot"], "'/' iceren klasor adi reddedilmeli"
g = tm_profiles.Active(["godot"], profs)
assert g.is_dev("godot --editor") and g.idle_label("godot x --headless") == "Sunucu"
open(cfg, "w").write("{bozuk json")
assert set(tm_profiles.load_profiles()) == set(tm_profiles.BUILTIN)
os.remove(cfg)

# ---- tarama: genel adlar (build/dist) yalnizca .gitignore'dayken
root = fresh_dir()
web = os.path.join(root, "web")
os.makedirs(os.path.join(web, "build"))
os.makedirs(os.path.join(web, "dist"))
os.makedirs(os.path.join(web, "node_modules", "pkg", "node_modules"))
open(os.path.join(web, "package.json"), "w").write("{}")
open(os.path.join(web, "build", "a.js"), "w").write("x")
open(os.path.join(web, "dist", "a.js"), "w").write("x")
subprocess.run(["git", "init", "-q", web], check=True)
open(os.path.join(web, ".gitignore"), "w").write("dist/\nnode_modules/\n")
act = tm_profiles.Active(["react"])
found = {os.path.relpath(p, web) for p, _a, _k in m.scan_clean([root], act)}
print("react:", sorted(found))
assert found == {"dist", "node_modules"}, "build .gitignore'da degil -> listelenmemeli; ic node_modules'e inilmemeli"
assert m.safe_delete(web + "/build", [root], act)[0] is False
assert m.safe_delete(web + "/dist", [root], act)[0] is True and not os.path.exists(web + "/dist")
assert m.scan_clean([root], tm_profiles.Active(["dotnet"])) == [], "dotnet profili node_modules'e dokunmamali"

# sanal ortam icine girilmez
venv = os.path.join(root, "py", ".venv")
os.makedirs(os.path.join(venv, "lib", "node_modules"))
open(os.path.join(venv, "pyvenv.cfg"), "w").write("")
open(os.path.join(root, "py", "package.json"), "w").write("{}")
assert not [p for p, _a, _k in m.scan_clean([root], act) if ".venv" in p]

# ---- uygulama: secim, kalicilik, ilk acilis
tm = m.TaskManager()
assert tm.first_run_profiles and tm.profile_ids() == ["dotnet"]
tm.apply_profiles(["angular", "dotnet"], save=False)
assert m.ACTIVE.ids == ["angular", "dotnet"]
assert "Angular" in tm.btn_dev.get_tooltip_text() and tm.btn_idle.get_sensitive()
tm.apply_profiles(["python"], save=False)
assert not tm.btn_idle.get_sensitive(), "python profilinde bosta kapatilacak tur yok"
tm.apply_profiles(["react"], save=True)
assert json.load(open(m.settings_path()))["profiles"] == ["react"]

# Ayarlar penceresinden secim
tm.open_settings()
boxes = tm.settings_win.test_parts["profiles"]
assert boxes["react"].get_active() and not boxes["dotnet"].get_active()
boxes["dotnet"].set_active(True)
assert m.ACTIVE.ids == ["dotnet", "react"] or set(m.ACTIVE.ids) == {"dotnet", "react"}
boxes["react"].set_active(False)
boxes["dotnet"].set_active(False)
assert m.ACTIVE.ids == []
assert tm.shutdown_box is not None
boxes["dotnet"].set_active(True)
assert [b.get_label() for b in tm.shutdown_box.get_children()] == ["dotnet build-server shutdown"]
tm.settings_win.destroy()

tm2 = m.TaskManager()
assert not tm2.first_run_profiles and m.ACTIVE.ids == ["dotnet"]

# bosta artik hedefleri profile gore, korunanlar haric
rows = [dict(pid=1, uid=m.ME, cpu_raw=0.0, cmd="dotnet /x/MSBuild.dll /nodeReuse:true", name="dotnet", rss=5),
        dict(pid=2, uid=m.ME, cpu_raw=50.0, cmd="dotnet /x/MSBuild.dll /nodeReuse:true", name="dotnet", rss=5),
        dict(pid=3, uid=m.ME + 1, cpu_raw=0.0, cmd="dotnet /x/VBCSCompiler.dll", name="dotnet", rss=5),
        dict(pid=4, uid=m.ME, cpu_raw=0.0, cmd="dotnet /x/VBCSCompiler.dll", name="dotnet", rss=5)]
tm2.last_procs = rows
tm2.proc_project = {}
assert [r["pid"] for r in tm2.cleanup_targets()] == [1, 4]
tm2.settings["protect"] = ["vbcscompiler"]
assert [r["pid"] for r in tm2.cleanup_targets()] == [1]
tm2.apply_profiles(["python"], save=False)
assert tm2.cleanup_targets() == []
print("TEST TAMAM")
