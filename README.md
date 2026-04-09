# Branch Bank API (Keskpanga integreeritud)

Täielik harukontori API implementatsioon vastavalt Branch Bank OpenAPI-le:
- https://test.diarainfra.com/central-bank/openapi/branch-bank.yaml

Keskpanga API integratsioon:
- https://test.diarainfra.com/central-bank/openapi/central-bank.yaml

## Live URL ja Swagger UI
- Live API: https://keskpank-production.up.railway.app
- Swagger UI: https://keskpank-production.up.railway.app/docs
- OpenAPI JSON: https://keskpank-production.up.railway.app/openapi.json

## Kasutatud tehnoloogiad
- Python 3.12
- FastAPI
- SQLAlchemy
- PostgreSQL
- HTTPX
- PyJWT + ES256 (EC võtmed)
- Docker Compose
- Pytest
- Railway deployment

## Mikroteenuste arhitektuuri kirjeldus
Lahendus on komponendipõhine ja iseseisvalt deployeritav:
1. `api` teenus
   - Avalikud endpointid (`/api/v1/...`) branch-bank lepingu järgi
   - Kasutaja-, konto- ja ülekandeloogika
2. `worker` teenus
   - Heartbeat loop keskpangale
   - Pankade vahemälu sünkroniseerimine
   - Pending ülekannete retry exponential backoffiga
3. `postgres` teenus
   - Püsiv andmesalvestus

Teenustevaheline suhtlus:
- `api` ja `worker` jagavad andmebaasi
- `api` ja `worker` suhtlevad Keskpangaga REST API kaudu
- Pankadevaheline ülekanne: REST + JWT ES256 allkiri (`POST /transfers/receive`)

Märkus:
- Swagger UI näitab peamist kasutajapinda kompaktse Users / Accounts / Transfers jaotisena.
- Tegelik backend toetab ka `/api/v1/...` ühilduvusroute, et vastata lepingu ja live-keskkonna nõuetele.

## Andmebaasi skeem
Peamised tabelid:
- `users`
  - `id`, `full_name`, `email`, `api_key`, `created_at`
- `accounts`
  - `account_number`, `owner_id`, `currency`, `balance`, `created_at`
- `transfers`
  - `transfer_id`, `status`, `source_account`, `destination_account`, `amount`
  - `converted_amount`, `exchange_rate`, `rate_captured_at`
  - `pending_since`, `next_retry_at`, `retry_count`, `error_message`
  - `source_bank_id`, `destination_bank_id`
- `bank_cache`
  - keskpanga kataloogi lokaalne cache
- `app_state`
  - `bank_id` jt runtime väärtused

Transaktsioonid:
- Saldo muudatused ja transfer kirjed commititakse ühes DB transaktsioonis.

## Käivitamine
### 1) Konfiguratsioon
Kopeeri `.env.example` -> `.env` ja kohanda väärtused.

Olulised väljad:
- `CENTRAL_BANK_BASE_URL=https://test.diarainfra.com/central-bank/api/v1`
- `BANK_PUBLIC_URL` peab viitama sinu API avalikule aadressile (kui testid päris pankadega)
- `BANK_NAME` võib olla lühike identifikaator, näiteks `OLL001`

### 2) Docker Compose
```bash
docker compose up --build
```

API on vaikimisi:
- `http://localhost:8081`

Health:
- `GET /health`

## Endpointid (Branch Bank)
- `POST /api/v1/users`
- `POST /api/v1/users/{userId}/accounts` (Bearer)
- `GET /api/v1/accounts/{accountNumber}`
- `POST /api/v1/transfers` (Bearer)
- `POST /api/v1/transfers/receive` (JWT ES256)
- `GET /api/v1/transfers/{transferId}` (Bearer)

## Auth ja turvalisus
- Bearer token kasutaja toimingutele (`accounts`, `transfers`, `transfer status`)
- API võtmega autentimine on samuti toetatud (`X-API-Key`)
- Pankadevaheline autentimine JWT ES256 allkirjaga
- Privaat/avalik võti genereeritakse automaatselt kausta `keys/`
- `transferId` tagab idempotentsuse

## Veakäsitlus
- Ühtne veakuju:
```json
{
  "code": "ERROR_CODE",
  "message": "Human readable message"
}
```
- Kasutatud on sobivaid HTTP staatuskoode (`400/401/403/404/409/422/423/503`)

## OpenAPI contract testid
Lepingu katmine on lisatud kahe kihina:
- `tests/test_openapi_contract.py`
  - kontrollib, et FastAPI genereeritud OpenAPI sisaldab kõiki `branch-bank.yaml` radu/meetodeid
  - kontrollib BearerAuth security skeemi ja endpointide auth nõudeid
  - kontrollib, et nõutud HTTP vastusekoodid on olemas
- `tests/test_runtime_contract.py`
  - katab runtime käitumise kõigi endpointide jaoks:
    - `POST /users`
    - `POST /users/{userId}/accounts`
    - `GET /accounts/{accountNumber}`
    - `POST /transfers`
    - `POST /transfers/receive`
    - `GET /transfers/{transferId}`

## Testimine
Käivita testid:
```bash
pytest -q
```

Testid sisaldavad:
- OpenAPI contract vastavus
- Auth vood
- Idempotentsus
- Pangasisene ja pankadevaheline transferi töötlus
- Live-käitumise kontroll

Viimane teadaolev tulemus:
- `13 passed`

## CI (GitHub Actions)
Fail: `.github/workflows/ci.yml`

Iga push/pull request käivitab:
1. sõltuvuste installi
2. syntax compile kontrolli (`python -m compileall app tests`)
3. automaattestid (`pytest -q`)

## Deploy (Railway)
Railway põhikonfiguratsioon:
- `railway.json` (API web service)

Teenuste soovituslik jaotus Railways:
- `branch-bank-api` (web service)
- `branch-bank-worker` (worker service)
- `Postgres` plugin (Railway Database)

### Railway deploy sammud
1. Push repo GitHubi.
2. Railways vali **New Project** -> **Deploy from GitHub Repo**.
3. Loo esimene service API jaoks samast repost (kasutab `railway.json`).
4. Lisa samasse projekti Postgres plugin.
5. Loo teine service workeri jaoks samast repost.
6. Worker service `Start Command`:
  - `python -m app.worker`
7. Sea mõlemale service'ile keskkonnamuutujad:
  - `DATABASE_URL` (Railway Postgres connection string)
  - `CENTRAL_BANK_BASE_URL=https://test.diarainfra.com/central-bank/api/v1`
  - `BANK_NAME`
  - `BANK_PREFIX`
  - `USER_JWT_SECRET`
  - `SUPPORTED_CURRENCIES`
  - `BANK_PUBLIC_URL` (API avalik URL, nt `https://<api-service>.up.railway.app`)
8. Pärast deployd kontrolli:
  - `GET /health`
  - `POST /api/v1/users`

### Live URL vorm
- API URL: `https://<api-service>.up.railway.app`
- Lisa see esitusele README-sse "Live URL" alla.

## Näidispäringud
Registreerimine:
```bash
curl -X POST http://localhost:8081/api/v1/users \
  -H "Content-Type: application/json" \
  -d '{"fullName":"Jane Doe","email":"jane@example.com"}'
```

Konto loomine:
```bash
curl -X POST http://localhost:8081/api/v1/users/<USER_ID>/accounts \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"currency":"EUR"}'
```

Ülekanne:
```bash
curl -X POST http://localhost:8081/api/v1/transfers \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "transferId":"550e8400-e29b-41d4-a716-446655440000",
    "sourceAccount":"EST12345",
    "destinationAccount":"EST54321",
    "amount":"10.00"
  }'
```

## Live URL
- `https://keskpank-production.up.railway.app`

## GitHub repo link
- `https://github.com/Olavi404/Harukontor`
