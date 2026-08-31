# 3D Slicer Server v1.1

`/quote` hata yanıtları ayrıntılandırıldı. Dosya indirme, desteklenmeyen dosya tipi, baskı alanına sığmama, PrusaSlicer çıkış kodu, zaman aşımı, doğrulama ve beklenmeyen hata durumları artık ayrı `error_type` değerleriyle dönüyor. WordPress'in gösterebilmesi için bütün bu yanıtlarda `message`, `detail` ve `error_type` alanları bulunuyor; mevcut olduğunda PrusaSlicer `stdout`/`stderr` çıktıları ve doğrulama sebebi de ekleniyor.

`/health`, Docker, Render `PORT` kullanımı ve mevcut endpoint adresleri değiştirilmedi.
