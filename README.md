# UbuntuTaskManager

Ubuntu için Windows Görev Yöneticisi benzeri, GTK3 tabanlı masaüstü uygulaması. Özellikle biriken
`dotnet` / MSBuild süreçlerini bulup temizlemek için yazıldı.

## Özellikler

- **İşlemler:** CPU / bellek / port / süre sütunlarıyla süreç listesi, sıralama, arama, SIGTERM / SIGKILL,
  süreç ağacı görünümü, "MSBuild artıklarını temizle", sütun seçimi (başlığa sağ tık).
  Sağ tık menüsü: dondur / devam ettir (SIGSTOP / SIGCONT), öncelik (nice), CPU çekirdeği atama.
  Çift tıklama: süreç ayrıntıları (açık dosyalar, bağlantılar, alt süreçler, ortam değişkenleri; anlık görünüm).
- **Doğru bellek (PSS):** paylaşılan sayfalar sürecler arasında bölüştürülür, toplam gerçek RAM'e yaklaşır.
  Yalnızca kendi süreçlerin için okunabilir; arka planda hesaplanır.
- **Performans:** CPU (çekirdek bazında), bellek, disk ve ağ grafikleri.
- **Docker:** konteyner listesi, istatistikler, başlat / durdur / sil, canlı loglar.
- **Projeler:** süreçleri çalışma dizinine göre git deposu / proje bazında gruplar.
- **Portlar:** tüm dinleyen portlar, sahibi süreç / proje / Docker konteyneri; portu kapat ya da konteyneri durdur.
- **Temizlik:** `bin/obj` (.NET), `node_modules`, `target` klasörlerinin boyutları ve toplu silme; Docker disk kullanımı,
  boşta imaj ve build önbelleği temizliği. Yalnızca sekme açılınca / "Yeniden tara" ile taranır; silmeden önce onay ister,
  çalışma dizini bu klasörlerde olan süreç varsa atlar. Volume'lar asla silinmez.
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
