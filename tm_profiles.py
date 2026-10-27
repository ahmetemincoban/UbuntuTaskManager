"""Gelistirme profilleri: hangi surecler "gelistirme sureci" sayilir, hangileri bostayken kapatilabilir,
hangi klasorler (bin/obj, node_modules...) temizlenebilir.

Profiller birden fazla secilebilir (ornegin .NET + Angular). Kendi profilini eklemek icin
~/.config/tskmngr/profiles.json dosyasina ayni alanlarla bir nesne yaz (ornek: README).
"""
import json
import os
import re

# clean kurali: (klasor adi, dosya isaretleri, tur etiketi, yalniz_gitignore)
#   isaretler: ayni dizinde bulunmasi gereken dosya adi ya da uzanti ('.csproj'); () = isaret aranmaz
#   yalniz_gitignore: 'build', 'dist' gibi genel adlar yalnizca git tarafindan yok sayiliyorsa silinebilir
BUILTIN = {
    "dotnet": dict(
        name=".NET",
        desc="dotnet, MSBuild, VBCSCompiler, testhost; bin/obj klasörleri",
        proc=r"dotnet|MSBuild|VBCSCompiler|testhost|OmniSharp|Microsoft\.CodeAnalysis",
        idle=[("MSBuild node", r"MSBuild\.dll.*nodeReuse:true"), ("VBCSCompiler", r"VBCSCompiler")],
        clean=[("bin", (".csproj", ".fsproj", ".vbproj"), ".NET bin", False),
               ("obj", (".csproj", ".fsproj", ".vbproj"), ".NET obj", False)],
        shutdown=["dotnet", "build-server", "shutdown"],
    ),
    "angular": dict(
        name="Angular",
        desc="node, ng, esbuild, tsserver, karma; node_modules, .angular, dist",
        proc=r"(^|/)(node|ng|npm|npx|pnpm|yarn|esbuild)( |$)|tsserver|typingsInstaller|karma|ChromeHeadless",
        idle=[("esbuild", r"esbuild.*--(service|ping)"), ("jest worker", r"jest-worker"),
              ("Nx daemon", r"nx/src/daemon")],
        clean=[("node_modules", ("package.json",), "node_modules", False),
               (".angular", ("angular.json",), "Angular önbelleği", False),
               ("dist", ("angular.json",), "Angular dist", True)],
    ),
    "react": dict(
        name="React / Next.js",
        desc="node, vite, webpack; node_modules, build, dist, .next",
        proc=r"(^|/)(node|npm|npx|pnpm|yarn|esbuild|vite|next)( |$)|webpack|tsserver|typingsInstaller|jest",
        idle=[("esbuild", r"esbuild.*--(service|ping)"), ("jest worker", r"jest-worker")],
        clean=[("node_modules", ("package.json",), "node_modules", False),
               (".next", ("package.json",), "Next.js önbelleği", False),
               (".parcel-cache", ("package.json",), "Parcel önbelleği", False),
               ("build", ("package.json",), "React build", True),
               ("dist", ("package.json",), "dist", True)],
    ),
    "vue": dict(
        name="Vue / Nuxt",
        desc="node, vite; node_modules, dist, .nuxt, .output",
        proc=r"(^|/)(node|npm|npx|pnpm|yarn|esbuild|vite|nuxt)( |$)|tsserver|typingsInstaller|vitest",
        idle=[("esbuild", r"esbuild.*--(service|ping)")],
        clean=[("node_modules", ("package.json",), "node_modules", False),
               (".nuxt", ("package.json",), "Nuxt önbelleği", False),
               (".output", ("package.json",), "Nuxt çıktısı", False),
               ("dist", ("package.json",), "dist", True)],
    ),
    "java": dict(
        name="Java / Kotlin",
        desc="java, Gradle, Maven; target, build, .gradle",
        proc=r"(^|/)java( |$)|gradle|mvn|maven|kotlin",
        idle=[("Gradle daemon", r"GradleDaemon"), ("Kotlin daemon", r"KotlinCompileDaemon")],
        clean=[("target", ("pom.xml",), "Maven target", False),
               ("build", ("build.gradle", "build.gradle.kts"), "Gradle build", True),
               (".gradle", ("build.gradle", "build.gradle.kts"), "Gradle önbelleği", False)],
        shutdown=["gradle", "--stop"],
    ),
    "python": dict(
        name="Python",
        desc="python, pytest, uvicorn; __pycache__, .pytest_cache, .mypy_cache",
        proc=r"(^|/)python[0-9.]*( |$)|pytest|uvicorn|gunicorn|celery|jupyter",
        idle=[],
        clean=[("__pycache__", (), "__pycache__", False),
               (".pytest_cache", (), "pytest önbelleği", False),
               (".mypy_cache", (), "mypy önbelleği", False),
               (".ruff_cache", (), "ruff önbelleği", False)],
    ),
    "rust": dict(
        name="Rust",
        desc="cargo, rustc; target",
        proc=r"cargo|rustc|rust-analyzer",
        idle=[],
        clean=[("target", ("Cargo.toml",), "Rust target", False)],
    ),
}
DEFAULT_PROFILES = ["dotnet"]
CUSTOM_FILE = "profiles.json"


def custom_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "tskmngr", CUSTOM_FILE)


def _valid_rx(text):
    try:
        re.compile(text)
        return True
    except (re.error, TypeError):
        return False


def _clean_custom(pid, raw):
    """Kullanici profilini dogrular; gecersizse None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"].strip():
        return None
    out = dict(name=raw["name"].strip(), desc=str(raw.get("desc", "özel profil")), proc=r"(?!x)x", idle=[],
               clean=[], shutdown=None, custom=True)
    if isinstance(raw.get("proc"), str) and _valid_rx(raw["proc"]):
        out["proc"] = raw["proc"]
    for item in raw.get("idle") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(x, str) for x in item) \
                and _valid_rx(item[1]):
            out["idle"].append((item[0], item[1]))
    for item in raw.get("clean") or []:
        # [ad, [isaretler], etiket, yalniz_gitignore]
        if isinstance(item, (list, tuple)) and len(item) >= 1 and isinstance(item[0], str):
            dn = item[0]
            if not dn or "/" in dn or dn in (".", ".."):
                continue
            marks = tuple(m for m in (item[1] if len(item) > 1 and isinstance(item[1], (list, tuple)) else ())
                          if isinstance(m, str))
            kind = item[2] if len(item) > 2 and isinstance(item[2], str) else dn
            # ozel profillerde de genel adlar yalnizca gitignore'daysa silinsin (guvenli taraf)
            out["clean"].append((dn, marks, kind, bool(item[3]) if len(item) > 3 else not marks))
    sh = raw.get("shutdown")
    if isinstance(sh, list) and sh and all(isinstance(x, str) for x in sh):
        out["shutdown"] = sh
    return out


def load_profiles():
    """Yerlesik + kullanici profilleri -> {id: profil}. Bozuk dosya yok sayilir."""
    profiles = {k: dict(v) for k, v in BUILTIN.items()}
    try:
        with open(custom_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return profiles
    if isinstance(data, dict):
        for pid, raw in data.items():
            if isinstance(pid, str) and pid not in BUILTIN:
                prof = _clean_custom(pid, raw)
                if prof:
                    profiles[pid] = prof
    return profiles


class Active:
    """Secili profillerin birlesimi: derlenmis regex'ler ve temizlik kurallari."""

    def __init__(self, ids, profiles=None):
        profiles = profiles or load_profiles()
        self.profiles = profiles
        self.ids = [i for i in ids if i in profiles] if isinstance(ids, (list, tuple)) else []
        self.names = [profiles[i]["name"] for i in self.ids]
        procs = [profiles[i]["proc"] for i in self.ids]
        self.proc_rx = re.compile("|".join(f"(?:{p})" for p in procs), re.I) if procs else None
        self.proc_by_profile = {i: re.compile(profiles[i]["proc"], re.I) for i in self.ids}
        seen, self.idle_rules = set(), []
        for i in self.ids:
            for label, rx in profiles[i]["idle"]:
                if (label, rx) not in seen:
                    seen.add((label, rx))
                    self.idle_rules.append((label, re.compile(rx)))
        seen, self.clean_rules = set(), []
        for i in self.ids:
            for rule in profiles[i]["clean"]:
                if rule not in seen:
                    seen.add(rule)
                    self.clean_rules.append(rule)
        self.shutdowns = [(profiles[i]["name"], profiles[i]["shutdown"]) for i in self.ids if profiles[i].get("shutdown")]

    def is_dev(self, cmd, name=""):
        return bool(self.proc_rx and (self.proc_rx.search(cmd) or self.proc_rx.search(name)))

    def idle_label(self, cmd):
        """Boşta kapatılabilir süreç türü etiketi | None"""
        for label, rx in self.idle_rules:
            if rx.search(cmd):
                return label
        return None

    @property
    def clean_names(self):
        return {r[0] for r in self.clean_rules}

    def needs_ignore(self, dirname):
        """Bu ad yalnızca git tarafından yok sayılıyorsa mı silinebilir (tüm kuralları 'yalnız gitignore' ise)."""
        rules = [r for r in self.clean_rules if r[0] == dirname]
        return bool(rules) and all(r[3] for r in rules)

    def match_clean(self, dirname, files):
        """Klasör adı + üst dizindeki dosya adları -> tur etiketi | None"""
        for name, marks, kind, _ign in self.clean_rules:
            if name != dirname:
                continue
            if not marks or any(f == m or (m.startswith(".") and f.endswith(m)) for f in files for m in marks):
                return kind
        return None
