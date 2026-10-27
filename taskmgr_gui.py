#!/usr/bin/python3
"""Gorev Yoneticisi - Windows Task Manager benzeri GTK3 penceresi.

Calistirma:  tskmngr   (ya da ./taskmgr_gui.py)
Gereksinim:  sistem python3 + python3-gi (GTK3) + psutil; Docker sekmesi icin docker CLI

Ayarlar (sutun genisligi/gorunurlugu, siralama, mod, pencere boyutu):
    ~/.config/tskmngr/settings.json
"""
import json
import math
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk  # noqa: E402

import psutil  # noqa: E402

APP_ID = "tr.teginsoft.TaskMgr"
NCPU = psutil.cpu_count() or 1
ME = os.getuid()
HOME = os.path.expanduser("~")
HISTORY = 60  # grafikte saniye
TAB_PROCESSES, TAB_PERFORMANCE, TAB_DOCKER, TAB_PROJECTS, TAB_PORTS, TAB_CLEAN = range(6)
STUCK_SECS = 6  # disk beklemesinde (D) bu kadar saniye kalan surec 'takili' sayilir

DOTNET_RX = re.compile(r"dotnet|MSBuild|VBCSCompiler|testhost|OmniSharp|Microsoft\.CodeAnalysis", re.I)
MSBUILD_NODE_RX = re.compile(r"MSBuild\.dll.*nodeReuse:true")
PORT_QUERY_RX = re.compile(r"^(?::|port:?\s*)(\d{1,5})$", re.I)
PROJECT_QUERY_RX = re.compile(r"^proje:(.*)$", re.I)
PROJECT_MARKERS = {".sln", ".csproj", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
                   "build.gradle", "composer.json", "Gemfile", "Makefile", "CMakeLists.txt"}
STATUS_TR = {"running": "çalışıyor", "sleeping": "uyuyor", "disk-sleep": "disk bekliyor (D)",
             "stopped": "durduruldu", "zombie": "zombi", "idle": "boşta", "tracing-stop": "izleniyor",
             "dead": "ölü", "wake-kill": "uyanıyor", "waking": "uyanıyor", "parked": "park"}

(COL_PID, COL_NAME, COL_CPU, COL_RSS, COL_THR, COL_USER, COL_STARTED, COL_CMD,
 COL_CWD, COL_STATUS, COL_PPID, COL_DOTNET, COL_SEARCH, COL_PORTS, COL_PORTNUMS, COL_PSS, COL_NICE) = range(17)
COL_TYPES = [int, str, float, GObject.TYPE_INT64, int, str, float, str, str, str, int, bool, str, str, str,
             GObject.TYPE_INT64, int]

(D_ID, D_NAME, D_PROJECT, D_IMAGE, D_STATE, D_STATUS, D_CPU, D_MEM, D_NET, D_BLOCK, D_PIDS,
 D_PORTS, D_SEARCH) = range(13)
D_TYPES = [str, str, str, str, str, str, float, GObject.TYPE_INT64, str, str, int, str, str]

(P_ROOT, P_NAME, P_PATH, P_COUNT, P_CPU, P_MEM, P_DOTNET) = range(7)
P_TYPES = [str, str, str, int, float, GObject.TYPE_INT64, int]

(N_KEY, N_PROTO, N_PORT, N_ADDR, N_PID, N_NAME, N_USER, N_PROJECT, N_DOCKER, N_CMD, N_SEARCH) = range(11)
N_TYPES = [str, str, int, str, int, str, str, str, str, str, str]

(C_PATH, C_PROJECT, C_KIND, C_SIZE, C_SHOWN) = range(5)
C_TYPES = [str, str, str, GObject.TYPE_INT64, str]
CLEAN_NAMES = {"bin", "obj", "node_modules", "target"}  # sadece bunlar silinebilir
DEFAULT_CLEAN_ROOTS = [os.path.join(HOME, "Desktop", "GitRepos")]

DOCKER_STATES = {
    "running": ("çalışıyor", "#3fb950"),
    "exited": ("durdu", None),
    "paused": ("duraklatıldı", "#e8890c"),
    "restarting": ("yeniden başlıyor", "#e8890c"),
    "created": ("oluşturuldu", None),
    "dead": ("ölü", "#e5484d"),
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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
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


def fmt_elapsed(started, now):
    s = max(0, int(now - started))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}g {h}sa"
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
                    "dotnet": bool(DOTNET_RX.search(st[4]) or DOTNET_RX.search(st[1])),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    for pid in set(_static) - live:
        del _static[pid]
    return out


def listening_sockets():
    """-> [(pid | None, port, 'tcp'|'udp', ip)]. Root'a ait sureclerin (docker-proxy vb.) pid'i None gelir."""
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    out = []
    for c in conns:
        if not c.laddr:
            continue
        if c.type == socket.SOCK_STREAM:
            if c.status != psutil.CONN_LISTEN:
                continue
            proto = "tcp"
        else:
            if c.laddr.port >= 32768:  # gecici (ephemeral) UDP portlari gurultu
                continue
            proto = "udp"
        out.append((c.pid or None, c.laddr.port, proto, c.laddr.ip))
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
        return "tümü"
    if ip.startswith("127.") or ip == "::1":
        return "yerel"
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


def find_owners(path):
    """Bir dosyayi/klasoru acik tutan, calisma dizini yapan ya da bellege haritalayan surecler.
    -> ([(pid, ad, tur, ornek yol)], okunamayan surec sayisi). Baska kullanicilarin surecleri root ister."""
    real = os.path.realpath(path)
    is_dir = os.path.isdir(real)
    prefix = real.rstrip("/") + "/"

    def hit(t):
        if t.endswith(" (deleted)"):
            t = t[:-10]
        return t == real or (is_dir and t.startswith(prefix))

    me, found, denied = os.getpid(), [], 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        pid, base, kinds = int(entry), f"/proc/{entry}", {}
        try:
            fds = os.listdir(base + "/fd")
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                t = os.readlink(f"{base}/fd/{fd}")
            except OSError:
                continue
            if t.startswith("/") and hit(t):
                kinds.setdefault("açık dosya", t)
        try:
            t = os.readlink(base + "/cwd")
            if hit(t):
                kinds.setdefault("çalışma dizini", t)
        except OSError:
            pass
        try:
            with open(base + "/maps", errors="replace") as f:
                for line in f:
                    if real not in line:
                        continue
                    parts = line.rstrip("\n").split(None, 5)
                    if len(parts) == 6 and hit(parts[5]):
                        kinds.setdefault("bellekte (mmap)", parts[5])
                        break
        except OSError:
            pass
        if kinds:
            try:
                with open(base + "/comm") as f:
                    name = f.read().strip()
            except OSError:
                name = "?"
            found.append((pid, name, ", ".join(kinds), next(iter(kinds.values()))))
    found.sort()
    return found, denied


class PssWorker(threading.Thread):
    """PSS (paylasilan bellegi sureclere bolusturen gercek pay). memory_full_info ~0.5 sn surdugu icin
    ana donguyu bloklamamak adina arka planda calisir; pids ana tarafta guncellenir."""

    def __init__(self):
        super().__init__(daemon=True)
        self.pids = []
        self.pss = {}

    def run(self):
        while True:
            pids, result = list(self.pids), {}
            for pid in pids:
                try:
                    result[pid] = psutil.Process(pid).memory_full_info().pss
                except (psutil.Error, OSError, AttributeError):
                    pass
            self.pss = result
            time.sleep(3.0 if pids else 0.3)


def process_details(pid):
    """Tek bir surecin anlik gorunumu -> {'general','files','conns','children','env': metin}"""
    p = psutil.Process(pid)

    def safe(fn):
        try:
            v = fn()
            return "–" if v in (None, "") else v
        except (psutil.Error, OSError):
            return "okunamadı (yetki yok)"

    ct = safe(p.create_time)
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ct)) if isinstance(ct, float) else ct
    ppid = safe(p.ppid)
    parent = safe(lambda: psutil.Process(ppid).name()) if isinstance(ppid, int) else "–"
    mem = safe(p.memory_full_info)
    aff = safe(p.cpu_affinity)
    cwd = safe(p.cwd)
    rows = [
        ("Ad", safe(p.name)), ("PID", pid), ("Üst süreç", f"{ppid} ({parent})"),
        ("Kullanıcı", safe(lambda: user_name(p.uids().real))),
        ("Durum", STATUS_TR.get(str(safe(p.status)), safe(p.status))),
        ("Başlangıç", started),
        ("Çalışma süresi", fmt_elapsed(ct, time.time()) if isinstance(ct, float) else "–"),
        ("Çalışma dizini", cwd), ("Çalıştırılabilir", safe(p.exe)),
        ("Komut satırı", safe(lambda: " ".join(p.cmdline()))),
        ("Öncelik (nice)", safe(p.nice)),
        ("Çekirdek ataması", "tümü" if isinstance(aff, list) and len(aff) == NCPU else aff),
        ("İş parçacığı", safe(p.num_threads)),
        ("Bellek (RSS)", fmt_bytes(mem.rss) if not isinstance(mem, str) else safe(lambda: fmt_bytes(p.memory_info().rss))),
        ("Bellek (PSS)", fmt_bytes(mem.pss) if not isinstance(mem, str) else mem),
        ("Özel bellek (USS)", fmt_bytes(mem.uss) if not isinstance(mem, str) else mem),
        ("Açık tanımlayıcı sayısı", safe(p.num_fds)),
        ("Proje", shorten_home(project_root(cwd)) or "–" if isinstance(cwd, str) else "–"),
    ]
    width = max(len(k) for k, _v in rows)
    d = {"general": "\n".join(f"{k.ljust(width)} : {v}" for k, v in rows)}

    def lines(fn, fmt, empty):
        try:
            out = [fmt(x) for x in fn()]
        except (psutil.Error, OSError) as e:
            return f"okunamadı: {type(e).__name__} (başka kullanıcının süreci olabilir)"
        return "\n".join(out) if out else empty

    d["files"] = lines(p.open_files, lambda f: f.path, "Açık normal dosya yok.")
    conn_fn = getattr(p, "net_connections", None) or p.connections

    def fmt_conn(c):
        proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-"
        remote = f"→ {c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
        return f"{proto}  {local:<28} {remote:<30} {c.status}"

    d["conns"] = lines(lambda: conn_fn(kind="inet"), fmt_conn, "Ağ bağlantısı yok.")
    d["children"] = lines(lambda: p.children(recursive=True), lambda c: f"{c.pid:>8}  {c.name()}",
                          "Alt süreç yok.")
    d["env"] = lines(lambda: sorted(p.environ().items()), lambda kv: f"{kv[0]}={kv[1]}", "Ortam değişkeni yok.")
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


def scan_clean(roots):
    """roots altinda yeniden uretilebilir klasorler: .NET bin/obj, node_modules, target. -> [(yol, proje, tur)]"""
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            has = set(filenames)
            keep = []
            for dn in dirnames:
                full = os.path.join(dirpath, dn)
                if os.path.islink(full) or dn.startswith(".") or dn == "node_modules" and "package.json" not in has:
                    continue
                kind = None
                if dn == "node_modules":
                    kind = "node_modules"
                elif dn in ("bin", "obj") and any(f.endswith((".csproj", ".fsproj", ".vbproj")) for f in has):
                    kind = ".NET " + dn
                elif dn == "target" and ("Cargo.toml" in has or "pom.xml" in has):
                    kind = "target (Rust/Maven)"
                if kind:
                    proj = project_root(full)
                    found.append((full, os.path.basename(proj) if proj else os.path.basename(dirpath), kind))
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


def safe_delete(path, roots):
    """Yalnizca taranan klasorlerin altindaki bin/obj/node_modules/target silinir. -> (ok, hata)"""
    if os.path.islink(path) or os.path.basename(path) not in CLEAN_NAMES:
        return False, "güvenlik: beklenmeyen klasör"
    real = os.path.realpath(path)
    if not any(real.startswith(os.path.realpath(os.path.expanduser(r)).rstrip("/") + "/") for r in roots):
        return False, "taranan klasörlerin dışında"
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
            errs.append(f"{pid}: korumali")
            continue
        try:
            proc.send_signal(sig)
            ok += 1
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            errs.append(f"{pid}: yetki yok")
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
        return "", "docker komutu bulunamadı"
    except subprocess.TimeoutExpired:
        return "", "docker cevap vermedi (zaman aşımı)"
    if res.returncode != 0:
        lines = res.stderr.strip().splitlines()
        return res.stdout, (lines[-1] if lines else "docker hata verdi")
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
            "cpu": None, "mem": None, "net": None, "block": None, "pids": None,
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
            r["mem"] = parse_size(d.get("MemUsage", "").split("/")[0])
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
            return f"Port {port}: docker konteyneri '{', '.join(names)}' yayınlıyor (Docker sekmesine bak)."
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                return f"Port {port} dinleniyor ama süreç bilgisi yok (root'a ait bir süreç olabilir: sudo ss -ltnp)."
    except (psutil.AccessDenied, OSError):
        pass
    return f"Port {port}'u dinleyen bir süreç bulunamadı."


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
        self.dotnet_only = bool(st.get("dotnet_only", False))
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
        self.pss_worker = PssWorker()
        self.pss_worker.start()
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
        self.root.append_page(self.build_processes(), Gtk.Label(label="İşlemler"))
        self.root.append_page(self.build_performance(), Gtk.Label(label="Performans"))
        self.root.append_page(self.build_docker(), Gtk.Label(label="Docker"))
        self.root.append_page(self.build_projects(), Gtk.Label(label="Projeler"))
        self.root.append_page(self.build_ports(), Gtk.Label(label="Portlar"))
        self.root.append_page(self.build_clean(), Gtk.Label(label="Temizlik"))
        self.root.connect("switch-page", self.on_switch_page)
        self.refresh_processes()
        GLib.timeout_add(1000, self.tick)

    # -------------------------------------------------------------- islemler sekmesi
    def build_processes(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.search = Gtk.SearchEntry(placeholder_text="Ara…")
        self.search.set_tooltip_text("Ad, komut, dizin ya da PID ara\n:5000  → o portu dinleyen süreç\ntakılı  → disk beklemesinde takılanlar\nproje:<ad veya yol>  → bir projenin süreçleri")
        self.search.connect("search-changed", self.on_search)
        bar.pack_start(self.search, True, True, 0)

        self.btn_dotnet = Gtk.ToggleButton(label="Sadece dotnet")
        self.btn_dotnet.set_tooltip_text("dotnet / MSBuild / VBCSCompiler / testhost süreçlerini göster")
        self.btn_dotnet.set_active(self.dotnet_only)
        self.btn_dotnet.connect("toggled", self.on_dotnet_toggle)
        bar.pack_start(self.btn_dotnet, False, False, 0)

        self.btn_tree = Gtk.ToggleButton(label="Ağaç görünümü")
        self.btn_tree.set_tooltip_text("Süreçleri ebeveyn-çocuk ilişkisiyle göster (dotnet run → uygulama → MSBuild)")
        self.btn_tree.set_active(self.tree_mode)
        self.btn_tree.connect("toggled", self.on_tree_toggle)
        bar.pack_start(self.btn_tree, False, False, 0)

        btn_find = Gtk.Button(label="Dosya sahibi…")
        btn_find.set_tooltip_text("Bir dosya/klasörü hangi süreç tutuyor? (Ctrl+O)")
        btn_find.connect("clicked", lambda _b: self.open_owner_finder())
        bar.pack_start(btn_find, False, False, 0)

        chk = Gtk.CheckButton(label="Tüm kullanıcılar")
        chk.set_active(self.show_all_users)
        chk.connect("toggled", self.on_all_users)
        bar.pack_start(chk, False, False, 0)

        self.btn_pause = Gtk.ToggleButton(label="Duraklat")
        self.btn_pause.connect("toggled", lambda b: setattr(self, "paused", b.get_active()))
        bar.pack_end(self.btn_pause, False, False, 0)

        btn_force = Gtk.Button(label="Zorla sonlandır")
        btn_force.get_style_context().add_class("destructive-action")
        btn_force.set_tooltip_text("SIGKILL (Shift+Delete)")
        btn_force.connect("clicked", lambda _b: self.end_tasks(signal.SIGKILL))
        bar.pack_end(btn_force, False, False, 0)

        btn_end = Gtk.Button(label="Görevi sonlandır")
        btn_end.set_tooltip_text("SIGTERM - nazikçe kapat (Delete)")
        btn_end.connect("clicked", lambda _b: self.end_tasks(signal.SIGTERM))
        bar.pack_end(btn_end, False, False, 0)

        btn_clean = Gtk.Button(label="MSBuild artıklarını temizle")
        btn_clean.set_tooltip_text("Boşta bekleyen MSBuild node'larını ve VBCSCompiler'ı kapatır")
        btn_clean.connect("clicked", lambda _b: self.cleanup_dotnet())
        bar.pack_end(btn_clean, False, False, 0)
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

        self.hdr_cpu = make_header("CPU", right=True)
        self.hdr_mem = make_header("Bellek", right=True)
        specs = [
            dict(key="name", title="Ad", col=COL_NAME, sort=COL_NAME, width=260, hideable=False, func=self.name_cell),
            dict(key="pid", title="PID", col=COL_PID, sort=COL_PID, width=80, xalign=1.0),
            dict(key="cpu", title="CPU", col=COL_CPU, sort=COL_CPU, width=80, xalign=1.0, func=self.cpu_cell,
                 header=self.hdr_cpu),
            dict(key="mem", title="Bellek", col=COL_RSS, sort=COL_RSS, width=105, xalign=1.0, func=self.mem_cell,
                 header=self.hdr_mem),
            dict(key="pss", title="Bellek (PSS)", col=COL_PSS, sort=COL_PSS, width=120, xalign=1.0,
                 func=self.pss_cell),
            dict(key="ports", title="Portlar", col=COL_PORTS, sort=COL_PORTS, width=110),
            dict(key="threads", title="İş parçacığı", col=COL_THR, sort=COL_THR, width=110, xalign=1.0),
            dict(key="nice", title="Öncelik (nice)", col=COL_NICE, sort=COL_NICE, width=115, xalign=1.0,
                 visible=False),
            dict(key="time", title="Süre", col=COL_STARTED, sort=COL_STARTED, width=85, xalign=1.0,
                 func=self.time_cell),
            dict(key="user", title="Kullanıcı", col=COL_USER, sort=COL_USER, width=95),
            dict(key="status", title="Durum", col=COL_STATUS, sort=COL_STATUS, width=120, visible=False,
                 func=self.status_cell),
            dict(key="cwd", title="Çalışma dizini", col=COL_CWD, sort=COL_CWD, width=230),
            dict(key="cmd", title="Komut satırı", col=COL_CMD, sort=COL_CMD, width=400, expand=True),
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

    def mem_cell(self, _col, r, model, it, _data):
        v = model.get_value(it, COL_RSS)
        r.set_property("text", fmt_bytes(v))
        heat(r, v / (2 << 30))

    def time_cell(self, _col, r, model, it, _data):
        r.set_property("text", fmt_elapsed(model.get_value(it, COL_STARTED), time.time()))

    # ---- filtre
    def self_matches(self, model, it):
        if self.dotnet_only and not model.get_value(it, COL_DOTNET):
            return False
        if self.port_query is not None:
            return f" {self.port_query} " in model.get_value(it, COL_PORTNUMS)
        if self.proj_filter is not None:
            proj = self.proc_project.get(model.get_value(it, COL_PID), "")
            f = self.proj_filter
            return proj == f or bool(proj and f and "/" not in f and os.path.basename(proj) == f)
        if self.query == "takılı":
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

    def on_dotnet_toggle(self, btn):
        self.dotnet_only = btn.get_active()
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
            ("Düşük (nice 10)", lambda: self.set_nice(10)),
            ("Çok düşük (nice 19)", lambda: self.set_nice(19)),
            ("Normal (nice 0) – root gerekebilir", lambda: self.set_nice(0)),
        ]
        entries = [
            ("Ayrıntılar…", self.show_selected_details),
            None,
            ("Görevi sonlandır (SIGTERM)", lambda: self.end_tasks(signal.SIGTERM)),
            ("Zorla sonlandır (SIGKILL)", lambda: self.end_tasks(signal.SIGKILL)),
            ("Süreç ağacını sonlandır (alt süreçlerle)", lambda: self.end_tasks(signal.SIGKILL, tree=True)),
            None,
            ("Dondur (SIGSTOP)", lambda: self.freeze(signal.SIGSTOP)),
            ("Devam ettir (SIGCONT)", lambda: self.freeze(signal.SIGCONT)),
            ("Öncelik", prio),
            ("CPU çekirdeği ata…", self.set_affinity),
            None,
            ("Komut satırını kopyala", self.copy_cmd),
            ("PID'yi kopyala", self.copy_pid),
            ("Çalışma dizinini aç", self.open_cwd),
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
            self.flash("Önce listeden bir süreç seç.")
            return
        ok, errs = kill_pids([s[0] for s in sel], sig, False)
        word = "donduruldu" if sig == signal.SIGSTOP else "devam ettirildi"
        msg = f"{ok} süreç {word}."
        if errs:
            msg += "  Hata: " + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def set_nice(self, value):
        sel = self.selected()
        if not sel:
            self.flash("Önce listeden bir süreç seç.")
            return
        ok, errs = 0, []
        for pid, _n, _c, _w in sel:
            try:
                psutil.Process(pid).nice(value)
                ok += 1
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                errs.append(f"{pid}: yetki yok" + (" (önceliği yükseltmek root ister)" if value < 10 else ""))
        msg = f"Öncelik nice {value}: {ok} süreç."
        if errs:
            msg += "  Hata: " + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def choose_affinity(self, current):
        """Cekirdek secim penceresi -> secilen cekirdek listesi | None (iptal)"""
        dlg = Gtk.Dialog(title="CPU çekirdeği ata", transient_for=self.toplevel(), modal=True)
        dlg.add_button("İptal", Gtk.ResponseType.CANCEL)
        dlg.add_button("Uygula", Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_spacing(10)
        for m in ("top", "bottom", "start", "end"):
            getattr(area, f"set_margin_{m}")(12)
        area.pack_start(Gtk.Label(label="Süreç yalnızca işaretli çekirdeklerde çalışır:", xalign=0), False, False, 0)
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        checks = []
        for i in range(NCPU):
            c = Gtk.CheckButton(label=f"Çekirdek {i}")
            c.set_active(i in current)
            checks.append(c)
            grid.attach(c, i % 4, i // 4, 1, 1)
        area.pack_start(grid, False, False, 0)
        quick = Gtk.Box(spacing=8)
        for label, pick in (("Tümü", lambda i: True), ("İlk yarısı", lambda i: i < NCPU // 2),
                            ("Son yarısı", lambda i: i >= NCPU // 2)):
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
            self.flash("Önce listeden bir süreç seç.")
            return
        try:
            current = psutil.Process(sel[0][0]).cpu_affinity()
        except psutil.Error:
            current = list(range(NCPU))
        chosen = self.choose_affinity(current)
        if chosen is None:
            return
        if not chosen:
            self.flash("En az bir çekirdek seçmelisin.")
            return
        ok, errs = 0, []
        for pid, _n, _c, _w in sel:
            try:
                psutil.Process(pid).cpu_affinity(chosen)
                ok += 1
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                errs.append(f"{pid}: yetki yok")
        msg = f"{ok} sürecin çekirdekleri {chosen} olarak ayarlandı."
        if errs:
            msg += "  Hata: " + "; ".join(errs[:3])
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
        win = Gtk.Window(title=f"Süreç ayrıntıları – {name} ({pid})")
        win.set_default_size(920, 560)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(top, f"set_margin_{m}")(8)
        info = Gtk.Label(label="Anlık görünüm – güncellemek için Yenile'ye bas.", xalign=0)
        btn = Gtk.Button(label="Yenile")
        top.pack_start(info, True, True, 0)
        top.pack_end(btn, False, False, 0)
        box.pack_start(top, False, False, 0)
        nb = Gtk.Notebook()
        views = {}
        for key, title in (("general", "Genel"), ("files", "Açık dosyalar"), ("conns", "Ağ bağlantıları"),
                           ("children", "Alt süreçler"), ("env", "Ortam değişkenleri")):
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
                info.set_text("Bu süreç artık çalışmıyor.")
                return
            for key, tv in views.items():
                tv.get_buffer().set_text(data[key])
            info.set_text(f"{time.strftime('%H:%M:%S')} anındaki görünüm")

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
            self.flash("Komut satırı kopyalandı.")

    def copy_pid(self):
        sel = self.selected()
        if sel:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(" ".join(str(s[0]) for s in sel), -1)
            self.flash("PID kopyalandı.")

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
        dlg.add_button("İptal", Gtk.ResponseType.CANCEL)
        b = dlg.add_button(ok_label, Gtk.ResponseType.OK)
        b.get_style_context().add_class("destructive-action")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def end_tasks(self, sig, tree=False):
        sel = self.selected()
        if not sel:
            self.flash("Önce listeden bir süreç seç.")
            return
        listing = "\n".join(f"{pid}  {name}" for pid, name, _c, _w in sel[:10])
        if len(sel) > 10:
            listing += f"\n… ve {len(sel) - 10} süreç daha"
        what = "ağacıyla birlikte " if tree else ""
        title = f"{len(sel)} süreç {what}{signal.Signals(sig).name} ile sonlandırılsın mı?"
        if not self.confirm(title, listing, "Sonlandır"):
            return
        self.send_signal([s[0] for s in sel], sig, tree)

    def send_signal(self, pids, sig, tree):
        ok, errs = kill_pids(pids, sig, tree)
        msg = f"{signal.Signals(sig).name}: {ok} sürece gönderildi."
        if errs:
            msg += "  Hata: " + "; ".join(errs[:3])
        self.flash(msg)
        GLib.timeout_add(800, self.refresh_once)

    def refresh_once(self):
        self.refresh_processes()
        return False

    def cleanup_targets(self):
        nodes, compilers = [], []
        for r in self.last_procs:
            if r["uid"] != ME or r["cpu_raw"] >= 2.0:
                continue
            if MSBUILD_NODE_RX.search(r["cmd"]):
                nodes.append(r)
            elif "VBCSCompiler" in r["cmd"]:
                compilers.append(r)
        return nodes, compilers

    def cleanup_dotnet(self):
        nodes, compilers = self.cleanup_targets()
        if not nodes and not compilers:
            self.flash("Temizlenecek boşta MSBuild node'u ya da VBCSCompiler yok.")
            return
        total = sum(r["rss"] for r in nodes + compilers)
        detail = (f"{len(nodes)} MSBuild node'u ve {len(compilers)} VBCSCompiler kapatılacak "
                  f"(toplam {fmt_bytes(total)} RAM).\n\n"
                  "CPU kullanan süreçler atlanır. Bir derleme sürüyorsa bitmesini bekle. "
                  "Sonraki build'de bu süreçler yeniden başlar.")
        if self.confirm("Boşta bekleyen MSBuild artıkları temizlensin mi?", detail, "Temizle"):
            self.send_signal([r["pid"] for r in nodes + compilers], signal.SIGTERM, False)

    def on_key(self, _win, ev):
        focus = self.win.get_focus() if self.win else None
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        key = ev.keyval
        page = self.root.get_current_page()
        if key == Gdk.KEY_f and ctrl:
            if page == TAB_DOCKER:
                self.docker_search.grab_focus()
            elif page == TAB_PORTS:
                self.ports_search.grab_focus()
            else:
                self.root.set_current_page(TAB_PROCESSES)
                self.search.grab_focus()
            return True
        if key == Gdk.KEY_o and ctrl:
            self.open_owner_finder()
            return True
        if key == Gdk.KEY_F5:
            if page == TAB_DOCKER:
                self.docker_refresh()
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
                r["cwd"], r["status"], r["ppid"], r["dotnet"], search, shown, nums, r.get("pss", 0), r["nice"]]

    def store_subtree(self, it, acc):
        acc.add(self.store[it][COL_PID])
        child = self.store.iter_children(it)
        while child:
            self.store_subtree(child, acc)
            child = self.store.iter_next(child)

    def refresh_processes(self):
        procs = collect(self.show_all_users)
        pss = self.pss_worker.pss
        for r in procs:
            r["pss"] = pss.get(r["pid"], 0)
        self.pss_worker.pids = [r["pid"] for r in procs if r["uid"] == ME]
        self.last_procs = procs
        self.last_socks = listening_sockets()
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
            self.store.set(it, [COL_CPU, COL_RSS, COL_THR, COL_STATUS, COL_PPID, COL_PSS, COL_NICE],
                           [r["cpu"], r["rss"], r["threads"], r["status"], r["ppid"], r["pss"], r["nice"]])
            shown, nums = ports_info(ports.get(pid))
            if self.store[it][COL_PORTNUMS] != nums:
                row = self.row_for(r, ports.get(pid))
                self.store.set(it, [COL_PORTS, COL_PORTNUMS, COL_SEARCH], [row[COL_PORTS], nums, row[COL_SEARCH]])

        if self.tree_mode:
            self.collapsed &= set(live)
            if self.query or self.dotnet_only or self.port_query is not None or self.proj_filter is not None:
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
        dn = [r for r in self.last_procs if r["dotnet"]]
        nodes = [r for r in self.last_procs if MSBUILD_NODE_RX.search(r["cmd"])]
        node_txt = f"MSBuild node: {len(nodes)} ({fmt_bytes(sum(r['rss'] for r in nodes))})"
        if len(nodes) > 8:
            node_txt = f"<span foreground='#e8890c'><b>{node_txt}</b></span>"
        text = (f"Listelenen: {shown}/{len(self.last_procs)}   │   "
                f"dotnet: {len(dn)} süreç ({fmt_bytes(sum(r['rss'] for r in dn))})   │   {node_txt}")
        if self.stuck:
            text += (f"   │   <span foreground='#e8890c'><b>⚠ {len(self.stuck)} süreç disk beklemesinde takılı</b>"
                     f"</span> (aramaya 'takılı' yaz)")
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
        self.s_cpu = StatBlock([("use", "Kullanım"), ("freq", "Hız"), ("proc", "Süreç"),
                                ("up", "Çalışma süresi"), ("load", "Yük ortalaması (1/5/15 dk)"),
                                ("cores", "Mantıksal işlemci")])
        self.stack.add_titled(make_page("CPU", f"<span alpha='65%'>{GLib.markup_escape_text(cpu_model())}</span>",
                                        self.g_cpu, self.s_cpu, self.cores), "cpu", "CPU")

        self.g_mem = Graph([purple])
        self.s_mem = StatBlock([("use", "Kullanımda"), ("avail", "Kullanılabilir"), ("cache", "Önbellek"),
                                ("total", "Toplam"), ("swap", "Swap"), ("pct", "Kullanım oranı")])
        self.stack.add_titled(make_page("Bellek", f"<span alpha='65%'>{fmt_bytes(psutil.virtual_memory().total)} RAM</span>",
                                        self.g_mem, self.s_mem), "mem", "Bellek")

        self.g_disk = Graph([green, orange], maxv=None, min_scale=1 << 20, label_fmt=lambda v: fmt_bytes(v) + "/s")
        self.s_disk = StatBlock([("r", "Okuma hızı"), ("w", "Yazma hızı"), ("rt", "Toplam okunan"),
                                 ("wt", "Toplam yazılan")])
        self.stack.add_titled(make_page("Disk",
                                        "<span foreground='#40b366'>■</span> Okuma   <span foreground='#f29926'>■</span> Yazma",
                                        self.g_disk, self.s_disk), "disk", "Disk")

        self.g_net = Graph([blue, orange], maxv=None, min_scale=100 << 10, label_fmt=lambda v: fmt_bytes(v) + "/s")
        self.s_net = StatBlock([("d", "İndirme"), ("u", "Yükleme"), ("dt", "Toplam indirilen"),
                                ("ut", "Toplam yüklenen")])
        self.stack.add_titled(make_page("Ağ",
                                        "<span foreground='#338cd9'>■</span> İndirme   <span foreground='#f29926'>■</span> Yükleme",
                                        self.g_net, self.s_net), "net", "Ağ")
        return box

    # -------------------------------------------------------------- docker sekmesi
    def build_docker(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.docker_search = Gtk.SearchEntry(placeholder_text="Ara: ad, imaj, proje, port…")
        self.docker_search.connect("search-changed", self.on_docker_search)
        bar.pack_start(self.docker_search, True, True, 0)

        chk = Gtk.CheckButton(label="Durdurulanlar da")
        chk.set_active(self.docker_all)
        chk.connect("toggled", self.on_docker_all)
        bar.pack_start(chk, False, False, 0)

        btn_rm = Gtk.Button(label="Sil")
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.set_tooltip_text("docker rm -f (konteyner silinir, volume'lar kalır)")
        btn_rm.connect("clicked", lambda _b: self.docker_action("rm"))
        bar.pack_end(btn_rm, False, False, 0)
        for label, verb, tip in (("Zorla durdur", "kill", "docker kill (SIGKILL)"),
                                 ("Logları göster", "logs", "Son 500 satır (çift tık da açar)"),
                                 ("Yeniden başlat", "restart", None), ("Durdur", "stop", None),
                                 ("Başlat", "start", None)):
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
            dict(key="name", title="Ad", col=D_NAME, sort=D_NAME, width=210, hideable=False),
            dict(key="project", title="Proje", col=D_PROJECT, sort=D_PROJECT, width=110),
            dict(key="state", title="Durum", col=D_STATE, sort=D_STATE, width=130, func=self.docker_state_cell),
            dict(key="cpu", title="CPU", col=D_CPU, sort=D_CPU, width=75, xalign=1.0, func=self.docker_cpu_cell),
            dict(key="mem", title="Bellek", col=D_MEM, sort=D_MEM, width=100, xalign=1.0, func=self.docker_mem_cell),
            dict(key="net", title="Ağ G/Ç", col=D_NET, sort=D_NET, width=130),
            dict(key="block", title="Disk G/Ç", col=D_BLOCK, sort=D_BLOCK, width=130),
            dict(key="pids", title="PID sayısı", col=D_PIDS, sort=D_PIDS, width=100, xalign=1.0),
            dict(key="ports", title="Portlar", col=D_PORTS, sort=D_PORTS, width=190),
            dict(key="image", title="İmaj", col=D_IMAGE, sort=D_IMAGE, width=200),
            dict(key="status", title="Çalışma durumu", col=D_STATUS, sort=D_STATUS, width=180, expand=True),
        ]
        self.dcols = ColumnSet(self.dtree, specs, self.settings.get("docker_columns"))
        self.dcols.apply_sort(self.dsort, self.settings.get("docker_sort"), ["name", "asc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.dtree)
        page.pack_start(scroll, True, True, 0)

        self.docker_status = Gtk.Label(label="Docker sekmesi açılınca yüklenir.", xalign=0)
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

    def on_docker_search(self, entry):
        self.docker_query = entry.get_text().strip().lower()
        self.dfilter.refilter()

    def on_docker_all(self, chk):
        self.docker_all = chk.get_active()
        self.docker_refresh()

    def on_switch_page(self, _nb, _page, num):
        if num == TAB_DOCKER:
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
        self.docker_msg.set_markup(f"<span foreground='#e8890c'><b>Docker erişilemiyor:</b> "
                                   f"{GLib.markup_escape_text(err)}</span>")
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
                    r["mem"] or 0, r["net"] or "", r["block"] or "", r["pids"] or 0, r["ports"], search])
            else:
                cols = [D_NAME, D_PROJECT, D_IMAGE, D_STATE, D_STATUS, D_PORTS, D_SEARCH]
                vals = [r["name"], r["project"], r["image"], r["state"], r["status"], r["ports"], search]
                if r["cpu"] is not None:
                    cols += [D_CPU, D_MEM, D_NET, D_BLOCK, D_PIDS]
                    vals += [r["cpu"], r["mem"], r["net"], r["block"], r["pids"]]
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
                f"Konteyner: {total} ({running} çalışıyor)   │   toplam CPU: {cpu:.1f}%   │   "
                f"toplam RAM: {fmt_bytes(mem)}")

    def docker_selected(self):
        model, paths = self.dtree.get_selection().get_selected_rows()
        return [(model[p][D_ID], model[p][D_NAME], model[p][D_STATE]) for p in paths]

    def docker_action(self, verb):
        sel = self.docker_selected()
        if not sel:
            self.flash("Önce listeden bir konteyner seç.")
            return
        if verb == "logs":
            self.show_logs(sel[0][0], sel[0][1])
            return
        names = "\n".join(n for _i, n, _s in sel[:10])
        if verb in ("rm", "kill"):
            title = (f"{len(sel)} konteyner silinsin mi?" if verb == "rm"
                     else f"{len(sel)} konteyner zorla durdurulsun mu?")
            if not self.confirm(title, names, "Sil" if verb == "rm" else "Zorla durdur"):
                return
        args = {"start": ["start"], "stop": ["stop"], "restart": ["restart"], "kill": ["kill"],
                "rm": ["rm", "-f"]}[verb]
        ids = [s[0] for s in sel]
        self.flash(f"docker {verb}: {len(ids)} konteyner için çalışıyor…", 60)

        def work():
            _out, err = _docker_run([*args, *ids], 90)
            GLib.idle_add(self.after_docker_action, verb, len(ids), err)

        threading.Thread(target=work, daemon=True).start()

    def after_docker_action(self, verb, n, err):
        self.flash(f"docker {verb}: {'hata – ' + err if err else str(n) + ' konteyner tamam'}.", 8)
        self.docker_refresh()
        return False

    def show_logs(self, cid, name):
        win = Gtk.Window(title=f"Loglar – {name}")
        win.set_default_size(980, 560)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(top, f"set_margin_{m}")(8)
        live = Gtk.CheckButton(label="Canlı (2 sn'de bir yenile)")
        live.set_active(True)
        btn = Gtk.Button(label="Yenile")
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
                    text = f"loglar alınamadı: {e}"
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
        info = Gtk.Label(label="Süreçler çalışma dizinine göre projelere (git deposu / .sln / package.json …) gruplanır.",
                         xalign=0)
        info.set_ellipsize(3)
        bar.pack_start(info, True, True, 0)
        btn_force = Gtk.Button(label="Zorla sonlandır")
        btn_force.get_style_context().add_class("destructive-action")
        btn_force.connect("clicked", lambda _b: self.project_action(signal.SIGKILL))
        bar.pack_end(btn_force, False, False, 0)
        btn_end = Gtk.Button(label="Projenin süreçlerini sonlandır")
        btn_end.set_tooltip_text("SIGTERM - projenin çalışma dizinindeki tüm süreçlere")
        btn_end.connect("clicked", lambda _b: self.project_action(signal.SIGTERM))
        bar.pack_end(btn_end, False, False, 0)
        btn_cont = Gtk.Button(label="Devam ettir")
        btn_cont.set_tooltip_text("SIGCONT - dondurulmuş süreçleri çalıştırır")
        btn_cont.connect("clicked", lambda _b: self.project_action(signal.SIGCONT))
        bar.pack_end(btn_cont, False, False, 0)
        btn_stop = Gtk.Button(label="Dondur")
        btn_stop.set_tooltip_text("SIGSTOP - öldürmeden duraklatır; kaynak kullanmaz")
        btn_stop.connect("clicked", lambda _b: self.project_action(signal.SIGSTOP))
        bar.pack_end(btn_stop, False, False, 0)
        btn_show = Gtk.Button(label="İşlemlerde göster")
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
            dict(key="name", title="Proje", col=P_NAME, sort=P_NAME, width=220, hideable=False),
            dict(key="count", title="Süreç", col=P_COUNT, sort=P_COUNT, width=80, xalign=1.0),
            dict(key="cpu", title="CPU", col=P_CPU, sort=P_CPU, width=80, xalign=1.0, func=self.proj_cpu_cell),
            dict(key="mem", title="Bellek (PSS)", col=P_MEM, sort=P_MEM, width=110, xalign=1.0, func=self.proj_mem_cell),
            dict(key="dotnet", title="dotnet süreç", col=P_DOTNET, sort=P_DOTNET, width=125, xalign=1.0),
            dict(key="path", title="Yol", col=P_PATH, sort=P_PATH, width=400, expand=True),
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

    def update_projects(self):
        stats = {}
        for r in self.last_procs:
            root = self.proc_project.get(r["pid"], "")
            s = stats.setdefault(root, [0, 0.0, 0, 0])
            s[0] += 1
            s[1] += r["cpu"]
            s[2] += r["pss"] or r["rss"]
            s[3] += r["dotnet"]
        for root, (n, cpu, rss, dn) in stats.items():
            it = self.proj_iters.get(root)
            if it is None:
                name = os.path.basename(root) if root else "(Proje dışı)"
                self.proj_iters[root] = self.pstore.append([root, name, shorten_home(root), n, cpu, rss, dn])
            else:
                self.pstore.set(it, [P_COUNT, P_CPU, P_MEM, P_DOTNET], [n, cpu, rss, dn])
        for root in [x for x in self.proj_iters if x not in stats]:
            self.pstore.remove(self.proj_iters.pop(root))
        projects = [v for k, v in stats.items() if k]
        self.proj_status.set_text(
            f"Proje: {len(projects)}   │   projelerin toplam RAM'i: {fmt_bytes(sum(v[2] for v in projects))}"
            f"   (PSS: paylaşılan bellek süreçlere bölüştürülür)")

    def project_selected(self):
        model, paths = self.ptree.get_selection().get_selected_rows()
        return [(model[p][P_ROOT], model[p][P_NAME]) for p in paths]

    def project_show(self):
        sel = self.project_selected()
        if not sel:
            self.flash("Önce listeden bir proje seç.")
            return
        self.root.set_current_page(TAB_PROCESSES)
        self.search.set_text("proje:" + shorten_home(sel[0][0]))
        self.search.grab_focus()

    def project_action(self, sig):
        sel = self.project_selected()
        if not sel:
            self.flash("Önce listeden bir proje seç.")
            return
        if any(not root for root, _n in sel):
            self.flash("'Proje dışı' grubu sistem ve masaüstü süreçlerini içerir, toplu işlem yapılamaz.")
            sel = [s for s in sel if s[0]]
            if not sel:
                return
        roots = {root for root, _n in sel}
        mine = {r["pid"]: r for r in self.last_procs if r["uid"] == ME}
        pids = [pid for pid, root in self.proc_project.items() if root in roots and pid in mine]
        if not pids:
            self.flash("Bu projede işlem yapılacak süreç yok.")
            return
        if sig in (signal.SIGSTOP, signal.SIGCONT):
            ok, errs = kill_pids(pids, sig, False)
            word = "donduruldu" if sig == signal.SIGSTOP else "devam ettirildi"
            self.flash(f"{ok} süreç {word}." + (f"  Hata: {errs[0]}" if errs else ""))
            GLib.timeout_add(800, self.refresh_once)
            return
        mem = sum(mine[p]["pss"] or mine[p]["rss"] for p in pids)
        detail = "\n".join(f"• {name}" for _r, name in sel[:10])
        detail += (f"\n\n{len(pids)} süreç ({fmt_bytes(mem)} RAM) {signal.Signals(sig).name} alacak. "
                   "Bu projenin dizininde çalışan editör eklentileri / dil sunucuları da dahildir.")
        if self.confirm(f"{len(sel)} projenin süreçleri sonlandırılsın mı?", detail, "Sonlandır"):
            self.send_signal(pids, sig, False)

    # -------------------------------------------------------------- portlar sekmesi
    def build_ports(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(bar, f"set_margin_{m}")(8)
        self.ports_search = Gtk.SearchEntry(placeholder_text="Ara: port, süreç, proje, konteyner…")
        self.ports_search.connect("search-changed", self.on_ports_search)
        bar.pack_start(self.ports_search, True, True, 0)
        b_kill = Gtk.Button(label="Zorla sonlandır")
        b_kill.get_style_context().add_class("destructive-action")
        b_kill.set_tooltip_text("Süreç: SIGKILL · Docker: docker kill")
        b_kill.connect("clicked", lambda _b: self.ports_end(signal.SIGKILL))
        bar.pack_end(b_kill, False, False, 0)
        b_term = Gtk.Button(label="Portu kapat")
        b_term.set_tooltip_text("Süreç: SIGTERM · Docker konteyneri: docker stop")
        b_term.connect("clicked", lambda _b: self.ports_end(signal.SIGTERM))
        bar.pack_end(b_term, False, False, 0)
        b_show = Gtk.Button(label="İşlemlerde göster")
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
            dict(key="port", title="Port", col=N_PORT, sort=N_PORT, width=80, xalign=1.0, hideable=False),
            dict(key="proto", title="Protokol", col=N_PROTO, sort=N_PROTO, width=85),
            dict(key="addr", title="Erişim", col=N_ADDR, sort=N_ADDR, width=100),
            dict(key="pid", title="PID", col=N_PID, sort=N_PID, width=80, xalign=1.0, func=self.port_pid_cell),
            dict(key="name", title="Süreç", col=N_NAME, sort=N_NAME, width=200),
            dict(key="project", title="Proje", col=N_PROJECT, sort=N_PROJECT, width=140),
            dict(key="docker", title="Docker konteyneri", col=N_DOCKER, sort=N_DOCKER, width=170),
            dict(key="user", title="Kullanıcı", col=N_USER, sort=N_USER, width=90),
            dict(key="cmd", title="Komut satırı", col=N_CMD, sort=N_CMD, width=300, expand=True),
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
                name = "docker-proxy" if docker else "(süreç bilgisi yok)"
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
        extra = f"   │   {unknown} port root'a ait bir süreçte (sudo ss -ltnp ile görülür)" if unknown else ""
        self.ports_status.set_text(f"Dinleyen port: {len(groups)}{extra}")

    def ports_selected(self):
        model, paths = self.ntree.get_selection().get_selected_rows()
        return [(model[p][N_PID], model[p][N_PORT], model[p][N_PROTO], model[p][N_DOCKER], model[p][N_NAME])
                for p in paths]

    def ports_show(self):
        sel = self.ports_selected()
        if not sel:
            self.flash("Önce listeden bir port seç.")
            return
        self.root.set_current_page(TAB_PROCESSES)
        self.search.set_text(f":{sel[0][1]}")

    def ports_end(self, sig):
        sel = self.ports_selected()
        if not sel:
            self.flash("Önce listeden bir port seç.")
            return
        pids = sorted({s[0] for s in sel if s[0]})
        dockers = sorted({s[3] for s in sel if not s[0] and s[3]})
        if not pids and not dockers:
            self.flash("Bu portun sahibi görünmüyor (root'a ait): sudo ss -ltnp ile bak.")
            return
        lines = [f"{port}/{proto} · {name}" + (f" ({pid})" if pid else f" → konteyner {docker}" if docker else "")
                 for pid, port, proto, docker, name in sel[:10]]
        title = f"{len(sel)} port kapatılsın mı?"
        if not self.confirm(title, "\n".join(lines), "Kapat"):
            return
        if pids:
            self.send_signal(pids, sig, False)
        if dockers:
            verb = "kill" if sig == signal.SIGKILL else "stop"
            self.flash(f"docker {verb}: {len(dockers)} konteyner için çalışıyor…", 60)

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
        b_del = Gtk.Button(label="Seçilenleri sil")
        b_del.get_style_context().add_class("destructive-action")
        b_del.set_tooltip_text("Seçili klasörleri diskten siler; build/restore ile yeniden oluşur")
        b_del.connect("clicked", lambda _b: self.clean_delete())
        bar.pack_end(b_del, False, False, 0)
        b_scan = Gtk.Button(label="Yeniden tara")
        b_scan.connect("clicked", lambda _b: self.clean_scan())
        bar.pack_end(b_scan, False, False, 0)
        b_reset = Gtk.Button(label="Varsayılan klasör")
        b_reset.connect("clicked", lambda _b: self.clean_set_roots(list(DEFAULT_CLEAN_ROOTS)))
        bar.pack_end(b_reset, False, False, 0)
        b_add = Gtk.Button(label="Klasör ekle…")
        b_add.connect("clicked", lambda _b: self.clean_add_root())
        bar.pack_end(b_add, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.cstore = Gtk.ListStore(*C_TYPES)
        self.csort = Gtk.TreeModelSort(model=self.cstore)
        self.ctree = Gtk.TreeView(model=self.csort)
        self.ctree.set_enable_search(False)
        self.ctree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        specs = [
            dict(key="project", title="Proje", col=C_PROJECT, sort=C_PROJECT, width=220, hideable=False),
            dict(key="kind", title="Tür", col=C_KIND, sort=C_KIND, width=160),
            dict(key="size", title="Boyut", col=C_SIZE, sort=C_SIZE, width=110, xalign=1.0, func=self.clean_size_cell),
            dict(key="path", title="Yol", col=C_SHOWN, sort=C_SHOWN, width=500, expand=True),
        ]
        self.ccols = ColumnSet(self.ctree, specs, self.settings.get("clean_columns"))
        self.ccols.apply_sort(self.csort, self.settings.get("clean_sort"), ["size", "desc"])
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.ctree)
        page.pack_start(scroll, True, True, 0)

        dock = Gtk.Box(spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(dock, f"set_margin_{m}")(8)
        self.docker_df_label = Gtk.Label(label="Docker: –", xalign=0)
        self.docker_df_label.set_ellipsize(3)
        dock.pack_start(self.docker_df_label, True, True, 0)
        b_cache = Gtk.Button(label="Build önbelleğini sil")
        b_cache.set_tooltip_text("docker builder prune -f")
        b_cache.connect("clicked", lambda _b: self.docker_prune("builder", "Build önbelleği"))
        dock.pack_end(b_cache, False, False, 0)
        b_img = Gtk.Button(label="Boşta imajları sil")
        b_img.set_tooltip_text("docker image prune -f (etiketsiz / kullanılmayan 'dangling' imajlar)")
        b_img.connect("clicked", lambda _b: self.docker_prune("image", "Boşta imajlar"))
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
        self.clean_label.set_text("Taranan klasörler: " + ", ".join(shorten_home(r) for r in self.clean_roots))

    def clean_set_roots(self, roots):
        self.clean_roots = roots
        self.update_clean_label()
        self.clean_scan()

    def clean_add_root(self):
        dlg = Gtk.FileChooserDialog(title="Taranacak klasörü seç", transient_for=self.toplevel(),
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("İptal", Gtk.ResponseType.CANCEL, "Ekle", Gtk.ResponseType.OK)
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
        self.clean_status.set_text("Taranıyor… (büyük klasörlerde birkaç saniye sürebilir)")
        roots = list(self.clean_roots)

        def work():
            found = scan_clean(roots)
            sizes = dir_sizes([p for p, _pr, _k in found])
            GLib.idle_add(self.on_clean_done, found, sizes)
        threading.Thread(target=work, daemon=True).start()
        self.docker_df_refresh()

    def on_clean_done(self, found, sizes):
        self.clean_busy = False
        self.cstore.clear()
        self.clean_iters.clear()
        for path, proj, kind in found:
            self.clean_iters[path] = self.cstore.append([path, proj, kind, sizes.get(path, 0), shorten_home(path)])
        self.update_clean_status()
        return False

    def update_clean_status(self):
        total = sum(r[C_SIZE] for r in self.cstore)
        self.clean_status.set_text(f"{len(self.cstore)} klasör  │  toplam {fmt_bytes(total)} geri kazanılabilir")

    def clean_selected(self):
        model, paths = self.ctree.get_selection().get_selected_rows()
        return [(model[p][C_PATH], model[p][C_PROJECT], model[p][C_KIND], model[p][C_SIZE]) for p in paths]

    def clean_delete(self):
        sel = self.clean_selected()
        if not sel:
            self.flash("Önce silinecek klasörleri seç (Ctrl+A hepsini seçer).")
            self.clean_status.set_text("Önce silinecek klasörleri seç.")
            return
        busy = busy_dirs([p for p, *_r in sel])
        total = sum(s[3] for s in sel)
        listing = "\n".join(f"{kind} · {proj} · {fmt_bytes(size)}" for _p, proj, kind, size in sel[:8])
        if len(sel) > 8:
            listing += f"\n… ve {len(sel) - 8} klasör daha"
        detail = listing + "\n\nBu klasörler build / restore ile yeniden oluşur; kaynak kodun silinmez."
        if busy:
            names = ", ".join(sorted({f"{n} ({pid})" for v in busy.values() for pid, n in v})[:5])
            detail += f"\n\nÇalışma dizini bu klasörlerde olan süreçler var, bu klasörler atlanır: {names}"
        if not self.confirm(f"{len(sel)} klasör silinsin mi? ({fmt_bytes(total)})", detail, "Sil"):
            return
        todo = [s for s in sel if s[0] not in busy]
        roots = list(self.clean_roots)
        self.clean_status.set_text("Siliniyor…")

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
        msg = f"{fmt_bytes(freed)} boşaltıldı."
        if skipped:
            msg += f"  {skipped} klasör kullanımda olduğu için atlandı."
        if errs:
            msg += "  Hata: " + "; ".join(errs[:2])
        self.flash(msg, 10)
        self.update_clean_status()
        return False

    def docker_df_refresh(self):
        def work():
            rows, err = docker_df()
            GLib.idle_add(self.on_docker_df, rows, err)
        threading.Thread(target=work, daemon=True).start()

    def on_docker_df(self, rows, err):
        names = {"Images": "İmaj", "Containers": "Konteyner", "Local Volumes": "Volume",
                 "Build Cache": "Build önbelleği"}
        if err:
            self.docker_df_label.set_text(f"Docker: erişilemiyor ({err})")
        else:
            parts = [f"{names.get(r.get('Type'), r.get('Type'))}: {r.get('TotalCount')} · {r.get('Size')} "
                     f"(kurtarılabilir {r.get('Reclaimable')})" for r in rows]
            self.docker_df_label.set_text("Docker: " + "   │   ".join(parts))
        return False

    def docker_prune(self, what, label):
        if not self.confirm(f"{label} silinsin mi?",
                            "Yalnızca kullanılmayan öğeler silinir; çalışan konteynerler ve volume'lar etkilenmez.",
                            "Sil"):
            return
        self.flash(f"docker {what} prune çalışıyor…", 120)

        def work():
            out, err = _docker_run([what, "prune", "-f"], 300)
            last = (out.strip().splitlines() or [""])[-1]
            GLib.idle_add(self.after_prune, label, err or last)
        threading.Thread(target=work, daemon=True).start()

    def after_prune(self, label, text):
        self.flash(f"{label}: {text}", 10)
        self.docker_df_refresh()
        return False

    # -------------------------------------------------------------- dosya / klasor sahibi
    def open_owner_finder(self):
        if self.finder is not None:
            self.finder.present()
            return
        win = self.finder = Gtk.Window(title="Dosya / klasör sahibini bul")
        win.set_default_size(880, 460)
        if self.toplevel():
            win.set_transient_for(self.toplevel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(10)
        top = Gtk.Box(spacing=8)
        entry = Gtk.Entry(placeholder_text="Dosya ya da klasör yolu (ör. ~/Desktop/proje/bin/Debug)")
        entry.set_hexpand(True)
        btn_file = Gtk.Button(label="Dosya seç…")
        btn_dir = Gtk.Button(label="Klasör seç…")
        btn_go = Gtk.Button(label="Ara")
        btn_go.get_style_context().add_class("suggested-action")
        for w in (entry, btn_file, btn_dir, btn_go):
            top.pack_start(w, w is entry, True, 0)
        box.pack_start(top, False, False, 0)

        store = Gtk.ListStore(int, str, str, str)
        tv = Gtk.TreeView(model=store)
        for i, (title, width) in enumerate((("PID", 70), ("Ad", 170), ("Nasıl kullanıyor", 150), ("Yol", 400))):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(ellipsize=3), text=i)
            col.set_resizable(True)
            col.set_min_width(width)
            col.set_sort_column_id(i)
            tv.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.add(tv)
        box.pack_start(scroll, True, True, 0)

        info = Gtk.Label(label="Bir yol seç; o yolu açık tutan, çalışma dizini yapan ya da belleğe yükleyen "
                               "süreçler listelenir.", xalign=0, wrap=True)
        box.pack_start(info, False, False, 0)
        bottom = Gtk.Box(spacing=8)
        b_show = Gtk.Button(label="İşlemlerde göster")
        b_term = Gtk.Button(label="Görevi sonlandır")
        b_kill = Gtk.Button(label="Zorla sonlandır")
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
                info.set_text("Bu yol bulunamadı.")
                return
            btn_go.set_sensitive(False)
            info.set_text("Taranıyor…")

            def work():
                found, denied = find_owners(path)
                GLib.idle_add(done, found, denied)
            threading.Thread(target=work, daemon=True).start()

        def done(found, denied):
            btn_go.set_sensitive(True)
            store.clear()
            for row in found:
                store.append(list(row))
            msg = f"{len(found)} süreç bu yolu kullanıyor." if found else "Bu yolu kullanan bir süreç bulunamadı."
            if denied:
                msg += f"  ({denied} sürecin dosyaları okunamadı; başka kullanıcılara ait olabilir, sudo gerekir.)"
            info.set_text(msg)
            return False

        def choose(action, title):
            dlg = Gtk.FileChooserDialog(title=title, transient_for=win, action=action)
            dlg.add_buttons("İptal", Gtk.ResponseType.CANCEL, "Seç", Gtk.ResponseType.OK)
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
                info.set_text("Önce listeden bir süreç seç.")
                return
            listing = "\n".join(f"{pid}  {name}" for pid, name in sel[:10])
            if not self.confirm(f"{len(sel)} süreç {signal.Signals(sig).name} ile sonlandırılsın mı?", listing,
                                "Sonlandır"):
                return
            ok, errs = kill_pids([p for p, _n in sel], sig, False)
            info.set_text(f"{signal.Signals(sig).name}: {ok} sürece gönderildi." + (f"  Hata: {errs[0]}" if errs else ""))
            GLib.timeout_add(900, lambda: search() or False)

        entry.connect("activate", search)
        btn_go.connect("clicked", search)
        btn_file.connect("clicked", lambda _b: choose(Gtk.FileChooserAction.OPEN, "Dosya seç"))
        btn_dir.connect("clicked", lambda _b: choose(Gtk.FileChooserAction.SELECT_FOLDER, "Klasör seç"))
        b_show.connect("clicked", lambda _b: show_in_list())
        b_term.connect("clicked", lambda _b: end(signal.SIGTERM))
        b_kill.connect("clicked", lambda _b: end(signal.SIGKILL))
        tv.connect("row-activated", lambda *_a: show_in_list())
        win.connect("destroy", lambda _w: setattr(self, "finder", None))
        win.show_all()
        entry.grab_focus()
        win.test_parts = (entry, store, info, btn_go)  # testler icin

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

        freq = psutil.cpu_freq()
        load = os.getloadavg()
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
        self.s_mem.set("swap", f"{fmt_bytes(sw.used)} / {fmt_bytes(sw.total)}")
        self.s_mem.set("pct", f"{vm.percent:.0f}%")
        self.s_disk.set("r", fmt_bytes(rd) + "/s")
        self.s_disk.set("w", fmt_bytes(wr) + "/s")
        self.s_disk.set("rt", fmt_bytes(disk.read_bytes))
        self.s_disk.set("wt", fmt_bytes(disk.write_bytes))
        self.s_net.set("d", fmt_bytes(dn) + "/s")
        self.s_net.set("u", fmt_bytes(up) + "/s")
        self.s_net.set("dt", fmt_bytes(net.bytes_recv))
        self.s_net.set("ut", fmt_bytes(net.bytes_sent))

        self.hdr_cpu.set_markup(f"<b>{self.cpu_pct:.0f}%</b>\nCPU")
        self.hdr_mem.set_markup(f"<b>{vm.percent:.0f}%</b>\nBellek")

        if self.tick_n % 2 == 1 and not self.paused and not self.menu_open:
            self.refresh_processes()
        elif time.monotonic() >= self.flash_until and self.flash_text:
            self.flash_text = ""
            self.update_status()
            self.update_docker_status()
        if (self.root.get_current_page() == TAB_DOCKER and self.tick_n % 4 == 0 and not self.paused
                and not self.docker_busy):
            self.docker_refresh()
        return True

    # -------------------------------------------------------------- ayarlari kaydet / yukle
    def restore_view(self):
        page = self.settings.get("tab")
        if isinstance(page, int) and 0 <= page <= TAB_CLEAN:
            self.root.set_current_page(page)
        perf = self.settings.get("perf_page")
        if perf and self.stack.get_child_by_name(perf):
            self.stack.set_visible_child_name(perf)

    def save_settings(self, win=None):
        data = {
            "version": 1,
            "tab": self.root.get_current_page(),
            "perf_page": self.stack.get_visible_child_name(),
            "tree_mode": self.tree_mode,
            "dotnet_only": self.dotnet_only,
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
            "clean_roots": self.clean_roots,
        }
        if win is not None:
            data["window"] = list(win.get_size())
        write_settings(data)


class App(Gtk.Application):
    def __init__(self, app_id=APP_ID):
        super().__init__(application_id=app_id)
        self.tm = None

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            tm = self.tm = TaskManager()
            win = Gtk.ApplicationWindow(application=self, title="Görev Yöneticisi")
            size = tm.settings.get("window")
            if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and v > 200 for v in size):
                win.set_default_size(*size)
            else:
                win.set_default_size(1180, 720)
            win.set_icon_name("utilities-system-monitor")
            win.add(tm.root)
            tm.win = win
            win.connect("key-press-event", tm.on_key)
            win.connect("delete-event", lambda w, _e: tm.save_settings(w) or False)
            win.show_all()
            tm.restore_view()
        win.present()


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
                            ("docker", TAB_DOCKER, None)):
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
    steps = steps or ["proc", "tree", "docker", "ports", "projects", "portstab", "clean"]
    for name in steps:
        if name == "proc":
            tm.root.set_current_page(TAB_PROCESSES)
        elif name == "tree":
            tm.root.set_current_page(TAB_PROCESSES)
            tm.search.set_text("")
            tm.btn_tree.set_active(True)
            tm.btn_dotnet.set_active(True)
        elif name == "docker":
            tm.btn_tree.set_active(False)
            tm.btn_dotnet.set_active(False)
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
    if len(sys.argv) > 2 and sys.argv[1] == "--selfshot":
        selfshot(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--screenshots":
        screenshots(sys.argv[2])
    else:
        sys.exit(App().run(sys.argv[:1]))
