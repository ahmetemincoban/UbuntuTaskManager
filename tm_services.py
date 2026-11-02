"""Kullanici servisleri (systemctl --user) ve oturum acilinca baslayan uygulamalar (autostart)."""
import configparser
import glob
import os
import subprocess

from tm_i18n import _

AUTOSTART_USER = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
                              "autostart")
AUTOSTART_SYSTEM = "/etc/xdg/autostart"
SERVICE_VERBS = ("start", "stop", "restart", "enable", "disable")


def _run(args, timeout=15):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", str(e)
    err = res.stderr.strip().splitlines()
    return res.stdout, (err[-1] if res.returncode and err else None)


def list_user_services():
    """-> ([dict(unit, active, sub, enabled, mem, pid, desc)], hata)"""
    out, err = _run(["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend", "--plain",
                     "--no-pager"])
    if err and not out:
        return [], err
    units = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or parts[1] == "not-found":
            continue
        units.append(dict(unit=parts[0], active=parts[2], sub=parts[3], desc=parts[4] if len(parts) > 4 else "",
                          enabled="", mem=0, pid=0))
    files, _e = _run(["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend", "--plain",
                      "--no-pager"])
    state = {}
    for line in files.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            state[parts[0]] = parts[1]
    for u in units:
        u["enabled"] = state.get(u["unit"], "")
    running = [u for u in units if u["active"] == "active"]
    if running:
        props, _e = _run(["systemctl", "--user", "show", "-p", "Id,MemoryCurrent,MainPID", "--no-pager",
                          *[u["unit"] for u in running]])
        by_id = {u["unit"]: u for u in running}
        cur = None
        for line in props.splitlines():
            key, _s, val = line.partition("=")
            if key == "Id":
                cur = by_id.get(val)
            elif cur is not None and key == "MemoryCurrent" and val.isdigit() and int(val) < 1 << 60:
                cur["mem"] = int(val)
            elif cur is not None and key == "MainPID" and val.isdigit():
                cur["pid"] = int(val)
    units.sort(key=lambda u: (u["active"] != "active", u["unit"]))
    return units, None


def service_action(unit, verb):
    """-> hata metni | None"""
    if verb not in SERVICE_VERBS or not unit.endswith(".service"):
        return _("geçersiz işlem")
    _out, err = _run(["systemctl", "--user", verb, unit], timeout=30)
    return err


# ---------------------------------------------------------------- autostart (.desktop)
def _parse(path):
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8")
        return cp["Desktop Entry"] if cp.has_section("Desktop Entry") else None
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None


def _enabled(entry):
    return (entry.get("Hidden", "false").lower() != "true"
            and entry.get("X-GNOME-Autostart-enabled", "true").lower() != "false")


def list_autostart():
    """Kullanici klasoru sistem klasorunu ezer. -> [dict(file, name, exec, enabled, system)]"""
    items = {}
    for folder, system in ((AUTOSTART_SYSTEM, True), (AUTOSTART_USER, False)):
        for path in sorted(glob.glob(os.path.join(folder, "*.desktop"))):
            entry = _parse(path)
            if entry is None:
                continue
            fname = os.path.basename(path)
            items[fname] = dict(file=fname, name=entry.get("Name", fname), exec=entry.get("Exec", ""),
                                enabled=_enabled(entry), system=system or items.get(fname, {}).get("system", False))
    return sorted(items.values(), key=lambda i: (not i["enabled"], i["name"].lower()))


def set_autostart(fname, enabled):
    """Kullanici klasorunde bir override yazar (sistem dosyasina dokunmaz). -> hata metni | None"""
    if os.sep in fname or not fname.endswith(".desktop"):
        return _("geçersiz dosya adı")
    user_path = os.path.join(AUTOSTART_USER, fname)
    src = user_path if os.path.exists(user_path) else os.path.join(AUTOSTART_SYSTEM, fname)
    try:
        with open(src, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return str(e)
    out, in_entry, done = [], False, set()
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_entry:  # [Desktop Entry] bitti: eksik anahtarlari ekle
                out.extend(_missing(done, enabled))
                in_entry = False
            in_entry = s == "[Desktop Entry]"
        elif in_entry and s.split("=", 1)[0] in ("Hidden", "X-GNOME-Autostart-enabled"):
            key = s.split("=", 1)[0]
            done.add(key)
            line = f"{key}={_value(key, enabled)}"
        out.append(line)
    if in_entry:
        out.extend(_missing(done, enabled))
    try:
        os.makedirs(AUTOSTART_USER, exist_ok=True)
        with open(user_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return str(e)
    return None


def _value(key, enabled):
    if key == "Hidden":
        return "false" if enabled else "true"
    return "true" if enabled else "false"


def _missing(done, enabled):
    return [f"{k}={_value(k, enabled)}" for k in ("Hidden", "X-GNOME-Autostart-enabled") if k not in done]


def desktop_exec(*argv):
    """Exec= satiri icin bosluk/ozel karakter iceren argumanlari tirnaklar (Desktop Entry spec: \\ ve % kacisli)."""
    out = []
    for a in argv:
        if a and not any(c in a for c in ' \t\n"\'\\><~|&;$*?#()`%'):
            out.append(a)
            continue
        esc = a.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
        out.append(f'"{esc}"')
    return " ".join(out)


def write_self_autostart(exec_line, enabled):
    """tskmngr'nin kendi arka plan baslatma dosyasini yazar / siler. -> hata metni | None"""
    path = os.path.join(AUTOSTART_USER, "tr.teginsoft.TaskMgr.desktop")
    if not enabled:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return str(e)
        return None
    try:
        os.makedirs(AUTOSTART_USER, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Görev Yöneticisi (arka plan)\n"
                    f"Exec={exec_line}\nIcon=utilities-system-monitor\nNoDisplay=true\n"
                    "X-GNOME-Autostart-enabled=true\n")
    except OSError as e:
        return str(e)
    return None


def self_autostart_enabled():
    return os.path.exists(os.path.join(AUTOSTART_USER, "tr.teginsoft.TaskMgr.desktop"))
