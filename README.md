# UbuntuTaskManager

Ubuntu için Windows Görev Yöneticisi benzeri, GTK3 tabanlı masaüstü uygulaması. Özellikle biriken
`dotnet` / MSBuild süreçlerini bulup temizlemek için yazıldı.

## Özellikler

- **İşlemler:** CPU / bellek / port / süre sütunlarıyla süreç listesi, sıralama, arama, SIGTERM / SIGKILL,
  süreç ağacı görünümü, "MSBuild artıklarını temizle", sütun seçimi (başlığa sağ tık).
- **Performans:** CPU (çekirdek bazında), bellek, disk ve ağ grafikleri.
- **Docker:** konteyner listesi, istatistikler, başlat / durdur / sil, canlı loglar.
- **Projeler:** süreçleri çalışma dizinine göre git deposu / proje bazında gruplar.
- **Dosya sahibi:** bir dosya ya da klasörü hangi sürecin tuttuğunu bulur (Ctrl+O).
- **Takılı süreç uyarısı:** uzun süre disk beklemesinde (D) kalan süreçleri işaretler; aramaya `takılı` yaz.
- **Port araması:** aramaya `:5000` yaz.

Hiçbir şey sürekli diske yazılmaz; yalnızca arayüz ayarları `~/.config/tskmngr/settings.json` içinde tutulur.

## Gereksinimler

Sistem `python3`, `python3-gi` (GTK 3), `python3-psutil`. Docker sekmesi için `docker` CLI.

## Kurulum

```bash
git clone https://github.com/ahmetemincoban/UbuntuTaskManager.git
cd UbuntuTaskManager
ln -sf "$PWD/taskmgr_gui.py" ~/.local/bin/tskmngr
sed "s|/path/to/UbuntuTaskManager|$PWD|" tr.teginsoft.TaskMgr.desktop \
  > ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop
```

Sonra terminalden `tskmngr` ya da uygulama menüsünden "Görev Yöneticisi".

Kaldırmak için: `rm ~/.local/bin/tskmngr ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop`
