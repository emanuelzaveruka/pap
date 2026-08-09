# Testing on the local WSL machine

Run everything here **before** touching `pap-server`. Two of these steps (Google consent, Telegram
setup) can only be done on a machine with a browser anyway, and their output is what you paste into
the server's `pap.env` later.

Nothing here writes to the server. The local Postgres is a throwaway container on port **55432**, so
it cannot collide with anything else you run.

---

## 0. One-time setup

```bash
cd /mnt/c/Users/zvkk/Projetos/2026/personalAutomationPlatform

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

pytest          # expect: all tests pass, no DB and no network needed
```

Create your local `.env` from the template. **`.env` is gitignored and Claude Code cannot read it.**

```bash
cp .env.example .env
```

Then edit `.env` and set at minimum:

```
PG_HOST=127.0.0.1
PG_PORT=55432
PG_DB=pap
PG_USER=pap
PG_PASSWORD=pick-any-local-password

# .env.example ships CONTAINER paths (/var/lib/pap/...). On the host, override all
# three or they point somewhere you cannot write.
LOG_DIR=./var/logs
PAP_STATE_DIR=./var/secrets
BACKUP_DIR=./var/backups

TELEGRAM_ENABLED=false
EMAIL_ENABLED=false
GOOGLE_ENABLED=false
```

```bash
mkdir -p var/secrets var/backups logs
chmod 700 var/secrets
```

---

## 1. Database up, schema applied

```bash
docker run -d --name pap-local-pg \
  -e POSTGRES_DB=pap -e POSTGRES_USER=pap \
  -e POSTGRES_PASSWORD=pick-any-local-password \
  -p 55432:5432 postgres:17-alpine

# Wait for it to genuinely accept connections. `pg_isready` alone is NOT enough:
# during first init the image runs a temp server on the unix socket that answers
# "ready" and then shuts down. Probing over TCP is what makes this honest.
until docker exec pap-local-pg psql -U pap -d pap -tAc 'select 1' >/dev/null 2>&1; do sleep 2; done
echo "postgres ready"

pap doctor            # expect exit 1 while channels are off — that is correct
pap migrate --dry-run
pap migrate
```

## 2. Prove the pipeline with the fake source

```bash
pap run fake --dry-run          # must write NOTHING
docker exec pap-local-pg psql -U pap -d pap -tAc 'select count(*) from pap.item'   # -> 0

pap run fake                    # -> new=3
pap run fake                    # -> new=0   <- idempotency; the important one
pap status
```

Then prove failures are visible:

```bash
pap run fake --boom ; echo "exit=$?"     # -> exit 1
docker exec pap-local-pg psql -U pap -d pap \
  -c "select id, status, left(error_text,60) from pap.source_run order by id desc limit 3"
```

---

## 3. Telegram token

1. Message **@BotFather** on Telegram → `/newbot` → copy the token (`123456789:AA...`).
2. Send **any message to your new bot** — a bot cannot start a conversation, so without this the
   chat does not exist and step 3 returns an empty result.
3. Find your chat id:

```bash
TOKEN='123456789:AA...'
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print([u["message"]["chat"]["id"] for u in d.get("result",[]) if "message" in u] or "no messages yet — message the bot first")'
```

Put both in `.env`:

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=987654321
```

Test the channel, then the full path:

```bash
pap notify --channel telegram --title "pap" --body "canal funcionando"

docker exec pap-local-pg psql -U pap -d pap -qc "update pap.item set notified_at = null"
pap run fake            # queues and delivers 3 messages
pap status              # queue should read: sent 3
```

---

## 4. Google tokens (the part that must happen here, not on the server)

### 4a. Create the OAuth client

1. <https://console.cloud.google.com/> → create or pick a project.
2. **APIs & Services → Enabled APIs** → enable **Google Drive API** and **Google Calendar API**.
3. **OAuth consent screen** → External. Then do **one** of these — skipping this step is what
   produces `Error 403: access_denied ... has not completed the Google verification process`:

   - **Publish app** (Publishing status → *Testing* → **Publish app**). ← **recommended**
   - or add your own address under **Test users → + Add users**.

   Publishing is the better default because **a Testing app's refresh token expires after 7 days**.
   Everything would work for a week, then the server would silently stop syncing with an auth error
   at whatever hour the timer fired. Publishing removes that rule.

   Because the app is not Google-verified you will see *"Google hasn't verified this app"* during
   consent → **Advanced → Go to <app> (unsafe)**. Verification only matters for distributing the app
   to other people; it does not change what the app can reach. The scopes stay narrow either way —
   `drive.file` can only touch files this app created.

   In the newer console these settings live under **Google Auth Platform → Audience**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. Copy the **Client ID** and **Client secret** straight into `.env`:

```
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
```

**No JSON download is needed.** A Desktop client's JSON contains nothing beyond those two values and
fixed Google endpoints, so `pap auth --login` builds the flow from `.env` directly — which keeps every
credential in one place. `GOOGLE_CLIENT_SECRETS_FILE` still works if you prefer the file.

### 4b. Run consent from WSL

```bash
pap auth --login --no-browser --port 8765
```

`--no-browser` matters on WSL: Python's automatic launch often does nothing without `wslu`
installed, and the flow just appears to hang. It prints a URL — paste that into your **Windows**
browser. The redirect to `http://localhost:8765` reaches WSL because WSL2 forwards localhost by
default.

It prints three lines. Paste them into `.env` and set `GOOGLE_ENABLED=true`:

```
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=1//...
```

**No file needs copying anywhere** — these same three lines go into the server's `/etc/pap/pap.env`
when you deploy.

If the browser never returns, `--port 8765` is already in use, or you want the random-port
behaviour: add `--port 0`. If you would rather keep a token file too, add `--write-token-file`.

### 4c. Verify

```bash
pap auth --status      # -> source: environment ... usable: yes  (makes no API call)
pap doctor             # google section all [+], and prints no values

pap backup db          # dumps, then uploads to Drive/_backups/pap
pap backup verify      # restores the newest dump and counts rows
```

`pap backup db` needs `pg_dump` on PATH, **and its major version must be >= the server's**. Ubuntu
24.04 ships client 16, which refuses to dump the Postgres 17 container. Either install the matching
client:

```bash
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update && sudo apt install -y postgresql-client-17
```

…or just run backups inside the container, where the versions already match:

```bash
docker compose --profile cli run --rm pap backup db
```

---

## 5. The same thing in Docker

```bash
docker build -t pap:local .
docker run --rm --network host --env-file .env \
  -v "$PWD/var:/var/lib/pap" pap:local doctor
```

## 6. Clean up

```bash
docker rm -f pap-local-pg
rm -rf var logs
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `pap: command not found` | `source .venv/bin/activate`, and `pip install -e .` was required |
| `Configuration error: PG_PASSWORD is not set` | `.env` missing, or you are in another directory — pass `--env-file` |
| Connection refused on 55432 | Container not up yet; use the `until` loop in step 1 |
| Consent flow hangs, no browser | Missing `--no-browser`; paste the printed URL into Windows |
| `redirect_uri_mismatch` | Client was created as *Web application*; it must be **Desktop app** |
| `Error 403: access_denied` / "has not completed the Google verification process" | App is in *Testing* and your address is not a Test user. Publish the app, or add yourself under Test users |
| "Google hasn't verified this app" | Expected for an unpublished personal app → **Advanced → Go to \<app\> (unsafe)** |
| Google returns no refresh token | Already consented once — revoke at <https://myaccount.google.com/permissions> and retry |
| Refresh token stops working after ~7 days | Consent screen still in *Testing*; publish it |
| `getUpdates` returns empty | You have not messaged the bot yet |
| Telegram 401 Unauthorized | Wrong token, or `bot` prefix duplicated in the URL |
