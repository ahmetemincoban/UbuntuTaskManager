# UbuntuTaskManager

Ubuntu için Windows Görev Yöneticisi benzeri, GTK3 tabanlı masaüstü uygulaması. Özellikle biriken
`dotnet` / MSBuild gibi geliştirme süreçlerini bulup temizlemek için yazıldı; **profil** sistemiyle .NET, Angular,
React, Vue, Java, Python ve Rust ekiplerine göre ayarlanır.

## Özellikler

- **İşlemler:** CPU / bellek / port / süre sütunlarıyla süreç listesi, sıralama, arama, SIGTERM / SIGKILL,
  süreç ağacı görünümü, "Sadece geliştirme" filtresi ve "Boşta artıkları temizle" (ikisi de seçili profile göre),
  sütun seçimi (başlığa sağ tık).
  Sağ tık menüsü: dondur / devam ettir (SIGSTOP / SIGCONT), öncelik (nice), CPU çekirdeği atama.
  Çift tıklama: süreç ayrıntıları (açık dosyalar, bağlantılar, alt süreçler, ortam değişkenleri; anlık görünüm).
- **Doğru bellek (PSS):** paylaşılan sayfalar sürecler arasında bölüştürülür, toplam gerçek RAM'e yaklaşır.
  Yalnızca kendi süreçlerin için okunabilir; arka planda hesaplanır.
- **Disk G/Ç:** süreç başına okuma / yazma hızı sütunları (MB/s).
- **Performans:** CPU (çekirdek bazında), bellek (swap kullanımı / trafiği ve **zram** sıkıştırma oranı dahil), disk ve ağ grafikleri; **Baskı (PSI)** sayfası: CPU / bellek / disk
  kaynağı beklerken işlerin ne kadar yavaşladığı ve o an en çok kullananlar.
- **Geçmiş:** sistem yükü ve en çok kaynak kullanan süreçler diske kaydedilir; grafik, "takılma anları" (hangi kaynak
  darboğazdı, suçlular kimdi), "en çok yiyenler", **projeye göre** toplamlar (ortalama / tepe CPU ve RAM, kaç takılmada suçlu çıktı) ve olaylar
  (otomatik temizlik, bellek / takılma / disk uyarısı) tabloları. Grafikte fareyle sürükleyip bir zaman aralığı seçersen grafik
  o aralığa yakınlaşır ve tüm tablolar yalnızca o aralığı gösterir (çift tık / sağ tık / "Seçimi kaldır" ile kalkar).
- **Docker:** konteyner listesi, istatistikler, başlat / durdur / sil, canlı loglar; bellek limiti sütunu,
  "aynı compose projesini seç" düğmesi.
- **Projeler:** süreçleri çalışma dizinine göre git deposu / proje bazında gruplar; git dalı ve değişiklik sayısı.
- **Servisler:** `systemctl --user` servisleri ve oturum açılışında başlayan uygulamalar; başlat / durdur / yeniden
  başlat, başlangıçta aç / kapat. Oturumu bozacak servisler (dbus, gnome-shell…) korumalıdır.
- **Portlar:** tüm dinleyen portlar, sahibi süreç / proje / Docker konteyneri; portu kapat ya da konteyneri durdur.
- **Temizlik:** seçili profillerin yeniden üretilebilir klasörlerinin (`bin/obj`, `node_modules`, `target`, `.angular`…)
  boyutları ve toplu silme; Docker disk kullanımı,
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
- **Otomatik temizlik (kapalı gelir):** RAM eşiğin üstündeyken ve boşta (CPU < %2) kalan artık süreçler (profile göre:
  MSBuild node, VBCSCompiler, Gradle daemon…) belirlenen süreden sonra kapatılır. Seçili profillerin derleme sunucusu
  kapatma komutları (`dotnet build-server shutdown`, `gradle --stop`) düğme olarak gelir.

## Profiller

**Ayarlar → Profil** bölümünden çalıştığın teknolojileri işaretle (birden fazla seçilebilir; ilk açılışta sorulur,
varsayılan .NET). Profil üç şeyi belirler: hangi süreçler "geliştirme süreci" sayılır, hangi artıklar boşta
kalınca kapatılabilir (otomatik temizlik dahil) ve Temizlik sekmesinde hangi klasörler taranır.

| Profil | Süreçler | Boşta kapatılabilen | Temizlenen klasörler |
|---|---|---|---|
| .NET | dotnet, MSBuild, VBCSCompiler, testhost | MSBuild node, VBCSCompiler | `bin`, `obj` (.csproj/.fsproj/.vbproj yanında) |
| Angular | node, ng, esbuild, tsserver, karma | esbuild, jest worker, Nx daemon | `node_modules`, `.angular`, `dist`* |
| React / Next.js | node, vite, webpack, jest | esbuild, jest worker | `node_modules`, `.next`, `.parcel-cache`, `build`*, `dist`* |
| Vue / Nuxt | node, vite, nuxt, vitest | esbuild | `node_modules`, `.nuxt`, `.output`, `dist`* |
| Java / Kotlin | java, gradle, mvn | Gradle / Kotlin daemon | `target` (pom.xml), `build`*, `.gradle` |
| Python | python, pytest, uvicorn, jupyter | – | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` |
| Rust | cargo, rustc, rust-analyzer | – | `target` (Cargo.toml yanında) |

\* `build` / `dist` gibi genel adlar yalnızca git tarafından yok sayılıyorsa (`.gitignore`'da) silinebilir. Klasörün
yanında ilgili proje dosyası (`package.json`, `.csproj`…) yoksa (Python önbellekleri hariç) listelenmez; sanal ortamların (`pyvenv.cfg`) ve
`node_modules` içlerine inilmez; git'in izlediği dosya içeren klasör ve çalışma dizini orası olan süreç varsa atlanır.

**Kendi profilin:** `~/.config/tskmngr/profiles.json` dosyasına ekle (Ayarlar'da "(özel)" olarak çıkar; bozuk girdiler
yok sayılır, yerleşik profil adları ezilemez):

```json
{
  "godot": {
    "name": "Godot",
    "desc": "godot; .godot önbelleği",
    "proc": "godot",
    "idle": [["Headless sunucu", "godot.*--headless"]],
    "clean": [[".godot", ["project.godot"], "Godot önbelleği", false]],
    "shutdown": ["echo", "kapat"]
  }
}
```

`proc` ve `idle` düzenli ifadedir (komut satırında aranır); `clean` satırı `[klasör adı, [yanında olması gereken dosyalar],
etiket, yalnızca .gitignore'dayken]` biçimindedir; `shutdown` isteğe bağlıdır (Ayarlar'da düğme olur).

## Bildirimler ve korunan süreçler

- **Takılma bildirimi:** baskı (PSI) "takılma eşiği"nin üstünde birkaç saniye kalırsa, nedene (CPU / bellek / disk) göre
  en çok kaynak yiyen **kendi** sürecin bildirilir ve bildirimde tek tıkla **sonlandır** düğmesi çıkar (5 dk'da bir).
  Düğme yalnızca aynı süreç (PID + başlangıç zamanı) hâlâ çalışıyorsa SIGTERM yollar; sistem süreçleri
  (gnome-shell, Xorg, systemd, pipewire…) ve korunanlar hiç önerilmez.
- **Disk dolunca:** kök disk ve ev dizininin diski eşiği (varsayılan %90) aşarsa "Temizlik'i aç" düğmeli bildirim
  (aynı disk için 6 saatte bir).
- **Korunan süreçler:** Ayarlar'daki kutuya her satıra bir ad, komut parçası ya da proje adı yaz (büyük/küçük harf fark
  etmez). Eşleşenler otomatik temizlikte kapatılmaz, takılma bildiriminde önerilmez; elle sonlandırırken uyarı çıkar.

## Yetkili okuma (isteğe bağlı)

Ayarlar ya da Portlar / Dosya sahibi penceresindeki **Yetkili okuma** `pkexec` ile salt okunur bir yardımcıyı
root olarak başlatır (parola sorar, her oturumda yeniden açılır, kalıcı değildir). Başka kullanıcıların süreçleri
için port, PSS, disk G/Ç ve dosya sahibi bilgisi görünür. Yardımcı yalnızca okur; sonlandırma işlemleri hep normal
kullanıcıyla yapılır.

> **Güvenlik notu:** Root'a verilen kod bu klasördeki `tm_procfs.py` dosyasıdır. Klasöre yazabilen biri bu kodu
> değiştirip yetkili okuma açıldığında root olarak çalıştırabilir; uygulama klasörünü yalnızca kendi kullanıcının
> yazabildiği bir yerde tut (`chmod go-w`). `.deb` ile kurulunca dosyalar `/usr/lib/tskmngr` altında root'a aittir,
> bu risk yoktur.

## Gereksinimler

Sistem `python3`, `python3-gi` (GTK 3), `python3-psutil`. Docker sekmesi için `docker` CLI.

## Kurulum

### .deb paketi (arkadaşlara vermek için)

```bash
packaging/build-deb.sh                          # dist/tskmngr_<surum>_all.deb üretir
sudo apt install ./dist/tskmngr_1.1.0_all.deb   # bağımlılıkları da kurar
```

Kurulunca terminalden `tskmngr` ve uygulama menüsünde "Görev Yöneticisi" gelir; kaldırmak için `sudo apt remove tskmngr`.
Ayarlar ve geçmiş kullanıcının kendi `~/.config/tskmngr` ve `~/.local/share/tskmngr` klasörlerinde kalır.
Sürüm: `tskmngr --version` (sürüm numarası `taskmgr_gui.py` içindeki `VERSION`).

### Kaynaktan

```bash
git clone https://github.com/ahmetemincoban/UbuntuTaskManager.git
cd UbuntuTaskManager
ln -sf "$PWD/taskmgr_gui.py" ~/.local/bin/tskmngr
sed "s|/path/to/UbuntuTaskManager|$PWD|" tr.teginsoft.TaskMgr.desktop \
  > ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop
```

Sonra terminalden `tskmngr` ya da uygulama menüsünden "Görev Yöneticisi".

Kaldırmak için: `rm ~/.local/bin/tskmngr ~/.local/share/applications/tr.teginsoft.TaskMgr.desktop`

## Testler

```bash
python3 tests/run_all.py            # hepsi (birkaç dakika sürer)
python3 tests/test_profiles.py      # tek dosya
```

Testler ekranı olan bir oturumda çalışır (pencereler ekran dışında oluşturulur) ve kullanıcının ayarlarına /
geçmişine dokunmaz: `XDG_CONFIG_HOME` ve `XDG_DATA_HOME` geçici klasöre yönlendirilir. Otomatik bir CI'a
bağlı değildir; elle çalıştırılır.
