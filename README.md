# BeeBot 🐝

Hive76's Slack assistant. Answers questions in `#new-members` and responds to
`@BeeBot` mentions anywhere. Pulls its knowledge from Google Drive docs, the
Hive76 WordPress site, and Eventbrite events.

---

## Prerequisites

- A Linux host with Docker + Docker Compose
- A Hive76 Google Workspace admin account
- An Anthropic API account (console.anthropic.com)

---

## Step 1 — Google Workspace: Create Service Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project called `hive76-beebot` (or use an existing one)
3. Enable the **Google Drive API**:
   - APIs & Services → Enable APIs → search "Google Drive API" → Enable
4. Create a Service Account:
   - APIs & Services → Credentials → Create Credentials → Service Account
   - Name: `beebot-sync`
   - Role: none needed (access granted via Drive sharing, not project roles)
5. Create and download a JSON key:
   - Click the service account → Keys → Add Key → Create new key → JSON
   - Save as `config/service-account.json` in this directory
6. Note the service account **email address** (looks like `beebot-sync@hive76-beebot.iam.gserviceaccount.com`)

### Share the Drive folder with the service account

1. Open the `HiveBot Docs` folder in Google Drive
2. Click Share
3. Paste the service account email
4. Set to **Viewer**
5. Uncheck "Notify people" → Share

The service account can now read all docs in that folder and subfolders.

---

## Step 2 — Slack: Create the App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch
2. Name: `BeeBot`, select your Hive76 workspace

### Enable Socket Mode
- Settings → Socket Mode → Enable
- Generate an App-Level Token with scope `connections:write`
- Save this token as `SLACK_APP_TOKEN` (starts with `xapp-`)

### Bot Token Scopes
- Features → OAuth & Permissions → Bot Token Scopes → Add:
  - `app_mentions:read`
  - `channels:history`
  - `chat:write`
  - `commands`
  - `users:read`

### Subscribe to Events
- Features → Event Subscriptions → Enable Events
- Subscribe to bot events:
  - `app_mention`
  - `message.channels`

### Slash Commands
- Features → Slash Commands → Create New Command for each:

| Command | Description |
|---------|-------------|
| `/beebot-sync` | Force knowledge base sync (admin only) |
| `/beebot-config` | View and manage bot configuration (admin only) |
| `/beebot-logs` | View recent bot logs (admin only) |
| `/beebot-restart` | Restart the bot process (admin only) |

### Install App
- Settings → Install App → Install to Workspace → Allow
- Copy the **Bot User OAuth Token** (starts with `xoxb-`) → `SLACK_BOT_TOKEN`

### Invite BeeBot to channels
```
/invite @BeeBot
```
Do this in `#new-members` and any other channel where you want mention support.

### Get the #new-members channel ID
Right-click `#new-members` → View channel details → scroll to bottom → Copy channel ID
Looks like: `C08XXXXXXXX`

---

## Step 3 — Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. API Keys → Create Key → name it `beebot`
3. Copy the key → `ANTHROPIC_API_KEY`

---

## Step 4 — Deploy

```bash
# SSH into your server
ssh user@your-server

# Clone the repo
git clone https://github.com/hive76/beebot.git /opt/beebot
cd /opt/beebot

# Copy env file and fill in values
cp .env.example .env
nano .env   # fill in all REPLACE-ME values

# Put your service account JSON in place
mkdir -p config
# scp service-account.json to config/service-account.json

# Pull the image from GitHub Container Registry
docker compose pull

# Run initial sync to populate knowledge base
docker compose run --rm beebot-sync

# Start the bot
docker compose up -d beebot

# Check logs
docker logs -f beebot
```

---

## Step 5 — Automatic Updates (Watchtower)

BeeBot uses a pull-based deployment model. When commits are merged to `main`,
GitHub Actions builds a new image and pushes it to
`ghcr.io/hive76/beebot:latest`. Watchtower on the host polls for new images
and restarts the container automatically — no SSH access from CI required.

Add Watchtower to your host's `docker-compose.yml` (or run it separately):

```yaml
  watchtower:
    image: containrrr/watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /root/.docker/config.json:/config.json:ro  # for GHCR auth
    command: --interval 300 beebot   # poll every 5 min, watch beebot only
```

To authenticate Watchtower with GHCR, create a GitHub PAT with `read:packages`
scope and run `docker login ghcr.io` on the host — Watchtower picks up the
stored credentials automatically.

---

## Step 6 — Daily Sync Cron

The sync container runs once and exits. Trigger it on a schedule via cron on the host:

```bash
crontab -e
# Add (runs at 3am daily):
0 3 * * * docker compose -f /opt/beebot/docker-compose.yml run --rm beebot-sync >> /var/log/beebot-sync.log 2>&1
```

Or trigger it manually anytime with `/beebot-sync` in Slack.

---

## Slash Commands Reference

### `/beebot-sync`
Triggers an immediate knowledge base sync from Google Drive, WordPress, and Eventbrite. Admin only.

### `/beebot-config`
View and manage runtime configuration. Admin only.

| Subcommand | Example | Description |
|------------|---------|-------------|
| `show` | `/beebot-config show` | Display all current settings |
| `set KEY value` | `/beebot-config set BOT_EMOJI :bee:` | Update a setting |
| `reset KEY` | `/beebot-config reset BOT_EMOJI` | Reset a setting to default |
| `export` | `/beebot-config export` | Export full config as JSON |
| `wp-blocklist-show` | `/beebot-config wp-blocklist-show` | List blocked WordPress slugs |
| `wp-blocklist-add slug` | `/beebot-config wp-blocklist-add events` | Block a WordPress slug from sync |
| `wp-blocklist-remove slug` | `/beebot-config wp-blocklist-remove events` | Unblock a slug |

### `/beebot-logs`
Returns the last 50 lines of the bot log as an ephemeral message. Admin only.

### `/beebot-restart`
Gracefully exits the bot process — Docker's restart policy brings it back within seconds. Admin only.

---

## Troubleshooting

**Bot not responding in #new-members**
- Check `docker logs beebot` for errors
- Verify `NEW_MEMBERS_CHANNEL_ID` matches the actual channel ID (not name)
- Confirm BeeBot is invited to the channel (`/invite @BeeBot`)

**Sync failing**
- Check service account JSON is at `config/service-account.json`
- Verify the Drive folder is shared with the service account email
- Run `docker compose run --rm beebot-sync` and check output
- Use `/beebot-logs` in Slack for recent error detail

**Wrong answers / missing info**
- Check knowledge base: `docker exec beebot cat /app/data/knowledge_base.txt`
- If a doc is missing, confirm it's a Google Doc (not a Sheet/Slide/PDF) in the folder
- Run `/beebot-sync` to force a refresh

**Rate limit hitting legitimate users**
- Increase `RATE_LIMIT_MAX` via `/beebot-config set RATE_LIMIT_MAX 20`

---

## Development

```bash
# Run tests (inside Docker test stage)
make test

# Build production image
make build

# Scan for secrets and CVEs
make scan
```

Tests run automatically in the Docker test stage — if they fail, the build fails.
The CI pipeline (GitHub Actions) runs tests and secret scanning on every PR.

See [docs/BeeBot Admin Guide.md](docs/BeeBot%20Admin%20Guide.md) for the
non-technical content management guide intended for Hive76 management.

---

## Architecture

```
Google Drive (HiveBot Docs folder)
  └── service account (read-only)
        └── sync_docs.py (daily cron + /beebot-sync)
              └── /app/data/knowledge_base.txt (Docker volume)
                    └── beebot.py (Slack Bolt, Socket Mode)
                          ├── #new-members: all messages
                          ├── anywhere: @BeeBot mentions
                          └── Anthropic API (claude-haiku-4-5)

GitHub (hive76/beebot)
  └── GitHub Actions CI (test + secret scan on PRs)
        └── GHCR (ghcr.io/hive76/beebot:latest)
              └── Watchtower on host (polls, pulls, restarts)
```
