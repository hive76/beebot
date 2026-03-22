"""
BeeBot Knowledge Base Sync
Fetches all Google Docs from the configured Drive folder and writes
them as plain text to knowledge_base.txt for the bot to use.

Google Docs whose names start with '_' are treated as config docs:
  _beebot-prompt  → written to system_prompt.txt (bot persona/rules)
  Other _* docs   → skipped entirely
"""

import json
import os
import sys
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "/app/config/service-account.json"
)
DRIVE_FOLDER_ID   = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
OUTPUT_PATH       = os.environ.get("KNOWLEDGE_BASE_PATH", "/app/data/knowledge_base.txt")
MANIFEST_PATH     = os.environ.get("MANIFEST_PATH", "/app/data/sync_manifest.json")
SYSTEM_PROMPT_PATH = "/app/data/system_prompt.txt"
SCOPES            = ["https://www.googleapis.com/auth/drive.readonly"]

WORDPRESS_BASE_URL      = os.environ.get("WORDPRESS_BASE_URL", "")
WORDPRESS_SYNC_CATEGORY = os.environ.get("WORDPRESS_SYNC_CATEGORY", "beebot-slackbot")

EVENTBRITE_PRIVATE_TOKEN = os.environ.get("EVENTBRITE_PRIVATE_TOKEN", "")
EVENTBRITE_ORG_ID        = os.environ.get("EVENTBRITE_ORG_ID", "")

# WordPress slug blocklist — override with comma-separated env var, or use defaults
_DEFAULT_WP_BLOCKLIST = {
    "billing", "redirect-handler", "redirect-handler-local", "new-member-signup",
    "membership-registration", "membership-profile", "password-reset", "wiki", "home",
}
_wp_blocklist_env = os.environ.get("WORDPRESS_SLUG_BLOCKLIST", "")
WORDPRESS_SLUG_BLOCKLIST = (
    {s.strip() for s in _wp_blocklist_env.split(",") if s.strip()}
    if _wp_blocklist_env
    else _DEFAULT_WP_BLOCKLIST
)

# ── Google Drive Auth ─────────────────────────────────────────────────────────

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Doc Fetching ──────────────────────────────────────────────────────────────

def list_docs_in_folder(service, folder_id: str) -> list[dict]:
    """
    Recursively list all Google Docs in a folder and its subfolders.
    Returns list of {id, name, modifiedTime} dicts.
    """
    docs = []
    _collect_docs(service, folder_id, folder_id, "", docs)
    return docs


def _collect_docs(service, root_folder_id: str, folder_id: str, path_prefix: str, docs: list):
    """Recursive helper to walk folder tree."""
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None

    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token,
            pageSize=100,
        ).execute()

        for f in resp.get("files", []):
            full_name = f"{path_prefix}/{f['name']}".lstrip("/")
            if f["mimeType"] == "application/vnd.google-apps.folder":
                _collect_docs(service, root_folder_id, f["id"], full_name, docs)
            elif f["mimeType"] == "application/vnd.google-apps.document":
                docs.append({"id": f["id"], "name": full_name, "modifiedTime": f.get("modifiedTime", "")})

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def export_doc_as_text(service, doc_id: str, doc_name: str) -> str | None:
    """Export a Google Doc as plain text. Returns None on failure (logged)."""
    try:
        request = service.files().export_media(
            fileId=doc_id, mimeType="text/plain"
        )
        content = request.execute()
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return content
    except HttpError as e:
        log.error("Failed to export doc '%s' (%s): %s", doc_name, doc_id, e)
        return None
    except Exception as e:
        log.error("Unexpected error exporting '%s': %s", doc_name, e)
        return None


# ── Config Doc Handling ───────────────────────────────────────────────────────

def handle_config_doc(doc_name: str, text: str):
    """
    Process a '_'-prefixed config doc. Currently handles:
      _beebot-prompt  → writes system_prompt.txt
    """
    basename = doc_name.split("/")[-1].lower()
    if basename == "_beebot-prompt":
        path = Path(SYSTEM_PROMPT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log.info("System prompt written: %s (%d chars)", SYSTEM_PROMPT_PATH, len(text))
    else:
        log.info("Config doc '%s' — no handler, skipping", doc_name)


# ── WordPress Sync ────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks = []
        self._current_href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href and href.startswith("http"):
                self._current_href = href

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href:
            self._chunks.append(f" ({self._current_href})")
            self._current_href = None

    def handle_data(self, data):
        self._chunks.append(data)

    def get_text(self):
        return re.sub(r'\n{3,}', '\n\n', "".join(self._chunks)).strip()


def strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text()


def fetch_wordpress_pages(base_url: str, category: str) -> list[dict]:
    """
    Fetch top-level published WP pages (parent=0) and published posts in the given category.
    Skips pages whose slugs are in WORDPRESS_SLUG_BLOCKLIST.
    Returns list of {name, content, modified} dicts.
    """
    import urllib.request, urllib.parse, urllib.error

    api = base_url.rstrip("/") + "/wp-json/wp/v2"
    results = []

    # Pages: top-level only (parent=0) — pages have no taxonomy support
    page = 1
    while True:
        url = (f"{api}/pages?parent=0&status=publish"
               f"&per_page=100&page={page}"
               f"&_fields=id,title,content,modified,slug")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                items = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 400:
                break  # WP returns 400 when page number exceeds total pages
            log.error("WordPress: pages page %d error: %s", page, e)
            break
        except Exception as e:
            log.error("WordPress: pages page %d error: %s", page, e)
            break

        if not items:
            break

        for item in items:
            slug = item.get("slug", "")
            if slug in WORDPRESS_SLUG_BLOCKLIST:
                log.info("  ⊘ WP skip (blocklist): %s", slug)
                continue
            title = item["title"]["rendered"]
            content = strip_html(item["content"]["rendered"])
            if content:
                results.append({
                    "name": f"wordpress/{title}",
                    "content": content,
                    "modified": item.get("modified", ""),
                })
                log.info("  ✓ WP page: %s", title)

        page += 1

    # Posts: use category filter
    cat_url = f"{api}/categories?slug={urllib.parse.quote(category)}&_fields=id,name"
    try:
        with urllib.request.urlopen(cat_url, timeout=15) as r:
            cats = json.loads(r.read())
        if cats:
            cat_id = cats[0]["id"]
            log.info("WordPress: posts category '%s' = ID %d", category, cat_id)
            page = 1
            while True:
                url = (f"{api}/posts?categories={cat_id}&status=publish"
                       f"&per_page=100&page={page}"
                       f"&_fields=id,title,content,modified,slug")
                with urllib.request.urlopen(url, timeout=15) as r:
                    items = json.loads(r.read())
                if not items:
                    break
                for item in items:
                    title = item["title"]["rendered"]
                    content = strip_html(item["content"]["rendered"])
                    if content:
                        results.append({
                            "name": f"wordpress/{title}",
                            "content": content,
                            "modified": item.get("modified", ""),
                        })
                        log.info("  ✓ WP post: %s", title)
                page += 1
        else:
            log.info("WordPress: posts category '%s' not found — no posts synced", category)
    except Exception as e:
        log.error("WordPress: failed to fetch posts: %s", e)

    return results


# ── Eventbrite Sync ───────────────────────────────────────────────────────────

def fetch_eventbrite_events(private_token: str, org_id: str) -> list[dict]:
    """
    Fetch upcoming published events from Eventbrite for the given org.
    Returns list of {name, content} dicts compatible with the knowledge base format.
    """
    import urllib.request, urllib.parse, urllib.error
    from datetime import datetime as dt

    base = "https://www.eventbriteapi.com/v3"
    results = []
    params = urllib.parse.urlencode({
        "status": "live",
        "time_filter": "current_future",
        "order_by": "start_asc",
        "expand": "venue",
        "page_size": 50,
    })
    url = f"{base}/organizations/{org_id}/events/?{params}"

    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {private_token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.error("Eventbrite API error %d: %s", e.code, e.reason)
            break
        except Exception as e:
            log.error("Eventbrite fetch error: %s", e)
            break

        for event in data.get("events", []):
            title = event["name"]["text"]
            summary = event.get("summary", "")
            event_url = event.get("url", "")

            start_local = event.get("start", {}).get("local", "")
            try:
                start_dt = dt.fromisoformat(start_local)
                date_str = start_dt.strftime("%A, %B %-d %Y at %-I:%M %p")
            except Exception:
                date_str = start_local

            venue = event.get("venue") or {}
            addr = venue.get("address") or {}
            venue_parts = [venue.get("name", ""), addr.get("address_1", ""),
                           addr.get("city", ""), addr.get("region", "")]
            venue_str = ", ".join(p for p in venue_parts if p) or "TBD"

            lines = [
                f"Title: {title}",
                f"Date: {date_str}",
                f"Location: {venue_str}",
            ]
            if summary:
                lines.append(f"Description: {summary}")
            if event_url:
                lines.append(f"URL: {event_url}")

            results.append({
                "name": f"eventbrite/{title}",
                "content": "\n".join(lines),
            })
            log.info("  ✓ Eventbrite: %s (%s)", title, date_str)

        pagination = data.get("pagination", {})
        if pagination.get("has_more_items"):
            continuation = pagination["continuation"]
            next_params = urllib.parse.urlencode({
                **dict(urllib.parse.parse_qsl(params)),
                "continuation": continuation,
            })
            url = f"{base}/organizations/{org_id}/events/?{next_params}"
        else:
            url = None

    return results


# ── Manifest ──────────────────────────────────────────────────────────────────

def load_manifest() -> dict:
    """Load previous sync manifest {doc_id: {name, modifiedTime}}. Empty dict if none."""
    path = Path(MANIFEST_PATH)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(docs: list):
    """Save current sync manifest."""
    manifest = {d["id"]: {"name": d["name"], "modifiedTime": d["modifiedTime"]} for d in docs}
    Path(MANIFEST_PATH).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def diff_manifest(old: dict, new_docs: list) -> dict:
    """Return {added: [...], changed: [...], removed: [...]} doc names."""
    new = {d["id"]: d for d in new_docs}
    added   = [d["name"] for id, d in new.items() if id not in old]
    changed = [d["name"] for id, d in new.items()
               if id in old and d["modifiedTime"] != old[id]["modifiedTime"]]
    removed = [old[id]["name"] for id in old if id not in new]
    return {"added": added, "changed": changed, "removed": removed}


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sync():
    log.info("Starting knowledge base sync from Google Drive folder: %s", DRIVE_FOLDER_ID)

    service = get_drive_service()
    all_docs = list_docs_in_folder(service, DRIVE_FOLDER_ID)

    if not all_docs:
        log.warning("No Google Docs found in folder %s", DRIVE_FOLDER_ID)
        sys.exit(1)

    log.info("Found %d docs total", len(all_docs))

    # Separate config docs (starting with '_') from knowledge base docs
    config_docs = [d for d in all_docs if d["name"].startswith("_")]
    kb_docs     = [d for d in all_docs if not d["name"].startswith("_")]

    # Process config docs first
    for doc in config_docs:
        log.info("Config doc: %s", doc["name"])
        text = export_doc_as_text(service, doc["id"], doc["name"])
        if text:
            handle_config_doc(doc["name"], text.strip())

    # Diff against previous run (KB docs only)
    old_manifest = load_manifest()
    diff = diff_manifest(old_manifest, kb_docs)
    first_run = not old_manifest

    sections = []
    failed = []
    synced = []

    for doc in kb_docs:
        log.info("Exporting: %s", doc["name"])
        text = export_doc_as_text(service, doc["id"], doc["name"])

        if text is None:
            failed.append(doc["name"])
            continue

        text = text.strip()
        if not text:
            log.warning("Doc '%s' exported empty — skipping", doc["name"])
            continue

        section = (
            f"=== DOCUMENT: {doc['name']} ===\n\n"
            f"{text}\n\n"
            f"=== END: {doc['name']} ==="
        )
        sections.append(section)
        synced.append(doc["name"])

    if not sections:
        log.error("All doc exports failed — not writing empty knowledge base")
        sys.exit(1)

    # ── WordPress pages ────────────────────────────────────────────────────────
    if WORDPRESS_BASE_URL:
        wp_pages = fetch_wordpress_pages(WORDPRESS_BASE_URL, WORDPRESS_SYNC_CATEGORY)
        for page in wp_pages:
            sections.append(
                f"=== DOCUMENT: {page['name']} ===\n\n"
                f"{page['content']}\n\n"
                f"=== END: {page['name']} ==="
            )
            synced.append(page["name"])
        if not wp_pages:
            log.info("WordPress: no pages synced (check WORDPRESS_BASE_URL and category '%s')", WORDPRESS_SYNC_CATEGORY)
    else:
        log.info("WordPress sync skipped — WORDPRESS_BASE_URL not set")

    # ── Eventbrite events ──────────────────────────────────────────────────────
    if EVENTBRITE_PRIVATE_TOKEN and EVENTBRITE_ORG_ID:
        eb_events = fetch_eventbrite_events(EVENTBRITE_PRIVATE_TOKEN, EVENTBRITE_ORG_ID)
        for event in eb_events:
            sections.append(
                f"=== DOCUMENT: {event['name']} ===\n\n"
                f"{event['content']}\n\n"
                f"=== END: {event['name']} ==="
            )
            synced.append(event["name"])
        if not eb_events:
            log.info("Eventbrite: no upcoming events found")
    else:
        log.info("Eventbrite sync skipped — EVENTBRITE_PRIVATE_TOKEN or EVENTBRITE_ORG_ID not set")

    # ── Write knowledge base (atomic) ─────────────────────────────────────────
    output_path = Path(OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        f"# BeeBot Knowledge Base\n"
        f"# Generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"# Documents: {len(synced)}\n"
        f"# Source folder: {DRIVE_FOLDER_ID}\n\n"
    )

    tmp_path = output_path.parent / (output_path.name + ".tmp")
    tmp_path.write_text(header + "\n\n".join(sections), encoding="utf-8")
    os.replace(tmp_path, output_path)
    save_manifest(kb_docs)

    kb_size = output_path.stat().st_size
    log.info("Knowledge base written: %s (%d bytes, %d docs)", OUTPUT_PATH, kb_size, len(synced))

    # Report changes
    if first_run:
        log.info("First sync — all docs loaded:")
        for name in synced:
            log.info("  ✓ %s", name)
    else:
        if not any([diff["added"], diff["changed"], diff["removed"]]):
            log.info("No changes since last sync.")
        else:
            for name in diff["added"]:
                log.info("  ➕ NEW: %s", name)
            for name in diff["changed"]:
                log.info("  ✏️  UPDATED: %s", name)
            for name in diff["removed"]:
                log.info("  🗑️  REMOVED: %s", name)

    for name in failed:
        log.warning("  ✗ FAILED: %s", name)

    if failed:
        log.warning("%d docs failed to export", len(failed))
        # Don't exit non-zero — partial KB is still useful


if __name__ == "__main__":
    run_sync()
