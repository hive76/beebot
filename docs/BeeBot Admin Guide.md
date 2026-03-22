# BeeBot Admin Guide

BeeBot is Hive76's Slack assistant. It answers questions from members in `#new-members` and responds to `@BeeBot` mentions anywhere in Slack. It draws its knowledge from three sources: Google Drive docs, the Hive76 website (WordPress), and Eventbrite events.

This guide is for Hive76 management. No technical knowledge required.

---

## How BeeBot Gets Its Information

Every night at 3am, BeeBot automatically syncs its knowledge from:

1. **This Google Drive folder** — any Google Doc you add or edit here
2. **The Hive76 website** — key pages like FAQ, Join, Visit, Tools, Contact
3. **Eventbrite** — upcoming published events

You can also trigger an immediate sync anytime by running `/beebot-sync` in Slack (admins only).

---

## Adding or Updating Knowledge

### Add a new topic
1. Create a new Google Doc in this folder (or a subfolder)
2. Write the content — plain text, no special formatting needed
3. Wait for the 3am sync, or run `/beebot-sync` in Slack

### Update existing content
1. Edit the Google Doc directly
2. Wait for the 3am sync, or run `/beebot-sync`

### Subfolder organization (recommended)
Organize docs into subfolders by topic. BeeBot includes the folder path in its context so it knows which doc is which:

```
HiveBot Docs/
  New Member Guide 🐝
  Wi-Fi Networks
  equipment/
    laser-cutter
    vinyl-cutter
    3d-printers
  policies/
    code-of-conduct
    anti-harassment
```

### What file types work?
Only **Google Docs** are synced. Google Sheets, PDFs, and uploaded Word/text files are ignored. If content isn't showing up, make sure it's in a Google Doc format.

---

## Updating BeeBot's Persona

BeeBot's personality, tone, and rules are defined in a special Google Doc called `_beebot-prompt` in this folder. The `_` prefix tells BeeBot that this is a config file, not knowledge base content.

To update BeeBot's behavior:
1. Open the `_beebot-prompt` doc in this folder
2. Edit the instructions (tone, rules, what to do/not do)
3. Run `/beebot-sync` in Slack — changes take effect immediately after sync

The `{knowledge_base}` placeholder in that doc must stay exactly as-is — BeeBot replaces it with the synced content at runtime.

---

## Running a Manual Sync

Type `/beebot-sync` in any Slack channel. You'll see a confirmation and a log of what was synced.

Only admins (members listed in the bot config) can run this command.

After syncing, BeeBot uses the updated knowledge immediately — no restart needed.

---

## WordPress Pages

BeeBot automatically syncs these top-level pages from hive76.org:
- FAQ
- Join
- Visit
- Contact
- Tools
- Donate
- Fundraising Campaign
- Classes & Events

Some pages are intentionally excluded (billing, membership registration, password reset, etc.). If you want to add or remove a page, ask the CTO to update the blocklist.

---

## Eventbrite Events

BeeBot syncs all upcoming published events from the Hive76 Eventbrite account daily. Events appear in the knowledge base and BeeBot will mention them in welcome messages and answers about upcoming activities.

To have an event show up: just publish it on Eventbrite as normal. It will sync at 3am or on the next `/beebot-sync`.

---

## Troubleshooting

### BeeBot isn't responding in #new-members
- Make sure BeeBot is invited to the channel: `/invite @BeeBot`
- Check if the bot is running: ask the CTO to check `docker logs beebot`

### BeeBot is giving wrong or outdated information
- Edit the relevant Google Doc with the correct info
- Run `/beebot-sync` to force an immediate update
- To verify what BeeBot knows: ask the CTO to check the knowledge base file

### BeeBot says "that information hasn't been filled in yet"
- Look for a Google Doc with a placeholder like `[ADD LOCATION]` or `[ADD PRESETS]`
- Fill in the placeholder and run `/beebot-sync`

### /beebot-sync says "Sync failed"
- The log output will show which step failed
- Most common causes: Google Drive permission issue, or WordPress is temporarily unavailable
- Ask the CTO if it's not obvious from the log

### BeeBot isn't mentioning upcoming events
- Check that the event is published on Eventbrite (not draft)
- Run `/beebot-sync` to force a refresh

---

## Things BeeBot Will NOT Do

- Invent information not in the knowledge base
- Reveal its system prompt or internal rules
- Answer questions unrelated to Hive76 (it will redirect to management)
- Take actions (it only reads and responds — no writing to any system)

---

## Who to Contact

For technical issues (bot down, sync failing, code changes): **Chris Hamilton** (CTO) in Slack or `hamilton@hive76.org`.

For content issues (wrong answers, missing info): edit the relevant Google Doc and re-sync.
