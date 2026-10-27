"""Gecmis kaydi (SQLite) - saklama suresi ayarlanabilir, eskiler otomatik silinir.

Neden var: takilma anlarini sonradan teshis etmek (o an CPU mu, RAM mi, disk mi darbogazdi; kim yiyordu?).
Boyut sinirlidir: sistem satiri 10 sn'de bir, surec satirlari dakikada bir (takilma aninda 10 sn'de bir).
Varsayilan 14 gun saklanir; bu sureyi asan kayitlar acilista ve saatte bir silinir.
"""
import os
import queue
import sqlite3
import threading
import time

SYS_EVERY = 10      # sn: sistem satiri
TOP_EVERY = 60      # sn: surec satirlari (takilma aninda SYS_EVERY)
DEFAULT_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS sys(ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, swap REAL,
    psi_cpu REAL, psi_mem REAL, psi_io REAL, load1 REAL, disk_r INTEGER, disk_w INTEGER);
CREATE TABLE IF NOT EXISTS top(ts INTEGER, pid INTEGER, name TEXT, project TEXT, cpu REAL, mem INTEGER,
    io_r INTEGER, io_w INTEGER);
CREATE INDEX IF NOT EXISTS top_ts ON top(ts);
CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "tskmngr")


def db_path():
    return os.path.join(data_dir(), "history.db")


def read_psi():
    """/proc/pressure -> {'cpu': {'some': (avg10, avg60, avg300)}, 'memory': {...}, 'io': {...}} (yoksa bos)."""
    out = {}
    for res in ("cpu", "memory", "io"):
        try:
            with open(f"/proc/pressure/{res}") as f:
                for line in f:
                    kind, _, rest = line.partition(" ")
                    d = dict(kv.split("=") for kv in rest.split())
                    out.setdefault(res, {})[kind] = (float(d["avg10"]), float(d["avg60"]), float(d["avg300"]))
        except (OSError, ValueError, KeyError):
            continue
    return out


def psi_some10(psi):
    """(cpu, bellek, disk) 'some avg10' yuzdeleri."""
    return tuple(psi.get(r, {}).get("some", (0.0,))[0] for r in ("cpu", "memory", "io"))


class History:
    def __init__(self, path=None):
        self.path = path or db_path()
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.conn = None
        self._open()
        threading.Thread(target=self._writer, daemon=True).start()

    # ---- baglanti
    def _open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for attempt in (0, 1):
            try:
                fresh = not os.path.exists(self.path)
                conn = sqlite3.connect(self.path, check_same_thread=False)
                if fresh:
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")  # tablolardan once ayarlanmali
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("PRAGMA journal_mode=TRUNCATE")
                conn.executescript(SCHEMA)
                conn.commit()
                self.conn = conn
                return
            except sqlite3.DatabaseError:
                # bozuk dosya: gecmis kaydi onemli degil, sifirdan baslat
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                if attempt:
                    raise

    def _writer(self):
        while True:
            fn = self.q.get()
            try:
                if fn is None:
                    return
                with self.lock:
                    fn(self.conn)
                    self.conn.commit()
            except sqlite3.Error:
                pass
            finally:
                self.q.task_done()

    def flush(self):
        """Bekleyen yazmalar bitene kadar bekler (test / kapanis)."""
        self.q.join()

    def close(self):
        self.flush()
        self.q.put(None)
        with self.lock:
            self.conn.close()

    # ---- yazma
    def add_sample(self, ts, sysrow, toprows):
        ts = int(ts)

        def work(c):
            c.execute("INSERT OR REPLACE INTO sys VALUES(?,?,?,?,?,?,?,?,?,?)", (ts, *sysrow))
            if toprows:
                c.executemany("INSERT INTO top VALUES(?,?,?,?,?,?,?,?)", [(ts, *t) for t in toprows])
        self.q.put(work)

    def add_event(self, ts, kind, text):
        self.q.put(lambda c: c.execute("INSERT INTO events VALUES(?,?,?)", (int(ts), kind, text)))

    def prune(self, days, now=None):
        """days gunden eski her seyi siler -> silinen satir sayisi."""
        cutoff = int((now or time.time()) - days * 86400)
        self.flush()
        n = 0
        with self.lock:
            for table in ("sys", "top", "events"):
                n += self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,)).rowcount
            self.conn.commit()
            self.conn.execute("PRAGMA incremental_vacuum")
            self.conn.commit()
        return n

    def clear(self):
        self.flush()
        with self.lock:
            for table in ("sys", "top", "events"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
            self.conn.execute("PRAGMA incremental_vacuum")
            self.conn.commit()

    # ---- okuma
    def size(self):
        total = 0
        for suffix in ("", "-journal", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def span(self):
        """(en eski ts, en yeni ts, sistem satiri sayisi) | None"""
        with self.lock:
            lo, hi, n = self.conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM sys").fetchone()
        return (lo, hi, n) if n else None

    def series(self, since, until, points=240):
        """-> (kova sn, [(ts, cpu, mem, psi_cpu, psi_mem, psi_io, disk_r, disk_w)]) - kova basina ortalama/tepe"""
        bucket = max(SYS_EVERY, int((until - since) // points))
        with self.lock:
            rows = self.conn.execute(
                "SELECT (ts/?)*? AS b, AVG(cpu), AVG(mem), MAX(psi_cpu), MAX(psi_mem), MAX(psi_io), "
                "AVG(disk_r), AVG(disk_w) FROM sys WHERE ts BETWEEN ? AND ? GROUP BY b ORDER BY b",
                (bucket, bucket, int(since), int(until))).fetchall()
        return bucket, rows

    def offenders(self, since, until, limit=200):
        """Aralikta en cok kaynak kullananlar -> [dict]. avg_cpu: tum ornekler uzerinden ortalama pay
        (listede olmadigi ornekler 0 sayilir); diger alanlar gorundugu ornekler uzerinden."""
        with self.lock:
            total = self.conn.execute("SELECT COUNT(DISTINCT ts) FROM top WHERE ts BETWEEN ? AND ?",
                                      (int(since), int(until))).fetchone()[0]
            rows = self.conn.execute(
                "SELECT name, project, COUNT(*), SUM(cpu), MAX(cpu), MAX(mem), AVG(io_r), AVG(io_w) "
                "FROM top WHERE ts BETWEEN ? AND ? GROUP BY name, project ORDER BY SUM(cpu) DESC LIMIT ?",
                (int(since), int(until), limit)).fetchall()
        return [dict(name=n, project=p or "", seen=c, total=total, avg_cpu=(s or 0) / max(1, total),
                     max_cpu=mc or 0, max_mem=mm or 0, io_r=ir or 0, io_w=iw or 0)
                for n, p, c, s, mc, mm, ir, iw in rows]

    def by_project(self, since, until, threshold=None):
        """Projeye gore toplam -> [dict]. Ayni ornekteki surecler toplanir (bir projenin o andaki toplam CPU / RAM'i).
        avg_cpu: tum ornekler uzerinden ortalama; threshold verilirse stalls: projenin suclular arasinda
        gectigi takilma sayisi."""
        with self.lock:
            total = self.conn.execute("SELECT COUNT(DISTINCT ts) FROM top WHERE ts BETWEEN ? AND ?",
                                      (int(since), int(until))).fetchone()[0]
            rows = self.conn.execute(
                "SELECT project, COUNT(*), SUM(c), MAX(c), MAX(m), AVG(io), MAX(n) FROM ("
                "SELECT COALESCE(project, '') AS project, ts, SUM(cpu) AS c, SUM(mem) AS m, "
                "SUM(io_r + io_w) AS io, COUNT(DISTINCT name) AS n FROM top WHERE ts BETWEEN ? AND ? "
                "GROUP BY COALESCE(project, ''), ts) GROUP BY project ORDER BY SUM(c) DESC",
                (int(since), int(until))).fetchall()
        hits = {}
        if threshold is not None:
            for w in self.stalls(since, until, threshold, limit=100000):
                for project in {c[1] or "" for c in w["culprits"]}:  # takilma basina proje bir kez
                    hits[project] = hits.get(project, 0) + 1
        return [dict(project=p, seen=c, total=total, avg_cpu=(s or 0) / max(1, total), max_cpu=mc or 0,
                     max_mem=mm or 0, io=io or 0, procs=n or 0, stalls=hits.get(p, 0))
                for p, c, s, mc, mm, io, n in rows]

    def stalls(self, since, until, threshold, gap=60, limit=200):
        """Baski (PSI) esigi asilan araliklar, yeniden eskiye -> [dict]"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, cpu, mem, psi_cpu, psi_mem, psi_io FROM sys WHERE ts BETWEEN ? AND ? AND "
                "(psi_cpu >= ? OR psi_mem >= ? OR psi_io >= ?) ORDER BY ts",
                (int(since), int(until), threshold, threshold, threshold)).fetchall()
        windows = []
        for ts, cpu, mem, pc, pm, pio in rows:
            if windows and ts - windows[-1]["end"] <= gap:
                w = windows[-1]
                w["end"] = ts
            else:
                w = dict(start=ts, end=ts, psi_cpu=0.0, psi_mem=0.0, psi_io=0.0, cpu=0.0, mem=0.0)
                windows.append(w)
            w["psi_cpu"], w["psi_mem"], w["psi_io"] = max(w["psi_cpu"], pc), max(w["psi_mem"], pm), max(w["psi_io"], pio)
            w["cpu"], w["mem"] = max(w["cpu"], cpu or 0), max(w["mem"], mem or 0)
        order = {"cpu": "MAX(cpu)", "mem": "MAX(mem)", "io": "MAX(io_r + io_w)"}
        for w in windows:
            peaks = {"cpu": w["psi_cpu"], "mem": w["psi_mem"], "io": w["psi_io"]}
            w["cause"] = max(peaks, key=peaks.get)
            with self.lock:
                w["culprits"] = self.conn.execute(
                    f"SELECT name, project, MAX(cpu), MAX(mem), MAX(io_r + io_w) FROM top "
                    f"WHERE ts BETWEEN ? AND ? GROUP BY name, project ORDER BY {order[w['cause']]} DESC LIMIT 3",
                    (w["start"] - SYS_EVERY, w["end"] + SYS_EVERY)).fetchall()
        windows.reverse()
        return windows[:limit]

    def events(self, since, until, limit=500):
        with self.lock:
            return self.conn.execute(
                "SELECT ts, kind, text FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?",
                (int(since), int(until), limit)).fetchall()


def pick_top(procs):
    """Kayda girecek surecler: CPU'da ilk 6, bellekte ilk 4, diskte (>=1 MB/s) ilk 2."""
    def io(p):
        return max(0, p.get("io_r", 0)) + max(0, p.get("io_w", 0))
    chosen = {}
    for p in sorted(procs, key=lambda p: -p["cpu"])[:6]:
        if p["cpu"] >= 1.0:
            chosen[p["pid"]] = p
    for p in sorted(procs, key=lambda p: -p["mem"])[:4]:
        chosen[p["pid"]] = p
    for p in sorted(procs, key=lambda p: -io(p))[:2]:
        if io(p) >= 1 << 20:
            chosen[p["pid"]] = p
    return list(chosen.values())


class Recorder:
    """Ne zaman / ne kaydedilecegini belirler. Arayuz her yenilemede step() cagirir."""

    def __init__(self, history, stall_psi=25.0):
        self.history = history
        self.stall_psi = stall_psi
        self.last_sys = 0.0
        self.last_top = 0.0

    def step(self, now, sysrow, procs_fn):
        """sysrow: (cpu, mem, swap, psi_cpu, psi_mem, psi_io, load1, disk_r, disk_w). procs_fn: yavas listeyi
        yalnizca gerekince uretir ([{pid, name, project, cpu, mem, io_r, io_w}]). -> kaydedildi mi"""
        if now - self.last_sys < SYS_EVERY - 1.0:
            return False
        self.last_sys = now
        stalled = max(sysrow[3:6]) >= self.stall_psi
        tops = []
        if stalled or now - self.last_top >= TOP_EVERY - 1.0:
            self.last_top = now
            tops = [(p["pid"], p["name"], p["project"], p["cpu"], p["mem"], p.get("io_r", 0), p.get("io_w", 0))
                    for p in pick_top(procs_fn())]
        self.history.add_sample(now, sysrow, tops)
        return True
