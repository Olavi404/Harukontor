# BranchBank25 API (Keskpanga integreeritud)

Täielik harukontori API implementatsioon vastavalt Branch Bank OpenAPI-le:
- https://test.diarainfra.com/central-bank/openapi/branch-bank.yaml

Keskpanga API integratsioon:
- https://test.diarainfra.com/central-bank/openapi/central-bank.yaml

## Live URL ja Swagger UI
 Live API: https://harukontor.onrender.com
 Swagger UI: https://harukontor.onrender.com/docs
 OpenAPI JSON: https://harukontor.onrender.com/openapi.json

## Kiirjuhend tavakasutajale
Kui soovid lihtsalt proovida, kas pank toimib, siis kasuta seda lihtsat järjekorda:

1. Ava Swagger: https://keskpank-production.up.railway.app/docs
2. Loo kasutaja endpointiga POST /users
3. Kopeeri vastuse headerist X-API-Key
4. Loo konto endpointiga POST /users/{userId}/accounts ja lisa header X-API-Key
5. Kontrolli kontot endpointiga GET /accounts/{accountNumber}

Kui tahad teha ülekannet:

1. Loo teine konto (teisele kasutajale)
2. Kasuta POST /transfers
3. Vaata tulemust endpointiga GET /transfers/{transferId}

Autentimine lihtsas keeles:
- X-API-Key: kõige lihtsam variant, sobib kohe kasutamiseks
- Bearer token: valikuline variant, selle saad endpointist POST /auth/token

Praktiline soovitus:
- Tavakasutajale on kõige lihtsam kasutada ainult X-API-Key meetodit.

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
  - Heartbeat loop keskpangale (vahemik max 30 min)
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
- `HEARTBEAT_INTERVAL_SECONDS` peab olema <= `1800` (30 min), et pank registrist välja ei kukuks

### 2) Docker Compose
```bash
docker compose up --build
```

API on vaikimisi:
- `http://localhost:8081`

Health:
- `GET /health`

## Integratsioonide Dokumentatsioon
Detailne dokumentatsioon harukontori API integratsioonide kohta, sh liidestused Keskpangaga ja teiste pankadega:
- [INTEGRATIONS.md](INTEGRATIONS.md) - Liidestused, turvalisus, autentimine, skeemid

## Endpointid (Branch Bank)
- `POST /api/v1/users`
- `POST /api/v1/auth/token` (`X-API-Key` -> Bearer token)
- `POST /api/v1/users/{userId}/accounts` (Bearer või `X-API-Key`)
- `GET /api/v1/accounts/{accountNumber}`
- `POST /api/v1/transfers` (Bearer või `X-API-Key`)
- `POST /api/v1/transfers/receive` (JWT ES256)
- `GET /api/v1/transfers/{transferId}` (Bearer või `X-API-Key`)

## Auth ja turvalisus
- Bearer tokeni saab võtta endpointist `POST /api/v1/auth/token`
- Kaitstud endpointid (`/users/{userId}/accounts`, `/transfers`, `/transfers/{transferId}`) aktsepteerivad `Authorization: Bearer <token>`.
- Samad endpointid aktsepteerivad ka `X-API-Key: <api_key>`.
- Bearer ja API võtme meetodid on OpenAPI security osas mõlemad dokumenteeritud.
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
  - kontrollib BearerAuth + ApiKeyAuth security skeeme ja endpointide auth nõudeid
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
- Tulemused võivad haru ja keskkonna lõikes erineda; kontrolli jooksvat seisu käsuga `pytest -q`.

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
- `BranchBank25-api` (web service)
- `BranchBank25-worker` (worker service)
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
