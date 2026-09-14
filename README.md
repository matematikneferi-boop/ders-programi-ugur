# Ders Programı CP-SAT API

Bu klasördeki 3 dosya, GitHub'a yeni bir repo olarak yüklenip Render.com'a
bağlanacak:

- `app.py` — Flask + Google OR-Tools CP-SAT çözücü
- `requirements.txt` — gerekli Python paketleri
- `render.yaml` — Render'ın otomatik algılayacağı deploy ayarı

## Adımlar

1. GitHub'da yeni, boş bir repo oluşturun (örn. `ders-programi-cpsat`).
2. Bu 3 dosyayı o reponun köküne yükleyin (GitHub'ın "Add file → Upload files"
   ekranından sürükle-bırak yeterli, terminal gerekmez).
3. Render.com → **New → Web Service** → GitHub hesabınızı bağlayın →
   `ders-programi-cpsat` reposunu seçin.
4. Render `render.yaml`'ı otomatik okur; "Create Web Service" deyin.
5. İlk deploy 2-3 dakika sürer (ortools kurulumu biraz zaman alır). Bitince
   Render size şöyle bir link verir: `https://ders-programi-cpsat.onrender.com`

## Test

Tarayıcıda `https://<linkiniz>/health` açıldığında `{"ok": true}` görmelisiniz.

Gerçek çözüm için HTML sayfası şu adrese POST atacak:

```
POST https://<linkiniz>/solve
Content-Type: application/json

{ "teachers": [...], "classes": [...], "subjects": [...],
  "assignments": [...], "settings": {"periods":7, "saturday":false} }
```

HTML tarafındaki `__CPSAT_SOLUTION__` donmuş çözümünü bu gerçek API çağrısıyla
değiştirme kısmını link elinize geçtikten sonra birlikte yapacağız.
