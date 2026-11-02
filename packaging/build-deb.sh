#!/bin/sh
# .deb paketi uretir:  packaging/build-deb.sh   ->  dist/tskmngr_<surum>_all.deb
# Kurulum:             sudo apt install ./dist/tskmngr_<surum>_all.deb
set -eu
umask 022

here=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$here/taskmgr_gui.py")
[ -n "$version" ] || { echo "VERSION taskmgr_gui.py icinde bulunamadi" >&2; exit 1; }

pkg=tskmngr
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/${pkg}_${version}_all"

install -d "$root/DEBIAN" "$root/usr/lib/$pkg" "$root/usr/bin" "$root/usr/share/applications" "$root/usr/share/doc/$pkg"
install -m 644 "$here"/taskmgr_gui.py "$here"/tm_*.py "$root/usr/lib/$pkg/"
chmod 755 "$root/usr/lib/$pkg/taskmgr_gui.py"
install -m 644 "$here/README.md" "$root/usr/share/doc/$pkg/README.md"
install -m 644 "$here/LICENSE" "$root/usr/share/doc/$pkg/copyright"

cat > "$root/usr/bin/tskmngr" <<'LAUNCHER'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/tskmngr/taskmgr_gui.py "$@"
LAUNCHER
chmod 755 "$root/usr/bin/tskmngr"

cat > "$root/usr/share/applications/tr.teginsoft.TaskMgr.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Görev Yöneticisi
Name[en]=Task Manager
Name[de]=Task-Manager
Name[fr]=Gestionnaire de tâches
Comment=Süreçleri, CPU/RAM/Disk/Ağ kullanımını izle ve sonlandır
Comment[en]=Monitor and end processes, track CPU/RAM/disk/network usage
Comment[de]=Prozesse überwachen und beenden, CPU-/RAM-/Datenträger-/Netzwerkauslastung verfolgen
Comment[fr]=Surveiller et terminer les processus, suivre l'utilisation CPU/RAM/disque/réseau
Exec=tskmngr
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
StartupWMClass=tr.teginsoft.TaskMgr
DESKTOP

size=$(du -sk "$root/usr" | cut -f1)
cat > "$root/DEBIAN/control" <<CONTROL
Package: $pkg
Version: $version
Section: utils
Priority: optional
Architecture: all
Installed-Size: $size
Depends: python3, python3-gi, gir1.2-gtk-3.0, python3-psutil, gir1.2-gdkpixbuf-2.0, librsvg2-common | glycin-loaders
Recommends: policykit-1 | pkexec
Suggests: docker.io | docker-ce-cli
Maintainer: ahmetemincoban <info@teginsoft.com>
Homepage: https://github.com/ahmetemincoban/UbuntuTaskManager
Description: Windows tarzı görev yöneticisi (GTK3)
 Süreçler, CPU/bellek/disk/ağ grafikleri, baskı (PSI) geçmişi, Docker, portlar,
 temizlik ve geliştirme profilleri (.NET, Angular, React, Java, Python, Rust...).
CONTROL

mkdir -p "$here/dist"
out="$here/dist/${pkg}_${version}_all.deb"
dpkg-deb --build --root-owner-group "$root" "$out" >/dev/null
echo "$out"
