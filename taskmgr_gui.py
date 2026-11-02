#!/usr/bin/python3
"""Gorev Yoneticisi - Windows Task Manager benzeri GTK3 penceresi.

Calistirma:  tskmngr   (ya da ./taskmgr_gui.py)
Gereksinim:  sistem python3 + python3-gi (GTK3) + psutil; Docker sekmesi icin docker CLI

Ayarlar (sutun genisligi/gorunurlugu, siralama, mod, pencere boyutu, gecmis suresi...):
    ~/.config/tskmngr/settings.json
Gecmis kaydi (varsayilan 14 gun, Ayarlar'dan degistirilir; eskileri otomatik silinir):
    ~/.local/share/tskmngr/history.db
"""
import json
import math
import os
import pwd
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

import psutil  # noqa: E402

# tskmngr bir symlink: kardes modulleri (tm_*.py) gercek dosyanin yanindan al
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import tm_alerts  # noqa: E402
import tm_i18n  # noqa: E402
import tm_profiles  # noqa: E402
import tm_services  # noqa: E402
from tm_i18n import _  # noqa: E402
from tm_history import DEFAULT_DAYS, SYS_EVERY, History, Recorder, psi_some10, read_psi  # noqa: E402
from tm_procfs import HelperError, PrivHelper, find_owners, listening_sockets, read_stats  # noqa: E402

VERSION = "1.2.0"
APP_ID = "tr.teginsoft.TaskMgr"
NCPU = psutil.cpu_count() or 1
ME = os.getuid()
HOME = os.path.expanduser("~")
HISTORY = 60  # grafikte saniye
(TAB_PROCESSES, TAB_PERFORMANCE, TAB_HISTORY, TAB_DOCKER, TAB_PROJECTS, TAB_PORTS, TAB_CLEAN,
 TAB_SERVICES) = range(8)
OLD_TABS = [0, 1, 3, 4, 5, 6]  # ayar dosyasi version 1: sekme sirasi (Gecmis/Servisler yoktu)
STUCK_SECS = 6  # disk beklemesinde (D) bu kadar saniye kalan surec 'takili' sayilir

ACTIVE = tm_profiles.Active(tm_profiles.DEFAULT_PROFILES)  # secili gelistirme profilleri (TaskManager gunceller)
MSBUILD_NODE_RX = re.compile(r"MSBuild\.dll.*nodeReuse:true")
PORT_QUERY_RX = re.compile(r"^(?::|port:?\s*)(\d{1,5})$", re.I)
PROJECT_QUERY_RX = re.compile(r"^(?:proje|" + re.escape(_("proje")) + r"):(.*)$", re.I)
PROJECT_MARKERS = {".sln", ".csproj", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
                   "build.gradle", "composer.json", "Gemfile", "Makefile", "CMakeLists.txt"}
STATUS_TR = {"running": _("çalışıyor"), "sleeping": _("uyuyor"), "disk-sleep": _("disk bekliyor (D)"),
             "stopped": _("durduruldu"), "zombie": _("zombi"), "idle": _("boşta"), "tracing-stop": _("izleniyor"),
             "dead": _("ölü"), "wake-kill": _("uyanıyor"), "waking": _("uyanıyor"), "parked": _("park")}

(COL_PID, COL_NAME, COL_CPU, COL_RSS, COL_THR, COL_USER, COL_STARTED, COL_CMD,
 COL_CWD, COL_STATUS, COL_PPID, COL_DEV, COL_SEARCH, COL_PORTS, COL_PORTNUMS, COL_PSS, COL_NICE,
 COL_IOR, COL_IOW) = range(19)
COL_TYPES = [int, str, float, GObject.TYPE_INT64, int, str, float, str, str, str, int, bool, str, str, str,
             GObject.TYPE_INT64, int, GObject.TYPE_INT64, GObject.TYPE_INT64]

(D_ID, D_NAME, D_PROJECT, D_IMAGE, D_STATE, D_STATUS, D_CPU, D_MEM, D_NET, D_BLOCK, D_PIDS,
 D_PORTS, D_SEARCH, D_LIMIT) = range(14)
D_TYPES = [str, str, str, str, str, str, float, GObject.TYPE_INT64, str, str, int, str, str, GObject.TYPE_INT64]

(P_ROOT, P_NAME, P_PATH, P_COUNT, P_CPU, P_MEM, P_DEV, P_BRANCH, P_DIRTY) = range(9)
P_TYPES = [str, str, str, int, float, GObject.TYPE_INT64, int, str, int]

(N_KEY, N_PROTO, N_PORT, N_ADDR, N_PID, N_NAME, N_USER, N_PROJECT, N_DOCKER, N_CMD, N_SEARCH) = range(11)
N_TYPES = [str, str, int, str, int, str, str, str, str, str, str]

(C_PATH, C_PROJECT, C_KIND, C_SIZE, C_SHOWN, C_GIT) = range(6)
C_TYPES = [str, str, str, GObject.TYPE_INT64, str, str]

(S_KEY, S_NAME, S_KIND, S_STATE, S_BOOT, S_MEM, S_DESC, S_SEARCH) = range(8)
S_TYPES = [str, str, str, str, str, GObject.TYPE_INT64, str, str]
SERVICE_STATES = {"active": (_("çalışıyor"), "#3fb950"), "activating": (_("başlıyor"), "#e8890c"),
                  "failed": (_("başarısız"), "#e5484d"), "inactive": (_("durdu"), None)}
BOOT_TR = {"enabled": _("açık"), "disabled": _("kapalı"), "static": _("otomatik"), "masked": _("maskeli"),
           "generated": _("otomatik"), "indirect": _("dolaylı"), "alias": _("takma ad")}
PROTECTED_SERVICES = ("dbus.service", "dbus-broker.service", "gnome-session", "gnome-shell")  # durdurulursa oturum kapanir

# kullanici tercihleri (Ayarlar penceresi). settings.json'da ayni anahtarlarla saklanir
PREFS = dict(log_enabled=True, log_days=DEFAULT_DAYS, stall_psi=25, bg_run=True, notify_mem=True, notify_mem_pct=90,
             auto_idle=False, auto_idle_min=10, auto_idle_mem=75, notify_stall=True, notify_disk=True,
             notify_disk_pct=90)
PREF_RANGE = dict(log_days=(1, 365), stall_psi=(5, 95), notify_mem_pct=(50, 99), auto_idle_min=(1, 240),
                  auto_idle_mem=(0, 99), notify_disk_pct=(50, 99))
MEM_NOTE_COOLDOWN = 600  # sn: bellek uyarisi en sik bu aralikla
STALL_TICKS = 5           # baski bu kadar saniye ust uste esigin ustunde kalirsa takilma bildirimi
STALL_NOTE_COOLDOWN = 300
DISK_CHECK_EVERY = 60
DISK_NOTE_COOLDOWN = 6 * 3600
HISTORY_RANGES = [(_("Son 15 dakika"), 900), (_("Son 1 saat"), 3600), (_("Son 6 saat"), 6 * 3600),
                  (_("Son 24 saat"), 86400), (_("Son 7 gün"), 7 * 86400), (_("Tümü"), 0)]
CAUSE_TR = {"cpu": _("CPU"), "mem": _("Bellek"), "io": _("Disk")}
DEFAULT_CLEAN_ROOTS = [os.path.join(HOME, "Desktop", "GitRepos")]

DOCKER_STATES = {
    "running": (_("çalışıyor"), "#3fb950"),
    "exited": (_("durdu"), None),
    "paused": (_("duraklatıldı"), "#e8890c"),
    "restarting": (_("yeniden başlıyor"), "#e8890c"),
    "created": (_("oluşturuldu"), None),
    "dead": (_("ölü"), "#e5484d"),
}

_user_cache = {}
_static = {}  # pid -> (create_time, raw_name, display_name, user, cmd, cwd)


# ---------------------------------------------------------------- ayarlar
def settings_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, "tskmngr", "settings.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(data):
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print("ayarlar kaydedilemedi:", e, file=sys.stderr)


# ---------------------------------------------------------------- yardimcilar
def user_name(uid):
    if uid not in _user_cache:
        try:
            _user_cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _user_cache[uid] = str(uid)
    return _user_cache[uid]


def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


_SIZE_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
               "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text):
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_UNITS.get(m.group(2).lower(), 1))


def fmt_rate(n):
    return fmt_bytes(n) + "/s"


def fmt_ts(ts, date=True):
    return time.strftime("%d.%m %H:%M:%S" if date else "%H:%M:%S", time.localtime(ts))


def fmt_duration(secs):
    secs = int(secs)
    if secs < 60:
        return _("{secs} sn").format(secs=secs)
    m, s = divmod(secs, 60)
    if m < 60:
        return _("{m} dk {s:02d} sn").format(m=m, s=s)
    h, m = divmod(m, 60)
    return _("{h} sa {m:02d} dk").format(h=h, m=m)


def fmt_elapsed(started, now):
    s = max(0, int(now - started))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return _("{d}g {h}sa").format(d=d, h=h)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def shorten_home(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def friendly_name(name, parts):
    cmd = " ".join(parts)
    if MSBUILD_NODE_RX.search(cmd):
        return "dotnet · MSBuild node"
    if "VBCSCompiler" in cmd:
        return "dotnet · VBCSCompiler"
    if parts and os.path.basename(parts[0]) == "dotnet":
        for a in parts[1:]:
            if a.endswith(".dll"):
                return "dotnet " + os.path.basename(a)
        for a in parts[1:]:
            if not a.startswith("-"):
                return "dotnet " + os.path.basename(a)
        return "dotnet"
    return name


def collect(show_all):
    """Surec listesi (dict listesi)."""
    out = []
    live = set()
    for p in psutil.process_iter():
        try:
            pid = p.pid
            live.add(pid)
            with p.oneshot():
                uid = p.uids().real
                if not show_all and uid != ME:
                    continue
                ct = p.create_time()
                st = _static.get(pid)
                if st is None or st[0] != ct:
                    try:
                        parts = p.cmdline()
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        parts = []
                    name = p.name()
                    try:
                        cwd = shorten_home(p.cwd())
                    except (psutil.Error, OSError):
                        cwd = ""
                    st = (ct, name, friendly_name(name, parts), user_name(uid),
                          " ".join(parts) or f"[{name}]", cwd)
                    _static[pid] = st
                cpu_raw = p.cpu_percent(None)
                try:
                    nice = p.nice()
                except (psutil.Error, OSError):
                    nice = 0
                out.append({
                    "nice": nice,
                    "pid": pid, "ppid": p.ppid(), "uid": uid, "started": ct,
                    "raw_name": st[1], "name": st[2], "user": st[3], "cmd": st[4], "cwd": st[5],
                    "cpu_raw": cpu_raw, "cpu": cpu_raw / NCPU,
                    "rss": p.memory_info().rss, "threads": p.num_threads(), "status": p.status(),
                    "dev": ACTIVE.is_dev(st[4], st[1]),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    for pid in set(_static) - live:
        del _static[pid]
    return out


def listening_ports(socks):
    """pid -> {(port, 'tcp'|'udp')}"""
    out = {}
    for pid, port, proto, _ip in socks:
        if pid:
            out.setdefault(pid, set()).add((port, proto))
    return out


def addr_label(ip):
    if ip in ("0.0.0.0", "::"):
        return _("tümü")
    if ip.startswith("127.") or ip == "::1":
        return _("yerel")
    return ip


def ports_info(ports):
    """-> (gorunen metin, ' 80 443 ' seklinde arama metni)"""
    if not ports:
        return "", ""
    items = sorted(ports)
    txt = [str(p) if proto == "tcp" else f"{p}/udp" for p, proto in items]
    shown = ", ".join(txt[:5]) + (f" +{len(txt) - 5}" if len(txt) > 5 else "")
    return shown, " " + " ".join(str(p) for p, _ in items) + " "


_root_cache = {}


def _is_marker_dir(d):
    try:
        names = os.listdir(d)
    except OSError:
        return False
    return any(n in PROJECT_MARKERS or os.path.splitext(n)[1] in PROJECT_MARKERS for n in names)


def project_root(cwd):
    """Calisma dizininden yukari cikarak proje kokunu bulur: once en yakin .git, yoksa .sln/.csproj/package.json...
    Bulunamazsa (dizin HOME / kok ise ya da yolda .gizli klasor / node_modules varsa) '' doner. cwd '~' ile kisaltilmis olabilir."""
    if not cwd:
        return ""
    if cwd in _root_cache:
        return _root_cache[cwd]
    d = os.path.expanduser(cwd)
    rel = d[len(HOME):] if d.startswith(HOME + "/") else d
    if any(part.startswith(".") or part == "node_modules" for part in rel.split("/")):
        _root_cache[cwd] = ""  # ~/.vscode/extensions, node_modules gibi arac dizinleri proje sayilmaz
        return ""
    chain = []
    while d not in ("", "/", HOME):
        chain.append(d)
        d = os.path.dirname(d)
    root = next((c for c in chain if os.path.exists(os.path.join(c, ".git"))), "")
    if not root:
        root = next((c for c in chain if _is_marker_dir(c)), "")
    _root_cache[cwd] = root
    return root


def git_status(root):
    """-> (dal metni, degisen dosya sayisi); git deposu degilse ('', -1)"""
    try:
        res = subprocess.run(["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "-C", root, "status", "--porcelain=v1", "-b"],
                             capture_output=True, text=True, timeout=15, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return "", -1
    if res.returncode != 0:
        return "", -1
    lines = res.stdout.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    m = re.match(r"(?:No commits yet on |Initial commit on )?(.+?)(?:\.\.\.\S+)?(?: \[(.+)\])?$", head)
    branch = m.group(1) if m else head
    if branch.startswith("HEAD (no branch)"):
        branch = _("(ayrık HEAD)")
    if m and m.group(2):
        for word, arrow in (("ahead", "↑"), ("behind", "↓")):
            n = re.search(word + r" (\d+)", m.group(2))
            if n:
                branch += f" {arrow}{n.group(1)}"
    return branch, max(0, len(lines) - 1)


def tracked_dirs(paths):
    """Icinde git'in izledigi dosya olan klasorler -> set. Emin olunamazsa klasor 'izleniyor' sayilir (silme)."""
    by_root = {}
    for p in paths:
        root = project_root(os.path.dirname(p))
        if root and os.path.exists(os.path.join(root, ".git")):
            by_root.setdefault(root, []).append(p)
    hit = set()
    for root, group in by_root.items():
        for i in range(0, len(group), 100):
            chunk = group[i:i + 100]
            rels = [os.path.relpath(p, root) for p in chunk]
            try:
                res = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", root, "ls-files", "-z", "--", *[":(literal)" + r for r in rels]],
                                     capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                hit.update(chunk)
                continue
            if res.returncode != 0:
                hit.update(chunk)
                continue
            files = [f.decode(errors="replace") for f in res.stdout.split(b"\0") if f]
            for p, rel in zip(chunk, rels):
                if any(f.startswith(rel + "/") for f in files):
                    hit.add(p)
    return hit


class StatsWorker(threading.Thread):
    """Surec basina PSS ve disk G/C hizlarini arka planda toplar (okumalar ana dongüyü bloklamasin).
    helper (Yetkili okuma) varsa tum kullanicilarin surecleri ve dinleyen soketler root yardimcisindan gelir."""

    def __init__(self):
        super().__init__(daemon=True)
        self.helper = None
        self.error = None
        self.interval = 3.0
        self.pss, self.io, self.socks = {}, {}, None
        self.prev = {}
        self.wake = threading.Event()

    def sample(self):
        helper, raw, socks = self.helper, None, None
        t = time.monotonic()
        if helper is not None:
            try:
                raw = {int(k): v for k, v in helper.request("stats", timeout=20).items()}
                socks = [tuple(x) for x in helper.request("sockets", timeout=20)]
            except HelperError as e:
                if self.helper is helper:  # kullanici kapattiysa hata sayma
                    self.error = str(e)
                    self.helper = None
                helper.stop()
                raw = socks = None
        if raw is None:
            raw = read_stats()
        pss, io, prev = {}, {}, {}
        for pid, (p, rb, wb) in raw.items():
            if p >= 0:
                pss[pid] = p
            if rb >= 0:
                prev[pid] = (t, rb, wb)
                old = self.prev.get(pid)
                if old and t > old[0]:
                    dt = t - old[0]
                    io[pid] = (int(max(0, rb - old[1]) / dt), int(max(0, wb - old[2]) / dt))
        self.prev = prev
        self.pss, self.io, self.socks = pss, io, socks

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as e:  # noqa: BLE001 - arka plan okuyucusu hicbir hatada olmemeli
                self.error = self.error or _("okuma hatası: {e}").format(e=e)
            self.wake.wait(self.interval)
            self.wake.clear()


def process_details(pid):
    """Tek bir surecin anlik gorunumu -> {'general','files','conns','children','env': metin}"""
    p = psutil.Process(pid)

    def safe(fn):
        try:
            v = fn()
            return "–" if v in (None, "") else v
        except (psutil.Error, OSError):
            return _("okunamadı (yetki yok)")

    ct = safe(p.create_time)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ct)) if isinstance(ct, float) else ct
    ppid = safe(p.ppid)
    parent = safe(lambda: psutil.Process(ppid).name()) if isinstance(ppid, int) else "–"
    mem = safe(p.memory_full_info)
    aff = safe(p.cpu_affinity)
    cwd = safe(p.cwd)
    rows = [
        (_("Ad"), safe(p.name)), (_("PID"), pid), (_("Üst süreç"), f"{ppid} ({parent})"),
        (_("Kullanıcı"), safe(lambda: user_name(p.uids().real))),
        (_("Durum"), STATUS_TR.get(str(safe(p.status)), safe(p.status))),
        (_("Başlangıç"), started),
        (_("Çalışma süresi"), fmt_elapsed(ct, time.time()) if isinstance(ct, float) else "–"),
        (_("Çalışma dizini"), cwd), (_("Çalıştırılabilir"), safe(p.exe)),
        (_("Komut satırı"), safe(lambda: " ".join(p.cmdline()))),
        (_("Öncelik (nice)"), safe(p.nice)),
        (_("Çekirdek ataması"), _("tümü") if isinstance(aff, list) and len(aff) == NCPU else aff),
        (_("İş parçacığı"), safe(p.num_threads)),
        (_("Bellek (RSS)"), fmt_bytes(mem.rss) if not isinstance(mem, str) else safe(lambda: fmt_bytes(p.memory_info().rss))),
        (_("Bellek (PSS)"), fmt_bytes(mem.pss) if not isinstance(mem, str) else mem),
        (_("Özel bellek (USS)"), fmt_bytes(mem.uss) if not isinstance(mem, str) else mem),
        (_("Açık tanımlayıcı sayısı"), safe(p.num_fds)),
        (_("Proje"), shorten_home(project_root(cwd)) or "–" if isinstance(cwd, str) else "–"),
    ]
    width = max(len(k) for k, _v in rows)
    d = {"general": "\n".join(f"{k.ljust(width)} : {v}" for k, v in rows)}

    def lines(fn, fmt, empty):
        try:
            out = [fmt(x) for x in fn()]
        except (psutil.Error, OSError) as e:
            return _("okunamadı: {name} (başka kullanıcının süreci olabilir)").format(name=type(e).__name__)
        return "\n".join(out) if out else empty

    d["files"] = lines(p.open_files, lambda f: f.path, _("Açık normal dosya yok."))
    conn_fn = getattr(p, "net_connections", None) or p.connections

    def fmt_conn(c):
        proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-"
        remote = f"→ {c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
        return f"{proto}  {local:<28} {remote:<30} {c.status}"

    d["conns"] = lines(lambda: conn_fn(kind="inet"), fmt_conn, _("Ağ bağlantısı yok."))
    d["children"] = lines(lambda: p.children(recursive=True), lambda c: f"{c.pid:>8}  {c.name()}",
                          _("Alt süreç yok."))
    d["env"] = lines(lambda: sorted(p.environ().items()), lambda kv: f"{kv[0]}={kv[1]}", _("Ortam değişkeni yok."))
    return d


def docker_port_map():
    """Yayinlanan host portu -> konteyner adi. docker yoksa/erisilemezse None."""
    out, err = _docker_run(["ps", "--format", "{{.Names}}\t{{.Ports}}"], 6)
    if err:
        return None
    mapping = {}
    for line in out.splitlines():
        name, _t, ports = line.partition("\t")
        for m in re.finditer(r"(?:\d+\.\d+\.\d+\.\d+|\[::\]):(\d+)(?:-(\d+))?->", ports):
            lo = int(m.group(1))
            hi = min(int(m.group(2) or lo), lo + 1000)
            for port in range(lo, hi + 1):
                mapping[port] = name
    return mapping


def git_ignored(path):
    """path git tarafindan yok sayiliyor mu? (git deposu degilse False)"""
    try:
        res = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", os.path.dirname(path), "check-ignore", "-q", "--", os.path.basename(path)],
                             capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


def scan_clean(roots, active=None):
    """roots altinda yeniden uretilebilir klasorler (secili profillerin kurallarina gore). -> [(yol, proje, tur)]"""
    active = active or ACTIVE
    names = active.clean_names
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        for dirpath, dirnames, filenames in os.walk(root):
            if "pyvenv.cfg" in filenames:  # sanal ortam: icine girme
                dirnames[:] = []
                continue
            depth = dirpath[len(root):].count(os.sep)
            has = set(filenames)
            keep = []
            for dn in dirnames:
                full = os.path.join(dirpath, dn)
                if os.path.islink(full):
                    continue
                kind = active.match_clean(dn, has) if dn in names else None
                if kind and active.needs_ignore(dn) and not git_ignored(full):
                    kind = None  # 'build' / 'dist' gibi genel adlar: yalnizca .gitignore'daysa
                    continue
                if kind:
                    proj = project_root(full)
                    found.append((full, os.path.basename(proj) if proj else os.path.basename(dirpath), kind))
                elif dn.startswith(".") or dn == "node_modules":
                    continue
                else:
                    keep.append(dn)
            dirnames[:] = keep if depth < 6 else []
    return found


def dir_sizes(paths):
    """du ile diskte kaplanan alan (bayt). -> {yol: bayt}"""
    sizes = {}
    for i in range(0, len(paths), 40):
        chunk = paths[i:i + 40]
        try:
            res = subprocess.run(["du", "-sB1", "--", *chunk], capture_output=True, text=True, timeout=900,
                                 errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in res.stdout.splitlines():
            size, _t, p = line.partition("\t")
            if size.isdigit():
                sizes[p] = int(size)
    return sizes


def busy_dirs(paths):
    """Calisma dizini bu klasorlerden birinin icinde olan surecler -> {yol: [(pid, ad)]}"""
    busy = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        for p in paths:
            if cwd == p or cwd.startswith(p + "/"):
                try:
                    with open(f"/proc/{entry}/comm") as f:
                        name = f.read().strip()
                except OSError:
                    name = "?"
                busy.setdefault(p, []).append((int(entry), name))
    return busy


def safe_delete(path, roots, active=None):
    """Yalnizca taranan klasorlerin altinda, secili profillerin temizlik adlarindan biri silinir. -> (ok, hata)"""
    active = active or ACTIVE
    name = os.path.basename(path)
    if os.path.islink(path) or name not in active.clean_names:
        return False, _("güvenlik: beklenmeyen klasör")
    real = os.path.realpath(path)
    if not any(real.startswith(os.path.realpath(os.path.expanduser(r)).rstrip("/") + "/") for r in roots):
        return False, _("taranan klasörlerin dışında")
    if active.needs_ignore(name) and not git_ignored(real):
        return False, _("git tarafından yok sayılmıyor (.gitignore'da değil)")
    if tracked_dirs([real]):
        return False, _("git'te izlenen dosyalar içeriyor")
    try:
        shutil.rmtree(real)
    except OSError as e:
        return False, str(e)
    return True, ""


def docker_df():
    """docker system df -> ([satirlar], hata)"""
    out, err = _docker_run(["system", "df", "--format", "{{json .}}"], 40)
    if err:
        return [], err
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows, None


def kill_pids(pids, sig, with_children):
    """Sinyal gonder -> (basarili, hata listesi)."""
    targets = {}
    protected = {os.getpid(), 1}
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            targets[pid] = proc
            if with_children:
                for ch in proc.children(recursive=True):
                    targets[ch.pid] = ch
        except psutil.NoSuchProcess:
            pass
    ok, errs = 0, []
    for pid, proc in sorted(targets.items(), key=lambda kv: -kv[0]):
        if pid in protected:
            errs.append(_("{pid}: korumali").format(pid=pid))
            continue
        try:
            proc.send_signal(sig)
            ok += 1
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            errs.append(_("{pid}: yetki yok").format(pid=pid))
    return ok, errs


def nice_ceil(v, minimum):
    v = max(v, minimum)
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


# ---------------------------------------------------------------- docker
def _docker_run(args, timeout):
    """-> (stdout, hata metni | None)"""
    try:
        res = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return "", _("docker komutu bulunamadı")
    except subprocess.TimeoutExpired:
        return "", _("docker cevap vermedi (zaman aşımı)")
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        return res.stdout, (lines[-1] if lines else _("docker hata verdi"))
    return res.stdout, None


def _docker_ports_text(raw):
    out = []
    for p in (raw or "").split(", "):
        p = p.strip()
        if not p or p.startswith("[::]"):
            continue
        if "->" in p:
            p = re.sub(r"^\d+\.\d+\.\d+\.\d+:", "", p).replace("->", "→")
            p = re.sub(r"/tcp$", "", p)
        else:
            p = "(" + re.sub(r"/tcp$", "", p) + ")"
        if p not in out:
            out.append(p)
    return ", ".join(out)


def docker_fetch(all_containers, on_partial):
    """Konteyner listesi + istatistik. on_partial(rows) liste gelince, bitince tekrar cagrilir.
    -> hata metni | None"""
    out, err = _docker_run(["ps", "--format", "{{json .}}"] + (["-a"] if all_containers else []), 15)
    if err:
        return err
    rows = {}
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        proj = re.search(r"com\.docker\.compose\.project=([^,]+)", d.get("Labels", ""))
        rows[d["ID"]] = {
            "id": d["ID"], "name": d.get("Names", ""), "project": proj.group(1) if proj else "",
            "image": d.get("Image", ""), "state": d.get("State", ""), "status": d.get("Status", ""),
            "ports": _docker_ports_text(d.get("Ports", "")),
            "cpu": None, "mem": None, "net": None, "block": None, "pids": None, "limit": None,
        }
    on_partial(rows)
    if not any(r["state"] == "running" for r in rows.values()):
        return None
    out, err = _docker_run(["stats", "--no-stream", "--format", "{{json .}}"], 25)
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        r = rows.get(d.get("ID"))
        if r:
            r["cpu"] = float(d.get("CPUPerc", "0").rstrip("%") or 0)
            usage = d.get("MemUsage", "").split("/")
            r["mem"] = parse_size(usage[0])
            lim = parse_size(usage[1]) if len(usage) > 1 else 0
            r["limit"] = 0 if lim >= 0.95 * psutil.virtual_memory().total else lim  # 0 = sinirsiz
            r["net"], r["block"] = d.get("NetIO", ""), d.get("BlockIO", "")
            r["pids"] = int(d.get("PIDs", 0) or 0)
    on_partial(rows)
    return err


def explain_port(port):
    """Pid'i bilinmeyen bir port icin aciklama (docker konteyneri / root sureci)."""
    out, err = _docker_run(["ps", "--format", "{{.Names}}\t{{.Ports}}"], 4)
    if not err:
        rx = re.compile(r"(?:^|[\s,:\]])%d->" % port)
        names = [line.split("\t")[0] for line in out.splitlines() if rx.search(line)]
        if names:
            return _("Port {port}: docker konteyneri '{items}' yayınlıyor (Docker sekmesine bak).").format(port=port, items=', '.join(names))
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                return _("Port {port} dinleniyor ama süreç bilgisi yok (root'a ait bir süreç olabilir: sudo ss "
                         "-ltnp).").format(port=port)
    except (psutil.AccessDenied, OSError):
        pass
    return _("Port {port}'u dinleyen bir süreç bulunamadı.").format(port=port)


# ------------------------------------------------------------------- grafikler
# Cizim SVG -> GdkPixbuf -> Gtk.Image ile yapilir (python3-gi-cairo gerektirmez).
def hexcolor(rgb):
    return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)


class SvgCanvas(Gtk.ScrolledWindow):
    """Boyutu ebeveynden alan, icerigi SVG olarak ureten taban sinif."""

    def __init__(self, min_height):
        # EXTERNAL: kaydirma cubugu yok ama cocugun boyutu pencereyi buyutmez (NEVER buyutur -> sonsuz dongu)
        super().__init__(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.set_size_request(80, min_height)
        self.set_shadow_type(Gtk.ShadowType.NONE)
        self.img = Gtk.Image()
        self.img.set_halign(Gtk.Align.START)
        self.img.set_valign(Gtk.Align.START)
        self.add(self.img)
        self.size = (0, 0)
        self.connect("size-allocate", self.on_size)
        self.connect("map", lambda _w: self.refresh())

    def on_size(self, _w, alloc):
        size = (alloc.width, alloc.height)
        if size != self.size and alloc.width > 20 and alloc.height > 20:
            self.size = size
            GLib.idle_add(self.refresh)

    def fg(self):
        c = self.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        return hexcolor((c.red, c.green, c.blue))

    def refresh(self):
        W, H = self.size
        if W < 20 or H < 20 or not self.get_mapped():
            return False
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
               f'font-family="sans-serif">{self.body(W, H)}</svg>')
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg.encode())
        loader.close()
        self.img.set_from_pixbuf(loader.get_pixbuf())
        return False


class Graph(SvgCanvas):
    """Kayan zaman grafigi. colors: [(r,g,b)]; maxv None ise otomatik olcek."""

    def __init__(self, colors, maxv=100.0, min_scale=1.0, label_fmt=None):
        super().__init__(200)
        self.colors = colors
        self.maxv = maxv
        self.min_scale = min_scale
        self.label_fmt = label_fmt or (lambda v: f"{v:.0f}%")
        self.data = [deque([0.0] * HISTORY, maxlen=HISTORY) for _ in colors]

    def push(self, *values):
        for d, v in zip(self.data, values):
            d.append(v)
        self.refresh()

    def body(self, W, H):
        fg = self.fg()
        top = self.maxv or nice_ceil(max(max(d) for d in self.data), self.min_scale)
        out = []
        grid = "".join(f'M0 {round(H * i / 10) + 0.5}H{W}' for i in range(1, 10))
        grid += "".join(f'M{round(W * i / 12) + 0.5} 0V{H}' for i in range(1, 12))
        out.append(f'<path d="{grid}" stroke="{fg}" stroke-opacity="0.12" fill="none"/>')
        step = W / (HISTORY - 1)
        for d, color in zip(self.data, self.colors):
            pts = " ".join(f"{i * step:.1f},{H - min(1.0, v / top) * (H - 2) - 1:.1f}" for i, v in enumerate(d))
            col = hexcolor(color)
            out.append(f'<polygon points="0,{H} {pts} {W},{H}" fill="{col}" fill-opacity="0.22"/>')
            out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.6" '
                       f'stroke-linejoin="round"/>')
        out.append(f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" fill="none" stroke="{fg}" '
                   f'stroke-opacity="0.45"/>')
        txt = f'fill="{fg}" fill-opacity="0.7" font-size="11"'
        out.append(f'<text x="{W - 8}" y="15" text-anchor="end" {txt}>{self.label_fmt(top)}</text>')
        out.append(f'<text x="6" y="{H - 6}" {txt}>{HISTORY} saniye</text>')
        out.append(f'<text x="{W - 6}" y="{H - 6}" text-anchor="end" {txt}>0</text>')
        return "".join(out)


class CoreGrid(SvgCanvas):
    """Mantiksal islemci basina kucuk doluluk kutulari."""

    def __init__(self, color):
        super().__init__(38 * math.ceil(NCPU / 6))
        self.color = color
        self.values = [0.0] * NCPU

    def set_values(self, values):
        self.values = values
        self.refresh()

    def body(self, W, H):
        fg = self.fg()
        col = hexcolor(self.color)
        cols = min(NCPU, 6)
        gap, ch = 6, 30
        cw = (W - gap * (cols - 1)) / cols
        out = []
        for i, v in enumerate(self.values):
            x, y = (i % cols) * (cw + gap), (i // cols) * (ch + gap)
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" height="{ch}" fill="{fg}" fill-opacity="0.08"/>')
            out.append(f'<rect x="{x:.1f}" y="{y + ch * (1 - v / 100):.1f}" width="{cw:.1f}" '
                       f'height="{ch * v / 100:.1f}" fill="{col}" fill-opacity="0.55"/>')
            out.append(f'<rect x="{x + 0.5:.1f}" y="{y + 0.5}" width="{cw - 1:.1f}" height="{ch - 1}" fill="none" '
                       f'stroke="{fg}" stroke-opacity="0.45"/>')
            out.append(f'<text x="{x + 6:.1f}" y="{y + 19}" font-size="11" fill="{fg}" fill-opacity="0.9">'
                       f'{i}: {v:.0f}%</text>')
        return "".join(out)


class HistoryGraph(SvgCanvas):
    """Gecmis grafigi: CPU % (mavi), RAM % (mor), en yuksek baski (kirmizi alan), takilma bantlari."""

    L, R, T, B = 38, 12, 24, 22
    MIN_SELECT = 5  # px: bundan kisa surukleme tiklama sayilir

    def __init__(self, on_select=None, on_clear=None):
        super().__init__(190)
        self.data = None
        self.on_select = on_select
        self.on_clear = on_clear
        self.drag = None  # (baslangic x, simdiki x) - surukleme suruyorken
        self.dirty = False
        # SvgCanvas'in Viewport'u icindeki resmi olay yakalayan bir kutuya sar
        viewport = self.get_child()
        viewport.remove(self.img)
        box = Gtk.EventBox()
        box.set_visible_window(False)
        box.add(self.img)
        viewport.add(box)
        box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                       Gdk.EventMask.POINTER_MOTION_MASK)
        box.connect("button-press-event", self.on_press)
        box.connect("motion-notify-event", self.on_motion)
        box.connect("button-release-event", self.on_release)
        box.show_all()
        self.set_tooltip_text(_("Fareyle sürükleyerek bir zaman aralığı seç; tablolar yalnızca o aralığı gösterir. "
                              "Çift tık ya da sağ tık: seçimi kaldır."))

    def set_data(self, data):
        self.data = data
        self.drag = None
        self.refresh()

    def plot_x(self, x):
        return min(max(x, self.L), self.size[0] - self.R)

    def ts_at(self, x):
        d = self.data
        pw = self.size[0] - self.L - self.R
        return d["since"] + (self.plot_x(x) - self.L) / max(1, pw) * (max(d["until"], d["since"] + 1) - d["since"])

    def on_press(self, _w, ev):
        if ev.button == 3 or (ev.button == 1 and ev.type == Gdk.EventType._2BUTTON_PRESS):
            self.drag = None
            if self.on_clear:
                self.on_clear()
            return True
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS and self.data and self.data.get("rows"):
            self.drag = (self.plot_x(ev.x), self.plot_x(ev.x))
        return True

    def on_motion(self, _w, ev):
        if self.drag:
            self.drag = (self.drag[0], self.plot_x(ev.x))
            if not self.dirty:  # SVG'yi her hareket olayinda degil, bosta kalinca bir kez yeniden ciz
                self.dirty = True
                GLib.idle_add(self.redraw)
        return True

    def redraw(self):
        self.dirty = False
        return self.refresh()

    def on_release(self, _w, ev):
        drag, self.drag = self.drag, None
        if drag and ev.button == 1:
            a, b = sorted((drag[0], self.plot_x(ev.x)))
            if b - a >= self.MIN_SELECT and self.on_select:
                self.on_select(self.ts_at(a), self.ts_at(b))
                return True
        self.refresh()
        return True

    def body(self, W, H):
        fg = self.fg()
        L, R, T, B = self.L, self.R, self.T, self.B
        pw, ph = W - L - R, H - T - B
        txt = f'fill="{fg}" fill-opacity="0.7" font-size="11"'
        out = []
        for pct in (0, 25, 50, 75, 100):
            y = T + ph - pct / 100 * ph
            out.append(f'<path d="M{L} {y:.1f}H{W - R}" stroke="{fg}" stroke-opacity="{0.35 if pct in (0, 100) else 0.12}" '
                       f'fill="none"/><text x="{L - 5}" y="{y + 4:.1f}" text-anchor="end" {txt}>{pct}%</text>')
        d = self.data
        if not d or not d.get("rows"):
            out.append(f'<text x="{L + pw / 2:.0f}" y="{T + ph / 2:.0f}" text-anchor="middle" fill="{fg}" fill-opacity="0.7" font-size="14">'
                       f'{_("Bu aralıkta kayıt yok")}</text>')
            return "".join(out)
        since, until, bucket = d["since"], max(d["until"], d["since"] + 1), d["bucket"]
        span = until - since

        def X(ts):
            return L + (ts - since) / span * pw

        def Y(v):
            return T + ph - min(100.0, max(0.0, v)) / 100 * ph

        fmt = "%H:%M:%S" if span <= 900 else "%H:%M" if span <= 86400 else "%d.%m %H:%M"
        for i in range(5):
            ts = since + span * i / 4
            anchor = "start" if i == 0 else "end" if i == 4 else "middle"
            out.append(f'<text x="{X(ts):.1f}" y="{H - 6}" text-anchor="{anchor}" {txt}>'
                       f'{time.strftime(fmt, time.localtime(ts))}</text>')
        for a, b in d.get("stalls", []):
            x0, x1 = max(float(L), X(a)), min(float(W - R), X(b + SYS_EVERY))  # yakinlastirinca plot disina tasmasin
            if x1 < x0:
                continue
            out.append(f'<rect x="{x0:.1f}" y="{T}" width="{max(2.0, x1 - x0):.1f}" height="{ph}" fill="#e5484d" '
                       f'fill-opacity="0.13"/>')

        def runs(points):
            cur, last = [], None
            for ts, v in points:
                if last is not None and ts - last > 3 * bucket:
                    yield cur
                    cur = []
                cur.append((ts, v))
                last = ts
            if cur:
                yield cur

        def draw(points, color, area):
            for run in runs(points):
                if len(run) == 1:
                    out.append(f'<circle cx="{X(run[0][0]):.1f}" cy="{Y(run[0][1]):.1f}" r="2.5" fill="{color}"/>')
                    continue
                pts = " ".join(f"{X(ts):.1f},{Y(v):.1f}" for ts, v in run)
                if area:
                    out.append(f'<polygon points="{X(run[0][0]):.1f},{Y(0):.1f} {pts} {X(run[-1][0]):.1f},{Y(0):.1f}" '
                               f'fill="{color}" fill-opacity="0.28"/>')
                out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" '
                           f'stroke-linejoin="round"/>')

        rows = d["rows"]
        draw([(r[0], max(r[3] or 0, r[4] or 0, r[5] or 0)) for r in rows], "#e5484d", True)
        draw([(r[0], r[2]) for r in rows if r[2] is not None], "#9966d9", False)
        draw([(r[0], r[1]) for r in rows if r[1] is not None], "#338cd9", False)
        ty = Y(d["thr"])
        out.append(f'<path d="M{L} {ty:.1f}H{W - R}" stroke="#e5484d" stroke-opacity="0.6" stroke-dasharray="4 4" fill="none"/>')
        out.append(f'<rect x="{L + 0.5}" y="{T + 0.5}" width="{pw - 1}" height="{ph - 1}" fill="none" stroke="{fg}" '
                   f'stroke-opacity="0.35"/>')
        if self.drag and abs(self.drag[1] - self.drag[0]) > 0:
            a, b = sorted(self.drag)
            out.append(f'<rect x="{a:.1f}" y="{T}" width="{b - a:.1f}" height="{ph}" fill="#338cd9" fill-opacity="0.18" '
                       f'stroke="#338cd9" stroke-opacity="0.8"/>')
            t0, t1 = self.ts_at(a), self.ts_at(b)
            out.append(f'<text x="{W - R}" y="18" text-anchor="end" {txt}>{time.strftime("%H:%M:%S", time.localtime(t0))}'
                       f' – {time.strftime("%H:%M:%S", time.localtime(t1))}  ({fmt_duration(t1 - t0)})</text>')
        x = L
        for color, label in (("#338cd9", _("CPU %")), ("#9966d9", _("RAM %")), ("#e5484d", _("En yüksek baskı (PSI)"))):
            out.append(f'<rect x="{x}" y="9" width="10" height="10" fill="{color}"/>'
                       f'<text x="{x + 15}" y="18" {txt}>{label}</text>')
            x += 28 + 7 * len(label)
        return "".join(out)


def simple_table(cols):
    """cols: [(baslik, genislik, saga_hizali)] -> (ListStore, ScrolledWindow). Tum hucreler metin."""
    store = Gtk.ListStore(*[str] * len(cols))
    tv = Gtk.TreeView(model=store)
    tv.set_enable_search(False)
    for i, (title, width, right) in enumerate(cols):
        r = Gtk.CellRendererText(ellipsize=3)
        if right:
            r.set_property("xalign", 1.0)
        c = Gtk.TreeViewColumn(title, r, text=i)
        c.set_resizable(True)
        c.set_min_width(width)
        c.set_expand(i == len(cols) - 1)
        tv.append_column(c)
    sc = Gtk.ScrolledWindow()
    sc.add(tv)
    return store, sc


class StatBlock(Gtk.Grid):
    def __init__(self, items, cols=4):
        super().__init__(column_spacing=36, row_spacing=10)
        self.vals = {}
        for i, (key, caption) in enumerate(items):
            cap = Gtk.Label(label="", xalign=0)
            cap.set_markup(f"<span size='small' alpha='65%'>{caption}</span>")
            val = Gtk.Label(label="", xalign=0)
            val.set_markup("<span size='x-large'>–</span>")
            self.vals[key] = val
            self.attach(cap, (i % cols), (i // cols) * 2, 1, 1)
            self.attach(val, (i % cols), (i // cols) * 2 + 1, 1, 1)

    def set(self, key, text):
        self.vals[key].set_markup(f"<span size='x-large'>{GLib.markup_escape_text(text)}</span>")


def make_page(title, subtitle, graph, stats, extra=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for m in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{m}")(16)
    head = Gtk.Box(spacing=12)
    t = Gtk.Label(label="", xalign=0)
    t.set_markup(f"<span size='xx-large' weight='bold'>{title}</span>")
    s = Gtk.Label(label="", xalign=1)
    s.set_markup(subtitle)
    s.set_ellipsize(3)
    head.pack_start(t, False, False, 0)
    head.pack_end(s, True, True, 0)
    box.pack_start(head, False, False, 0)
    box.pack_start(graph, True, True, 0)
    if extra is not None:
        box.pack_start(extra, False, False, 0)
    box.pack_start(stats, False, False, 0)
    return box


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def heat(renderer, frac):
    if frac < 0.03:
        renderer.set_property("cell-background-set", False)
    else:
        renderer.set_property("cell-background-rgba", Gdk.RGBA(1.0, 0.62, 0.1, 0.10 + 0.55 * min(1.0, frac)))


# ------------------------------------------------------------------- sutunlar
def make_header(text, right=False):
    lbl = Gtk.Label(label=text)
    if right:
        lbl.set_justify(Gtk.Justification.RIGHT)
        lbl.set_xalign(1.0)
    lbl.show()
    return lbl


class ColumnSet:
    """TreeView sutunlarini kurar. Genislik/gorunurluk kaydi ve baslikta sag tik 'sutun sec' menusu saglar.

    spec: key, title, col (metin kolonu), sort, width, xalign, func, expand, visible, header, hideable
    """

    def __init__(self, tree, specs, saved=None):
        self.tree, self.specs, self.cols, self.widths = tree, specs, {}, {}
        saved = saved if isinstance(saved, dict) else {}
        for s in specs:
            sv = saved.get(s["key"]) if isinstance(saved.get(s["key"]), dict) else {}
            xalign = s.get("xalign", 0.0)
            r = Gtk.CellRendererText()
            r.set_property("xalign", xalign)
            if not xalign:
                r.set_property("ellipsize", 3)  # END
            c = Gtk.TreeViewColumn()
            c.set_widget(s.get("header") or make_header(s["title"], right=bool(xalign)))
            c.pack_start(r, True)
            if s.get("func"):
                c.set_cell_data_func(r, s["func"])
            else:
                c.add_attribute(r, "text", s["col"])
            c.set_sort_column_id(s["sort"])
            c.set_resizable(True)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            width = int(sv.get("w") or s["width"])
            self.widths[s["key"]] = width
            c.set_fixed_width(max(30, width))
            c.set_expand(s.get("expand", False))
            c.set_alignment(xalign)
            c.set_visible(bool(sv.get("v", s.get("visible", True))))
            tree.append_column(c)
            c.get_button().connect("button-press-event", self.on_header_press)
            self.cols[s["key"]] = c

    def on_header_press(self, _btn, ev):
        if ev.button != 3:
            return False
        menu = Gtk.Menu()
        for s in self.specs:
            item = Gtk.CheckMenuItem(label=s["title"])
            item.set_active(self.cols[s["key"]].get_visible())
            item.set_sensitive(s.get("hideable", True))
            item.connect("toggled", lambda i, col=self.cols[s["key"]]: col.set_visible(i.get_active()))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(ev)
        return True

    def to_dict(self):
        out = {}
        for s in self.specs:
            col = self.cols[s["key"]]
            w = col.get_width()
            if col.get_visible() and w >= 30:
                self.widths[s["key"]] = w
            out[s["key"]] = {"w": self.widths[s["key"]], "v": col.get_visible()}
        return out

    def sort_to_list(self, sort_model):
        res = sort_model.get_sort_column_id()  # PyGObject sürümüne göre (ok, id, order) ya da (id, order)
        cid, order = res[-2:]
        if len(res) == 3 and not res[0]:
            return None
        for s in self.specs:
            if s["sort"] == cid:
                return [s["key"], "desc" if order == Gtk.SortType.DESCENDING else "asc"]
        return None

    def apply_sort(self, sort_model, saved, default):
        for cand in (saved, default):
            if isinstance(cand, (list, tuple)) and len(cand) == 2:
                for s in self.specs:
                    if s["key"] == cand[0]:
                        sort_model.set_sort_column_id(
                            s["sort"], Gtk.SortType.DESCENDING if cand[1] == "desc" else Gtk.SortType.ASCENDING)
                        return


# ------------------------------------------------------------------ ana uygulama
class TaskManager:
    def __init__(self):
        self.settings = load_settings()
        st = self.settings
        self.win = None
        self.show_all_users = bool(st.get("all_users", False))
        self.profiles = tm_profiles.load_profiles()
        self.dev_only = bool(st.get("dev_only", st.get("dotnet_only", False)))
        self.tree_mode = bool(st.get("tree_mode", False))
        self.docker_all = bool(st.get("docker_all", True))
        self.paused = False
        self.query = ""
        self.port_query = None
        self.explain_src = None
        self.iters = {}
        self.collapsed = set()
        self.last_procs = []
        self.menu_open = False
        self.flash_text = ""
        self.flash_until = 0.0
        self.tick_n = 0
        self.last_t = time.monotonic()
        self.last_disk = psutil.disk_io_counters()
        self.last_net = psutil.net_io_counters()
        self.cpu_pct = 0.0
        self.mem_pct = 0.0
        self.docker_busy = False
        self.docker_iters = {}
        self.docker_query = ""
        self.dsince = {}      # pid -> disk beklemesine (D) girdigi an
        self.stuck = set()    # STUCK_SECS'ten uzun suredir D durumundaki pid'ler
        self.proc_project = {}  # pid -> proje kok dizini ('' = proje disi)
        self.proj_filter = None
        self.proj_iters = {}
        self.finder = None
        self.stats = StatsWorker()
        self.stats.start()
        self.app = None
        self.hidden = False       # pencere gizli (arka plan modu): arayuz guncellemeleri atlanir
        self.quitting = False
        self.priv_helper = None
        self.priv_starting = False
        self.priv_toggles = []
        self._priv_sync = False
        self.settings_win = None
        self.hist_info_label = None
        self.mem_note_t = -MEM_NOTE_COOLDOWN
        self.idle_since = {}      # pid -> bosta gorulmeye basladigi an (monotonic)
        self.prune_busy = False
        self.last_prune = 0.0
        self.hist_token = 0
        self.hist_sel = None  # (since, until) | None: grafikten secilen aralik
        self.git_cache = {}       # proje koku -> (zaman, dal, degisen dosya sayisi)
        self.git_busy = False
        self.clean_tracked = set()
        self.svc_iters = {}
        self.svc_query = ""
        self.svc_busy = False
        self.shutdown_box = None
        self.stall_ticks = 0
        self.stall_note_t = -STALL_NOTE_COOLDOWN
        self.disk_note_t = {}     # yol -> son bildirim (time.time)
        self.disk_check_t = 0.0
        self.last_swap = None     # (sin, sout, monotonic)
        self.first_run_profiles = "profiles" not in self.settings
        self.chooser = None
        self.apply_profiles(self.profile_ids(), save=False)
        try:
            self.history = History()
        except (sqlite3.Error, OSError) as e:
            print("gecmis veritabani acilamadi:", e, file=sys.stderr)
            self.history = None
        self.recorder = Recorder(self.history, self.pref("stall_psi")) if self.history else None
        self.last_socks = []
        self.docker_ports = {}
        self.docker_ports_t = 0.0
        self.docker_ports_busy = False
        self.ports_query = ""
        self.ports_iters = {}
        self.clean_roots = [r for r in (st.get("clean_roots") or []) if isinstance(r, str)] or list(DEFAULT_CLEAN_ROOTS)
        self.clean_busy = False
        self.clean_scanned = False
        self.clean_iters = {}
        self.details = {}

        psutil.cpu_percent(percpu=True)
        collect(False)  # cpu_percent icin isinma turu

        self.root = Gtk.Notebook()
        self.root.append_page(self.build_processes(), Gtk.Label(label=_("İşlemler")))
        self.root.append_page(self.build_performance(), Gtk.Label(label=_("Performans")))
        self.root.append_page(self.build_history(), Gtk.Label(label=_("Geçmiş")))
        self.root.append_page(self.build_docker(), Gtk.Label(label=_("Docker")))
        self.root.append_page(self.build_projects(), Gtk.Label(label=_("Projeler")))
        self.root.append_page(self.build_ports(), Gtk.Label(label=_("Portlar")))
        self.root.append_page(self.build_clean(), Gtk.Label(label=_("Temizlik")))
        self.root.append_page(self.build_services(), Gtk.Label(label=_("Servisler")))
        btn_settings = Gtk.Button(label=_("⚙ Ayarlar"))
        btn_settings.set_relief(Gtk.ReliefStyle.NONE)
        btn_settings.set_tooltip_text(_("Geçmiş kaydı, arka plan, bildirimler, otomatik temizlik (Ctrl+,)"))
        btn_settings.connect("clicked", lambda _b: self.open_settings())
        btn_settings.show()
        self.root.set_action_widget(btn_settings, Gtk.PackType.END)
        self.root.connect("switch-page", self.on_switch_page)
        self.update_profile_labels()
        self.refresh_processes()
        GLib.timeout_add(1000, self.tick)

    # -------------------------------------------------------------- tercihler
    def pref(self, key):
        """Gecerli tercih degeri (dosyadaki bozuk / aralik disi deger varsayilana ya da sinira cekilir)."""
        default = PREFS[key]
        v = self.settings.get(key, default)
        if isinstance(default, bool):
            return v if isinstance(v, bool) else default
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        lo, hi = PREF_RANGE[key]
        return int(min(hi, max(lo, v)))

    def set_pref(self, key, value):
        """Ayari hemen kaydeder (pencere kapanmadan da saklansin)."""
        self.settings[key] = value
        self.save_settings(self.win)

    # -------------------------------------------------------------- korunan surecler
    def protect_patterns(self):
        return tm_alerts.clean_patterns(self.settings.get("protect"))

    def is_protected(self, r):
        """r: surec satiri (name, cmd, pid). Kalip ad / komut / proje adi ya da yolunda gecerse korunur."""
        pats = self.protect_patterns()
        if not pats:
            return False
        return tm_alerts.is_protected(pats, r["name"], r["cmd"], self.proc_project.get(r["pid"]) or "")

    # -------------------------------------------------------------- profiller
    def profile_ids(self):
        v = self.settings.get("profiles")
        ids = [i for i in v if isinstance(i, str) and i in self.profiles] if isinstance(v, list) else None
        return ids if ids is not None else list(tm_profiles.DEFAULT_PROFILES)

    def apply_profiles(self, ids, save=True):
        """Profil secimini uygular: sureç filtresi, bosta artiklar, temizlik kurallari yeniden hesaplanir."""
        global ACTIVE
        ACTIVE = tm_profiles.Active(ids, self.profiles)
        self.settings["profiles"] = list(ACTIVE.ids)
        if not hasattr(self, "btn_dev"):
            return
        self.update_profile_labels()
        self.clean_scanned = False
        if self.root.get_current_page() == TAB_CLEAN:
            self.clean_scan()
        self.refresh_processes()
        self.fill_shutdown_box()
        if save:
            self.save_settings(self.win)

    def profile_summary(self):
        return " + ".join(ACTIVE.names) or _("profil seçilmedi")

    def update_profile_labels(self):
        self.btn_dev.set_tooltip_text(_("Yalnızca geliştirme süreçlerini göster ({profile_summary})").format(profile_summary=self.profile_summary()))
        labels = ", ".join(l for l, _rx in ACTIVE.idle_rules) or _("yok")
        self.btn_idle.set_tooltip_text(_("Boşta bekleyen artıkları kapatır: {labels}").format(labels=labels))
        self.btn_idle.set_sensitive(bool(ACTIVE.idle_rules))

    def log_event(self, kind, text):
        if self.history is not None and self.pref("log_enabled"):
            self.history.add_event(time.time(), kind, text)

    def notify(self, title, body, buttons=()):
        """Masaustu bildirimi (sistem tepsisi yok). Gio.Notification, olmazsa notify-send (dugmesiz).
        buttons: [(etiket, 'app.eylem', GLib.Variant | None)]"""
        app = Gio.Application.get_default()
        try:
            if app is not None and app.get_is_registered():
                n = Gio.Notification.new(title)
                n.set_body(body)
                for label, action, target in buttons:
                    if target is None:
                        n.add_button(label, action)
                    else:
                        n.add_button_with_target_value(label, action, target)
                app.send_notification(None, n)
                return
        except (GLib.Error, TypeError):
            pass
        try:
            subprocess.Popen(["notify-send", "-a", _("Görev Yöneticisi"), "--", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def top_by(self, key, fmt, n=3):
        best = sorted((p for p in self.last_procs if key(p) > 0), key=key, reverse=True)[:n]
        return ", ".join(f"{p['name']} {fmt(key(p))}" for p in best) or "–"

    def history_procs(self):
        out = []
        for r in self.last_procs:
            out.append(dict(pid=r["pid"], name=r["name"],
                            project=os.path.basename(self.proc_project.get(r["pid"]) or ""),
                            cpu=r["cpu"], mem=r["pss"] or r["rss"],
                            io_r=max(0, r.get("io_r", 0)), io_w=max(0, r.get("io_w", 0))))
        return out

    # -------------------------------------------------------------- islemler sekmesi
    def build_processes(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Ara…"))
        self.search.set_tooltip_text(_("Ad, komut, dizin ya da PID ara\n:5000  → o portu dinleyen süreç\n"
                                       "takılı  → disk beklemesinde takılanlar\nproje:<ad veya yol>  → bir projenin süreçleri"))
        self.search.connect("search-changed", self.on_search)
        bar.pack_start(self.search, True, True, 0)

        self.btn_dev = Gtk.ToggleButton(label=_("Sadece geliştirme"))
        self.btn_dev.set_active(self.dev_only)
        self.btn_dev.connect("toggled", self.on_dev_toggle)
        bar.pack_start(self.btn_dev, False, False, 0)

        self.btn_tree = Gtk.ToggleButton(label=_("Ağaç görünümü"))
        self.btn_tree.set_tooltip_text(_("Süreçleri ebeveyn-çocuk ilişkisiyle göster (dotnet run → uygulama → MSBuild)"))
        self.btn_tree.set_active(self.tree_mode)
        self.btn_tree.connect("toggled", self.on_tree_toggle)
        bar.pack_start(self.btn_tree, False, False, 0)

        btn_find = Gtk.Button(label=_("Dosya sahibi…"))
        btn_find.set_tooltip_text(_("Bir dosya/klasörü hangi süreç tutuyor? (Ctrl+O)"))
        btn_find.connect("clicked", lambda _b: self.open_owner_finder())
        bar.pack_start(btn_find, False, False, 0)

        chk = Gtk.CheckButton(label=_("Tüm kullanıcılar"))
        chk.set_active(self.show_all_users)
        chk.connect("toggled", self.on_all_users)
        bar.pack_start(chk, False, False, 0)

        self.btn_pause = Gtk.ToggleButton(label=_("Duraklat"))
        self.btn_pause.connect("toggled", lambda b: setattr(self, "paused", b.get_active()))
        bar.pack_end(self.btn_pause, False, False, 0)

        btn_force = Gtk.Button(label=_("Zorla sonlandır"))
        btn_force.get_style_context().add_class("destructive-action")
        btn_force.set_tooltip_text(_("SIGKILL (Shift+Delete)"))
        btn_force.connect("clicked", lambda _b: self.end_tasks(signal.SIGKILL))
        bar.pack_end(btn_force, False, False, 0)

        btn_end = Gtk.Button(label=_("Görevi sonlandır"))
        btn_end.set_tooltip_text(_("SIGTERM - nazikçe kapat (Delete)"))
        btn_end.connect("clicked", lambda _b: self.end_tasks(signal.SIGTERM))
        bar.pack_end(btn_end, False, False, 0)

        self.btn_idle = Gtk.Button(label=_("Boşta artıkları temizle"))
        self.btn_idle.connect("clicked", lambda _b: self.cleanup_idle())
        bar.pack_end(self.btn_idle, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.store = Gtk.TreeStore(*COL_TYPES)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.row_visible)
        self.sort = Gtk.TreeModelSort(model=self.filter)

        self.tree = Gtk.TreeView(model=self.sort)
        self.tree.set_enable_search(False)
        self.tree.set_rubber_banding(True)
        self.tree.set_fixed_height_mode(True)
        self.tree.set_show_expanders(self.tree_mode)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.connect("button-press-event", self.on_tree_button)
        self.tree.connect("popup-menu", lambda _t: self.popup_menu(None) or True)
        self.tree.connect("row-collapsed", self.on_row_collapsed)
        self.tree.connect("row-expanded", self.on_row_expanded)
        self.tree.connect("row-activated", lambda *_a: self.show_selected_details())

        self.hdr_cpu = make_header(_("CPU"), right=True)
        self.hdr_mem = make_header(_("Bellek"), right=True)
        specs = [
            dict(key="name", title=_("Ad"), col=COL_NAME, sort=COL_NAME, width=260, hideable=False, func=self.name_cell),
            dict(key="pid", title=_("PID"), col=COL_PID, sort=COL_PID, width=80, xalign=1.0),
            dict(key="cpu", title=_("CPU"), col=COL_CPU, sort=COL_CPU, width=80, xalign=1.0, func=self.cpu_cell,
                 header=self.hdr_cpu),
            dict(key="mem", title=_("Bellek"), col=COL_RSS, sort=COL_RSS, width=105, xalign=1.0, func=self.mem_cell,
                 header=self.hdr_mem),
            dict(key="pss", title=_("Bellek (PSS)"), col=COL_PSS, sort=COL_PSS, width=120, xalign=1.0,
                 func=self.pss_cell),
            dict(key="ports", title=_("Portlar"), col=COL_PORTS, sort=COL_PORTS, width=110),
            dict(key="ior", title=_("Disk okuma"), col=COL_IOR, sort=COL_IOR, width=105, xalign=1.0,
                 func=self.io_cell(COL_IOR)),
            dict(key="iow", title=_("Disk yazma"), col=COL_IOW, sort=COL_IOW, width=105, xalign=1.0,
                 func=self.io_cell(COL_IOW)),
            dict(key="threads", title=_("İş parçacığı"), col=COL_THR, sort=COL_THR, width=110, xalign=1.0),
            dict(key="nice", title=_("Öncelik (nice)"), col=COL_NICE, sort=COL_NICE, width=115, xalign=1.0,
                 visible=False),
            dict(key="time", title=_("Süre"), col=COL_STARTED, sort=COL_STARTED, width=85, xalign=1.0,
                 func=self.time_cell),
            dict(key="user", title=_("Kullanıcı"), col=COL_USER, sort=COL_USER, width=95),
            dict(key="status", title=_("Durum"), col=COL_STATUS, sort=COL_STATUS, width=120, visible=False,
                 func=self.status_cell),
            dict(key="cwd", title=_("Çalışma dizini"), col=COL_CWD, sort=COL_CWD, width=230),
            dict(key="cmd", title=_("Komut satırı"), col=COL_CMD, sort=COL_CMD, width=400, expand=True),
        ]
        self.pcols = ColumnSet(self.tree, specs, self.settings.get("proc_columns"))
        self.pcols.apply_sort(self.sort, self.settings.get("proc_sort"), ["cpu", "desc"])

        scroll = Gtk.ScrolledWindow()
        scroll.add(self.tree)
        page.pack_start(scroll, True, True, 0)

        self.status = Gtk.Label(label="", xalign=0, use_markup=True)
        self.status.set_margin_start(10)
        self.status.set_margin_end(10)
        self.status.set_margin_top(5)
        self.status.set_margin_bottom(5)
        page.pack_start(self.status, False, False, 0)

        self.build_menu()
        return page

    def name_cell(self, _col, r, model, it, _data):
        stuck = model.get_value(it, COL_PID) in self.stuck
        frozen = model.get_value(it, COL_STATUS) == psutil.STATUS_STOPPED
        r.set_property("text", ("⚠ " if stuck else "⏸ " if frozen else "") + model.get_value(it, COL_NAME))
        if stuck:
            r.set_property("foreground", "#e8890c")
        else:
            r.set_property("foreground-set", False)

    def status_cell(self, _col, r, model, it, _data):
        st = model.get_value(it, COL_STATUS)
        r.set_property("text", STATUS_TR.get(st, st))
        if model.get_value(it, COL_PID) in self.stuck:
            r.set_property("foreground", "#e8890c")
        else:
            r.set_property("foreground-set", False)

    def cpu_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, COL_CPU)
        r.set_property("text", f"{v:.1f}%")
        heat(r, v / 30.0)

    def pss_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, COL_PSS)
        r.set_property("text", fmt_bytes(v) if v else "–")
        heat(r, v / (2 << 30))

    def io_cell(self, col):
        def cell(_col, r, model, it, _data):
            v = model.get_value(it, col)
            r.set_property("text", fmt_rate(v) if v >= 0 else "–")
            heat(r, max(0, v) / (50 << 20))
        return cell

    def mem_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, COL_RSS)
        r.set_property("text", fmt_bytes(v))
        heat(r, v / (2 << 30))

    def time_cell(self, _col, r, model, it, _data):
        r.set_property("text", fmt_elapsed(model.get_value(it, COL_STARTED), time.time()))

    # ---- filtre
    def self_matches(self, model, it):
        if self.dev_only and not model.get_value(it, COL_DEV):
            return False
        if self.port_query is not None:
            return f" {self.port_query} " in model.get_value(it, COL_PORTNUMS)
        if self.proj_filter is not None:
            proj = self.proc_project.get(model.get_value(it, COL_PID), "")
            f = self.proj_filter
            return proj == f or bool(proj and f and "/" not in f and os.path.basename(proj) == f)
        if self.query in ("takılı", _("takılı")):
            return model.get_value(it, COL_PID) in self.stuck
        return not self.query or self.query in model.get_value(it, COL_SEARCH)

    def row_visible(self, model, it, _data):
        if self.self_matches(model, it):
            return True
        if self.tree_mode:  # bir alt sureç eşleşiyorsa ata da görünsün
            child = model.iter_children(it)
            while child:
                if self.row_visible(model, child, None):
                    return True
                child = model.iter_next(child)
        return False

    def count_rows(self, model, it=None):
        n = 0
        child = model.iter_children(it)
        while child:
            n += 1 + self.count_rows(model, child)
            child = model.iter_next(child)
        return n

    def on_search(self, entry):
        text = entry.get_text().strip()
        self.query = text.lower()
        m = PORT_QUERY_RX.match(text)
        self.port_query = int(m.group(1)) if m else None
        pm = PROJECT_QUERY_RX.match(text)
        self.proj_filter = (os.path.expanduser(pm.group(1).strip()).rstrip("/") if pm else None)
        self.filter.refilter()
        self.update_status()
        if self.explain_src:
            GLib.source_remove(self.explain_src)
            self.explain_src = None
        if self.port_query is not None:
            self.explain_src = GLib.timeout_add(500, self.explain_port_result)

    def explain_port_result(self):
        self.explain_src = None
        if self.port_query is not None and self.count_rows(self.sort) == 0:
            self.flash(explain_port(self.port_query), 12)
        return False

    def on_dev_toggle(self, btn):
        self.dev_only = btn.get_active()
        self.filter.refilter()
        self.update_status()

    def on_all_users(self, chk):
        self.show_all_users = chk.get_active()
        self.rebuild_store()

    def on_tree_toggle(self, btn):
        self.tree_mode = btn.get_active()
        self.tree.set_show_expanders(self.tree_mode)
        self.rebuild_store()

    def rebuild_store(self):
        self.store.clear()
        self.iters.clear()
        self.collapsed.clear()
        self.refresh_processes()

    def on_row_collapsed(self, _tree, it, _path):
        if self.tree_mode:
            self.collapsed.add(self.sort.get_value(it, COL_PID))

    def on_row_expanded(self, _tree, it, _path):
        self.collapsed.discard(self.sort.get_value(it, COL_PID))

    # ---- baglam menusu
    def build_menu(self):
        self.menu = Gtk.Menu()
        prio = [
            (_("Düşük (nice 10)"), lambda: self.set_nice(10)),
            (_("Çok düşük (nice 19)"), lambda: self.set_nice(19)),
            (_("Normal (nice 0) – root gerekebilir"), lambda: self.set_nice(0)),
        ]
        entries = [
            (_("Ayrıntılar…"), self.show_selected_details),
            None,
            (_("Görevi sonlandır (SIGTERM)"), lambda: self.end_tasks(signal.SIGTERM)),
            (_("Zorla sonlandır (SIGKILL)"), lambda: self.end_tasks(signal.SIGKILL)),
            (_("Süreç ağacını sonlandır (alt süreçlerle)"), lambda: self.end_tasks(signal.SIGKILL, tree=True)),
            None,
            (_("Dondur (SIGSTOP)"), lambda: self.freeze(signal.SIGSTOP)),
            (_("Devam ettir (SIGCONT)"), lambda: self.freeze(signal.SIGCONT)),
            (_("Öncelik"), prio),
            (_("CPU çekirdeği ata…"), self.set_affinity),
            None,
            (_("Komut satırını kopyala"), self.copy_cmd),
            (_("PID'yi kopyala"), self.copy_pid),
            (_("Çalışma dizinini aç"), self.open_cwd),
        ]
        for e in entries:
            if e is None:
                self.menu.append(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=e[0])
            if isinstance(e[1], list):
                sub = Gtk.Menu()
                for label, fn in e[1]:
                    si = Gtk.MenuItem(label=label)
                    si.connect("activate", lambda _i, fn=fn: fn())
                    sub.append(si)
                item.set_submenu(sub)
            else:
                item.connect("activate", lambda _i, fn=e[1]: fn())
            self.menu.append(item)
        self.menu.show_all()
        self.menu.connect("show", lambda _m: setattr(self, "menu_open", True))
        self.menu.connect("hide", lambda _m: setattr(self, "menu_open", False))

    # ---- dondur / oncelik / cekirdek
    def freeze(self, sig):
        sel = self.selected()
        if not sel:
            self.flash(_("Önce listeden bir süreç seç."))
            return
        ok, errs = kill_pids([s[0] for s in sel], sig, False)
        word = _("donduruldu") if sig == signal.SIGSTOP else _("devam ettirildi")
        msg = _("{ok} süreç {word}.").format(ok=ok, word=word)
        if errs:
            msg += _("  Hata: ") + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def set_nice(self, value):
        sel = self.selected()
        if not sel:
            self.flash(_("Önce listeden bir süreç seç."))
            return
        ok, errs = 0, []
        for pid, _n, _c, _w in sel:
            try:
                psutil.Process(pid).nice(value)
                ok += 1
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                errs.append(_("{pid}: yetki yok").format(pid=pid) + (_(" (önceliği yükseltmek root ister)") if value < 10 else ""))
        msg = _("Öncelik nice {value}: {ok} süreç.").format(value=value, ok=ok)
        if errs:
            msg += _("  Hata: ") + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def choose_affinity(self, current):
        """Cekirdek secim penceresi -> secilen cekirdek listesi | None (iptal)"""
        dlg = Gtk.Dialog(title=_("CPU çekirdeği ata"), transient_for=self.toplevel(), modal=True)
        dlg.add_button(_("İptal"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Uygula"), Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(10)
        for m in ("top", "bottom", "start", "end"):
            getattr(area, f"set_margin_{m}")(12)
        area.pack_start(Gtk.Label(label=_("Süreç yalnızca işaretli çekirdeklerde çalışır:"), xalign=0), False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        checks = []
        for i in range(NCPU):
            c = Gtk.CheckButton(label=_("Çekirdek {i}").format(i=i))
            c.set_active(i in current)
            checks.append(c)
            grid.attach(c, i % 4, i // 4, 1, 1)
        area.pack_start(grid, False, False, 0)
        quick = Gtk.Box(spacing=8)
        for label, pick in ((_("Tümü"), lambda i: True), (_("İlk yarısı"), lambda i: i < NCPU // 2),
                            (_("Son yarısı"), lambda i: i >= NCPU // 2)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, pick=pick: [c.set_active(pick(i)) for i, c in enumerate(checks)])
            quick.pack_start(b, False, False, 0)
        area.pack_start(quick, False, False, 0)
        dlg.show_all()
        resp = dlg.run()
        chosen = [i for i, c in enumerate(checks) if c.get_active()]
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return None
        return chosen

    def set_affinity(self):
        sel = self.selected()
        if not sel:
            self.flash(_("Önce listeden bir süreç seç."))
            return
        try:
            current = psutil.Process(sel[0][0]).cpu_affinity()
        except psutil.Error:
            current = list(range(NCPU))
        chosen = self.choose_affinity(current)
        if chosen is None:
            return
        if not chosen:
            self.flash(_("En az bir çekirdek seçmelisin."))
            return
        ok, errs = 0, []
        for pid, _n, _c, _w in sel:
            try:
                psutil.Process(pid).cpu_affinity(chosen)
                ok += 1
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                errs.append(_("{pid}: yetki yok").format(pid=pid))
        msg = _("{ok} sürecin çekirdekleri {chosen} olarak ayarlandı.").format(ok=ok, chosen=chosen)
        if errs:
            msg += _("  Hata: ") + "; ".join(errs[:3])
        self.flash(msg)

    # ---- ayrintilar penceresi
    def show_selected_details(self):
        sel = self.selected()
        if sel:
            self.show_details(sel[0][0], sel[0][1])

    def show_details(self, pid, name):
        old = self.details.get(pid)
        if old is not None:
            old.present()
            return
        win = Gtk.Window(title=_("Süreç ayrıntıları – {name} ({pid})").format(name=name, pid=pid))
        win.set_default_size(920, 560)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(top, f"set_margin_{m}")(8)
        info = Gtk.Label(label=_("Anlık görünüm – güncellemek için Yenile'ye bas."), xalign=0)
        btn = Gtk.Button(label=_("Yenile"))
        top.pack_start(info, True, True, 0)
        top.pack_end(btn, False, False, 0)
        box.pack_start(top, False, False, 0)
        nb = Gtk.Notebook()
        views = {}
        for key, title in (("general", _("Genel")), ("files", _("Açık dosyalar")), ("conns", _("Ağ bağlantıları")),
                           ("children", _("Alt süreçler")), ("env", _("Ortam değişkenleri"))):
            tv = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
            tv.set_left_margin(10)
            tv.set_top_margin(8)
            sc = Gtk.ScrolledWindow()
            sc.add(tv)
            nb.append_page(sc, Gtk.Label(label=title))
            views[key] = tv
        box.pack_start(nb, True, True, 0)
        win.add(box)

        def load(*_a):
            try:
                data = process_details(pid)
            except psutil.NoSuchProcess:
                info.set_text(_("Bu süreç artık çalışmıyor."))
                return
            for key, tv in views.items():
                tv.get_buffer().set_text(data[key])
            info.set_text(_("{strftime} anındaki görünüm").format(strftime=time.strftime('%H:%M:%S')))

        btn.connect("clicked", load)
        win.connect("destroy", lambda _w: self.details.pop(pid, None))
        self.details[pid] = win
        win.test_views = views
        load()
        win.show_all()

    def on_tree_button(self, tree, ev):
        if ev.button != 3:
            return False
        hit = tree.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return True
        sel = tree.get_selection()
        if not sel.path_is_selected(hit[0]):
            sel.unselect_all()
            sel.select_path(hit[0])
        self.popup_menu(ev)
        return True

    def popup_menu(self, ev):
        if ev is not None:
            self.menu.popup_at_pointer(ev)
        else:
            self.menu.popup_at_widget(self.tree, Gdk.Gravity.CENTER, Gdk.Gravity.CENTER, None)

    def selected(self):
        model, paths = self.tree.get_selection().get_selected_rows()
        return [(model[p][COL_PID], model[p][COL_NAME], model[p][COL_CMD], model[p][COL_CWD]) for p in paths]

    def copy_cmd(self):
        sel = self.selected()
        if sel:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(sel[0][2], -1)
            self.flash(_("Komut satırı kopyalandı."))

    def copy_pid(self):
        sel = self.selected()
        if sel:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(" ".join(str(s[0]) for s in sel), -1)
            self.flash(_("PID kopyalandı."))

    def open_cwd(self):
        sel = self.selected()
        if sel and sel[0][3]:
            path = os.path.expanduser(sel[0][3])
            if os.path.isdir(path):
                subprocess.Popen(["xdg-open", path])

    # ---- sonlandirma
    def toplevel(self):
        top = self.root.get_toplevel()
        return top if isinstance(top, Gtk.Window) else None

    def confirm(self, title, detail, ok_label):
        dlg = Gtk.MessageDialog(transient_for=self.toplevel(), modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(detail)
        dlg.add_button(_("İptal"), Gtk.ResponseType.CANCEL)
        b = dlg.add_button(ok_label, Gtk.ResponseType.OK)
        b.get_style_context().add_class("destructive-action")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def end_tasks(self, sig, tree=False):
        sel = self.selected()
        if not sel:
            self.flash(_("Önce listeden bir süreç seç."))
            return
        listing = "\n".join(f"{pid}  {name}" for pid, name, _c, _w in sel[:10])
        if len(sel) > 10:
            listing += _("\n… ve {v} süreç daha").format(v=len(sel) - 10)
        pats = self.protect_patterns()
        guarded = [f"{pid}  {name}" for pid, name, cmd, _w in sel
                   if tm_alerts.is_protected(pats, name, cmd, self.proc_project.get(pid) or "")]
        if guarded:
            listing += (_("\n\n⚠ Korunanlar listesindeki süreçler de seçili:\n") + "\n".join(guarded[:5]))
        what = _("ağacıyla birlikte ") if tree else ""
        title = _("{n} süreç {what}{name} ile sonlandırılsın mı?").format(n=len(sel), what=what, name=signal.Signals(sig).name)
        if not self.confirm(title, listing, _("Sonlandır")):
            return
        self.send_signal([s[0] for s in sel], sig, tree)

    def send_signal(self, pids, sig, tree):
        ok, errs = kill_pids(pids, sig, tree)
        msg = _("{name}: {ok} sürece gönderildi.").format(name=signal.Signals(sig).name, ok=ok)
        if errs:
            msg += _("  Hata: ") + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def refresh_once(self):
        self.refresh_processes()
        return False

    def cleanup_targets(self):
        """Boşta (CPU < %2), kendi kullanıcının, korunmayan ve secili profillerin 'bosta kapatilabilir' kurallarina
        uyan surecler -> [satir + 'idle' etiketi]"""
        out = []
        for r in self.last_procs:
            if r["uid"] != ME or r["cpu_raw"] >= 2.0:
                continue
            label = ACTIVE.idle_label(r["cmd"])
            if label and not self.is_protected(r):
                out.append(dict(r, idle=label))
        return out

    def cleanup_idle(self):
        rows = self.cleanup_targets()
        if not rows:
            self.flash(_("Temizlenecek boşta artık süreç yok.") if ACTIVE.idle_rules
                       else _("Seçili profillerde boşta kapatılacak süreç türü tanımlı değil."))
            return
        kinds = {}
        for r in rows:
            kinds[r["idle"]] = kinds.get(r["idle"], 0) + 1
        what = ", ".join(_("{n} {k}").format(n=n, k=k) for k, n in kinds.items())
        detail = (_("{what} kapatılacak (toplam {size} RAM).\n\nCPU kullanan ve korunan süreçler atlanır. Bir "
                    "derleme sürüyorsa bitmesini bekle. Sonraki build'de bu süreçler yeniden başlar.").format(what=what, size=fmt_bytes(sum(r['rss'] for r in rows))))
        if self.confirm(_("Boşta bekleyen artıklar temizlensin mi?"), detail, _("Temizle")):
            self.send_signal([r["pid"] for r in rows], signal.SIGTERM, False)

    def on_key(self, _win, ev):
        focus = self.win.get_focus() if self.win else None
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        key = ev.keyval
        page = self.root.get_current_page()
        if key == Gdk.KEY_f and ctrl:
            if page == TAB_DOCKER:
                self.docker_search.grab_focus()
            elif page == TAB_SERVICES:
                self.svc_search.grab_focus()
            elif page == TAB_PORTS:
                self.ports_search.grab_focus()
            else:
                self.root.set_current_page(TAB_PROCESSES)
                self.search.grab_focus()
            return True
        if key == Gdk.KEY_o and ctrl:
            self.open_owner_finder()
            return True
        if key == Gdk.KEY_comma and ctrl:
            self.open_settings()
            return True
        if key == Gdk.KEY_q and ctrl:
            self.quit_app()
            return True
        if key == Gdk.KEY_F5:
            if page == TAB_DOCKER:
                self.docker_refresh()
            elif page == TAB_HISTORY:
                self.refresh_history()
            elif page == TAB_SERVICES:
                self.services_refresh()
            else:
                self.refresh_processes()
            return True
        if key == Gdk.KEY_Delete and page == TAB_PROCESSES and not isinstance(focus, Gtk.Editable):
            shift = ev.state & Gdk.ModifierType.SHIFT_MASK
            self.end_tasks(signal.SIGKILL if shift else signal.SIGTERM)
            return True
        return False

    # ---- yenileme
    def parent_pid(self, r, live):
        """Ağaç modunda satırın ebeveyni (listede yoksa en üst seviye)."""
        if self.tree_mode and r["ppid"] in live and r["ppid"] != r["pid"]:
            return r["ppid"]
        return None

    def row_for(self, r, ports):
        shown, nums = ports_info(ports)
        search = f"{r['raw_name']} {r['name']} {r['cmd']} {r['cwd']} {r['pid']}{nums}".lower()
        return [r["pid"], r["name"], r["cpu"], r["rss"], r["threads"], r["user"], r["started"], r["cmd"],
                r["cwd"], r["status"], r["ppid"], r["dev"], search, shown, nums, r.get("pss", 0), r["nice"],
                r.get("io_r", -1), r.get("io_w", -1)]

    def store_subtree(self, it, acc):
        acc.add(self.store[it][COL_PID])
        child = self.store.iter_children(it)
        while child:
            self.store_subtree(child, acc)
            child = self.store.iter_next(child)

    def refresh_processes(self):
        procs = collect(self.show_all_users)
        pss, io = self.stats.pss, self.stats.io
        for r in procs:
            r["pss"] = pss.get(r["pid"], 0)
            r["io_r"], r["io_w"] = io.get(r["pid"], (-1, -1))
        self.last_procs = procs
        socks = self.stats.socks
        self.last_socks = socks if socks is not None else listening_sockets()
        ports = listening_ports(self.last_socks)
        live = {r["pid"]: r for r in procs}
        self.update_stuck(procs)
        self.proc_project = {r["pid"]: project_root(r["cwd"]) for r in procs}

        # 1) bayat satirlar: sonlanmis, PID'i yeniden kullanilmis ya da ebeveyni degismis olanlar.
        #    TreeStore'da bir satiri silmek altindakileri de siler; onlari da yeniden ekleriz.
        stale = set()
        for pid, it in self.iters.items():
            r = live.get(pid)
            if r is None or self.store[it][COL_STARTED] != r["started"]:
                stale.add(pid)
                continue
            par = self.store.iter_parent(it)
            if (self.store[par][COL_PID] if par else None) != self.parent_pid(r, live):
                stale.add(pid)
        if stale:
            doomed = set()
            for pid in stale:
                if pid not in doomed:
                    self.store_subtree(self.iters[pid], doomed)
            roots = []
            for pid in doomed:
                par = self.store.iter_parent(self.iters[pid])
                if par is None or self.store[par][COL_PID] not in doomed:
                    roots.append(self.iters[pid])
            for it in roots:
                self.store.remove(it)
            for pid in doomed:
                del self.iters[pid]

        # 2) eksik satirlari ekle (ebeveyn once)
        added, expand = set(), set()

        def add(pid):
            r = live[pid]
            par_pid = self.parent_pid(r, live)
            par_it = None
            if par_pid is not None:
                if par_pid not in self.iters:
                    add(par_pid)
                par_it = self.iters[par_pid]
                expand.add(par_pid)
            self.iters[pid] = self.store.append(par_it, self.row_for(r, ports.get(pid)))
            added.add(pid)

        for pid in live:
            if pid not in self.iters:
                add(pid)

        # 3) mevcut satirlarin degerlerini yerinde guncelle
        for pid, r in live.items():
            if pid in added:
                continue
            it = self.iters[pid]
            self.store.set(it, [COL_CPU, COL_RSS, COL_THR, COL_STATUS, COL_PPID, COL_PSS, COL_NICE, COL_IOR, COL_IOW],
                           [r["cpu"], r["rss"], r["threads"], r["status"], r["ppid"], r["pss"], r["nice"],
                            r["io_r"], r["io_w"]])
            shown, nums = ports_info(ports.get(pid))
            if self.store[it][COL_PORTNUMS] != nums:
                row = self.row_for(r, ports.get(pid))
                self.store.set(it, [COL_PORTS, COL_PORTNUMS, COL_SEARCH], [row[COL_PORTS], nums, row[COL_SEARCH]])

        if self.tree_mode:
            self.collapsed &= set(live)
            if self.query or self.dev_only or self.port_query is not None or self.proj_filter is not None:
                self.filter.refilter()  # alt surec degisince ata gorunurlugu de degisebilir
            for pid in sorted(expand, key=lambda p: len(self.store.get_path(self.iters[p]).get_indices())):
                if pid not in self.collapsed:
                    self.expand_pid(pid)
        self.update_projects()
        self.update_ports()
        self.update_status()

    def expand_pid(self, pid):
        it = self.iters.get(pid)
        if it is None:
            return
        fpath = self.filter.convert_child_path_to_path(self.store.get_path(it))
        spath = self.sort.convert_child_path_to_path(fpath) if fpath is not None else None
        if spath is not None:
            self.tree.expand_row(spath, False)

    def flash(self, text, secs=6):
        self.flash_text = text
        self.flash_until = time.monotonic() + secs
        self.update_status()
        self.update_docker_status()

    def update_status(self):
        if time.monotonic() < self.flash_until:
            self.status.set_markup(f"<b>{GLib.markup_escape_text(self.flash_text)}</b>")
            return
        shown = self.count_rows(self.sort)
        dev = [r for r in self.last_procs if r["dev"]]
        idle = self.cleanup_targets()
        text = (_("Listelenen: {shown}/{n}   │   {profile_summary}: {n2} süreç ({size})").format(
                shown=shown, n=len(self.last_procs), profile_summary=self.profile_summary(), n2=len(dev),
                size=fmt_bytes(sum(r['rss'] for r in dev))))
        if ACTIVE.idle_rules:
            idle_txt = _("boşta artık: {n} ({size})").format(n=len(idle), size=fmt_bytes(sum(r['rss'] for r in idle)))
            if len(idle) > 8:
                idle_txt = f"<span foreground='#e8890c'><b>{idle_txt}</b></span>"
            text += f"   │   {idle_txt}"
        if self.stuck:
            text += (_("   │   <span foreground='#e8890c'><b>⚠ {n} süreç disk beklemesinde takılı</b></span> "
                       "(aramaya 'takılı' yaz)").format(n=len(self.stuck)))
        self.status.set_markup(text)

    # -------------------------------------------------------------- performans sekmesi
    def build_performance(self):
        box = Gtk.Box()
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        side = Gtk.StackSidebar()
        side.set_stack(self.stack)
        side.set_size_request(150, -1)
        box.pack_start(side, False, False, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        box.pack_start(self.stack, True, True, 0)

        blue, purple = (0.20, 0.55, 0.85), (0.60, 0.40, 0.85)
        green, orange = (0.25, 0.70, 0.40), (0.95, 0.60, 0.15)

        self.g_cpu = Graph([blue])
        self.cores = CoreGrid(blue)
        self.s_cpu = StatBlock([("use", _("Kullanım")), ("freq", _("Hız")), ("proc", _("Süreç")),
                                ("up", _("Çalışma süresi")), ("load", _("Yük ortalaması (1/5/15 dk)")),
                                ("cores", _("Mantıksal işlemci"))])
        self.stack.add_titled(make_page(_("CPU"), f"<span alpha='65%'>{GLib.markup_escape_text(cpu_model())}</span>",
                                        self.g_cpu, self.s_cpu, self.cores), "cpu", _("CPU"))

        self.g_mem = Graph([purple])
        self.s_mem = StatBlock([("use", _("Kullanımda")), ("avail", _("Kullanılabilir")), ("cache", _("Önbellek")),
                                ("total", _("Toplam")), ("swap", _("Swap (kullanılan / toplam)")),
                                ("swapio", _("Swap trafiği (içeri / dışarı)")), ("zram", _("zram (sıkıştırılmış RAM swap)")),
                                ("pct", _("Kullanım oranı"))])
        self.stack.add_titled(make_page(_("Bellek"), f"<span alpha='65%'>{fmt_bytes(psutil.virtual_memory().total)} RAM</span>",
                                        self.g_mem, self.s_mem), "mem", _("Bellek"))

        self.g_disk = Graph([green, orange], maxv=None, min_scale=1 << 20, label_fmt=lambda v: fmt_bytes(v) + "/s")
        self.s_disk = StatBlock([("r", _("Okuma hızı")), ("w", _("Yazma hızı")), ("rt", _("Toplam okunan")),
                                 ("wt", _("Toplam yazılan"))])
        self.stack.add_titled(make_page(_("Disk"),
                                        _("<span foreground='#40b366'>■</span> Okuma   <span foreground='#f29926'>■</span> Yazma"),
                                        self.g_disk, self.s_disk), "disk", _("Disk"))

        self.g_net = Graph([blue, orange], maxv=None, min_scale=100 << 10, label_fmt=lambda v: fmt_bytes(v) + "/s")
        self.s_net = StatBlock([("d", _("İndirme")), ("u", _("Yükleme")), ("dt", _("Toplam indirilen")),
                                ("ut", _("Toplam yüklenen"))])
        self.stack.add_titled(make_page(_("Ağ"),
                                        _("<span foreground='#338cd9'>■</span> İndirme   <span foreground='#f29926'>■</span> Yükleme"),
                                        self.g_net, self.s_net), "net", _("Ağ"))

        self.g_psi = Graph([blue, purple, orange])
        self.s_psi = StatBlock([("cpu", _("CPU baskısı (10 sn / 60 sn / 5 dk)")),
                                ("mem", _("Bellek baskısı (10 sn / 60 sn / 5 dk)")),
                                ("io", _("Disk baskısı (10 sn / 60 sn / 5 dk)"))], cols=3)
        self.psi_top = Gtk.Label(label="", xalign=0, wrap=True, use_markup=True)
        self.stack.add_titled(make_page(
            _("Baskı (PSI)"),
            _("<span foreground='#338cd9'>■</span> CPU   <span foreground='#9966d9'>■</span> Bellek   "
            "<span foreground='#f29926'>■</span> Disk   <span alpha='65%'>(işlerin kaynak beklediği zaman %)</span>"),
            self.g_psi, self.s_psi, self.psi_top), "psi", _("Baskı (PSI)"))
        return box

    # -------------------------------------------------------------- gecmis sekmesi
    def build_history(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.hist_range = Gtk.ComboBoxText()
        for label, _secs in HISTORY_RANGES:
            self.hist_range.append_text(label)
        idx = self.settings.get("hist_range")
        self.hist_range.set_active(idx if isinstance(idx, int) and 0 <= idx < len(HISTORY_RANGES) else 1)
        self.hist_range.connect("changed", self.on_hist_range)
        bar.pack_start(self.hist_range, False, False, 0)
        btn = Gtk.Button(label=_("Yenile"))
        btn.connect("clicked", lambda _b: self.refresh_history())
        bar.pack_start(btn, False, False, 0)
        self.hist_clear = Gtk.Button(label=_("Seçimi kaldır"))
        self.hist_clear.set_no_show_all(True)
        self.hist_clear.connect("clicked", lambda _b: self.clear_hist_select())
        bar.pack_start(self.hist_clear, False, False, 0)
        self.hist_hint = Gtk.Label(label="", xalign=0, use_markup=True)
        self.hist_hint.set_ellipsize(3)
        bar.pack_start(self.hist_hint, True, True, 0)
        page.pack_start(bar, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.hist_graph = HistoryGraph(self.on_hist_select, self.clear_hist_select)
        paned.pack1(self.hist_graph, True, False)
        nb = Gtk.Notebook()
        self.hist_stalls, sc = simple_table([(_("Başlangıç"), 130, False), (_("Süre"), 90, True), (_("Neden"), 70, False),
                                             (_("Tepe baskı"), 85, True), (_("CPU"), 60, True), (_("RAM"), 60, True),
                                             (_("Suçlular"), 300, False)])
        nb.append_page(sc, Gtk.Label(label=_("Takılma anları")))
        self.hist_offs, sc = simple_table([(_("Süreç"), 180, False), (_("Proje"), 140, False), (_("Ort. CPU"), 80, True),
                                           (_("En yüksek CPU"), 100, True), (_("En yüksek RAM"), 100, True),
                                           (_("Ort. disk G/Y"), 110, True), (_("Görülme"), 75, True)])
        nb.append_page(sc, Gtk.Label(label=_("En çok yiyenler")))
        self.hist_projs, sc = simple_table([(_("Proje"), 200, False), (_("Ort. CPU"), 80, True),
                                            (_("En yüksek CPU"), 100, True), (_("En yüksek RAM"), 110, True),
                                            (_("Ort. disk G/Y"), 110, True), (_("Takılmada suçlu"), 110, True),
                                            (_("Görülme"), 75, True)])
        nb.append_page(sc, Gtk.Label(label=_("Projeler")))
        self.hist_events, sc = simple_table([(_("Zaman"), 130, False), (_("Tür"), 80, False), (_("Olay"), 400, False)])
        nb.append_page(sc, Gtk.Label(label=_("Olaylar")))
        paned.pack2(nb, True, False)
        paned.set_position(270)
        page.pack_start(paned, True, True, 0)
        return page

    def on_hist_range(self, combo):
        self.set_pref("hist_range", combo.get_active())
        self.hist_sel = None  # hazir aralik secilince grafikten yapilan secim gecersiz
        self.refresh_history()

    def on_hist_select(self, t0, t1):
        """Grafikte surukleyerek secilen aralik: grafik o aralia yakinlasir, tablolar yalniz onu gosterir."""
        self.hist_sel = (int(t0), max(int(t1), int(t0) + 1))
        self.refresh_history()

    def clear_hist_select(self):
        if self.hist_sel is not None:
            self.hist_sel = None
            self.refresh_history()

    def refresh_history(self):
        """Sorgular arka planda; eski sorgunun cevabi (token) yok sayilir."""
        h = self.history
        if h is None:
            self.hist_hint.set_markup(_("<span foreground='#e8890c'>Geçmiş veritabanı açılamadı.</span>"))
            return
        self.hist_token += 1
        token, thr = self.hist_token, self.pref("stall_psi")
        secs = HISTORY_RANGES[max(0, self.hist_range.get_active())][1]
        sel = self.hist_sel
        until = sel[1] if sel else time.time()

        def work():
            try:
                span = h.span()
                since = sel[0] if sel else until - secs if secs else (span[0] if span else until - 3600)
                bucket, rows = h.series(since, until)
                res = dict(since=since, until=until, bucket=bucket, rows=rows, thr=thr, n=span[2] if span else 0,
                           stall_rows=h.stalls(since, until, thr), offs=h.offenders(since, until),
                           projs=h.by_project(since, until, thr),
                           events=h.events(since, until))
            except sqlite3.Error as e:
                res = dict(error=str(e))
            GLib.idle_add(self.on_history, token, res)
        threading.Thread(target=work, daemon=True).start()

    def on_history(self, token, res):
        if token != self.hist_token:
            return False
        if "error" in res:
            self.hist_hint.set_markup(f"<span foreground='#e8890c'>{GLib.markup_escape_text(res['error'])}</span>")
            return False
        stalls = res["stall_rows"]
        res["stalls"] = [(w["start"], w["end"]) for w in stalls]
        self.hist_graph.set_data(res)
        self.hist_stalls.clear()
        for w in stalls:
            peak = {"cpu": w["psi_cpu"], "mem": w["psi_mem"], "io": w["psi_io"]}[w["cause"]]
            culprits = []
            for name, project, mcpu, mmem, mio in w["culprits"]:
                val = {"cpu": _("CPU %{mcpu:.0f}").format(mcpu=mcpu), "mem": fmt_bytes(mmem), "io": fmt_rate(mio)}[w["cause"]]
                culprits.append(f"{name}{f' ({project})' if project else ''} · {val}")
            self.hist_stalls.append([fmt_ts(w["start"]), fmt_duration(w["end"] - w["start"] + SYS_EVERY),
                                     CAUSE_TR[w["cause"]], _("%{peak:.0f}").format(peak=peak), _("%{cpu:.0f}").format(cpu=w['cpu']), _("%{mem:.0f}").format(mem=w['mem']),
                                     "; ".join(culprits) or "–"])
        self.hist_offs.clear()
        for o in res["offs"]:
            self.hist_offs.append([o["name"], o["project"] or "–", _("%{avg_cpu:.1f}").format(avg_cpu=o['avg_cpu']), _("%{max_cpu:.0f}").format(max_cpu=o['max_cpu']),
                                   fmt_bytes(o["max_mem"]), fmt_rate(o["io_r"] + o["io_w"]),
                                   _("%{v:.0f}").format(v=o['seen'] * 100 / max(1, o['total']))])
        self.hist_projs.clear()
        for o in res["projs"]:
            self.hist_projs.append([o["project"] or _("(proje dışı)"), _("%{avg_cpu:.1f}").format(avg_cpu=o['avg_cpu']), _("%{max_cpu:.0f}").format(max_cpu=o['max_cpu']),
                                    fmt_bytes(o["max_mem"]), fmt_rate(o["io"]), str(o["stalls"]),
                                    _("%{v:.0f}").format(v=o['seen'] * 100 / max(1, o['total']))])
        self.hist_events.clear()
        for ts, kind, text in res["events"]:
            self.hist_events.append([fmt_ts(ts), _(kind), text])
        self.hist_clear.set_visible(self.hist_sel is not None)
        if self.hist_sel:
            a, b = self.hist_sel
            hint = (_("<b>Seçili aralık:</b> {v} – {strftime} ({fmt_duration})  │  {n} takılma").format(
                    v=fmt_ts(a), strftime=time.strftime('%H:%M:%S', time.localtime(b)),
                    fmt_duration=fmt_duration(b - a), n=len(stalls)))
        elif not self.pref("log_enabled"):
            hint = _("<span foreground='#e8890c'>Geçmiş kaydı kapalı – Ayarlar'dan aç</span>")
        else:
            hint = _("{n} kayıt  │  {n2} takılma (baskı ≥ %{thr})  │  {pref} gün saklanır").format(
                n=res['n'], n2=len(stalls), thr=res['thr'], pref=self.pref('log_days'))
        self.hist_hint.set_markup(hint)
        return False

    # -------------------------------------------------------------- docker sekmesi
    def build_docker(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.docker_search = Gtk.SearchEntry(placeholder_text=_("Ara: ad, imaj, proje, port…"))
        self.docker_search.connect("search-changed", self.on_docker_search)
        bar.pack_start(self.docker_search, True, True, 0)

        chk = Gtk.CheckButton(label=_("Durdurulanlar da"))
        chk.set_active(self.docker_all)
        chk.connect("toggled", self.on_docker_all)
        bar.pack_start(chk, False, False, 0)

        btn_proj = Gtk.Button(label=_("Aynı projeyi seç"))
        btn_proj.set_tooltip_text(_("Seçili konteynerlerin compose projesindeki tüm konteynerleri seç"))
        btn_proj.connect("clicked", lambda _b: self.docker_select_project())
        bar.pack_end(btn_proj, False, False, 0)
        btn_rm = Gtk.Button(label=_("Sil"))
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.set_tooltip_text(_("docker rm -f (konteyner silinir, volume'lar kalır)"))
        btn_rm.connect("clicked", lambda _b: self.docker_action("rm"))
        bar.pack_end(btn_rm, False, False, 0)
        for label, verb, tip in ((_("Zorla durdur"), "kill", _("docker kill (SIGKILL)")),
                                 (_("Logları göster"), "logs", _("Son 500 satır (çift tık da açar)")),
                                 (_("Yeniden başlat"), "restart", None), (_("Durdur"), "stop", None),
                                 (_("Başlat"), "start", None)):
            b = Gtk.Button(label=label)
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, v=verb: self.docker_action(v))
            bar.pack_end(b, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.docker_msg = Gtk.Label(label="", xalign=0, use_markup=True, wrap=True)
        self.docker_msg.set_margin_start(12)
        self.docker_msg.set_margin_bottom(6)
        self.docker_msg.set_no_show_all(True)
        page.pack_start(self.docker_msg, False, False, 0)

        self.dstore = Gtk.ListStore(*D_TYPES)
        self.dfilter = self.dstore.filter_new()
        self.dfilter.set_visible_func(
            lambda m, it, _d: not self.docker_query or self.docker_query in m.get_value(it, D_SEARCH))
        self.dsort = Gtk.TreeModelSort(model=self.dfilter)
        self.dtree = Gtk.TreeView(model=self.dsort)
        self.dtree.set_enable_search(False)
        self.dtree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.dtree.connect("row-activated", lambda *_a: self.docker_action("logs"))
        specs = [
            dict(key="name", title=_("Ad"), col=D_NAME, sort=D_NAME, width=210, hideable=False),
            dict(key="project", title=_("Proje"), col=D_PROJECT, sort=D_PROJECT, width=110),
            dict(key="state", title=_("Durum"), col=D_STATE, sort=D_STATE, width=130, func=self.docker_state_cell),
            dict(key="cpu", title=_("CPU"), col=D_CPU, sort=D_CPU, width=75, xalign=1.0, func=self.docker_cpu_cell),
            dict(key="mem", title=_("Bellek"), col=D_MEM, sort=D_MEM, width=100, xalign=1.0, func=self.docker_mem_cell),
            dict(key="limit", title=_("RAM sınırı"), col=D_LIMIT, sort=D_LIMIT, width=150, xalign=1.0,
                 func=self.docker_limit_cell),
            dict(key="net", title=_("Ağ G/Ç"), col=D_NET, sort=D_NET, width=130),
            dict(key="block", title=_("Disk G/Ç"), col=D_BLOCK, sort=D_BLOCK, width=130),
            dict(key="pids", title=_("PID sayısı"), col=D_PIDS, sort=D_PIDS, width=100, xalign=1.0),
            dict(key="ports", title=_("Portlar"), col=D_PORTS, sort=D_PORTS, width=190),
            dict(key="image", title=_("İmaj"), col=D_IMAGE, sort=D_IMAGE, width=200),
            dict(key="status", title=_("Çalışma durumu"), col=D_STATUS, sort=D_STATUS, width=180, expand=True),
        ]
        self.dcols = ColumnSet(self.dtree, specs, self.settings.get("docker_columns"))
        self.dcols.apply_sort(self.dsort, self.settings.get("docker_sort"), ["name", "asc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.dtree)
        page.pack_start(scroll, True, True, 0)

        self.docker_status = Gtk.Label(label=_("Docker sekmesi açılınca yüklenir."), xalign=0)
        for m in ("start", "end"):
            getattr(self.docker_status, f"set_margin_{m}")(10)
        self.docker_status.set_margin_top(5)
        self.docker_status.set_margin_bottom(5)
        page.pack_start(self.docker_status, False, False, 0)
        return page

    def docker_state_cell(self, _col, r, model, it, _data):
        st = model.get_value(it, D_STATE)
        label, color = DOCKER_STATES.get(st, (st, None))
        r.set_property("text", ("● " if st == "running" else "○ ") + label)
        if color:
            r.set_property("foreground", color)
        else:
            r.set_property("foreground-set", False)

    def docker_cpu_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, D_CPU)
        r.set_property("text", f"{v:.1f}%")
        heat(r, v / 100.0)

    def docker_mem_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, D_MEM)
        r.set_property("text", fmt_bytes(v) if v else "–")
        heat(r, v / (2 << 30))

    def docker_limit_cell(self, _col, r, model, it, _data):
        v, mem = model.get_value(it, D_LIMIT), model.get_value(it, D_MEM)
        if v < 0:
            r.set_property("text", "–")
        elif v == 0:
            r.set_property("text", _("sınırsız"))
        else:
            r.set_property("text", f"{fmt_bytes(v)}  (%{mem * 100 / v:.0f})")
        heat(r, mem / v if v > 0 else 0.0)

    def docker_select_project(self):
        model, paths = self.dtree.get_selection().get_selected_rows()
        projects = {model[p][D_PROJECT] for p in paths if model[p][D_PROJECT]}
        if not projects:
            self.flash(_("Önce bir compose projesine ait konteyner seç."))
            return
        sel, n = self.dtree.get_selection(), 0
        for row in self.dsort:
            if row[D_PROJECT] in projects:
                sel.select_path(row.path)
                n += 1
        self.flash(_("{n} konteyner seçildi ({items}).").format(n=n, items=', '.join(sorted(projects))))

    def on_docker_search(self, entry):
        self.docker_query = entry.get_text().strip().lower()
        self.dfilter.refilter()

    def on_docker_all(self, chk):
        self.docker_all = chk.get_active()
        self.docker_refresh()

    def on_switch_page(self, _nb, _page, num):
        if num == TAB_HISTORY:
            self.refresh_history()
        elif num == TAB_SERVICES:
            self.services_refresh()
        elif num == TAB_PROJECTS:
            self.update_projects(force_git=True)
        elif num == TAB_DOCKER:
            self.docker_refresh()
        elif num == TAB_PORTS:
            self.docker_ports_t = 0.0
            self.update_ports()
        elif num == TAB_CLEAN and not self.clean_scanned:
            self.clean_scan()

    def docker_refresh(self):
        """Arka planda docker ps + stats calistirir (stats ~2 sn surer)."""
        if self.docker_busy:
            return
        self.docker_busy = True
        all_c = self.docker_all

        def work():
            err = docker_fetch(all_c, lambda rows: GLib.idle_add(self.on_docker_rows, rows))
            GLib.idle_add(self.on_docker_done, err)

        threading.Thread(target=work, daemon=True).start()

    def on_docker_done(self, err):
        self.docker_busy = False
        if err:
            self.docker_store_clear_if_empty_error(err)
        else:
            self.docker_msg.hide()
        return False

    def docker_store_clear_if_empty_error(self, err):
        self.docker_msg.set_markup(_("<span foreground='#e8890c'><b>Docker erişilemiyor:</b> "
                                     "{markup_escape_text}</span>").format(markup_escape_text=GLib.markup_escape_text(err)))
        self.docker_msg.show()

    def on_docker_rows(self, rows):
        seen = set()
        for cid, r in rows.items():
            seen.add(cid)
            search = f"{r['name']} {r['image']} {r['project']} {r['ports']} {r['state']}".lower()
            it = self.docker_iters.get(cid)
            if it is None:
                self.docker_iters[cid] = self.dstore.append([
                    cid, r["name"], r["project"], r["image"], r["state"], r["status"], r["cpu"] or 0.0,
                    r["mem"] or 0, r["net"] or "", r["block"] or "", r["pids"] or 0, r["ports"], search,
                    r["limit"] if r.get("limit") is not None else -1])
            else:
                cols = [D_NAME, D_PROJECT, D_IMAGE, D_STATE, D_STATUS, D_PORTS, D_SEARCH]
                vals = [r["name"], r["project"], r["image"], r["state"], r["status"], r["ports"], search]
                if r["cpu"] is not None:
                    cols += [D_CPU, D_MEM, D_NET, D_BLOCK, D_PIDS, D_LIMIT]
                    vals += [r["cpu"], r["mem"], r["net"], r["block"], r["pids"],
                             r["limit"] if r.get("limit") is not None else -1]
                elif r["state"] != "running":
                    cols += [D_CPU, D_MEM, D_PIDS]
                    vals += [0.0, 0, 0]
                self.dstore.set(it, cols, vals)
        for cid in [c for c in self.docker_iters if c not in seen]:
            self.dstore.remove(self.docker_iters.pop(cid))
        running = [r for r in rows.values() if r["state"] == "running"]
        self.docker_summary = (len(rows), len(running),
                               sum(r["cpu"] or 0 for r in running), sum(r["mem"] or 0 for r in running))
        self.update_docker_status()
        return False

    def update_docker_status(self):
        if not hasattr(self, "docker_status"):
            return
        if time.monotonic() < self.flash_until:
            self.docker_status.set_markup(f"<b>{GLib.markup_escape_text(self.flash_text)}</b>")
            return
        if getattr(self, "docker_summary", None):
            total, running, cpu, mem = self.docker_summary
            self.docker_status.set_markup(
                _("Konteyner: {total} ({running} çalışıyor)   │   toplam CPU: {cpu:.1f}%   │   toplam RAM: "
                  "{size}").format(total=total, running=running, cpu=cpu, size=fmt_bytes(mem)))

    def docker_selected(self):
        model, paths = self.dtree.get_selection().get_selected_rows()
        return [(model[p][D_ID], model[p][D_NAME], model[p][D_STATE]) for p in paths]

    def docker_action(self, verb):
        sel = self.docker_selected()
        if not sel:
            self.flash(_("Önce listeden bir konteyner seç."))
            return
        if verb == "logs":
            self.show_logs(sel[0][0], sel[0][1])
            return
        names = "\n".join(n for _i, n, _s in sel[:10])
        if verb in ("rm", "kill"):
            title = (_("{n} konteyner silinsin mi?").format(n=len(sel)) if verb == "rm"
                     else _("{n} konteyner zorla durdurulsun mu?").format(n=len(sel)))
            if not self.confirm(title, names, _("Sil") if verb == "rm" else _("Zorla durdur")):
                return
        args = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "kill": ["kill"],
                "rm": ["rm", "-f"]}[verb]
        ids = [s[0] for s in sel]
        self.flash(_("docker {verb}: {n} konteyner için çalışıyor…").format(verb=verb, n=len(ids)), 60)

        def work():
            _out, err = _docker_run([*args, *ids], 90)
            GLib.idle_add(self.after_docker_action, verb, len(ids), err)

        threading.Thread(target=work, daemon=True).start()

    def after_docker_action(self, verb, n, err):
        self.flash(_("docker {verb}: {v}.").format(verb=verb, v=_('hata – ') + err if err else _('{n} konteyner tamam').format(n=n)), 8)
        self.docker_refresh()
        return False

    def show_logs(self, cid, name):
        win = Gtk.Window(title=_("Loglar – {name}").format(name=name))
        win.set_default_size(980, 560)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(top, f"set_margin_{m}")(8)
        live = Gtk.CheckButton(label=_("Canlı (2 sn'de bir yenile)"))
        live.set_active(True)
        btn = Gtk.Button(label=_("Yenile"))
        top.pack_start(live, False, False, 0)
        top.pack_end(btn, False, False, 0)
        tv = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        tv.set_left_margin(8)
        scroll = Gtk.ScrolledWindow()
        scroll.add(tv)
        box.pack_start(top, False, False, 0)
        box.pack_start(scroll, True, True, 0)
        win.add(box)
        alive = {"v": True}

        def load(*_a):
            def work():
                try:
                    res = subprocess.run(["docker", "logs", "--tail", "500", cid], capture_output=True,
                                         text=True, timeout=10, errors="replace")
                    text = (res.stdout + res.stderr) if res.returncode == 0 else res.stderr
                except (OSError, subprocess.TimeoutExpired) as e:
                    text = _("loglar alınamadı: {e}").format(e=e)
                GLib.idle_add(show, text)
            threading.Thread(target=work, daemon=True).start()

        def show(text):
            if not alive["v"]:
                return False
            buf = tv.get_buffer()
            buf.set_text(text)
            if live.get_active():
                buf.place_cursor(buf.get_end_iter())
                tv.scroll_mark_onscreen(buf.get_insert())
            return False

        def tick():
            if live.get_active():
                load()
            return alive["v"]

        btn.connect("clicked", load)
        win.connect("destroy", lambda _w: alive.update(v=False))
        GLib.timeout_add(2000, tick)
        win.show_all()
        load()

    # -------------------------------------------------------------- takili surecler (D durumu)
    def update_stuck(self, procs, now=None):
        """Kisa D durumu normaldir; STUCK_SECS'ten uzun suren (disk/NFS/ag paylasimi bekleyen) surecleri isaretler."""
        now = time.monotonic() if now is None else now
        cur = {r["pid"] for r in procs if r["status"] == psutil.STATUS_DISK_SLEEP}
        for pid in [p for p in self.dsince if p not in cur]:
            del self.dsince[pid]
        for pid in cur:
            self.dsince.setdefault(pid, now)
        self.stuck = {pid for pid, t in self.dsince.items() if now - t >= STUCK_SECS}

    # -------------------------------------------------------------- projeler sekmesi
    def build_projects(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        info = Gtk.Label(label=_("Süreçler çalışma dizinine göre projelere (git deposu / .sln / package.json …) gruplanır."),
                         xalign=0)
        info.set_ellipsize(3)
        bar.pack_start(info, True, True, 0)
        btn_force = Gtk.Button(label=_("Zorla sonlandır"))
        btn_force.get_style_context().add_class("destructive-action")
        btn_force.connect("clicked", lambda _b: self.project_action(signal.SIGKILL))
        bar.pack_end(btn_force, False, False, 0)
        btn_end = Gtk.Button(label=_("Projenin süreçlerini sonlandır"))
        btn_end.set_tooltip_text(_("SIGTERM - projenin çalışma dizinindeki tüm süreçlere"))
        btn_end.connect("clicked", lambda _b: self.project_action(signal.SIGTERM))
        bar.pack_end(btn_end, False, False, 0)
        btn_cont = Gtk.Button(label=_("Devam ettir"))
        btn_cont.set_tooltip_text(_("SIGCONT - dondurulmuş süreçleri çalıştırır"))
        btn_cont.connect("clicked", lambda _b: self.project_action(signal.SIGCONT))
        bar.pack_end(btn_cont, False, False, 0)
        btn_stop = Gtk.Button(label=_("Dondur"))
        btn_stop.set_tooltip_text(_("SIGSTOP - öldürmeden duraklatır; kaynak kullanmaz"))
        btn_stop.connect("clicked", lambda _b: self.project_action(signal.SIGSTOP))
        bar.pack_end(btn_stop, False, False, 0)
        btn_show = Gtk.Button(label=_("İşlemlerde göster"))
        btn_show.connect("clicked", lambda _b: self.project_show())
        bar.pack_end(btn_show, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.pstore = Gtk.ListStore(*P_TYPES)
        self.psort = Gtk.TreeModelSort(model=self.pstore)
        self.ptree = Gtk.TreeView(model=self.psort)
        self.ptree.set_enable_search(False)
        self.ptree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.ptree.connect("row-activated", lambda *_a: self.project_show())
        specs = [
            dict(key="name", title=_("Proje"), col=P_NAME, sort=P_NAME, width=220, hideable=False),
            dict(key="count", title=_("Süreç"), col=P_COUNT, sort=P_COUNT, width=80, xalign=1.0),
            dict(key="cpu", title=_("CPU"), col=P_CPU, sort=P_CPU, width=80, xalign=1.0, func=self.proj_cpu_cell),
            dict(key="mem", title=_("Bellek (PSS)"), col=P_MEM, sort=P_MEM, width=110, xalign=1.0, func=self.proj_mem_cell),
            dict(key="dev", title=_("Geliştirme süreci"), col=P_DEV, sort=P_DEV, width=125, xalign=1.0),
            dict(key="branch", title=_("Git dalı"), col=P_BRANCH, sort=P_BRANCH, width=150),
            dict(key="dirty", title=_("Git durumu"), col=P_DIRTY, sort=P_DIRTY, width=130, func=self.proj_dirty_cell),
            dict(key="path", title=_("Yol"), col=P_PATH, sort=P_PATH, width=400, expand=True),
        ]
        self.jcols = ColumnSet(self.ptree, specs, self.settings.get("proj_columns"))
        self.jcols.apply_sort(self.psort, self.settings.get("proj_sort"), ["mem", "desc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.ptree)
        page.pack_start(scroll, True, True, 0)
        self.proj_status = Gtk.Label(label="", xalign=0)
        self.proj_status.set_margin_start(10)
        self.proj_status.set_margin_top(5)
        self.proj_status.set_margin_bottom(5)
        page.pack_start(self.proj_status, False, False, 0)
        return page

    def proj_cpu_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, P_CPU)
        r.set_property("text", f"{v:.1f}%")
        heat(r, v / 30.0)

    def proj_mem_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, P_MEM)
        r.set_property("text", fmt_bytes(v))
        heat(r, v / (4 << 30))

    def proj_dirty_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, P_DIRTY)
        r.set_property("text", "–" if v < 0 else _("temiz") if v == 0 else _("{v} değişiklik").format(v=v))
        if v > 0:
            r.set_property("foreground", "#e8890c")
        else:
            r.set_property("foreground-set", False)

    def update_git(self, force=False):
        """Git dali / degisiklik sayisi: arka planda, 15 sn'den eski kayitlar icin (Projeler sekmesi aciksa)."""
        if self.git_busy or self.hidden:
            return
        now = time.monotonic()
        visible = self.root.get_current_page() == TAB_PROJECTS
        roots = [r for r in self.proj_iters if r]
        stale = [r for r in roots
                 if r not in self.git_cache or (visible and (force or now - self.git_cache[r][0] > 15))][:40]
        if not stale:
            return
        self.git_busy = True

        def work():
            res = {root: git_status(root) for root in stale}
            GLib.idle_add(self.on_git, res)
        threading.Thread(target=work, daemon=True).start()

    def on_git(self, res):
        self.git_busy = False
        t = time.monotonic()
        for root, (branch, dirty) in res.items():
            self.git_cache[root] = (t, branch, dirty)
            it = self.proj_iters.get(root)
            if it is not None:
                self.pstore.set(it, [P_BRANCH, P_DIRTY], [branch, dirty])
        return False

    def update_projects(self, force_git=False):
        stats = {}
        for r in self.last_procs:
            root = self.proc_project.get(r["pid"], "")
            s = stats.setdefault(root, [0, 0.0, 0, 0])
            s[0] += 1
            s[1] += r["cpu"]
            s[2] += r["pss"] or r["rss"]
            s[3] += r["dev"]
        for root, (n, cpu, rss, dn) in stats.items():
            it = self.proj_iters.get(root)
            if it is None:
                name = os.path.basename(root) if root else _("(Proje dışı)")
                _t, branch, dirty = self.git_cache.get(root, (0, "", -1))
                self.proj_iters[root] = self.pstore.append([root, name, shorten_home(root), n, cpu, rss, dn,
                                                            branch, dirty])
            else:
                self.pstore.set(it, [P_COUNT, P_CPU, P_MEM, P_DEV], [n, cpu, rss, dn])
        for root in [x for x in self.proj_iters if x not in stats]:
            self.pstore.remove(self.proj_iters.pop(root))
        projects = [v for k, v in stats.items() if k]
        self.proj_status.set_text(
            _("Proje: {n}   │   projelerin toplam RAM'i: {size}   (PSS: paylaşılan bellek süreçlere "
              "bölüştürülür)").format(n=len(projects), size=fmt_bytes(sum(v[2] for v in projects))))
        self.update_git(force_git)

    def project_selected(self):
        model, paths = self.ptree.get_selection().get_selected_rows()
        return [(model[p][P_ROOT], model[p][P_NAME]) for p in paths]

    def project_show(self):
        sel = self.project_selected()
        if not sel:
            self.flash(_("Önce listeden bir proje seç."))
            return
        self.root.set_current_page(TAB_PROCESSES)
        self.search.set_text("proje:" + shorten_home(sel[0][0]))
        self.search.grab_focus()

    def project_action(self, sig):
        sel = self.project_selected()
        if not sel:
            self.flash(_("Önce listeden bir proje seç."))
            return
        if any(not root for root, _n in sel):
            self.flash(_("'Proje dışı' grubu sistem ve masaüstü süreçlerini içerir, toplu işlem yapılamaz."))
            sel = [s for s in sel if s[0]]
            if not sel:
                return
        roots = {root for root, _n in sel}
        mine = {r["pid"]: r for r in self.last_procs if r["uid"] == ME}
        pids = [pid for pid, root in self.proc_project.items() if root in roots and pid in mine]
        if not pids:
            self.flash(_("Bu projede işlem yapılacak süreç yok."))
            return
        if sig in (signal.SIGSTOP, signal.SIGCONT):
            ok, errs = kill_pids(pids, sig, False)
            word = _("donduruldu") if sig == signal.SIGSTOP else _("devam ettirildi")
            self.flash(_("{ok} süreç {word}.").format(ok=ok, word=word) + (_("  Hata: {v}").format(v=errs[0]) if errs else ""))
            GLib.timeout_add(800, self.refresh_once)
            return
        mem = sum(mine[p]["pss"] or mine[p]["rss"] for p in pids)
        detail = "\n".join(f"• {name}" for _r, name in sel[:10])
        detail += (_("\n\n{n} süreç ({size} RAM) {name} alacak. Bu projenin dizininde çalışan editör eklentileri "
                     "/ dil sunucuları da dahildir.").format(n=len(pids), size=fmt_bytes(mem), name=signal.Signals(sig).name))
        if self.confirm(_("{n} projenin süreçleri sonlandırılsın mı?").format(n=len(sel)), detail, _("Sonlandır")):
            self.send_signal(pids, sig, False)

    # -------------------------------------------------------------- portlar sekmesi
    def build_ports(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.ports_search = Gtk.SearchEntry(placeholder_text=_("Ara: port, süreç, proje, konteyner…"))
        self.ports_search.connect("search-changed", self.on_ports_search)
        bar.pack_start(self.ports_search, True, True, 0)
        bar.pack_start(self.make_priv_toggle(_("Yetkili okuma")), False, False, 0)
        b_kill = Gtk.Button(label=_("Zorla sonlandır"))
        b_kill.get_style_context().add_class("destructive-action")
        b_kill.set_tooltip_text(_("Süreç: SIGKILL · Docker: docker kill"))
        b_kill.connect("clicked", lambda _b: self.ports_end(signal.SIGKILL))
        bar.pack_end(b_kill, False, False, 0)
        b_term = Gtk.Button(label=_("Portu kapat"))
        b_term.set_tooltip_text(_("Süreç: SIGTERM · Docker konteyneri: docker stop"))
        b_term.connect("clicked", lambda _b: self.ports_end(signal.SIGTERM))
        bar.pack_end(b_term, False, False, 0)
        b_show = Gtk.Button(label=_("İşlemlerde göster"))
        b_show.connect("clicked", lambda _b: self.ports_show())
        bar.pack_end(b_show, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.nstore = Gtk.ListStore(*N_TYPES)
        self.nfilter = self.nstore.filter_new()
        self.nfilter.set_visible_func(
            lambda m, it, _d: not self.ports_query or self.ports_query in m.get_value(it, N_SEARCH))
        self.nsort = Gtk.TreeModelSort(model=self.nfilter)
        self.ntree = Gtk.TreeView(model=self.nsort)
        self.ntree.set_enable_search(False)
        self.ntree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.ntree.connect("row-activated", lambda *_a: self.ports_show())
        specs = [
            dict(key="port", title=_("Port"), col=N_PORT, sort=N_PORT, width=80, xalign=1.0, hideable=False),
            dict(key="proto", title=_("Protokol"), col=N_PROTO, sort=N_PROTO, width=85),
            dict(key="addr", title=_("Erişim"), col=N_ADDR, sort=N_ADDR, width=100),
            dict(key="pid", title=_("PID"), col=N_PID, sort=N_PID, width=80, xalign=1.0, func=self.port_pid_cell),
            dict(key="name", title=_("Süreç"), col=N_NAME, sort=N_NAME, width=200),
            dict(key="project", title=_("Proje"), col=N_PROJECT, sort=N_PROJECT, width=140),
            dict(key="docker", title=_("Docker konteyneri"), col=N_DOCKER, sort=N_DOCKER, width=170),
            dict(key="user", title=_("Kullanıcı"), col=N_USER, sort=N_USER, width=90),
            dict(key="cmd", title=_("Komut satırı"), col=N_CMD, sort=N_CMD, width=300, expand=True),
        ]
        self.ncols = ColumnSet(self.ntree, specs, self.settings.get("ports_columns"))
        self.ncols.apply_sort(self.nsort, self.settings.get("ports_sort"), ["port", "asc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.ntree)
        page.pack_start(scroll, True, True, 0)
        self.ports_status = Gtk.Label(label="", xalign=0)
        self.ports_status.set_margin_start(10)
        self.ports_status.set_margin_top(5)
        self.ports_status.set_margin_bottom(5)
        page.pack_start(self.ports_status, False, False, 0)
        return page

    def port_pid_cell(self, _col, r, model, it, _data):
        pid = model.get_value(it, N_PID)
        r.set_property("text", str(pid) if pid else "–")

    def on_ports_search(self, entry):
        self.ports_query = entry.get_text().strip().lower()
        self.nfilter.refilter()

    def refresh_docker_ports(self):
        """Docker'in yayinladigi portlari arka planda (en fazla 15 sn'de bir) ogrenir."""
        if self.docker_ports_busy or time.monotonic() - self.docker_ports_t < 15:
            return
        self.docker_ports_busy = True

        def work():
            mapping = docker_port_map()
            GLib.idle_add(self.on_docker_ports, mapping)
        threading.Thread(target=work, daemon=True).start()

    def on_docker_ports(self, mapping):
        self.docker_ports_busy = False
        self.docker_ports_t = time.monotonic()
        if mapping is not None:
            self.docker_ports = mapping
            self.update_ports()
        return False

    def update_ports(self):
        procs = {r["pid"]: r for r in self.last_procs}
        groups = {}
        for pid, port, proto, ip in self.last_socks:
            groups.setdefault((proto, port, pid or 0), set()).add(addr_label(ip))
        seen = set()
        unknown = 0
        for (proto, port, pid), addrs in groups.items():
            key = f"{proto}:{port}:{pid}"
            seen.add(key)
            r = procs.get(pid)
            docker = "" if pid else self.docker_ports.get(port, "")
            if pid:
                name = r["name"] if r else f"pid {pid}"
            else:
                name = "docker-proxy" if docker else _("(süreç bilgisi yok)")
                unknown += not docker
            proj = os.path.basename(self.proc_project.get(pid, "")) if pid else ""
            row = [key, proto, port, ", ".join(sorted(addrs)), pid, name, r["user"] if r else "", proj, docker,
                   r["cmd"] if r else "", f"{port} {name} {proj} {docker} {r['cmd'] if r else ''}".lower()]
            it = self.ports_iters.get(key)
            if it is None:
                self.ports_iters[key] = self.nstore.append(row)
            elif list(self.nstore[it]) != row:
                self.nstore.set(it, list(range(len(row))), row)
        for key in [k for k in self.ports_iters if k not in seen]:
            self.nstore.remove(self.ports_iters.pop(key))
        if self.root.get_current_page() == TAB_PORTS:
            self.refresh_docker_ports()
        extra = _("   │   {unknown} port root'a ait bir süreçte (sudo ss -ltnp ile görülür)").format(unknown=unknown) if unknown else ""
        self.ports_status.set_text(_("Dinleyen port: {n}{extra}").format(n=len(groups), extra=extra))

    def ports_selected(self):
        model, paths = self.ntree.get_selection().get_selected_rows()
        return [(model[p][N_PID], model[p][N_PORT], model[p][N_PROTO], model[p][N_DOCKER], model[p][N_NAME])
                for p in paths]

    def ports_show(self):
        sel = self.ports_selected()
        if not sel:
            self.flash(_("Önce listeden bir port seç."))
            return
        self.root.set_current_page(TAB_PROCESSES)
        self.search.set_text(f":{sel[0][1]}")

    def ports_end(self, sig):
        sel = self.ports_selected()
        if not sel:
            self.flash(_("Önce listeden bir port seç."))
            return
        pids = sorted({s[0] for s in sel if s[0]})
        dockers = sorted({s[3] for s in sel if not s[0] and s[3]})
        if not pids and not dockers:
            self.flash(_("Bu portun sahibi görünmüyor (root'a ait): sudo ss -ltnp ile bak."))
            return
        lines = [_("{port}/{proto} · {name}").format(port=port, proto=proto, name=name)
                 + (_(" ({pid})").format(pid=pid) if pid
                    else _(" → konteyner {docker}").format(docker=docker) if docker else "")
                 for pid, port, proto, docker, name in sel[:10]]
        title = _("{n} port kapatılsın mı?").format(n=len(sel))
        if not self.confirm(title, "\n".join(lines), _("Kapat")):
            return
        if pids:
            self.send_signal(pids, sig, False)
        if dockers:
            verb = "kill" if sig == signal.SIGKILL else "stop"
            self.flash(_("docker {verb}: {n} konteyner için çalışıyor…").format(verb=verb, n=len(dockers)), 60)

            def work():
                _out, err = _docker_run([verb, *dockers], 90)
                GLib.idle_add(self.after_docker_action, verb, len(dockers), err)
            threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- temizlik sekmesi
    def build_clean(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.clean_label = Gtk.Label(label="", xalign=0)
        self.clean_label.set_ellipsize(3)
        bar.pack_start(self.clean_label, True, True, 0)
        b_del = Gtk.Button(label=_("Seçilenleri sil"))
        b_del.get_style_context().add_class("destructive-action")
        b_del.set_tooltip_text(_("Seçili klasörleri diskten siler; build/restore ile yeniden oluşur"))
        b_del.connect("clicked", lambda _b: self.clean_delete())
        bar.pack_end(b_del, False, False, 0)
        b_scan = Gtk.Button(label=_("Yeniden tara"))
        b_scan.connect("clicked", lambda _b: self.clean_scan())
        bar.pack_end(b_scan, False, False, 0)
        b_reset = Gtk.Button(label=_("Varsayılan klasör"))
        b_reset.connect("clicked", lambda _b: self.clean_set_roots(list(DEFAULT_CLEAN_ROOTS)))
        bar.pack_end(b_reset, False, False, 0)
        b_add = Gtk.Button(label=_("Klasör ekle…"))
        b_add.connect("clicked", lambda _b: self.clean_add_root())
        bar.pack_end(b_add, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.cstore = Gtk.ListStore(*C_TYPES)
        self.csort = Gtk.TreeModelSort(model=self.cstore)
        self.ctree = Gtk.TreeView(model=self.csort)
        self.ctree.set_enable_search(False)
        self.ctree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        specs = [
            dict(key="project", title=_("Proje"), col=C_PROJECT, sort=C_PROJECT, width=220, hideable=False),
            dict(key="kind", title=_("Tür"), col=C_KIND, sort=C_KIND, width=160),
            dict(key="size", title=_("Boyut"), col=C_SIZE, sort=C_SIZE, width=110, xalign=1.0, func=self.clean_size_cell),
            dict(key="git", title=_("Git"), col=C_GIT, sort=C_GIT, width=140),
            dict(key="path", title=_("Yol"), col=C_SHOWN, sort=C_SHOWN, width=500, expand=True),
        ]
        self.ccols = ColumnSet(self.ctree, specs, self.settings.get("clean_columns"))
        self.ccols.apply_sort(self.csort, self.settings.get("clean_sort"), ["size", "desc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.ctree)
        page.pack_start(scroll, True, True, 0)

        dock = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(dock, f"set_margin_{m}")(8)
        self.docker_df_label = Gtk.Label(label=_("Docker: –"), xalign=0)
        self.docker_df_label.set_ellipsize(3)
        dock.pack_start(self.docker_df_label, True, True, 0)
        b_cache = Gtk.Button(label=_("Build önbelleğini sil"))
        b_cache.set_tooltip_text(_("docker builder prune -f"))
        b_cache.connect("clicked", lambda _b: self.docker_prune("builder", _("Build önbelleği")))
        dock.pack_end(b_cache, False, False, 0)
        b_img = Gtk.Button(label=_("Boşta imajları sil"))
        b_img.set_tooltip_text(_("docker image prune -f (etiketsiz / kullanılmayan 'dangling' imajlar)"))
        b_img.connect("clicked", lambda _b: self.docker_prune("image", _("Boşta imajlar")))
        dock.pack_end(b_img, False, False, 0)
        page.pack_start(Gtk.Separator(), False, False, 0)
        page.pack_start(dock, False, False, 0)

        self.clean_status = Gtk.Label(label="", xalign=0)
        self.clean_status.set_margin_start(10)
        self.clean_status.set_margin_top(5)
        self.clean_status.set_margin_bottom(5)
        page.pack_start(self.clean_status, False, False, 0)
        self.update_clean_label()
        return page

    def clean_size_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, C_SIZE)
        r.set_property("text", fmt_bytes(v))
        heat(r, v / (1 << 30))

    def update_clean_label(self):
        self.clean_label.set_text(_("Taranan klasörler: ") + ", ".join(shorten_home(r) for r in self.clean_roots))

    def clean_set_roots(self, roots):
        self.clean_roots = roots
        self.update_clean_label()
        self.clean_scan()

    def clean_add_root(self):
        dlg = Gtk.FileChooserDialog(title=_("Taranacak klasörü seç"), transient_for=self.toplevel(),
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons(_("İptal"), Gtk.ResponseType.CANCEL, _("Ekle"), Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            folder = dlg.get_filename()
            dlg.destroy()
            if folder not in self.clean_roots:
                self.clean_set_roots(self.clean_roots + [folder])
        else:
            dlg.destroy()

    def clean_scan(self):
        """Arka planda tarar; ilk gosterimde ve 'Yeniden tara'da calisir (surekli degil)."""
        if self.clean_busy:
            return
        self.clean_busy = True
        self.clean_scanned = True
        self.clean_status.set_text(_("Taranıyor… (büyük klasörlerde birkaç saniye sürebilir)"))
        roots = list(self.clean_roots)

        def work():
            found = scan_clean(roots)
            paths = [p for p, _pr, _k in found]
            sizes = dir_sizes(paths)
            GLib.idle_add(self.on_clean_done, found, sizes, tracked_dirs(paths))
        threading.Thread(target=work, daemon=True).start()
        self.docker_df_refresh()

    def on_clean_done(self, found, sizes, tracked=()):
        self.clean_busy = False
        self.clean_tracked = set(tracked)
        self.cstore.clear()
        self.clean_iters.clear()
        for path, proj, kind in found:
            self.clean_iters[path] = self.cstore.append([path, proj, kind, sizes.get(path, 0), shorten_home(path),
                                                         _("git'te izleniyor") if path in self.clean_tracked else ""])
        self.update_clean_status()
        return False

    def update_clean_status(self):
        total = sum(r[C_SIZE] for r in self.cstore)
        self.clean_status.set_text(_("{n} klasör  │  toplam {size} geri kazanılabilir").format(n=len(self.cstore), size=fmt_bytes(total)))

    def clean_selected(self):
        model, paths = self.ctree.get_selection().get_selected_rows()
        return [(model[p][C_PATH], model[p][C_PROJECT], model[p][C_KIND], model[p][C_SIZE]) for p in paths]

    def clean_delete(self):
        sel = self.clean_selected()
        if not sel:
            self.flash(_("Önce silinecek klasörleri seç (Ctrl+A hepsini seçer)."))
            self.clean_status.set_text(_("Önce silinecek klasörleri seç."))
            return
        busy = busy_dirs([p for p, *_r in sel])
        total = sum(s[3] for s in sel)
        listing = "\n".join(_("{kind} · {proj} · {size}").format(kind=kind, proj=proj, size=fmt_bytes(size)) for _p, proj, kind, size in sel[:8])
        if len(sel) > 8:
            listing += _("\n… ve {v} klasör daha").format(v=len(sel) - 8)
        detail = listing + _("\n\nBu klasörler build / restore ile yeniden oluşur; kaynak kodun silinmez.")
        if busy:
            names = ", ".join(sorted({_("{n} ({pid})").format(n=n, pid=pid) for v in busy.values() for pid, n in v})[:5])
            detail += _("\n\nÇalışma dizini bu klasörlerde olan süreçler var, bu klasörler atlanır: {names}").format(names=names)
        tracked = {p for p, *_r in sel if p in self.clean_tracked}
        if tracked:
            detail += (_("\n\n{n} klasörde git'in izlediği dosyalar var (depoya eklenmiş); bunlar silinmez.").format(n=len(tracked)))
        if not self.confirm(_("{n} klasör silinsin mi? ({size})").format(n=len(sel), size=fmt_bytes(total)), detail, _("Sil")):
            return
        todo = [s for s in sel if s[0] not in busy and s[0] not in tracked]
        roots = list(self.clean_roots)
        self.clean_status.set_text(_("Siliniyor…"))

        def work():
            results = [(p, size, *safe_delete(p, roots)) for p, _pr, _k, size in todo]
            GLib.idle_add(self.on_clean_deleted, results, len(sel) - len(todo))
        threading.Thread(target=work, daemon=True).start()

    def on_clean_deleted(self, results, skipped):
        freed = 0
        errs = []
        for path, size, ok, err in results:
            if ok:
                freed += size
                it = self.clean_iters.pop(path, None)
                if it is not None:
                    self.cstore.remove(it)
            else:
                errs.append(f"{os.path.basename(os.path.dirname(path))}/{os.path.basename(path)}: {err}")
        msg = _("{size} boşaltıldı.").format(size=fmt_bytes(freed))
        if skipped:
            msg += _("  {skipped} klasör kullanımda ya da git'te izlendiği için atlandı.").format(skipped=skipped)
        if errs:
            msg += _("  Hata: ") + "; ".join(errs[:2])
        self.flash(msg, 10)
        self.update_clean_status()
        return False

    def docker_df_refresh(self):
        def work():
            rows, err = docker_df()
            GLib.idle_add(self.on_docker_df, rows, err)
        threading.Thread(target=work, daemon=True).start()

    def on_docker_df(self, rows, err):
        names = {"Images": _("İmaj"), "Containers": _("Konteyner"), "Local Volumes": _("Volume"),
                 "Build Cache": _("Build önbelleği")}
        if err:
            self.docker_df_label.set_text(_("Docker: erişilemiyor ({err})").format(err=err))
        else:
            parts = [_("{get}: {get2} · {get3} (kurtarılabilir {get4})").format(get=names.get(r.get('Type'), r.get('Type')), get2=r.get('TotalCount'),
                       get3=r.get('Size'), get4=r.get('Reclaimable')) for r in rows]
            self.docker_df_label.set_text(_("Docker: ") + "   │   ".join(parts))
        return False

    def docker_prune(self, what, label):
        if not self.confirm(_("{label} silinsin mi?").format(label=label),
                            _("Yalnızca kullanılmayan öğeler silinir; çalışan konteynerler ve volume'lar etkilenmez."),
                            _("Sil")):
            return
        self.flash(_("docker {what} prune çalışıyor…").format(what=what), 120)

        def work():
            out, err = _docker_run([what, "prune", "-f"], 300)
            last = (out.strip().splitlines() or [""])[-1]
            GLib.idle_add(self.after_prune, label, err or last)
        threading.Thread(target=work, daemon=True).start()

    def after_prune(self, label, text):
        self.flash(_("{label}: {text}").format(label=label, text=text), 10)
        self.docker_df_refresh()
        return False

    # -------------------------------------------------------------- dosya / klasor sahibi
    # -------------------------------------------------------------- ayarlar penceresi
    def open_settings(self):
        if self.settings_win is not None:
            self.settings_win.present()
            return
        dlg = self.settings_win = Gtk.Dialog(title=_("Ayarlar"))
        if self.toplevel():
            dlg.set_transient_for(self.toplevel())
        dlg.set_default_size(580, 700)
        dlg.add_button(_("Kapat"), Gtk.ResponseType.CLOSE)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.connect("destroy", self.on_settings_closed)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        scroll.add(box)
        dlg.get_content_area().pack_start(scroll, True, True, 0)
        widgets = dlg.test_parts = {}

        def section(title):
            lbl = Gtk.Label(label="", xalign=0)
            lbl.set_markup(f"<span size='large' weight='bold'>{title}</span>")
            lbl.set_margin_top(10)
            box.pack_start(lbl, False, False, 0)

        def note(text):
            lbl = Gtk.Label(label=text, xalign=0, wrap=True)
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(lbl, False, False, 0)
            return lbl

        def check(text, key, tip=None):
            chk = Gtk.CheckButton(label=text)
            chk.set_active(self.pref(key))
            if tip:
                chk.set_tooltip_text(tip)
            chk.connect("toggled", lambda b: self.set_pref(key, b.get_active()))
            box.pack_start(chk, False, False, 0)
            widgets[key] = chk
            return chk

        def spin_row(text, key, unit):
            row = Gtk.Box(spacing=8)
            row.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
            lo, hi = PREF_RANGE[key]
            sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
            sp.set_value(self.pref(key))
            sp.connect("value-changed", lambda b: self.set_pref(key, int(b.get_value())))
            row.pack_start(sp, False, False, 0)
            row.pack_start(Gtk.Label(label=unit, xalign=0), False, False, 0)
            box.pack_start(row, False, False, 0)
            widgets[key] = sp

        section(_("Dil / Language"))
        self.build_language_row(box, widgets, note)

        section(_("Profil"))
        note(_("Hangi teknolojilerle çalışıyorsan işaretle (birden fazla seçilebilir). Profil; hangi süreçlerin "
             "'geliştirme süreci' sayıldığını, boşta bırakılınca kapatılabilecek artıkları ve Temizlik sekmesinde "
             "taranacak klasörleri belirler."))
        self.build_profile_checks(box, widgets)

        section(_("Geçmiş"))
        check(_("Geçmişi diske kaydet"), "log_enabled",
              _("Sistem yükü ~10 sn'de, en çok kaynak kullanan süreçler dakikada bir (takılma anında 10 sn'de bir) yazılır"))
        spin_row(_("Kayıtları şu kadar gün sakla"), "log_days", _("gün"))
        spin_row(_("Takılma eşiği (baskı)"), "stall_psi", "%")
        self.hist_info_label = note("")
        widgets["hist_info"] = self.hist_info_label
        note(_("Süresi dolan kayıtlar açılışta ve saatte bir otomatik silinir. Süreyi değiştirince hemen silinmesini "
             "istiyorsan aşağıdaki düğmeyi kullan."))
        row = Gtk.Box(spacing=8)
        b_prune = Gtk.Button(label=_("Şimdi eski kayıtları sil"))
        b_prune.set_tooltip_text(_("Saklama süresinden eski kayıtları şimdi siler"))
        b_prune.connect("clicked", lambda _b: self.settings_prune())
        b_clear = Gtk.Button(label=_("Tüm geçmişi sil"))
        b_clear.get_style_context().add_class("destructive-action")
        b_clear.connect("clicked", lambda _b: self.settings_clear())
        row.pack_start(b_prune, False, False, 0)
        row.pack_start(b_clear, False, False, 0)
        box.pack_start(row, False, False, 0)
        widgets["prune"], widgets["clear"] = b_prune, b_clear

        section(_("Arka plan"))
        check(_("Pencere kapanınca arka planda çalışmaya devam et"), "bg_run",
              _("Kapalıysa pencereyi kapatmak uygulamadan çıkar ve geçmiş kaydı durur"))
        auto = Gtk.CheckButton(label=_("Oturum açılınca arka planda başlat"))
        auto.set_active(tm_services.self_autostart_enabled())
        auto.connect("toggled", self.on_self_autostart)
        box.pack_start(auto, False, False, 0)
        widgets["autostart"] = auto

        section(_("Bildirimler"))
        check(_("RAM dolmaya yaklaşınca masaüstü bildirimi gönder"), "notify_mem")
        spin_row(_("RAM doluluk eşiği"), "notify_mem_pct", "%")
        check(_("Sistem takılınca suçluyu bildir (tek tıkla sonlandırma düğmesiyle)"), "notify_stall",
              _("Baskı (PSI) 'Takılma eşiği'nin üstünde birkaç saniye kalırsa; en çok kaynak yiyen kendi sürecin bildirilir. "
              "Korunanlar ve sistem süreçleri hiç önerilmez. En sık 5 dakikada bir."))
        check(_("Disk dolmaya yaklaşınca bildirim gönder"), "notify_disk",
              _("Kök disk ve ev dizininin diski için; dakikada bir kontrol, aynı disk için 6 saatte bir bildirim"))
        spin_row(_("Disk doluluk eşiği"), "notify_disk_pct", "%")

        section(_("Korunan süreçler"))
        note(_("Her satıra bir ad, komut parçası ya da proje adı yaz (büyük/küçük harf fark etmez). Eşleşen süreçler "
             "otomatik temizlikte kapatılmaz, takılma bildiriminde önerilmez; elle sonlandırırken uyarı çıkar. "
             "Örnek: LosevApi"))
        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_size_request(-1, 90)
        tv.get_buffer().set_text("\n".join(self.protect_patterns()))
        tv.get_buffer().connect("changed", self.on_protect_changed)
        frame = Gtk.Frame()
        frame.add(tv)
        box.pack_start(frame, False, False, 0)
        widgets["protect"] = tv

        section(_("Otomatik temizlik"))
        check(_("Boşta kalan artık süreçleri otomatik kapat (profile göre)"), "auto_idle",
              _("MSBuild node, VBCSCompiler, Gradle daemon vb. Yalnızca RAM aşağıdaki eşiğin üstündeyken ve süreç "
              "belirtilen süre boyunca boştayken (CPU < %2)"))
        spin_row(_("Şu kadar dakika boşta kalırsa"), "auto_idle_min", "dk")
        spin_row(_("RAM doluluğu en az"), "auto_idle_mem", _("%  (0 = her zaman)"))
        self.shutdown_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.pack_start(self.shutdown_box, False, False, 0)
        self.fill_shutdown_box()

        section(_("Yetkili okuma"))
        box.pack_start(self.make_priv_toggle(_("Yetkili okuma (root, salt okunur)")), False, False, 0)
        note(_("Parola sorar. Başka kullanıcıların süreçleri için port, bellek (PSS), disk G/Ç ve dosya sahibi bilgisini "
             "gösterir. Her oturumda yeniden açılır."))

        b_quit = Gtk.Button(label=_("Tamamen çık"))
        b_quit.set_halign(Gtk.Align.START)
        b_quit.set_margin_top(14)
        b_quit.set_tooltip_text(_("Uygulamadan çıkar; geçmiş kaydı durur (Ctrl+Q)"))
        b_quit.connect("clicked", lambda _b: self.quit_app())
        box.pack_start(b_quit, False, False, 0)
        widgets["quit"] = b_quit
        dlg.show_all()
        self.update_history_info()

    def build_language_row(self, box, widgets, note):
        """Dil seciminde ayar kaydedilir ve uygulama kendini yeniden baslatir (dil yalnizca acilista okunur)."""
        started = self.settings.get("language", "auto")
        started = started if started in tm_i18n.CODES else "auto"
        combo = Gtk.ComboBoxText()
        for code, name in [("auto", _("Sistem dili"))] + tm_i18n.LANGUAGES:
            combo.append(code, name)
        combo.set_active_id(started)
        combo.set_halign(Gtk.Align.START)
        box.pack_start(combo, False, False, 0)

        def changed(c):
            code = c.get_active_id() or "auto"
            self.set_pref("language", code)
            if code != started:
                self.flash(_("Dil değiştiriliyor, uygulama yeniden başlıyor…"), 10)
                GLib.timeout_add(250, self.restart_app)
        combo.connect("changed", changed)
        widgets["language"] = combo

    def build_profile_checks(self, box, widgets):
        def toggled(_chk):
            ids = [pid for pid, c in widgets["profiles"].items() if c.get_active()]
            self.apply_profiles(ids)

        widgets["profiles"] = {}
        for pid, prof in self.profiles.items():
            chk = Gtk.CheckButton(label=prof["name"] + (_("  (özel)") if prof.get("custom") else ""))
            chk.set_tooltip_text(prof["desc"])
            chk.set_active(pid in ACTIVE.ids)
            chk.connect("toggled", toggled)
            box.pack_start(chk, False, False, 0)
            widgets["profiles"][pid] = chk
            lbl = Gtk.Label(label=prof["desc"], xalign=0)
            lbl.get_style_context().add_class("dim-label")
            lbl.set_margin_start(26)
            box.pack_start(lbl, False, False, 0)

    def on_protect_changed(self, buf):
        if getattr(self, "_protect_timer", None):
            GLib.source_remove(self._protect_timer)
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self._protect_timer = GLib.timeout_add(600, self.save_protect, text)

    def save_protect(self, text):
        self._protect_timer = None
        self.settings["protect"] = tm_alerts.parse_patterns(text)
        self.save_settings(self.win)
        return False

    def on_settings_closed(self, _dlg):
        self.shutdown_box = None
        self.settings_win = None
        self.hist_info_label = None

    def on_self_autostart(self, chk):
        path = os.path.realpath(__file__)
        err = tm_services.write_self_autostart(tm_services.desktop_exec(sys.executable, path, "--background"),
                                               chk.get_active())
        if err:
            self.flash(_("Oturum açılışı ayarı yazılamadı: {err}").format(err=err), 10)

    def update_history_info(self):
        h = self.history
        if h is None or self.hist_info_label is None:
            return

        def work():
            try:
                size, span = h.size(), h.span()
            except sqlite3.Error:
                size, span = 0, None
            GLib.idle_add(self.show_history_info, size, span)
        threading.Thread(target=work, daemon=True).start()

    def show_history_info(self, size, span):
        if self.hist_info_label is not None:
            if span:
                self.hist_info_label.set_text(_("Veritabanı: {size}  │  {v} kayıt  │  {v2} – {v3}").format(size=fmt_bytes(size), v=span[2], v2=fmt_ts(span[0]),
                                                v3=fmt_ts(span[1])))
            else:
                self.hist_info_label.set_text(_("Veritabanı: {size}  │  henüz kayıt yok").format(size=fmt_bytes(size)))
        return False

    def settings_prune(self):
        days = self.pref("log_days")
        if not self.confirm(_("{days} günden eski kayıtlar silinsin mi?").format(days=days), _("Bu işlem geri alınamaz."), _("Sil")):
            return
        self.prune_history(days, self.after_settings_delete)

    def settings_clear(self):
        h = self.history
        if h is None or not self.confirm(_("Tüm geçmiş silinsin mi?"), _("Kaydedilmiş bütün kayıtlar silinir."),
                                         _("Hepsini sil")):
            return

        def work():
            try:
                h.clear()
            except sqlite3.Error:
                pass
            GLib.idle_add(self.after_settings_delete, 0)
        threading.Thread(target=work, daemon=True).start()

    def after_settings_delete(self, n):
        self.flash(_("Geçmiş silindi.") if n <= 0 else _("{n} eski kayıt silindi.").format(n=n), 8)
        self.update_history_info()
        if self.root.get_current_page() == TAB_HISTORY:
            self.refresh_history()
        return False

    def open_owner_finder(self):
        if self.finder is not None:
            self.finder.present()
            return
        win = self.finder = Gtk.Window(title=_("Dosya / klasör sahibini bul"))
        win.set_default_size(880, 460)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(10)
        top = Gtk.Box(spacing=8)
        entry = Gtk.Entry(placeholder_text=_("Dosya ya da klasör yolu (ör. ~/Desktop/proje/bin/Debug)"))
        entry.set_hexpand(True)
        btn_file = Gtk.Button(label=_("Dosya seç…"))
        btn_dir = Gtk.Button(label=_("Klasör seç…"))
        btn_go = Gtk.Button(label=_("Ara"))
        btn_go.get_style_context().add_class("suggested-action")
        for w in (entry, btn_file, btn_dir, self.make_priv_toggle(_("Yetkili (root) tara")), btn_go):
            top.pack_start(w, w is entry, True, 0)
        box.pack_start(top, False, False, 0)

        store = Gtk.ListStore(int, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, (title, width) in enumerate(((_("PID"), 70), (_("Ad"), 170), (_("Nasıl kullanıyor"), 150), (_("Yol"), 400))):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(ellipsize=3), text=i)
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_sort_column_id(i)
            tv.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.add(tv)
        box.pack_start(scroll, True, True, 0)

        info = Gtk.Label(label=_("Bir yol seç; o yolu açık tutan, çalışma dizini yapan ya da belleğe yükleyen "
                               "süreçler listelenir."), xalign=0, wrap=True)
        box.pack_start(info, False, False, 0)
        bottom = Gtk.Box(spacing=8)
        b_show = Gtk.Button(label=_("İşlemlerde göster"))
        b_term = Gtk.Button(label=_("Görevi sonlandır"))
        b_kill = Gtk.Button(label=_("Zorla sonlandır"))
        b_kill.get_style_context().add_class("destructive-action")
        bottom.pack_start(b_show, False, False, 0)
        bottom.pack_end(b_kill, False, False, 0)
        bottom.pack_end(b_term, False, False, 0)
        box.pack_start(bottom, False, False, 0)
        win.add(box)

        def selected():
            model, paths = tv.get_selection().get_selected_rows()
            return [(model[p][0], model[p][1]) for p in paths]

        def search(*_a):
            path = os.path.expanduser(entry.get_text().strip())
            if not path or not os.path.exists(path):
                info.set_text(_("Bu yol bulunamadı."))
                return
            btn_go.set_sensitive(False)
            info.set_text(_("Taranıyor…"))

            def work():
                helper, found = self.priv_helper, None
                if helper is not None and helper.alive:
                    try:
                        found, denied = helper.request("owners", timeout=180, path=path)
                    except HelperError:
                        found = None
                if found is None:
                    found, denied = find_owners(path)
                GLib.idle_add(done, found, denied)
            threading.Thread(target=work, daemon=True).start()

        def done(found, denied):
            btn_go.set_sensitive(True)
            store.clear()
            for row in found:
                kind_row = list(row)
                kind_row[2] = _(kind_row[2])
                store.append(kind_row)
            msg = _("{n} süreç bu yolu kullanıyor.").format(n=len(found)) if found else _("Bu yolu kullanan bir süreç bulunamadı.")
            if denied:
                msg += _("  ({denied} sürecin dosyaları okunamadı; başka kullanıcılara ait olabilir – 'Yetkili "
                         "tara'yı aç.)").format(denied=denied)
            info.set_text(msg)
            return False

        def choose(action, title):
            dlg = Gtk.FileChooserDialog(title=title, transient_for=win, action=action)
            dlg.add_buttons(_("İptal"), Gtk.ResponseType.CANCEL, _("Seç"), Gtk.ResponseType.OK)
            if dlg.run() == Gtk.ResponseType.OK:
                entry.set_text(dlg.get_filename())
                dlg.destroy()
                search()
            else:
                dlg.destroy()

        def show_in_list():
            sel = selected()
            if sel:
                self.root.set_current_page(TAB_PROCESSES)
                self.search.set_text(str(sel[0][0]))
                if self.win:
                    self.win.present()

        def end(sig):
            sel = selected()
            if not sel:
                info.set_text(_("Önce listeden bir süreç seç."))
                return
            listing = "\n".join(f"{pid}  {name}" for pid, name in sel[:10])
            if not self.confirm(_("{n} süreç {name} ile sonlandırılsın mı?").format(n=len(sel), name=signal.Signals(sig).name), listing,
                                _("Sonlandır")):
                return
            ok, errs = kill_pids([p for p, _n in sel], sig, False)
            info.set_text(_("{name}: {ok} sürece gönderildi.").format(name=signal.Signals(sig).name, ok=ok) + (_("  Hata: {v}").format(v=errs[0]) if errs else ""))
            GLib.timeout_add(900, lambda: search() or False)

        entry.connect("activate", search)
        btn_go.connect("clicked", search)
        btn_file.connect("clicked", lambda _b: choose(Gtk.FileChooserAction.OPEN, _("Dosya seç")))
        btn_dir.connect("clicked", lambda _b: choose(Gtk.FileChooserAction.SELECT_FOLDER, _("Klasör seç")))
        b_show.connect("clicked", lambda _b: show_in_list())
        b_term.connect("clicked", lambda _b: end(signal.SIGTERM))
        b_kill.connect("clicked", lambda _b: end(signal.SIGKILL))
        tv.connect("row-activated", lambda *_a: show_in_list())
        win.connect("destroy", lambda _w: setattr(self, "finder", None))
        win.show_all()
        entry.grab_focus()
        win.test_parts = (entry, store, info, btn_go)  # testler icin

    # -------------------------------------------------------------- servisler sekmesi
    def build_services(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.svc_search = Gtk.SearchEntry(placeholder_text=_("Ara: servis, uygulama, açıklama…"))
        self.svc_search.connect("search-changed", self.on_svc_search)
        bar.pack_start(self.svc_search, True, True, 0)
        for label, verb, tip in ((_("Yenile"), None, _("Listeyi yenile (F5)")),
                                 (_("Başlangıçta kapat"), "disable", _("Oturum açılınca başlamasın")),
                                 (_("Başlangıçta aç"), "enable", _("Oturum açılınca başlasın")),
                                 (_("Yeniden başlat"), "restart", None), (_("Durdur"), "stop", None),
                                 (_("Başlat"), "start", None)):
            b = Gtk.Button(label=label)
            if tip:
                b.set_tooltip_text(tip)
            if verb is None:
                b.connect("clicked", lambda _b: self.services_refresh())
            else:
                b.connect("clicked", lambda _b, v=verb: self.service_action(v))
            bar.pack_end(b, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.sstore = Gtk.ListStore(*S_TYPES)
        self.sfilter = self.sstore.filter_new()
        self.sfilter.set_visible_func(
            lambda m, it, _d: not self.svc_query or self.svc_query in m.get_value(it, S_SEARCH))
        self.ssort = Gtk.TreeModelSort(model=self.sfilter)
        self.stree = Gtk.TreeView(model=self.ssort)
        self.stree.set_enable_search(False)
        self.stree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        specs = [
            dict(key="name", title=_("Ad"), col=S_NAME, sort=S_NAME, width=280, hideable=False),
            dict(key="kind", title=_("Tür"), col=S_KIND, sort=S_KIND, width=150),
            dict(key="state", title=_("Durum"), col=S_STATE, sort=S_STATE, width=120, func=self.svc_state_cell),
            dict(key="boot", title=_("Başlangıçta"), col=S_BOOT, sort=S_BOOT, width=110),
            dict(key="mem", title=_("Bellek"), col=S_MEM, sort=S_MEM, width=100, xalign=1.0, func=self.svc_mem_cell),
            dict(key="desc", title=_("Açıklama / komut"), col=S_DESC, sort=S_DESC, width=400, expand=True),
        ]
        self.scols = ColumnSet(self.stree, specs, self.settings.get("services_columns"))
        self.scols.apply_sort(self.ssort, self.settings.get("services_sort"), ["state", "asc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.stree)
        page.pack_start(scroll, True, True, 0)
        self.svc_status = Gtk.Label(label=_("Servisler sekmesi açılınca yüklenir."), xalign=0)
        for m in ("start", "end"):
            getattr(self.svc_status, f"set_margin_{m}")(10)
        self.svc_status.set_margin_top(5)
        self.svc_status.set_margin_bottom(5)
        page.pack_start(self.svc_status, False, False, 0)
        return page

    def svc_state_cell(self, _col, r, model, it, _data):
        st = model.get_value(it, S_STATE)
        label, color = SERVICE_STATES.get(st, (st or "–", None))
        r.set_property("text", ("● " if st == "active" else "") + label)
        if color:
            r.set_property("foreground", color)
        else:
            r.set_property("foreground-set", False)

    def svc_mem_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, S_MEM)
        r.set_property("text", fmt_bytes(v) if v else "–")
        heat(r, v / (2 << 30))

    def on_svc_search(self, entry):
        self.svc_query = entry.get_text().strip().lower()
        self.sfilter.refilter()

    def services_refresh(self):
        if self.svc_busy:
            return
        self.svc_busy = True

        def work():
            units, err = tm_services.list_user_services()
            apps = tm_services.list_autostart()
            GLib.idle_add(self.on_services, units, apps, err)
        threading.Thread(target=work, daemon=True).start()

    def on_services(self, units, apps, err):
        self.svc_busy = False
        rows = {}
        for u in units:
            rows["svc:" + u["unit"]] = [u["unit"], _("Kullanıcı servisi"), u["active"],
                                        BOOT_TR.get(u["enabled"], u["enabled"] or "–"), u["mem"], u["desc"]]
        for a in apps:
            rows["app:" + a["file"]] = [a["name"], _("Başlangıç uygulaması") + (_(" (sistem)") if a["system"] else ""), "",
                                        _("açık") if a["enabled"] else _("kapalı"), 0, a["exec"]]
        for key, (name, kind, state, boot, mem, desc) in rows.items():
            search = f"{name} {kind} {desc}".lower()
            it = self.svc_iters.get(key)
            if it is None:
                self.svc_iters[key] = self.sstore.append([key, name, kind, state, boot, mem, desc, search])
            else:
                self.sstore.set(it, [S_NAME, S_KIND, S_STATE, S_BOOT, S_MEM, S_DESC, S_SEARCH],
                                [name, kind, state, boot, mem, desc, search])
        for key in [k for k in self.svc_iters if k not in rows]:
            self.sstore.remove(self.svc_iters.pop(key))
        running = sum(1 for r in rows.values() if r[2] == "active")
        failed = sum(1 for r in rows.values() if r[2] == "failed")
        text = (_("Servis: {n} ({running} çalışıyor)   │   başlangıç uygulaması: {n2}").format(n=len(units), running=running, n2=len(apps))
                + (_("   │   {failed} başarısız").format(failed=failed) if failed else ""))
        if err:
            text += _("   │   systemctl hatası: {err}").format(err=err)
        self.svc_status.set_text(text)
        return False

    def services_selected(self):
        model, paths = self.stree.get_selection().get_selected_rows()
        return [(model[p][S_KEY], model[p][S_NAME]) for p in paths]

    def service_action(self, verb):
        sel = self.services_selected()
        if not sel:
            self.flash(_("Önce listeden bir servis ya da uygulama seç."))
            return
        if verb in ("start", "stop", "restart"):
            sel = [x for x in sel if x[0].startswith("svc:")]
            if not sel:
                self.flash(_("Başlat / durdur / yeniden başlat yalnızca servisler için; uygulamalarda 'Başlangıçta aç/kapat' kullan."))
                return
        if verb in ("stop", "restart", "disable"):
            prot = [n for k, n in sel if k.startswith("svc:") and k[4:].startswith(PROTECTED_SERVICES)]
            if prot:
                self.flash(_("Korumalı servis (oturumu bozar): {items}").format(items=', '.join(prot)), 8)
                sel = [x for x in sel if x[1] not in prot]
                if not sel:
                    return
        if verb in ("stop", "disable"):
            what = _("durdurulsun") if verb == "stop" else _("başlangıçta kapatılsın")
            listing = "\n".join(n for _k, n in sel[:10])
            if not self.confirm(_("{n} öğe {what} mı?").format(n=len(sel), what=what), listing, _("Evet")):
                return

        def work():
            errs = []
            for key, name in sel:
                if key.startswith("svc:"):
                    err = tm_services.service_action(key[4:], verb)
                else:
                    err = tm_services.set_autostart(key[4:], verb == "enable")
                if err:
                    errs.append(f"{name}: {err}")
            GLib.idle_add(self.after_service_action, verb, len(sel), errs)
        threading.Thread(target=work, daemon=True).start()

    def after_service_action(self, verb, n, errs):
        words = {"start": _("başlatıldı"), "stop": _("durduruldu"), "restart": _("yeniden başlatıldı"),
                 "enable": _("başlangıçta açıldı"), "disable": _("başlangıçta kapatıldı")}
        msg = _("{v} öğe {v2}.").format(v=n - len(errs), v2=words[verb]) + (_("  Hata: {v}").format(v=errs[0]) if errs else "")
        self.flash(msg, 8)
        self.svc_busy = False
        self.services_refresh()
        return False

    # -------------------------------------------------------------- periyodik
    def tick(self):
        self.tick_n += 1
        now = time.monotonic()
        dt = max(0.001, now - self.last_t)
        self.last_t = now

        per = psutil.cpu_percent(percpu=True)
        self.cpu_pct = sum(per) / len(per)
        self.g_cpu.push(self.cpu_pct)
        self.cores.set_values(per)
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        self.mem_pct = vm.percent
        self.g_mem.push(vm.percent)

        disk, net = psutil.disk_io_counters(), psutil.net_io_counters()
        rd, wr = (disk.read_bytes - self.last_disk.read_bytes) / dt, (disk.write_bytes - self.last_disk.write_bytes) / dt
        dn, up = (net.bytes_recv - self.last_net.bytes_recv) / dt, (net.bytes_sent - self.last_net.bytes_sent) / dt
        self.last_disk, self.last_net = disk, net
        self.g_disk.push(rd, wr)
        self.g_net.push(dn, up)

        psi = read_psi()
        pc, pm, pio = psi_some10(psi)
        self.g_psi.push(pc, pm, pio)
        load = os.getloadavg()

        if not self.hidden:
            freq = psutil.cpu_freq()
            up_s = int(time.time() - psutil.boot_time())
            self.s_cpu.set("use", f"{self.cpu_pct:.0f}%")
            self.s_cpu.set("freq", f"{freq.current / 1000:.2f} GHz" if freq else "–")
            self.s_cpu.set("proc", str(len(psutil.pids())))
            self.s_cpu.set("up", f"{up_s // 86400}:{up_s % 86400 // 3600:02d}:{up_s % 3600 // 60:02d}:{up_s % 60:02d}")
            self.s_cpu.set("load", f"{load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}")
            self.s_cpu.set("cores", str(NCPU))
            self.s_mem.set("use", fmt_bytes(vm.used))
            self.s_mem.set("avail", fmt_bytes(vm.available))
            self.s_mem.set("cache", fmt_bytes(getattr(vm, "cached", 0) + getattr(vm, "buffers", 0)))
            self.s_mem.set("total", fmt_bytes(vm.total))
            self.s_mem.set("swap", f"{fmt_bytes(sw.used)} / {fmt_bytes(sw.total)}" if sw.total else _("yok"))
            self.s_mem.set("swapio", self.swap_traffic(sw))
            self.s_mem.set("zram", self.zram_text())
            self.s_mem.set("pct", f"{vm.percent:.0f}%")
            self.s_disk.set("r", fmt_bytes(rd) + "/s")
            self.s_disk.set("w", fmt_bytes(wr) + "/s")
            self.s_disk.set("rt", fmt_bytes(disk.read_bytes))
            self.s_disk.set("wt", fmt_bytes(disk.write_bytes))
            self.s_net.set("d", fmt_bytes(dn) + "/s")
            self.s_net.set("u", fmt_bytes(up) + "/s")
            self.s_net.set("dt", fmt_bytes(net.bytes_recv))
            self.s_net.set("ut", fmt_bytes(net.bytes_sent))
            for key, res in (("cpu", "cpu"), ("mem", "memory"), ("io", "io")):
                a = psi.get(res, {}).get("some")
                self.s_psi.set(key, f"{a[0]:.1f}%  ·  {a[1]:.1f}%  ·  {a[2]:.1f}%" if a else _("bu çekirdekte yok"))
            self.hdr_cpu.set_markup(_("<b>{cpu_pct:.0f}%</b>\nCPU").format(cpu_pct=self.cpu_pct))
            self.hdr_mem.set_markup(_("<b>{percent:.0f}%</b>\nBellek").format(percent=vm.percent))
            page = self.root.get_current_page()
            if page == TAB_PERFORMANCE and self.stack.get_visible_child_name() == "psi" and self.tick_n % 2 == 0:
                self.update_psi_top()
            elif page == TAB_HISTORY and self.tick_n % 30 == 0:
                self.refresh_history()
            elif page == TAB_SERVICES and self.tick_n % 10 == 0:
                self.services_refresh()

        refreshed = False
        due = self.tick_n % 10 == 1 if self.hidden else self.tick_n % 2 == 1
        if due and not self.paused and not self.menu_open:
            self.refresh_processes()
            refreshed = True
        elif time.monotonic() >= self.flash_until and self.flash_text:
            self.flash_text = ""
            self.update_status()
            self.update_docker_status()
        if (not self.hidden and self.root.get_current_page() == TAB_DOCKER and self.tick_n % 4 == 0
                and not self.paused and not self.docker_busy):
            self.docker_refresh()
        self.check_helper()
        self.background_work(vm, (self.cpu_pct, vm.percent, sw.percent, pc, pm, pio, load[0], int(rd), int(wr)),
                             refreshed)
        return True

    def swap_traffic(self, sw):
        """Swap'e giren/cikan veri hizi - cikis surekli yuksekse RAM yetmiyor demektir."""
        now = time.monotonic()
        last, self.last_swap = self.last_swap, (sw.sin, sw.sout, now)
        if last is None or now <= last[2]:
            return "–"
        dt = now - last[2]
        return f"{fmt_rate(max(0, sw.sin - last[0]) / dt)}  /  {fmt_rate(max(0, sw.sout - last[1]) / dt)}"

    def zram_text(self):
        z = tm_alerts.zram_stats()
        if z is None:
            return _("yok")
        orig, compr, _used = z
        if not orig:
            return _("kullanılmıyor")
        return f"{fmt_bytes(orig)} → {fmt_bytes(compr)}  ({orig / max(1, compr):.1f}×)"

    def update_psi_top(self):
        cpu = self.top_by(lambda p: p["cpu"], lambda v: _("%{v:.0f}").format(v=v))
        mem = self.top_by(lambda p: p["pss"] or p["rss"], fmt_bytes)
        io = self.top_by(lambda p: max(0, p.get("io_r", 0)) + max(0, p.get("io_w", 0)), fmt_rate)
        esc = GLib.markup_escape_text
        self.psi_top.set_markup(_("<b>Şu an en çok kullananlar</b>\nCPU: {v}\nBellek: {v2}\nDisk: {v3}").format(v=esc(cpu), v2=esc(mem), v3=esc(io)))

    # -------------------------------------------------------------- arka plan isleri
    def background_work(self, vm, sysrow, refreshed):
        """Pencere gizliyken de her saniye calisir: kayit, bellek uyarisi, otomatik temizlik, eski kayit silme."""
        now = time.time()
        if self.recorder is not None and self.pref("log_enabled"):
            self.recorder.stall_psi = self.pref("stall_psi")
            self.recorder.step(now, sysrow, self.history_procs)
        if (self.pref("notify_mem") and vm.percent >= self.pref("notify_mem_pct")
                and now - self.mem_note_t >= MEM_NOTE_COOLDOWN):
            self.mem_note_t = now
            msg = (_("RAM %{percent:.0f} dolu. En çok yiyenler: {top_by}").format(percent=vm.percent, top_by=self.top_by(lambda p: p['pss'] or p['rss'], fmt_bytes)))
            self.notify(_("Bellek azalıyor"), msg)
            self.log_event("bellek", msg)
        if refreshed:
            self.auto_idle_step(vm.percent)
        self.stall_step(now, sysrow[3], sysrow[4], sysrow[5])
        self.disk_step(now)
        if self.history is not None and time.monotonic() - self.last_prune > 3600:
            self.last_prune = time.monotonic()
            self.prune_history()

    def stall_step(self, now, pc, pm, pio):
        """Baski (PSI) esigin ustunde STALL_TICKS sn kalirsa suclu sureci ve 'Sonlandir' dugmesiyle bildir. -> bildirildi mi"""
        if max(pc, pm, pio) < self.pref("stall_psi"):
            self.stall_ticks = 0
            return False
        self.stall_ticks += 1
        if (self.stall_ticks < STALL_TICKS or not self.pref("notify_stall")
                or now - self.stall_note_t < STALL_NOTE_COOLDOWN):
            return False
        self.stall_note_t = now
        peaks = {"cpu": pc, "mem": pm, "io": pio}
        cause = max(peaks, key=peaks.get)
        hit = tm_alerts.pick_culprit(self.last_procs, cause, ME, self.protect_patterns(), self.proc_project)
        title = _("Sistem takılıyor ({v} baskısı %{v2:.0f})").format(v=CAUSE_TR[cause], v2=peaks[cause])
        buttons = []
        if hit is None:
            body = _("Belirgin tek bir suçlu bulunamadı (ya da suçlu korunan/sistem sürecidir). Ayrıntı: Performans → Baskı.")
        else:
            p, v = hit
            proj = os.path.basename(self.proc_project.get(p["pid"]) or "")
            shown = {"cpu": _("CPU %{cpu:.0f}").format(cpu=p['cpu']), "mem": _("bellek {size}").format(size=fmt_bytes(v)), "io": _("disk {v}").format(v=fmt_rate(v))}[cause]
            body = _("En çok kullanan: {name}").format(name=p['name']) + (_(" ({proj})").format(proj=proj) if proj else "") + _(" – {shown}").format(shown=shown)
            try:
                ct = psutil.Process(p["pid"]).create_time()
                buttons.append((_("{name} sonlandır").format(name=p['name']), "app.kill-stall",
                                GLib.Variant("s", f"{p['pid']}:{ct}")))
            except psutil.Error:
                pass
        self.notify(title, body, buttons)
        self.log_event("takılma", _("{title}. {body}").format(title=title, body=body))
        return True

    def kill_stalled(self, target):
        """Bildirimdeki 'sonlandir' dugmesi: ayni surec (PID + baslangic zamani) hala calisiyorsa SIGTERM."""
        try:
            pid_s, ct_s = target.split(":")
            pid, ct = int(pid_s), float(ct_s)
            p = psutil.Process(pid)
            if abs(p.create_time() - ct) > 0.5 or p.uids().real != ME:
                raise psutil.NoSuchProcess(pid)
            name, cmd = p.name(), " ".join(p.cmdline())
        except (ValueError, psutil.Error):
            self.notify(_("Süreç bulunamadı"), _("Süreç zaten kapanmış ya da başka bir süreç olmuş; hiçbir şey yapılmadı."))
            return False
        if tm_alerts.is_essential(name, cmd) or tm_alerts.is_protected(
                self.protect_patterns(), name, cmd, self.proc_project.get(pid) or ""):
            self.notify(_("Sonlandırılmadı"), _("{name} korunan ya da sistem sürecidir.").format(name=name))
            return False
        ok, errs = kill_pids([pid], signal.SIGTERM, False)
        msg = (_("{name} (PID {pid}) sonlandırıldı.").format(name=name, pid=pid) if ok
               else _("{name} sonlandırılamadı: {items}").format(name=name, items='; '.join(errs[:1])))
        self.flash(msg, 10)
        self.notify(_("Süreç sonlandırıldı") if ok else _("Sonlandırılamadı"), msg)
        self.log_event("takılma", msg)
        return bool(ok)

    def disk_step(self, now):
        """Disk doluluğu eşiği aşarsa (dakikada bir kontrol, yol başına 6 saatte bir) 'Temizlik'i aç' düğmeli bildirim."""
        if not self.pref("notify_disk") or now - self.disk_check_t < DISK_CHECK_EVERY:
            return []
        self.disk_check_t = now
        sent = []
        for path, pct, free in tm_alerts.full_disks(self.pref("notify_disk_pct")):
            if now - self.disk_note_t.get(path, -DISK_NOTE_COOLDOWN) < DISK_NOTE_COOLDOWN:
                continue
            self.disk_note_t[path] = now
            msg = _("{path} %{pct:.0f} dolu, {size} boş. Yeniden üretilebilir klasörleri Temizlik sekmesinden "
                    "silebilirsin.").format(path=path, pct=pct, size=fmt_bytes(free))
            self.notify(_("Disk dolmak üzere"), msg, [(_("Temizlik'i aç"), "app.open-tab", GLib.Variant("i", TAB_CLEAN))])
            self.log_event("disk", msg)
            sent.append(path)
        return sent

    def auto_idle_step(self, mem_pct):
        """Bosta (CPU < %2) artik surecleri (profile gore: MSBuild node, Gradle daemon...), RAM yuksekse ve yeterince uzun
        sure bosta kaldiysa kapat."""
        rows = self.cleanup_targets()
        now = time.monotonic()
        idle = {r["pid"]: self.idle_since.get(r["pid"], now) for r in rows}
        self.idle_since = idle
        if not self.pref("auto_idle") or mem_pct < self.pref("auto_idle_mem"):
            return
        limit = self.pref("auto_idle_min") * 60
        old = [r for r in rows if now - idle[r["pid"]] >= limit]
        if not old:
            return
        kill_pids([r["pid"] for r in old], signal.SIGTERM, False)
        msg = _("Otomatik temizlik: boşta bekleyen {n} süreç ({items}) kapatıldı ({size}), RAM %{mem_pct:.0f}.").format(
            n=len(old), items=', '.join(sorted({r['idle'] for r in old})),
            size=fmt_bytes(sum(r['rss'] for r in old)), mem_pct=mem_pct)
        self.flash(msg, 10)
        self.notify(_("Otomatik temizlik"), msg)
        self.log_event("temizlik", msg)

    def prune_history(self, days=None, done=None):
        h = self.history
        if h is None or self.prune_busy:
            return
        self.prune_busy = True
        days = days or self.pref("log_days")

        def work():
            try:
                n = h.prune(days)
            except sqlite3.Error:
                n = -1
            GLib.idle_add(self.on_pruned, n, done)
        threading.Thread(target=work, daemon=True).start()

    def on_pruned(self, n, done):
        self.prune_busy = False
        if done:
            done(n)
        return False

    def build_server_shutdown(self, name, cmd):
        self.flash(_("{name}: {items} çalışıyor…").format(name=name, items=' '.join(cmd)))

        def work():
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                msg = (_("{name} derleme sunucuları kapatıldı.").format(name=name) if res.returncode == 0
                       else _("{v} hata verdi: ").format(v=cmd[0]) + (res.stderr.strip().splitlines() or ["?"])[-1])
            except FileNotFoundError:
                msg = _("{v} bulunamadı.").format(v=cmd[0])
            except subprocess.TimeoutExpired:
                msg = _("{items} zaman aşımına uğradı.").format(items=' '.join(cmd))
            GLib.idle_add(self.flash_idle, msg)
        threading.Thread(target=work, daemon=True).start()

    def fill_shutdown_box(self):
        """Ayarlar'daki 'derleme sunucusunu kapat' düğmeleri: secili profillere gore."""
        box = self.shutdown_box
        if box is None:
            return
        for child in box.get_children():
            box.remove(child)
        for name, cmd in ACTIVE.shutdowns:
            b = Gtk.Button(label=" ".join(cmd))
            b.set_halign(Gtk.Align.START)
            b.set_tooltip_text(_("{name}: arka planda kalan derleme sunucularını kapatır").format(name=name))
            b.connect("clicked", lambda _b, n=name, c=cmd: self.build_server_shutdown(n, c))
            box.pack_start(b, False, False, 0)
        if not ACTIVE.shutdowns:
            lbl = Gtk.Label(label=_("Seçili profillerde kapatılacak bir derleme sunucusu komutu yok."), xalign=0)
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(lbl, False, False, 0)
        box.show_all()

    def flash_idle(self, msg):
        self.flash(msg, 10)
        return False

    # -------------------------------------------------------------- yetkili okuma (pkexec)
    def make_priv_toggle(self, label):
        chk = Gtk.CheckButton(label=label)
        chk.set_tooltip_text(_("pkexec ile salt-okunur bir yardımcı başlatır: tüm kullanıcıların süreçleri için "
                             "port, PSS, disk G/Ç ve dosya sahibi bilgisi. Her açılışta parola sorar; "
                             "sonlandırma işlemleri yine normal kullanıcı olarak yapılır."))
        chk.set_active(self.priv_helper is not None or self.priv_starting)
        chk.connect("toggled", self.on_priv_toggled)
        chk.connect("destroy", lambda w: w in self.priv_toggles and self.priv_toggles.remove(w))
        self.priv_toggles.append(chk)
        return chk

    def sync_priv(self, on):
        self._priv_sync = True
        for w in self.priv_toggles:
            w.set_active(on)
        self._priv_sync = False

    def on_priv_toggled(self, chk):
        if self._priv_sync:
            return
        on = chk.get_active()
        self.sync_priv(on)
        self.set_privileged(on)

    def set_privileged(self, on):
        if on:
            if self.priv_helper is not None or self.priv_starting:
                return
            self.priv_starting = True
            helper = PrivHelper()
            self.flash(_("Yetki isteniyor… (parola penceresi)"), 30)

            def work():
                try:
                    helper.start()
                    err = None
                except HelperError as e:
                    helper.stop()
                    err = str(e)
                GLib.idle_add(self.on_priv_started, helper, err)
            threading.Thread(target=work, daemon=True).start()
        else:
            helper, self.priv_helper = self.priv_helper, None
            self.stats.helper = None
            if helper is not None:
                threading.Thread(target=helper.stop, daemon=True).start()
            self.sync_priv(False)
            self.stats.wake.set()

    def on_priv_started(self, helper, err):
        self.priv_starting = False
        if err or self.quitting:
            helper.stop()
            self.sync_priv(False)
            self.flash(_("Yetkili okuma açılamadı: {err}").format(err=err) if err else "", 10)
            return False
        self.priv_helper = helper
        self.stats.error = None
        self.stats.helper = helper
        self.stats.wake.set()
        self.sync_priv(True)
        self.flash(_("Yetkili okuma açık: tüm kullanıcıların süreçleri okunuyor."), 8)
        return False

    def check_helper(self):
        err = self.stats.error
        if not err:
            return
        self.stats.error = None
        if self.priv_helper is not None:
            self.priv_helper = None
            self.sync_priv(False)
            self.flash(_("Yetkili okuma kapandı: {err}").format(err=err), 10)

    # -------------------------------------------------------------- kapanis
    def set_hidden(self, hidden):
        self.hidden = hidden
        self.stats.interval = 10.0 if hidden else 3.0
        self.stats.wake.set()
        if not hidden:
            self.refresh_processes()
            if self.first_run_profiles and self.win is not None:
                GLib.idle_add(self.show_profile_chooser)

    def show_profile_chooser(self):
        """Ilk acilista: hangi teknolojilerle calisildigini sor (sonradan Ayarlar -> Profil'den degisir)."""
        if not self.first_run_profiles:
            return False
        self.first_run_profiles = False
        dlg = Gtk.Dialog(title=_("Hoş geldin – profilini seç"), transient_for=self.toplevel(), modal=True)
        dlg.add_button(_("Tamam"), Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        lbl = Gtk.Label(label=_("Hangi teknolojilerle çalışıyorsun? Seçimine göre 'geliştirme süreçleri', boşta "
                              "temizlenecek artıklar ve temizlenecek klasörler (bin/obj, node_modules…) belirlenir. "
                              "İstediğin zaman Ayarlar → Profil'den değiştirebilirsin."),
                        xalign=0, wrap=True)
        lbl.set_max_width_chars(60)
        box.pack_start(lbl, False, False, 0)
        widgets = {}
        self.build_profile_checks(box, widgets)
        dlg.test_parts = widgets
        dlg.show_all()
        self.chooser = dlg
        dlg.run()
        dlg.destroy()
        self.chooser = None
        self.save_settings(self.win)
        return False

    def restart_app(self):
        """Dil gibi yalnizca acilista okunan ayarlar icin: bu surec kapaninca yenisi baslar."""
        args = [a for a in sys.argv[1:] if a != "--background"]
        try:
            # yeni surec, bu surec tamamen cikana (en cok ~10 sn) kadar bekler; yoksa tek-ornek (GApplication) kurali
            # onu hala acik olan eski surece devredip kapanmasina yol acar
            wait = 'i=0; while kill -0 "$0" 2>/dev/null && [ $i -lt 50 ]; do sleep 0.2; i=$((i+1)); done; exec "$@"'
            subprocess.Popen(["sh", "-c", wait, str(os.getpid()), sys.executable, os.path.realpath(__file__), *args],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.flash(_("Yeniden başlatılamadı: {err}").format(err=e), 10)
            return False
        self.quit_app()
        return False  # GLib.timeout_add geri cagrisi olarak da kullanilir

    def quit_app(self):
        self.quitting = True
        self.save_settings(self.win)
        if self.app is not None:
            self.app.quit()

    def close(self):
        helper, self.priv_helper = self.priv_helper, None
        self.stats.helper = None
        if helper is not None:
            helper.stop()
        if self.history is not None:
            h, self.history, self.recorder = self.history, None, None
            try:
                h.close()
            except sqlite3.Error:
                pass

    # -------------------------------------------------------------- ayarlari kaydet / yukle
    def restore_view(self):
        page = self.settings.get("tab")
        if isinstance(page, int):
            if self.settings.get("version", 1) < 2:  # eski dosya: Gecmis/Servisler sekmeleri yoktu
                page = OLD_TABS[page] if 0 <= page < len(OLD_TABS) else None
            if page is not None and 0 <= page <= TAB_SERVICES:
                self.root.set_current_page(page)
        perf = self.settings.get("perf_page")
        if perf and self.stack.get_child_by_name(perf):
            self.stack.set_visible_child_name(perf)

    def save_settings(self, win=None):
        data = {
            "version": 2,
            "tab": self.root.get_current_page(),
            "perf_page": self.stack.get_visible_child_name(),
            "tree_mode": self.tree_mode,
            "dev_only": self.dev_only,
            "profiles": list(ACTIVE.ids),
            "all_users": self.show_all_users,
            "docker_all": self.docker_all,
            "proc_columns": self.pcols.to_dict(),
            "proc_sort": self.pcols.sort_to_list(self.sort),
            "docker_columns": self.dcols.to_dict(),
            "docker_sort": self.dcols.sort_to_list(self.dsort),
            "proj_columns": self.jcols.to_dict(),
            "proj_sort": self.jcols.sort_to_list(self.psort),
            "ports_columns": self.ncols.to_dict(),
            "ports_sort": self.ncols.sort_to_list(self.nsort),
            "clean_columns": self.ccols.to_dict(),
            "clean_sort": self.ccols.sort_to_list(self.csort),
            "services_columns": self.scols.to_dict(),
            "services_sort": self.scols.sort_to_list(self.ssort),
            "clean_roots": self.clean_roots,
            "hist_range": self.hist_range.get_active(),
            "bg_hint": bool(self.settings.get("bg_hint")),
            "protect": self.protect_patterns(),
        }
        for key in PREFS:
            data[key] = self.pref(key)
        lang = self.settings.get("language", "auto")
        data["language"] = lang if lang in tm_i18n.CODES else "auto"
        if win is not None:
            data["window"] = list(win.get_size())
        elif self.settings.get("window") is not None:
            data["window"] = self.settings["window"]
        write_settings(data)


class App(Gtk.Application):
    def __init__(self, app_id=APP_ID, background=False):
        super().__init__(application_id=app_id)
        self.tm = None
        self.background = background
        kill = Gio.SimpleAction.new("kill-stall", GLib.VariantType.new("s"))
        kill.connect("activate", lambda _a, v: self.tm and self.tm.kill_stalled(v.get_string()))
        self.add_action(kill)
        open_tab = Gio.SimpleAction.new("open-tab", GLib.VariantType.new("i"))
        open_tab.connect("activate", self.on_open_tab)
        self.add_action(open_tab)

    def on_open_tab(self, _action, value):
        if self.tm is None:
            return
        self.tm.set_hidden(False)
        wins = self.get_windows()
        if wins:
            wins[0].present()
        page = value.get_int32()
        if 0 <= page <= TAB_SERVICES:
            self.tm.root.set_current_page(page)

    def do_activate(self):
        wins = self.get_windows()
        if wins:  # ikinci "tskmngr" ya da bildirim tiklamasi: gizli pencereyi geri getir
            self.tm.set_hidden(False)
            wins[0].present()
            return
        self.hold()  # pencere gizlenince de calismaya (gecmis kaydina) devam et
        tm = self.tm = TaskManager()
        tm.app = self
        win = Gtk.ApplicationWindow(application=self, title=_("Görev Yöneticisi"))
        size = tm.settings.get("window")
        if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and v > 200 for v in size):
            win.set_default_size(*size)
        else:
            win.set_default_size(1180, 720)
        win.set_icon_name("utilities-system-monitor")
        win.add(tm.root)
        tm.win = win
        win.connect("key-press-event", tm.on_key)
        win.connect("delete-event", self.on_delete)
        tm.root.show_all()
        tm.restore_view()
        if self.background:
            tm.set_hidden(True)
        else:
            win.show()
            win.present()
            if tm.first_run_profiles:
                GLib.idle_add(tm.show_profile_chooser)

    def on_delete(self, win, _ev):
        tm = self.tm
        tm.save_settings(win)
        if tm.pref("bg_run") and not tm.quitting:
            win.hide()
            tm.set_hidden(True)
            if not tm.settings.get("bg_hint"):
                tm.set_pref("bg_hint", True)
                tm.notify(_("Arka planda çalışıyor"),
                          _("Pencere kapandı ama geçmiş kaydı sürüyor. Tamamen kapatmak için Ctrl+Q "
                          "ya da Ayarlar → Tamamen çık. Açmak için tskmngr'yi tekrar çalıştır."))
        else:
            tm.quit_app()
        return True

    def do_shutdown(self):
        if self.tm is not None:
            self.tm.close()
        Gtk.Application.do_shutdown(self)


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while Gtk.events_pending():
            Gtk.main_iteration()
        time.sleep(0.01)


def screenshots(outdir):
    """Ekrana pencere acmadan (offscreen) PNG uretir; gelistirme/test icin."""
    os.makedirs(outdir, exist_ok=True)
    t0 = time.monotonic()
    tm = TaskManager()
    print(f"baslatma: {time.monotonic() - t0:.2f}s, satir: {len(tm.store)}")
    t0 = time.monotonic()
    tm.refresh_processes()
    print(f"yenileme: {time.monotonic() - t0:.3f}s")
    win = Gtk.OffscreenWindow()
    win.set_size_request(1180, 720)
    win.add(tm.root)
    win.show_all()
    spin(3.2)
    for name, tab, page in (("proc", TAB_PROCESSES, None), ("cpu", TAB_PERFORMANCE, "cpu"),
                            ("psi", TAB_PERFORMANCE, "psi"), ("history", TAB_HISTORY, None),
                            ("services", TAB_SERVICES, None), ("docker", TAB_DOCKER, None)):
        tm.root.set_current_page(tab)
        if page:
            tm.stack.set_visible_child_name(page)
        spin(4.5 if tab == TAB_DOCKER else 0.5)
        win.get_pixbuf().savev(os.path.join(outdir, name + ".png"), "png", [], [])
    print("tamam:", outdir)


def selfshot(outdir, steps=None):
    """Gercek pencereyi acip kendi icinden PNG alir (test icin)."""
    os.makedirs(outdir, exist_ok=True)
    app = App(APP_ID + ".Selfshot")  # calisan gercek kopyaya yonlenmesin
    app.register()
    app.activate()
    win, tm = app.props.active_window, app.tm
    spin(3.0)
    steps = steps or ["proc", "tree", "docker", "ports", "projects", "portstab", "clean", "history", "services",
                      "psi"]
    for name in steps:
        if name == "proc":
            tm.root.set_current_page(TAB_PROCESSES)
        elif name == "tree":
            tm.root.set_current_page(TAB_PROCESSES)
            tm.search.set_text("")
            tm.btn_tree.set_active(True)
            tm.btn_dev.set_active(True)
        elif name == "docker":
            tm.btn_tree.set_active(False)
            tm.btn_dev.set_active(False)
            tm.root.set_current_page(TAB_DOCKER)
            spin(3.5)
        elif name == "portstab":
            tm.root.set_current_page(TAB_PORTS)
            spin(2.5)
        elif name == "clean":
            tm.root.set_current_page(TAB_CLEAN)
            spin(6.0)
        elif name == "projects":
            tm.search.set_text("")
            tm.root.set_current_page(TAB_PROJECTS)
            spin(2.0)
        elif name == "history":
            tm.root.set_current_page(TAB_HISTORY)
            spin(1.5)
        elif name == "services":
            tm.root.set_current_page(TAB_SERVICES)
            spin(2.5)
        elif name == "psi":
            tm.root.set_current_page(TAB_PERFORMANCE)
            tm.stack.set_visible_child_name("psi")
            spin(2.5)
        elif name == "ports":
            tm.root.set_current_page(TAB_PROCESSES)
            tm.search.set_text(":3306")
            spin(2.0)
        spin(1.5)
        gw = win.get_window()
        Gdk.pixbuf_get_from_window(gw, 0, 0, gw.get_width(), gw.get_height()).savev(
            os.path.join(outdir, "real_" + name + ".png"), "png", [], [])
    print("pencere boyutu:", gw.get_width(), gw.get_height())
    win.destroy()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"tskmngr {VERSION}")
    elif len(sys.argv) > 2 and sys.argv[1] == "--selfshot":
        selfshot(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--screenshots":
        screenshots(sys.argv[2])
    else:
        bg = "--background" in sys.argv[1:]
        app = App(background=bg)
        app.register()
        if bg and app.get_is_remote():  # zaten calisiyor: oturum acilisinda pencereyi one cikarma
            sys.exit(0)
        sys.exit(app.run(sys.argv[:1]))
