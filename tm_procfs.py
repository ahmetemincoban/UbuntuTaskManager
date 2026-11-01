"""/proc ve soket okuyan islevler + istege bagli root yardimcisi.

Bu dosyanin tamami hem normal kullanici olarak hem de 'Yetkili okuma' acilinca pkexec ile root olarak
calistirilir (python3 -I -c <bu dosyanin metni>). Root tarafi YALNIZCA OKUR: surec bellegi/disk sayaclari,
dinleyen soketler, dosya sahipleri. Hicbir sey yazmaz, sinyal gondermez; sonlandirma islemleri hep normal
kullanici olarak yapilir. Uygulama kapaninca boru kapanir ve yardimci kendiliginden cikar.
"""
import json
import os
import select
import subprocess
import sys
import threading

try:
    import psutil
except ImportError:  # root ortaminda yoksa soket okuma devre disi kalir
    psutil = None

try:
    from tm_i18n import _
except ImportError:  # root yardimcisi (python3 -I -c) kardes modulleri goremez; orada metin cevrilmez
    def _(text):
        return text


# ---------------------------------------------------------------- okuma islevleri (root tarafi da kullanir)
def read_stats():
    """{pid: [pss_bayt, okunan_bayt, yazilan_bayt]} (okunamayan alan -1). Tum surecler (root ise)."""
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pss = rb = wb = -1
        try:
            with open(f"/proc/{entry}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        pss = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass
        try:
            with open(f"/proc/{entry}/io") as f:
                for line in f:
                    if line.startswith("read_bytes:"):
                        rb = int(line.split()[1])
                    elif line.startswith("write_bytes:"):
                        wb = int(line.split()[1])
        except OSError:
            pass
        if pss >= 0 or rb >= 0:
            out[int(entry)] = [pss, rb, wb]
    return out


def listening_sockets():
    """-> [(pid | None, port, 'tcp'|'udp', ip)]. Root'a ait sureclerin pid'i root olmadan None gelir."""
    if psutil is None:
        return []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    out = []
    for c in conns:
        if not c.laddr:
            continue
        if c.type == 1:  # SOCK_STREAM
            if c.status != "LISTEN":
                continue
            proto = "tcp"
        else:
            if c.laddr.port >= 32768:  # gecici (ephemeral) UDP portlari gurultu
                continue
            proto = "udp"
        out.append((c.pid or None, c.laddr.port, proto, c.laddr.ip))
    return out


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


# ---------------------------------------------------------------- root tarafi: satir tabanli JSON dongusu
def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        req = {}
        try:
            req = json.loads(line)
            op = req.get("op")
            if op == "ping":
                res = os.geteuid()
            elif op == "stats":
                res = read_stats()
            elif op == "sockets":
                res = listening_sockets()
            elif op == "owners" and isinstance(req.get("path"), str):
                res = find_owners(req["path"])
            elif op == "quit":
                return
            else:
                raise ValueError("bilinmeyen istek")
            reply = {"id": req.get("id"), "ok": True, "result": res}
        except Exception as e:  # noqa: BLE001 - dongu hicbir istekte olmemeli
            reply = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False, "error": str(e)}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


# ---------------------------------------------------------------- istemci (normal kullanici tarafi)
class HelperError(Exception):
    pass


def helper_source():
    """Root'a verilecek kod: bu dosyanin kendisi."""
    with open(os.path.realpath(__file__), encoding="utf-8") as f:
        return f.read()


class PrivHelper:
    """pkexec ile root olarak calisan okuma yardimcisi. request() thread-guvenlidir."""

    def __init__(self, cmd=None):
        self.cmd = cmd or ["pkexec", "/usr/bin/python3", "-I", "-c", helper_source()]
        self.proc = None
        self.lock = threading.Lock()
        self.seq = 0

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Parola penceresi acilir; kullanici iptal ederse HelperError."""
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            raise HelperError(_("başlatılamadı: {e}").format(e=e))
        uid = self.request("ping", timeout=180)
        return uid

    def request(self, op, timeout=30, **args):
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                raise HelperError(_("yardımcı çalışmıyor (parola penceresi iptal edilmiş olabilir)"))
            self.seq += 1
            msg = dict(args, op=op, id=self.seq)
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                ready, _w, _x = select.select([proc.stdout], [], [], timeout)
                if not ready:
                    self.stop()
                    raise HelperError(_("yardımcı cevap vermedi"))
                line = proc.stdout.readline()
            except (OSError, ValueError):
                self.stop()
                raise HelperError(_("yardımcıyla bağlantı koptu"))
            if not line:
                self.stop()
                raise HelperError(_("yardımcı kapandı (parola iptal edildi ya da yetki verilmedi)"))
            reply = json.loads(line)
            if not reply.get("ok"):
                raise HelperError(reply.get("error") or "hata")
            return reply["result"]

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # EOF -> root tarafi cikar
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    serve()
