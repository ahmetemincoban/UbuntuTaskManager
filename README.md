# UbuntuTaskManager

**English** · [Türkçe](README.tr.md)

A GTK3 desktop app for Ubuntu in the spirit of the Windows Task Manager. It was written to find and clean up piled-up
development processes such as `dotnet` / MSBuild, and a **profile** system tunes it for .NET, Angular, React, Vue,
Java, Python and Rust teams.

## Features

- **Processes:** process list with CPU / memory / port / uptime columns, sorting, search, SIGTERM / SIGKILL, a process
  tree view, a "Development only" filter and "Clean up idle leftovers" (both follow the selected profiles), column
  chooser (right-click the header).
  Context menu: freeze / resume (SIGSTOP / SIGCONT), priority (nice), CPU core affinity.
  Double-click: process details (open files, connections, child processes, environment variables; a snapshot).
- **Accurate memory (PSS):** shared pages are divided between processes, so the total approaches real RAM.
  Readable only for your own processes; computed in the background.
- **Disk I/O:** per-process read / write rate columns (MB/s).
- **Performance:** CPU (per core), memory (including swap usage / traffic and the **zram** compression ratio), disk and
  network graphs; a **Pressure (PSI)** page showing how much work slows down while waiting for CPU / memory / disk,
  and who is using the most right now.
- **History:** system load and the top resource users are recorded to disk; a graph, "stalls" (which resource was the
  bottleneck, who the culprits were), "top consumers", totals **per project** (average / peak CPU and RAM, in how many
  stalls it was a culprit) and events (automatic cleanup, memory / stall / disk warnings) tables. Drag over the graph
  to select a time range: the graph zooms in and all tables show only that range (double-click / right-click /
  "Clear selection" resets it).
- **Docker:** container list, statistics, start / stop / remove, live logs; a memory-limit column and a "select the
  same compose project" button.
- **Projects:** groups processes by working directory per git repository / project; git branch and change count.
- **Services:** `systemctl --user` services and apps that start at login; start / stop / restart, enable / disable at
  startup. Services that would break the session (dbus, gnome-shell…) are protected.
- **Ports:** all listening ports with their owner process / project / Docker container; close the port or stop the
  container.
- **Cleanup:** sizes of the selected profiles' regenerable folders (`bin/obj`, `node_modules`, `target`,
  `.angular`…) and bulk deletion; Docker disk usage, idle image and build-cache cleanup. Scanned only when the tab
  is opened / on "Rescan"; asks before deleting, and skips folders where a process has its working directory or that
  contain files tracked by git. Volumes are never deleted.
- **File owner:** finds which process holds a file or folder (Ctrl+O).
- **Stuck-process warning:** marks processes stuck in disk wait (D) for a long time; type `stuck` in the search box.
- **Port search:** type `:5000` in the search box.

## Settings and history

**⚙ Settings** (Ctrl+,) in the top right; every change is written to `~/.config/tskmngr/settings.json` immediately.

- **History logging:** `~/.local/share/tskmngr/history.db` (SQLite). A system row is written about every 10 s and
  process rows once a minute (every 10 s during a stall); it stays within a few MB. **14 days** are kept by default;
  the **number of days can be changed in Settings (1–365)**. Expired records are deleted at startup and once an hour.
  Shortening the period does not delete anything immediately, to avoid accidents: press "Delete old records now".
  "Delete all history" and turning logging off completely are here too. The stall threshold (PSI %) is also set here.
- **Background:** when the window is closed the app hides and logging / warnings continue (can be turned off). To quit
  completely use Ctrl+Q or "Quit completely". `tskmngr --background` starts hidden; it can be added to session startup
  in Settings. While hidden, UI updates are throttled and CPU use is negligible.
- **Memory notification:** a desktop notification when the RAM threshold (default 90%) is exceeded (once every 10 min).
- **Automatic cleanup (off by default):** while RAM is above the threshold, leftover processes that are idle (CPU < 2%)
  (per profile: MSBuild node, VBCSCompiler, Gradle daemon…) are closed after the configured time. The selected
  profiles' build-server shutdown commands (`dotnet build-server shutdown`, `gradle --stop`) appear as buttons.

## Language

The interface is available in **Türkçe, English, Deutsch and Français**. The default is "System language"
(`LANGUAGE` / `LC_ALL` / `LANG`); an unsupported language falls back to English. Change it in
**⚙ Settings → Language** or with `"language"` in `settings.json` (`auto`, `tr`, `en`, `de`, `fr`); choosing a language
restarts the app automatically (the language is read only at startup). Source strings are Turkish; translations live in
the catalog in `tm_lang.py` (`Turkish text -> (en, de, fr)`), and a string missing from the catalog stays Turkish. To add
a string, use `_("...")` in the code and add the three translations to the catalog; `tests/test_i18n.py` catches
missing translations and broken `{placeholders}`.

## Profiles

In **Settings → Profile** tick the technologies you work with (several can be selected; asked on first launch, default
.NET). A profile decides three things: which processes count as "development processes", which leftovers may be closed
when idle (including automatic cleanup) and which folders the Cleanup tab scans.

| Profile | Processes | Closable when idle | Cleaned folders |
|---|---|---|---|
| .NET | dotnet, MSBuild, VBCSCompiler, testhost | MSBuild node, VBCSCompiler | `bin`, `obj` (next to .csproj/.fsproj/.vbproj) |
| Angular | node, ng, esbuild, tsserver, karma | esbuild, jest worker, Nx daemon | `node_modules`, `.angular`, `dist`* |
| React / Next.js | node, vite, webpack, jest | esbuild, jest worker | `node_modules`, `.next`, `.parcel-cache`, `build`*, `dist`* |
| Vue / Nuxt | node, vite, nuxt, vitest | esbuild | `node_modules`, `.nuxt`, `.output`, `dist`* |
| Java / Kotlin | java, gradle, mvn | Gradle / Kotlin daemon | `target` (pom.xml), `build`*, `.gradle` |
| Python | python, pytest, uvicorn, jupyter | – | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` |
| Rust | cargo, rustc, rust-analyzer | – | `target` (next to Cargo.toml) |

\* Generic names such as `build` / `dist` can be deleted only if git ignores them (they are in `.gitignore`). A folder
is not listed unless the matching project file (`package.json`, `.csproj`…) is next to it (except Python caches);
virtual environments (`pyvenv.cfg`) and the insides of `node_modules` are not entered; folders that contain
git-tracked files, or where a process has its working directory, are skipped.

**Your own profile:** add it to `~/.config/tskmngr/profiles.json` (it shows up as "(custom)" in Settings; broken
entries are ignored, built-in profile names cannot be overridden):

```json
{
  "godot": {
    "name": "Godot",
    "desc": "godot; .godot cache",
    "proc": "godot",
    "idle": [["Headless server", "godot.*--headless"]],
    "clean": [[".godot", ["project.godot"], "Godot cache", false]],
    "shutdown": ["echo", "stop"]
  }
}
```

`proc` and `idle` are regular expressions (matched against the command line); a `clean` row has the form
`[folder name, [files that must be next to it], label, only when in .gitignore]`; `shutdown` is optional (it becomes
a button in Settings).

## Notifications and protected processes

- **Stall notification:** if pressure (PSI) stays above the "stall threshold" for a few seconds, your own most
  resource-hungry process for that cause (CPU / memory / disk) is reported, and the notification has a one-click
  **terminate** button (at most once every 5 min). The button sends SIGTERM only if the same process (PID + start time)
  is still running; system processes (gnome-shell, Xorg, systemd, pipewire…) and protected ones are never suggested.
- **Disk full:** if the root disk or the home directory's disk exceeds the threshold (default 90%), a notification with
  an "Open Cleanup" button appears (once every 6 hours per disk).
- **Protected processes:** enter one name, command fragment or project name per line in the box in Settings
  (case-insensitive). Matches are not closed by automatic cleanup, not suggested in the stall notification, and you
  get a warning when terminating them by hand.

## Privileged reading (optional)

**Privileged reading** in Settings or in the Ports / File owner window starts a read-only helper as root via `pkexec`
(asks for a password, is reopened each session, not permanent). Port, PSS, disk I/O and file-owner information
becomes visible for other users' processes. The helper only reads; terminating processes is always done as the normal
user.

> **Security note:** The code given to root is `tm_procfs.py` in this folder. Someone who can write to the folder
> could modify that code and have it run as root when privileged reading is turned on; keep the application folder
> somewhere only your own user can write to (`chmod go-w`). When installed from the `.deb`, the files are owned by
> root under `/usr/lib/tskmngr`, so this risk does not exist.

## Requirements

System `python3`, `python3-gi` (GTK 3), `python3-psutil`. The `docker` CLI for the Docker tab.

## Installation

### .deb package (to hand to friends)

```bash
packaging/build-deb.sh                          # produces dist/tskmngr_<version>_all.deb
sudo apt install ./dist/tskmngr_1.2.0_all.deb   # also installs the dependencies
```

Prebuilt packages are on the [Releases](https://github.com/ahmetemincoban/UbuntuTaskManager/releases) page.
After installing, run `tskmngr` from a terminal or "Task Manager" from the application menu; to remove it use
`sudo apt remove tskmngr`. Settings and history stay in the user's own `~/.config/tskmngr` and
`~/.local/share/tskmngr` folders. Version: `tskmngr --version` (the version number is `VERSION` in `taskmgr_gui.py`).

### From source

```bash
git clone https://github.com/ahmetemincoban/UbuntuTaskManager.git
cd UbuntuTaskManager
ln -sf "$PWD/taskmgr_gui.py" ~/.local/bin/tskmngr
sed "s|/path/to/UbuntuTaskManager|$PWD|" tr.teginsoft.TaskMgr.desktop \
  > ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop
```

Then run `tskmngr` from a terminal or "Task Manager" from the application menu.

To remove: `rm ~/.local/bin/tskmngr ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop`

## Tests

```bash
python3 tests/run_all.py            # everything (takes a few minutes)
python3 tests/test_profiles.py      # one file
```

The tests run in a session with a display (windows are created offscreen) and do not touch the user's settings /
history: `XDG_CONFIG_HOME` and `XDG_DATA_HOME` are redirected to a temp folder. They are not wired to CI; run them by
hand. See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute.

## License

[MIT](LICENSE) © 2026 Teginsoft
