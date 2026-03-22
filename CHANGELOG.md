# Changelog

All notable changes to BeeBot are documented here.
Follows [Semantic Versioning](https://semver.org/): MAJOR.MINOR.PATCH

---

## [1.1.0] — 2026-03-22

### Added
- Eventbrite integration: upcoming events synced into knowledge base daily
- System prompt now loaded from `_beebot-prompt` Google Doc in Drive folder (non-technical admins can update bot persona without code changes)
- `_` prefix convention for config docs in Drive — excluded from knowledge base
- `CLAUDE_MODEL` env var — switch models without rebuilding
- `BOT_EMOJI` env var — customize bot emoji per deployment
- `WORDPRESS_SLUG_BLOCKLIST` env var — override blocked WP slugs without rebuilding
- Input length cap (2000 chars) to prevent token cost abuse
- Startup environment validation — fails fast with clear error if required vars missing
- Graceful SIGTERM shutdown handler
- Version logging at startup (`BeeBot v1.1.0 starting up`)
- Response logging for observability (first 200 chars of each answer)
- `VERSION` file as single source of truth for version string
- `Makefile` for common operations (build, deploy, sync, logs)
- `CHANGELOG.md` (this file)
- Pinned dependency versions in `requirements.txt`

### Changed
- WordPress sync: switched from category filter to `parent=0` (top-level pages only) — categories not supported on WP `page` type
- WordPress URL construction: `WORDPRESS_SYNC_CATEGORY` now URL-encoded
- Knowledge base write is now atomic (write to `.tmp`, then `os.replace()`)
- `/beebot-sync` output in Slack: filtered to log lines only (strips internal paths)
- Container now runs as non-root user `appuser` (UID 1000)
- System prompt uses XML `<knowledge_base>` tags (Anthropic-recommended for RAG injection defense)
- User input wrapped in `<user_message>` tags before sending to Claude
- Added instruction-persistence statement to default system prompt

### Fixed
- `ADMIN_SLACK_USER_IDS` empty set now logs a warning at startup
- Real Google Drive folder ID removed from `.env.example`
- Hardcoded `"Hive76"` venue fallback in Eventbrite replaced with `"TBD"`
- Anthropic client now has 30s timeout (previously could hang indefinitely)

---

## [1.0.0] — 2026-03-21

### Initial deployment
- Slack bot (Socket Mode) responding to messages in `#new-members` and `@BeeBot` mentions
- Google Drive sync: recursively exports all Google Docs from configured folder
- WordPress sync: top-level pages via REST API with slug blocklist
- Sync change detection via manifest file (`sync_manifest.json`)
- Admin `/beebot-sync` slash command
- Per-user rate limiting (10 requests/hour default)
- New member welcome message on `member_joined_channel` event
- Docker Compose deployment with separate sync container
