# 3D Slicer Server v1.2.0

- Model ölçeği ve X/Y/Z dönüşleri `/quote` isteğinden PrusaSlicer'a aktarılır.
- Ölçek değiştiğinde filament, süre ve fiyat gerçek dilimleme sonucuna göre yeniden hesaplanır.
- Geçersiz ölçek/dönüş değerleri ayrıntılı JSON hata olarak döner.
- “Objects could not fit on the bed” hatası baskı alanına sığmama olarak ayrıştırılır.
- `/health` yanıtı ve Render kurulumu değişmedi.
