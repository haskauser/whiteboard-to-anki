# whiteboard-to-anki

A self-hosted automation pipeline that turns whiteboard photos into Anki flashcards using Claude Vision.

Take a photo of a lecture whiteboard on your phone → it appears in your Anki deck within 2 minutes, fully formatted with both basic and cloze cards, organised by medical topic.

---

## Architecture

```
Phone camera
     │  (iOS/Android Immich app)
     ▼
┌─────────┐
│  Immich │  ← self-hosted photo server
└────┬────┘
     │  asset ID (every 1 min, n8n polls album)
     ▼
┌─────────────┐
│     n8n     │  ← workflow automation
└──────┬──────┘
       │  POST /process  {assetId}
       ▼
┌──────────────┐
│  card-maker  │  ← FastAPI microservice
│  (Python)    │    fetches image from Immich
│              │    sends to Claude Vision API
│              │    returns structured cards JSON
└──────┬───────┘
       │  POST /cards  {cards[], topic}
       ▼
┌──────────────┐
│   anki-api   │  ← FastAPI microservice
│  (Python)    │    stops anki-sync container
│              │    writes cards to SQLite DB
│              │    restarts anki-sync
└──────┬───────┘
       │  (Anki clients sync on next open)
       ▼
┌────────────────────┐
│  Anki desktop/iOS  │
└────────────────────┘
```

### Inter-service communication

All services share a Docker network named `proxy`. Caddy reverse-proxies each service by container name. The `card-maker` service additionally joins the `immich_default` network to reach Immich directly.

---

## Data flow (step by step)

1. **Photo taken** — Immich iOS/Android app uploads to a specific album
2. **n8n polls** — every 60 seconds, fetches album contents via Immich API
3. **Filter fresh** — JS node keeps only assets created within the last 2 minutes
4. **card-maker** — downloads the image thumbnail from Immich, base64-encodes it, sends to `claude-opus-4-5` with a medical flashcard prompt
5. **Claude responds** — returns JSON with a `topic` and array of `cards` (basic + cloze types)
6. **anki-api** — receives cards, stops `anki-sync` container to get exclusive SQLite access, inserts notes and cards into `collection.anki2`, updates `col.mod` so sync clients detect changes, restarts `anki-sync`
7. **Anki syncs** — next time Anki desktop or mobile opens, it pulls the new deck `autogen::Topic`

---

## Repository structure

```
whiteboard-to-anki/
├── docker-compose.yml        # full stack
├── .env.example              # all required variables
│
├── card-maker/               # image → Claude → cards JSON
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
│
├── anki-api/                 # cards JSON → Anki SQLite
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
│
├── caddy/                    # reverse proxy
│   ├── Dockerfile            # custom build with Cloudflare DNS plugin
│   └── Caddyfile
│
└── n8n/
    └── whiteboard-workflow.json   # import this into n8n
```

---

## Prerequisites

- A server or machine with Docker and Docker Compose
- An [Anthropic API key](https://console.anthropic.com)
- [Immich](https://immich.app) running (its compose stack creates `immich_default` network)
- A domain with DNS you can manage (Cloudflare recommended)
- Anki desktop with the self-hosted sync server configured

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/your-username/whiteboard-to-anki
cd whiteboard-to-anki
cp .env.example .env
# Edit .env and fill in all values
```

### 2. Start Immich first (creates immich_default network)

Follow [Immich's official docs](https://immich.app/docs/install/docker-compose). Once running, create an album in the web UI and note its UUID from the URL.

### 3. Start the stack

```bash
docker compose up -d
```

### 4. Import the n8n workflow

1. Open `https://flow.your.domain.com`
2. Go to **Workflows → Import from file**
3. Upload `n8n/whiteboard-workflow.json`
4. Edit the **Get Album** node: replace `YOUR_ALBUM_ID` and `YOUR_IMMICH_API_KEY`
5. Activate the workflow

### 5. Configure Anki sync

In Anki desktop: `Tools → Preferences → Syncing` → set server to `https://anki.your.domain.com`

---

## Security notes

### What was done in production (not in this repo)

| Concern | Production approach |
|---|---|
| Ports | All services bound to `127.0.0.1`; only Caddy exposed on 80/443 |
| Firewall | UFW + cloud firewall (Hetzner) blocking all direct container ports |
| SSH | Password auth disabled; key-only; fail2ban active |
| TLS | Cloudflare DNS-01 ACME challenge (works even when 80/443 are firewalled) |
| Secrets | Stored in `/root/secrets/` as env files, never in compose files |
| Docker socket | anki-api mounts `/var/run/docker.sock` — in production consider a socket proxy |

### What to do before deploying publicly

- Change all default passwords in `.env`
- Do not commit `.env` (it is in `.gitignore`)
- Consider replacing the Docker socket mount in `anki-api` with [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)
- Restrict n8n with strong basic auth credentials
- Set `SIGNUPS_ALLOWED=false` in Vaultwarden after creating your account

---

## Simplification for local use

If you just want the core pipeline without a domain or reverse proxy:

```bash
# Start only the pipeline services
docker compose up -d card-maker anki-api n8n

# card-maker available at http://localhost:8766
# anki-api available at http://localhost:8765
# n8n available at http://localhost:5678
```

Then in the n8n workflow replace container-name URLs with `localhost` equivalents.

---

## Card format

Claude generates two card types:

**Basic** — question / answer
```json
{"type": "basic", "front": "What causes type II respiratory failure?", "back": "Hypoventilation leading to CO2 retention"}
```

**Cloze** — fill-in-the-blank
```json
{"type": "cloze", "text": "{{c1::Surfactant}} is produced by {{c2::type II pneumocytes}}"}
```

Cards are placed in `autogen::Topic` decks (e.g. `autogen::Respiratory`).

---

## Production (not included)

The following services run in production but are excluded from this repo as they are standard deployments with no custom code:

- **Vaultwarden** — self-hosted Bitwarden-compatible password manager
- **Paperless-ngx** — document OCR and management
- **Obsidian LiveSync** via CouchDB — real-time vault sync
- **Excalidraw** — self-hosted whiteboard
- **Dashboard** — static HTML homepage at the root domain

All are connected to the same `proxy` Docker network and served through Caddy.
