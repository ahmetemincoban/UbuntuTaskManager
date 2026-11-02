# Contributing / Katkıda bulunma

Contributions are welcome. Changes come in through pull requests from a fork; the maintainer
merges them into `main`. / Katkılarınız memnuniyetle karşılanır. Değişiklikler fork üzerinden pull request ile
gelir; `main`'e birleştirmeyi depo sahibi yapar.

## English

1. Fork the repo and create a branch (`git switch -c my-branch`).
2. Make your change. Source strings and code comments are in Turkish; match the surrounding code (naming, comment
   density, line length ~120).
3. Run the tests (they need a display session; windows are created offscreen):
   ```bash
   python3 tests/run_all.py            # everything (takes a few minutes)
   python3 tests/test_profiles.py      # one file
   ```
   Tests never touch your settings or history (`XDG_*` is redirected to a temp dir). Add a test under `tests/` for
   new behaviour.
4. Open a pull request describing what changed and why. Small, single-purpose PRs get reviewed faster.

### New strings and translations

UI strings are written in Turkish and wrapped in `_("...")`. When you add one, add the same text as a key in the
[tm_lang.py](tm_lang.py) catalog with three translations (en, de, fr). Keep placeholders (`{n}`, `{size:.1f}`) and
`<b>` / `<span>` markup unchanged in every translation; `tests/test_i18n.py` catches missing translations and
broken placeholders. To add a language, extend `LANGUAGES` in `tm_i18n.py` and `tm_lang.ORDER`.

### Security

If you find a vulnerability, please do **not** open a public issue; report it privately via Security →
"Report a vulnerability" on GitHub.

## Türkçe

1. Depoyu fork'layın, yeni bir dal açın (`git switch -c dal-adi`).
2. Değişikliğinizi yapın. Kod yorumları ve kaynak metinler Türkçedir; çevresindeki koda uyum sağlayın (isimlendirme,
   yorum yoğunluğu, satır uzunluğu ~120).
3. Testleri çalıştırın (ekranı olan bir oturum gerekir, pencereler ekran dışında oluşturulur):
   ```bash
   python3 tests/run_all.py            # hepsi (birkaç dakika sürür)
   python3 tests/test_profiles.py      # tek dosya
   ```
   Testler ayarlarınıza ve geçmişinize dokunmaz (`XDG_*` geçici klasöre yönlendirilir). Yeni bir davranış ekliyorsanız
   `tests/` altına test ekleyin.
4. Pull request açın: ne değiştiğini ve nedenini kısaca yazın. Küçük ve tek konulu PR'lar daha hızlı incelenir.

### Yeni metin ve çeviri

Arayüz metinleri Türkçe yazılır ve `_("...")` ile sarılır. Yeni bir metin eklerken [tm_lang.py](tm_lang.py)
kataloğuna aynı metni anahtar yaparak üç çeviriyi (en, de, fr) ekleyin. Yer tutucular (`{n}`, `{size:.1f}`) ve
`<b>` / `<span>` işaretlemeleri çeviride aynen korunmalıdır; `tests/test_i18n.py` eksik çeviriyi ve bozuk yer
tutucuyu yakalar. Yeni bir dil eklemek için `tm_i18n.py` içindeki `LANGUAGES` listesine ve `tm_lang.ORDER`'a girin.

### Güvenlik

Güvenlik açığı bulursanız herkese açık issue **açmayın**; GitHub'da Security → "Report a vulnerability" ile özel
olarak bildirin.

## License / Lisans

Your contributions are licensed under the project's [MIT](LICENSE) license. / Katkılarınız projenin lisansı olan
[MIT](LICENSE) altında yayınlanır.
