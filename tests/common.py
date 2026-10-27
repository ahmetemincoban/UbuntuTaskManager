"""Test ortak kurulumu: kullanici ayarlarina / gecmisine dokunmamak icin XDG dizinleri gecici klasore yonlendirilir.

Her test dosyasi tek basina calisir:  python3 tests/test_processes.py   (hepsi: python3 tests/run_all.py)
Gtk testleri ekran gerektirir (Gtk.OffscreenWindow kullanir ama bir X/Wayland oturumu olmali).
"""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCR = tempfile.mkdtemp(prefix="tskmngr-test-")  # gecici is klasoru; testler icinde proje / dosya olusturur
os.environ["SCR"] = SCR
os.environ["XDG_CONFIG_HOME"] = os.path.join(SCR, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(SCR, "data")
atexit.register(shutil.rmtree, SCR, True)

sys.path.insert(0, ROOT)
sys.argv = ["tskmngr-test"]


def load_app():
    spec = importlib.util.spec_from_file_location("tg", os.path.join(ROOT, "taskmgr_gui.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_dir():
    return tempfile.mkdtemp(dir=SCR)
