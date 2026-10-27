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
- **Disk G/Ç:** süreç başına okuma / yazma hızı sütunları (MB/s).
- **Performans:** CPU (çekirdek bazında), bellek, disk ve ağ grafikleri; **Baskı (PSI)** sayfası: CPU / bellek / disk
  kaynağı beklerken işlerin ne kadar yavaşladığı ve o an en çok kullananlar.
- **Geçmiş:** sistem yükü ve en çok kaynak kullanan süreçler diske kaydedilir; grafik, "takılma anları" (hangi kaynak
  darboğazdı, suçlular kimdi), "en çok yiyenler" ve olaylar (otomatik temizlik, bellek uyarısı) tabloları.
- **Docker:** konteyner listesi, istatistikler, başlat / durdur / sil, canlı loglar; bellek limiti sütunu,
  "aynı compose projesini seç" düğmesi.
- **Projeler:** süreçleri çalışma dizinine göre git deposu / proje bazında gruplar; git dalı ve değişiklik sayısı.
- **Servisler:** `systemctl --user` servisleri ve oturum açılışında başlayan uygulamalar; başlat / durdur / yeniden
  başlat, başlangıçta aç / kapat. Oturumu bozacak servisler (dbus, gnome-shell…) korumalıdır.
- **Portlar:** tüm dinleyen portlar, sahibi süreç / proje / Docker konteyneri; portu kapat ya da konteyneri durdur.
- **Temizlik:** `bin/obj` (.NET), `node_modules`, `target` klasörlerinin boyutları ve toplu silme; Docker disk kullanımı,
  boşta imaj ve build önbelleği temizliği. Yalnızca sekme açılınca / "Yeniden tara" ile taranır; silmeden önce onay ister,
  çalışma dizini bu klasörlerde olan süreç varsa ya da içinde git'in izlediği dosya varsa atlar. Volume'lar asla silinmez.
- **Dosya sahibi:** bir dosya ya da klasörü hangi sürecin tuttuğunu bulur (Ctrl+O).
- **Takılı süreç uyarısı:** uzun süre disk beklemesinde (D) kalan süreçleri işaretler; aramaya `takılı` yaz.
- **Port araması:** aramaya `:5000` yaz.

## Ayarlar ve geçmiş kaydı

Sağ üstteki **⚙ Ayarlar** (Ctrl+,) penceresi; her değişiklik hemen `~/.config/tskmngr/settings.json` dosyasına yazılır.

- **Geçmiş kaydı:** `~/.local/share/tskmngr/history.db` (SQLite). Sistem satırı ~10 sn'de, süreç satırları dakikada
  bir (takılma anında 10 sn'de bir) yazılır; birkaç MB'ı geçmez. Varsayılan **14 gün** saklanır; **gün sayısı
  Ayarlar'dan değiştirilebilir (1–365)**. Süresi dolan kayıtlar açılışta ve saatte bir otomatik silinir. Süreyi
  kısaltınca silme kazara olmasın diye hemen yapılmaz: "Şimdi eski kayıtları sil" düğmesine bas. "Tüm geçmişi sil"
  ve kaydı tamamen kapatma da buradadır. Takılma eşiği (PSI %) de ayarlanır.
- **Arka plan:** pencere kapanınca uygulama gizlenir ve kayıt / uyarılar sürer (kapatılabilir). Tamamen çıkmak için
  Ctrl+Q ya da "Tamamen çık". `tskmngr --background` gizli başlatır; Ayarlar'dan oturum açılışına eklenebilir.
  Gizliyken arayüz güncellemeleri seyreltilir, CPU kullanımı ihmal edilebilir düzeydedir.
- **Bellek bildirimi:** RAM eşiği (varsayılan %90) aşılınca masaüstü bildirimi (10 dk'da bir).
- **Otomatik temizlik (kapalı gelir):** RAM eşiğin üstündeyken ve boşta (CPU < %2) kalan MSBuild node /
  VBCSCompiler belirlenen süreden sonra kapatılır. "dotnet build-server shutdown" düğmesi de vardır.

## Yetkili okuma (isteğe bağlı)

Ayarlar ya da Portlar / Dosya sahibi penceresindeki **Yetkili okuma** `pkexec` ile salt okunur bir yardımcıyı
root olarak başlatır (parola sorar, her oturumda yeniden açılır, kalıcı değildir). Başka kullanıcıların süreçleri
için port, PSS, disk G/Ç ve dosya sahibi bilgisi görünür. Yardımcı yalnızca okur; sonlandırma işlemleri hep normal
kullanıcıyla yapılır.

> **Güvenlik notu:** Root'a verilen kod bu klasördeki `tm_procfs.py` dosyasıdır. Klasöre yazabilen biri bu kodu
> değiştirip yetkili okuma açıldığında root olarak çalıştırabilir; uygulama klasörünü yalnızca kendi kullanıcının
> yazabildiği bir yerde tut (`chmod go-w`).

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
