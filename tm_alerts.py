"""Uyari mantigi (arayuzden bagimsiz, test edilebilir): korunan surecler, takilma suclusu, swap/zram, disk doluluk."""
import glob
import os

import psutil

# Oturumu ya da masaustunu tasiyan surecler: otomatik olarak asla "suclu" gosterilmez / oldurme duğmesi verilmez
ESSENTIAL = {
    "systemd", "init", "gnome-shell", "gnome-session-binary", "gnome-session", "xorg", "xwayland", "mutter",
    "kwin_x11", "kwin_wayland", "plasmashell", "gdm", "gdm3", "gdm-wayland-session", "dbus-daemon", "dbus-broker",
    "pipewire", "pipewire-pulse", "wireplumber", "pulseaudio", "ibus-daemon", "networkmanager", "polkitd",
    "gnome-keyring-daemon", "login", "sshd",
}
MAX_PATTERNS = 100
MAX_PATTERN_LEN = 200


def clean_patterns(raw):
    """Ayar dosyasindan gelen korunanlar listesi -> temiz (kucuk harf olmayan) satirlar"""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            item = item.strip()[:MAX_PATTERN_LEN]
            if item and item.lower() not in [o.lower() for o in out]:
                out.append(item)
    return out[:MAX_PATTERNS]


def parse_patterns(text):
    """Ayarlar'daki metin kutusu (her satir bir kalip) -> liste"""
    return clean_patterns(text.splitlines())


def is_protected(patterns, name="", cmd="", project=""):
    """Kalip; ad, komut satiri ya da proje adi/yolunda (buyuk-kucuk harf duyarsiz) geciyorsa korunur."""
    if not patterns:
        return False
    hay = " \n".join((name or "", cmd or "", project or "")).lower()
    return any(p.lower() in hay for p in patterns)


def is_essential(name, cmd=""):
    low = (name or "").lower()
    if low in ESSENTIAL or low.startswith("gnome-shell") or low.startswith("gdm"):
        return True
    return "taskmgr_gui.py" in (cmd or "")  # kendimizi oldurmeyelim


def pick_culprit(procs, cause, me_uid, patterns=(), project_of=None):
    """Baski nedenine (cpu | mem | io) gore en buyuk kullanici sureci -> (satir, deger) | None.
    Yalnizca kendi kullanicimizin, korunmayan, temel olmayan ve anlamli miktarda kaynak kullanan sureclerden secer."""
    floors = {"cpu": 10.0, "mem": 300 << 20, "io": 1 << 20}

    def value(p):
        if cause == "cpu":
            return p["cpu"]
        if cause == "mem":
            return p.get("pss") or p["rss"]
        return max(0, p.get("io_r", 0)) + max(0, p.get("io_w", 0))

    best = None
    for p in procs:
        if p["uid"] != me_uid or is_essential(p["name"], p["cmd"]):
            continue
        if is_protected(patterns, p["name"], p["cmd"], (project_of or {}).get(p["pid"], "")):
            continue
        v = value(p)
        if v >= floors[cause] and (best is None or v > best[1]):
            best = (p, v)
    return best


# ---------------------------------------------------------------- swap / zram
def zram_stats(pattern="/sys/block/zram*/mm_stat"):
    """zram aygitlari toplami -> (orijinal bayt, sikistirilmis bayt, bellekte kaplanan bayt) | None"""
    orig = compr = used = 0
    found = False
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                parts = f.read().split()
            orig += int(parts[0])
            compr += int(parts[1])
            used += int(parts[2])
            found = True
        except (OSError, ValueError, IndexError):
            continue
    return (orig, compr, used) if found else None


def swap_devices(path="/proc/swaps"):
    """-> [(aygit, tur, boyut bayt, kullanilan bayt, oncelik)]"""
    out = []
    try:
        with open(path) as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 5:
                    out.append((parts[0], parts[1], int(parts[2]) * 1024, int(parts[3]) * 1024, int(parts[4])))
    except (OSError, ValueError):
        return []
    return out


# ---------------------------------------------------------------- disk doluluk
def watched_mounts():
    """Izlenecek bagli noktalar: '/' ve ev dizininin bulundugu disk (ayni disk ise tek)."""
    seen, out = set(), []
    for path in ("/", os.path.expanduser("~")):
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev not in seen:
            seen.add(dev)
            out.append(path)
    return out


def full_disks(threshold, paths=None):
    """Doluluk yuzdesi esigi asan yerler -> [(yol, yuzde, bos bayt)]"""
    out = []
    for path in paths or watched_mounts():
        try:
            u = psutil.disk_usage(path)
        except OSError:
            continue
        if u.percent >= threshold:
            out.append((path, u.percent, u.free))
    return out
