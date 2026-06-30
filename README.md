# Boost — SMM Panel

Telegram botunuzdakı `users` / `products` / `orders` cədvəlləri ilə eyni quruluşda işləyən sadə veb panel. Eyni Railway PostgreSQL bazasına qoşula bilərsiniz, ya da ayrı baza yaradıb botla əlaqəli sinxronizasiya özünüz qurarsınız.

## Railway-də işə salma

1. Bu qovluğu GitHub repo-suna yükləyin (və ya Railway CLI ilə birbaşa deploy edin).
2. Railway-də "New Project" → "Deploy from GitHub repo".
3. Railway-də PostgreSQL əlavə edin (Add → Database → PostgreSQL).
4. Bu xidmətin Variables bölməsinə `DATABASE_URL` əlavə edin (Postgres xidmətindən referans verə bilərsiniz: `${{Postgres.DATABASE_URL}}`).
5. Deploy olduqdan sonra Railway sizə `xxx.up.railway.app` ünvanı verəcək — panel dərhal işləyəcək.
6. Domeniniz olanda: Settings → Networking → Custom Domain.

## Lokal test

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://user:password@localhost/eren_smm"
uvicorn main:app --reload
```

## Qeyd

- `init_db()` ilk açılışda lazımi cədvəlləri avtomatik yaradır (mövcuddursa toxunmur).
- Hazırda nümunə üçün məhsul siyahısı boşdur — `products` cədvəlinə birbaşa SQL ilə (ya da botunuzdakı `/urunekle` komandası ilə, əgər eyni bazaya qoşulubsa) məhsul əlavə edin.
- Ödəniş inteqrasiyası daxil deyil — hazırkı axın sifarişi qeyd edir, ödənişi admin manual təsdiqləyir (botunuzdakı məntiqə bənzər). Kart/onlayn ödəniş gateway-i əlavə etmək istəsəniz, ayrıca bildirin.

