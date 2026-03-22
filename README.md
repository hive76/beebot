# BeeBot 🐝

Hive76's Slack assistant. Answers questions in `#new-members` and responds to
`@BeeBot` mentions anywhere. Pulls its knowledge from Google Drive docs.

---

## Prerequisites

- Docker + Docker Compose installed on the Linode
- Portainer running (or plain Docker Compose if you prefer)
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
- Features → Slash Commands → Create New Command
  - Command: `/beebot-sync`
  - Short description: `Force knowledge base sync (admin only)`
  - Usage hint: (leave blank)

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
2. API Keys → Create Key → name it `beebot-linode`
3. Copy the key → `ANTHROPIC_API_KEY`

---

## Step 4 — Deploy on Linode

```bash
# SSH into your Linode
ssh user@your-linode-ip

# Clone or copy the beebot directory
mkdir -p /opt/beebot
# scp or git clone the files here

cd /opt/beebot

# Copy env file and fill in values
cp .env.example .env
nano .env   # fill in all REPLACE-ME values

# Put your service account JSON in place
mkdir -p config
# scp service-account.json to config/service-account.json

# Build the image
docker build -t beebot:latest .

# Run initial sync to populate knowledge base
docker compose run --rm beebot-sync

# Verify knowledge base was created
docker compose run --rm beebot-sync  # check logs for "✓" entries

# Start the bot
docker compose up -d beebot

# Check logs
docker logs -f beebot
```

### Set up daily sync cron

```bash
chmod +x /opt/beebot/cron-sync.sh

# Edit crontab
crontab -e

# Add this line (runs at 3am UTC daily):
0 3 * * * /opt/beebot/cron-sync.sh >> /var/log/beebot-sync.log 2>&1
```

---

## Step 5 — Portainer Setup

If you're managing via Portainer instead of CLI:

1. In Portainer → Stacks → Add Stack
2. Name: `beebot`
3. Upload or paste the `docker-compose.yml`
4. Set environment variables in the "Environment variables" section
   (or use the .env file approach if your Portainer is on the same host)
5. Deploy the stack

To run a manual sync from Portainer:
- Containers → beebot-sync → run a one-off task
- Or: use the "Exec console" on the beebot container to run `python sync/sync_docs.py`

---

## Managing Knowledge Base Docs

### Adding a new doc
1. Create a Google Doc in the `HiveBot Docs` Drive folder
2. Wait for 3am cron, OR run `/beebot-sync` in Slack

### Updating an existing doc
1. Edit the Google Doc normally
2. Wait for 3am cron, OR run `/beebot-sync` in Slack

### Subfolder structure (recommended)
```
HiveBot Docs/
  bylaws.gdoc
  orientation-guide.gdoc
  equipment/
    laser-cutter.gdoc
    summacut-d120.gdoc
    3d-printer.gdoc
    cnc.gdoc
  faq.gdoc
```

The sync script recursively walks all subfolders. Doc names appear in the
knowledge base headers so Claude knows which doc each section came from.

---

## Slash Commands

| Command | Who | What |
|---|---|---|
| `/beebot-sync` | Admins only | Force immediate knowledge base sync from Drive |

Admin user IDs are set in `.env` as `ADMIN_SLACK_USER_IDS`.

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

**Wrong answers / missing info**
- Check knowledge base: `docker exec beebot cat /app/data/knowledge_base.txt`
- If doc is missing, confirm it's a Google Doc (not a Sheet/Slide/PDF) in the folder
- Run `/beebot-sync` to force a refresh

**Rate limit hitting legitimate users**
- Increase `RATE_LIMIT_MAX` in `.env` and restart: `docker compose restart beebot`

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
```
