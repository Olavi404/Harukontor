# Panga Harukontori API Integratsioonide Dokumentatsioon

**Projekt:** BranchBank25 (Keskpanga integreeritud)  
**Versioon:** 1.0.0  
**Kuupäev:** Mai 2026

---

## 1. Liidestatud Süsteemid

Panga harukontori API suhtleb järgmiste süsteemidega:

| Süsteem | Kirjeldus | Suhtluse tüüp |
|---------|-----------|---------------|
| **API klient / Kasutajarakendus** | Mobiiliäpp, veebiklient, curl, Postman vms | REST JSON HTTP |
| **PostgreSQL andmebaas** | Kasutajate, kontode ja ülekannete hoiustamine | SQLAlchemy ORM |
| **Keskpanga API** | Panka registreerimine, heartbeat, pankade nimekirja saamine, valuutakursid | REST JSON HTTP |
| **Teised harukontori pangas** | Pankadevaheline raha ülekanne | REST JSON + JWT ES256 |
| **Swagger UI** | API dokumentatsioon ja interaktiivne testimine | OpenAPI 3.1 |

---

## 2. Peamised Integratsioonipunktid

### Kasutajarakenduse ja Harukontori API vahel

- POST /api/v1/users - Kasutaja registreerimine, X-API-Key väljastamine
- POST /api/v1/auth/token - X-API-Key vahetamine Bearer tokeni vastu
- POST /api/v1/users/{userId}/accounts - Konto loomine (10.00 EUR algse saldoga)
- GET /api/v1/users/{userId}/accounts - Kasutaja kontode nimekirja
- POST /api/v1/transfers - Ülekande alustamine (pangasisene VÖI pankadevaheline)
- GET /api/v1/transfers/{transferId} - Ülekande staatuse kontrollimine
- GET /api/v1/accounts/{accountNumber} - Konto avalike andmete küsimine
  - Public lookup: returns `accountNumber`, `ownerName`, `currency` (no balance)
  - Authenticated owner: same endpoint `GET /api/v1/accounts/{accountNumber}` returns `balance` and `ownerId` when caller is the account owner.

Examples (curl):

- Public lookup (no auth):

```bash
curl -sS https://harukontor.onrender.com/api/v1/accounts/OLL12345
```

- Authenticated owner lookup (Bearer token):

```bash
curl -sS -H "Authorization: Bearer $TOKEN" https://harukontor.onrender.com/api/v1/accounts/OLL12345
```

- Authenticated owner lookup (API key):

```bash
curl -sS -H "X-API-Key: $API_KEY" https://harukontor.onrender.com/api/v1/accounts/OLL12345
```

### Harukontori API ja Keskpanga API vahel

- POST /central-bank/api/v1/banks - Panga registreerimine
- POST /central-bank/api/v1/banks/{bankId}/heartbeat - Pank elus hoidmine (iga 900s, max 30min)
- GET /central-bank/api/v1/banks - Pankade nimekirja saamine ja cache'imine (iga 300s)
- GET /central-bank/api/v1/exchange-rates - Valuutakursside saamine

### Harukontori API ja teiste harukontori vahel

- POST /api/v1/transfers/receive - Pankadevaheline ülekande vastuvõtmine (JWT ES256)

---

## 3. Integratsioonimeetodid

### REST API ja JSON
- HTTP meetodid: GET, POST
- Kõik päringud ja vastused JSON vormingus
- Veastruktuur: {"code": "ERROR_CODE", "message": "..."}

### Autentimine

**X-API-Key** (lihtsaim)
- Header: X-API-Key: <api_key>
- Sobib testamiseks ja lihtsamateks integratsioonideks

**Bearer Token (JWT)**
- Header: Authorization: Bearer <jwt>
- Token ajaga piiratud (120 minutit)
- HMAC-SHA256 allkirjastatud
- Turvalisem pikemaajalisele kasutamisele

**JWT ES256** (pankadevaheliseks)
- ECDSA SECP256R1 allkiri
- Allkirjastaja: Lähtetava panga privaatvõti
- Kontrollija: Sihtpanga avalik võti (Keskpangast cache'st)

### Perioodiline Heartbeat
- Worker service käivitab iga 900 sekundit (15 minutit)
- Kui heartbeat'e ei saadeta 30 minutit, pank eemaldatakse registrist
- Heartbeat käivitab ka pankade cache'i värskendamise (iga 300s)

### Swagger UI Testimine
- URL: https://harukontor.onrender.com/docs
- Interaktiivne API testimine otse brauseris

---

## 4. Turvalisuse Reeglid

### Bearer Token Autentimine
- Token ajaga piiratud (120 minutit)
- Token sisaldab kasutaja ID-d JWT payload'is
- Kaitstud endpoint'idel kontrollitakse, et autentitud kasutaja ID vastab resourcesse

### Kasutajaga Seotud Endpoint'ide Kaitsmine
- Server tõendab, et autentitud kasutaja omab seotud ressurssi
- Näide: kasutaja user-123 ei saa käivitada teise kasutaja operatsioone (403 FORBIDDEN)
- Ülekande staatust saavad vaadata nii saatja kui vastuvõtja

### Pankadevaheliste Ülekannete Allkirjastamine
- JWT ES256 meetod
- Lähtetava pank genereerib ja allkirjastab oma privaatvõtmega
- Sihtpank kontrollib lähtetava panga avaliku võtmega (Keskpangast saadud)
- Allkiri kontrollimata ülekanded lükatakse tagasi (401)

### Privaatvõtme Hoiustamine
- Asukoht: ./keys/bank_private_key.pem
- Genereeritakse automaatselt startup'il
- Ei logita kunagi
- Hoiustatakse ainult serveri kettal, mitte Git repos'sse

### Logimine ja Privaatsus
**Logitakse:**
- user.registered user_id=... (ID ja email)
- account.created account=... (ID ja kontonomber)
- transfer.initiated transfer_id=... (ülekande ID)
- central.heartbeat.sent bank_id=... (panga ID)

**Ei logita:**
- Paroolid, API võtmed, privaatvõtmed
- Täielikud JWT tokenid

### Topeltülekannete Vältimisne (Idempotentsus)
- Kasutaja genereerib UUID iga ülekande jaoks (transferId)
- Server kontrollib, et transferId ei ole juba olemas
- Turvaline uuesti saatmine võrguvigade puhul sama transferId'ga

### HTTP Staatuskoodid Vigade Korral

| Kood | Tähendus | Näide |
|------|----------|-------|
| 200 | OK | Ülekande detailide vaatamine |
| 201 | Created | Kasutaja/konto loomine |
| 400 | Bad Request | Vigane sisend, topeltülekanne |
| 401 | Unauthorized | Vale API võti, aegunud token |
| 403 | Forbidden | Pole õigusi (teise kasutaja ressurss) |
| 404 | Not Found | Kasutaja/konto/ülekanne puudub |
| 409 | Conflict | Duplikaat (kasutaja juba registreeritud) |
| 422 | Unprocessable | Valideerimise viga (vale email, valuuta) |
| 423 | Locked | Ülekanne timeout'i, ei saa muuta |
| 503 | Service Unavailable | Keskpank/sihtpank offline |

---

## 5. Integratsiooniskeem

### Üldskeem

\\\
┌──────────────────────────────────┐
│    KASUTAJARAKENDUS              │
│  (Mobiiliäpp, Veebiklient)       │
└────────────┬─────────────────────┘
             │ REST API (Bearer/X-API-Key)
             ▼
┌──────────────────────────────────┐
│  HARUKONTORI API (FastAPI)       │
│  - Kasutajad, kontod, ülekanded  │
│  - Autentimine (Bearer, API-Key) │
└─┬──────────┬──────────────────┬──┘
  │          │                  │
  ▼          ▼                  ▼
PostgreSQL  Keskpank API    Teine Pank
Andmebaas   (REST)          (REST + JWT ES256)

WORKER SERVICE (paralleelselt)
- Heartbeat loop (900s)
- Bank cache sync (300s)
- Pending transfers retry
\\\

### Ülekande Voog (Pankadevaheline)

1. Kasutaja: POST /api/v1/transfers (sourceAccount: OLL, destinationAccount: EST)
2. API kontrollib autentimine ja saldo
3. API otsib sihtpanka Keskpangast (cache'st)
4. API hankkib valuutakursid (kui vaja)
5. API allkirjastab JWT ES256
6. API saadab: POST https://est-bank.com/api/v1/transfers/receive
7. EST pank kontrollib JWT allkirja
8. EST pank krediteerib saldo
9. OLL pank märkib: status = completed

---

## 6. Peamiste Liideste Tabel

| # | Liides | HTTP | Autentimine | Eesmärk | Periood |
|----|--------|------|---|---|---|
| 1 | POST /api/v1/users | POST | pole | Kasutaja registreerimine | nõudmisel |
| 2 | POST /api/v1/auth/token | POST | X-API-Key | Bearer token saamine | nõudmisel |
| 3 | POST /api/v1/users/{id}/accounts | POST | Bearer/Key | Konto loomine | nõudmisel |
| 4 | POST /api/v1/transfers | POST | Bearer/Key | Ülekanne | nõudmisel |
| 5 | GET /api/v1/transfers/{id} | GET | Bearer/Key | Ülekande otsing | nõudmisel |
| 6 | POST /central-bank/api/v1/banks | POST | pole | Panga registreerimine | startup |
| 7 | POST /banks/{id}/heartbeat | POST | pole | Heartbeat | 900s (worker) |
| 8 | GET /central-bank/api/v1/banks | GET | pole | Pankade cache sync | 300s (worker) |
| 9 | POST /api/v1/transfers/receive | POST | JWT ES256 | Teisest pangast ülekanne | nõudmisel |

---

## 7. Testimine

- **OpenAPI contract testid:** tests/test_openapi_contract.py
- **Runtime testid:** tests/test_api.py
- **Swagger UI:** https://harukontor.onrender.com/docs
- **curl näited:** vt. README.md

---

## 8. Viited

- **OpenAPI spec:** branch-bank.yaml (repositoorium)
- **Keskpanga API:** https://test.diarainfra.com/central-bank/openapi/central-bank.yaml
- **GitHub:** https://github.com/Olavi404/Harukontor
- **Live:** https://harukontor.onrender.com

---

**Viimati värskendatud:** Mai 2, 2026
