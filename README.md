# Render için 3D Slicer Sunucusu

**Sürüm: v1.1.1**

FastAPI ve Docker tabanlı küçük bir PrusaSlicer API'sidir. `/quote` modeli gerçekten dilimler ve G-code yorumlarından süre/filament tahmini çıkarır; `/slice` üretilen G-code'u indirir. `/health` kimlik doğrulama istemez. `/quote`, doğrudan multipart dosya yüklemenin yanında WordPress eklentisinin gönderdiği JSON `file_url` biçimini de destekler.

## Gerçek entegrasyon ve sınırlar

Docker imajı Debian Bookworm deposundan `prusa-slicer` ve başsız çalıştırma için `xvfb` kurar. Paket hedef mimaride mevcutsa entegrasyon gerçektir; uygulama sahte fiyat veya süre üretmez. Paket kurulamazsa Docker build açıkça başarısız olur. Prusa'nın güncel Linux dağıtımlarının Flatpak'a yönelmesi ve Render'ın başsız/kısıtlı ortamı nedeniyle bu paket sürümü en yeni PrusaSlicer olmayabilir.

Ücretsiz Render servisi üretim için uygun değildir: boşta kalınca uyur, ilk istek yavaş olabilir; CPU/RAM ve geçici disk sınırları büyük/karmaşık modelleri başarısız kılabilir. `/slice` çıktıları kalıcı saklanmaz. Varsayılan profil genel PLA başlangıç profilidir; yazıcınız için `profiles/default.ini` dosyasını mutlaka uyarlayın. `total_cost`, profil içindeki filament maliyet birimine bağlıdır ve kesin satış fiyatı değildir.

## GitHub'a yükleme

1. ZIP'i açın.
2. GitHub'da boş deponuzu açın ve **uploading an existing file** bağlantısına tıklayın.
3. ZIP'in kendisini değil, açılan `3d-slicer-server` klasörünün içindeki bütün dosya ve klasörleri sürükleyin. `Dockerfile` depo kökünde görünmelidir.
4. **Commit changes** düğmesine basın.

## Render kurulumu

### En kolay yöntem: Blueprint

1. Render panelinde **New > Blueprint** seçin.
2. GitHub deponuzu bağlayın.
3. Render kökteki `render.yaml` dosyasını okuyacaktır. Servisi oluşturmayı onaylayın.
4. Deploy tamamlanınca **Environment** bölümündeki otomatik üretilen `API_KEY` değerini güvenli bir yere kopyalayın.

### Elle kurulum

1. **New > Web Service** ile GitHub deponuzu bağlayın.
2. Runtime olarak **Docker**, plan olarak **Free** seçin.
3. Health Check Path değerini `/health` yapın.
4. `API_KEY` adlı gizli bir environment variable ekleyin. Uzun ve rastgele bir değer kullanın.
5. Render `PORT` değerini sağlar; `start.sh` bu porta `0.0.0.0` üzerinden bağlanır. Start Command girmeniz gerekmez.

## Kullanım

Servis adresini ve anahtarı değiştirin:

```bash
curl https://SERVISINIZ.onrender.com/health

curl -X POST https://SERVISINIZ.onrender.com/quote \
  -H "X-API-Key: API_ANAHTARINIZ" \
  -F "file=@model.stl"

curl -X POST https://SERVISINIZ.onrender.com/slice \
  -H "Authorization: Bearer API_ANAHTARINIZ" \
  -F "file=@model.stl" \
  -o output.gcode
```

Swagger arayüzü: `https://SERVISINIZ.onrender.com/docs`

`API_KEY` boş bırakılırsa koruma devre dışı kalır; internete açık bir serviste bunu yapmayın. Yükleme sınırı `MAX_UPLOAD_MB`, işlem zaman aşımı `SLICER_TIMEOUT` ile değiştirilebilir.

## Yerelde çalıştırma

```bash
docker build -t 3d-slicer-server .
docker run --rm -p 10000:10000 -e API_KEY=degistir 3d-slicer-server
```

Ardından `http://localhost:10000/docs` adresini açın.

## Endpoint özeti

- `GET /health`: Servis ve PrusaSlicer bulunabilirlik durumu.
- `POST /quote`: STL/OBJ/3MF/AMF yükler, gerçek dilimleme sonrası süre ve filament metadatası döndürür.
- `POST /slice`: Aynı modeli dilimler ve G-code dosyası döndürür.

## v1.1 hata yanıtları

`/quote` artık 4xx/5xx durumlarında WordPress'in doğrudan gösterebileceği tutarlı JSON döndürür:

```json
{
  "message": "PrusaSlicer modeli dilimleyemedi",
  "detail": "PrusaSlicer başarısız çıkış kodu döndürdü: 1",
  "error_type": "slicer_exit_error",
  "exit_code": 1,
  "prusa_stdout": "...",
  "prusa_stderr": "..."
}
```

Ayırt edilen hata türleri: `file_download_error`, `unsupported_file_type`, `file_too_large`, `model_outside_print_area`, `slicer_exit_error`, `slicer_timeout`, `slicer_unavailable`, `request_validation_error`, `authentication_error`, `http_error` ve `unexpected_error`. Doğrulama ayrıntıları varsa `validation_reason`, PrusaSlicer çıktıları varsa `prusa_stdout` ve `prusa_stderr` alanları eklenir. Çıktılar yanıtın aşırı büyümemesi için son 12.000 karakterle sınırlandırılır. `/health` yanıtı v1.0 ile aynıdır.

Testleri çalıştırmak için `pip install -r requirements-dev.txt` ardından `pytest -q` kullanabilirsiniz.

## Kısa güncelleme notu

v1.1 ile `/quote` hata teşhisi geliştirildi. Dosya indirme, desteklenmeyen biçim, baskı alanına sığmama, PrusaSlicer çıkış kodu, zaman aşımı ve beklenmeyen hata durumları ayrı JSON hata türleri olarak döndürülüyor. WordPress uyumluluğu için `message`, `detail` ve `error_type` alanları eklendi; `/health` ve Render çalışma şekli değiştirilmedi.

v1.1.1 ile PrusaSlicer işi web sunucusunun ana döngüsünden ayrıldı. Uzun dilimleme sırasında `/health` yanıt vermeye devam eder; Render'ın servisi sağlıksız kabul edip 502 üretme riski azaltıldı. `/health` yanıt gövdesi değiştirilmedi.

Güvenlik notu: Bu servis temel API anahtarı kontrolü sağlar; rate limit, kullanıcı hesabı, virüs taraması ve kalıcı iş kuyruğu içermez.
