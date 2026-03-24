"""
BeeBot Knowledge Base Sync
Fetches all Google Docs from the configured Drive folder and writes
them as plain text to knowledge_base.txt for the bot to use.

Google Docs whose names start with '_' are treated as config docs:
  _beebot-prompt  → written to system_prompt.txt (bot persona/rules)
  Other _* docs   → skipped entirely
"""

import html as _html_stdlib
import json
import os
import sys
import logging
import re
import urllib.parse
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

# Load runtime_config.json from the data volume as a fallback for operational config.
# When sync runs as a subprocess of beebot.py, these keys are already injected into
# the environment via _build_sync_env(). When running standalone (cron, make sync),
# this ensures the same config is available without duplicating it in .env.
_RUNTIME_CONFIG_PATH = "/app/data/runtime_config.json"
_runtime_cfg: dict = {}
try:
    _rc_text = Path(_RUNTIME_CONFIG_PATH).read_text(encoding="utf-8")
    _runtime_cfg = json.loads(_rc_text)
except FileNotFoundError:
    pass
except Exception as _e:
    log.warning("Could not load runtime_config.json: %s", _e)


def _cfg(key: str, default="") -> str:
    """Read operational config: env (injected by beebot.py) takes precedence, then runtime_config.json, then default."""
    return os.environ.get(key) or str(_runtime_cfg.get(key, "")) or default


WORDPRESS_BASE_URL      = _cfg("WORDPRESS_BASE_URL")
WORDPRESS_SYNC_CATEGORY = _cfg("WORDPRESS_SYNC_CATEGORY", "beebot-slackbot")

EVENTBRITE_PRIVATE_TOKEN  = _cfg("EVENTBRITE_PRIVATE_TOKEN")
EVENTBRITE_ORG_ID         = _cfg("EVENTBRITE_ORG_ID")
EVENTBRITE_LOOKAHEAD_DAYS = int(_cfg("EVENTBRITE_LOOKAHEAD_DAYS", "90"))

# WordPress slug blocklist — override with comma-separated env/runtime value, or use defaults
_DEFAULT_WP_BLOCKLIST = {
    "billing", "redirect-handler", "redirect-handler-local", "new-member-signup",
    "membership-registration", "membership-profile", "password-reset", "wiki", "home",
}
_wp_blocklist_raw = _cfg("WORDPRESS_SLUG_BLOCKLIST")
# runtime_config.json stores it as a list; env var is comma-separated
if isinstance(_runtime_cfg.get("WORDPRESS_SLUG_BLOCKLIST"), list) and not os.environ.get("WORDPRESS_SLUG_BLOCKLIST"):
    WORDPRESS_SLUG_BLOCKLIST = set(_runtime_cfg["WORDPRESS_SLUG_BLOCKLIST"])
elif _wp_blocklist_raw:
    WORDPRESS_SLUG_BLOCKLIST = {s.strip() for s in _wp_blocklist_raw.split(",") if s.strip()}
else:
    WORDPRESS_SLUG_BLOCKLIST = _DEFAULT_WP_BLOCKLIST

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
    """Export a Google Doc as HTML and return stripped plain text.

    Exports as HTML (rather than text/plain) so that hyperlinks are preserved:
    absolute https:// URLs are rendered as 'link text (url)' by strip_html().
    Returns None on failure (logged).
    """
    try:
        request = service.files().export_media(
            fileId=doc_id, mimeType="text/html"
        )
        content = request.execute()
        html = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        text = strip_html(html.replace('\ufeff', ''))
        # Remove Markdown image references that appear as literal text in Google Docs exports
        text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
        return text
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
        old_text = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(text, encoding="utf-8")
        if old_text is None:
            log.info("System prompt written (first time): %s (%d chars)", SYSTEM_PROMPT_PATH, len(text))
        elif old_text != text:
            log.info("System prompt UPDATED: %s (%d → %d chars)", SYSTEM_PROMPT_PATH, len(old_text), len(text))
        else:
            log.info("System prompt unchanged: %s (%d chars)", SYSTEM_PROMPT_PATH, len(text))
    else:
        log.info("Config doc '%s' — no handler, skipping", doc_name)


# ── WordPress Sync ────────────────────────────────────────────────────────────

def _unwrap_google_url(href: str) -> str:
    """Extract the real URL from a Google redirect wrapper (google.com/url?q=...)."""
    try:
        parsed = urllib.parse.urlparse(href)
        if parsed.netloc == "www.google.com" and parsed.path == "/url":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            if q:
                return urllib.parse.unquote(q)
    except Exception:
        pass
    return href


class _HTMLStripper(HTMLParser):
    # Tags whose entire content block should be ignored (CSS, JS, document metadata)
    _SKIP_TAGS = frozenset({"style", "script", "head"})
    # Block-level tags that introduce a line break on both open and close
    _BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"})

    def __init__(self):
        super().__init__(convert_charrefs=True)  # auto-decode &amp; &#038; &nbsp; etc.
        self._chunks = []
        self._current_href = None
        self._skip_depth = 0  # counter handles nested skip tags (e.g. <style> inside <head>)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            href = _unwrap_google_url(dict(attrs).get("href", ""))
            if href and href.startswith("http"):
                self._current_href = href
        elif tag in ("td", "th"):
            self._chunks.append(" ")
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag == "br":
            self._chunks.append("\n")
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._current_href:
            self._chunks.append(f" ({self._current_href})")
            self._current_href = None
        elif tag in ("tr", *self._BLOCK_TAGS):
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data.replace('\xa0', ' '))  # non-breaking space → regular space

    def get_text(self):
        text = "".join(self._chunks)
        text = re.sub(r'[ \t]+', ' ', text)        # collapse runs of spaces/tabs to one space
        text = re.sub(r' *\n *', '\n', text)        # strip spaces that flank newlines
        text = re.sub(r'\n- *\n', '\n', text)       # remove empty bullet points (bare "- " lines)
        text = re.sub(r'\n{3,}', '\n\n', text)      # max two consecutive newlines
        return text.strip()


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
            title = _html_stdlib.unescape(item["title"]["rendered"])
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
                    title = _html_stdlib.unescape(item["title"]["rendered"])
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

def _log_available_eventbrite_orgs(private_token: str):
    """On org-not-found error, fetch and log the org IDs this token can actually access."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://www.eventbriteapi.com/v3/users/me/organizations/",
            headers={"Authorization": f"Bearer {private_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        orgs = data.get("organizations", [])
        if orgs:
            for org in orgs:
                log.info("  Available Eventbrite org: id=%s  name=%s", org.get("id"), org.get("name"))
            log.info("Set EVENTBRITE_ORG_ID to one of the IDs above via /beebot-config set")
        else:
            log.warning("No Eventbrite organizations found for this token")
    except Exception as e:
        log.warning("Could not fetch Eventbrite org list: %s", e)


def fetch_eventbrite_events(
    private_token: str, org_id: str, lookahead_days: int = EVENTBRITE_LOOKAHEAD_DAYS
) -> list[dict] | None:
    """
    Fetch upcoming published events from Eventbrite for the given org within lookahead_days.
    Recurring events (same title) are collapsed into a single entry listing upcoming dates.
    Returns list of {name, content} dicts, or None if the API call failed
    (distinguishes API error from legitimately empty event list).
    """
    import urllib.request, urllib.parse, urllib.error
    from collections import defaultdict
    from datetime import datetime as dt, timedelta, timezone

    now = dt.now(timezone.utc)
    cutoff = now + timedelta(days=lookahead_days)
    base = "https://www.eventbriteapi.com/v3"
    _raw_events = []
    had_error = False
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
            if e.code == 404:
                log.error("Eventbrite API error 404: organization '%s' not found — check EVENTBRITE_ORG_ID", org_id)
                _log_available_eventbrite_orgs(private_token)
            elif e.code == 401:
                log.error("Eventbrite API error 401: invalid or expired token — check EVENTBRITE_PRIVATE_TOKEN")
            else:
                log.error("Eventbrite API error %d: %s", e.code, e.reason)
            had_error = True
            break
        except Exception as e:
            log.error("Eventbrite fetch error: %s", e)
            had_error = True
            break

        past_cutoff = False
        for event in data.get("events", []):
            title = event["name"]["text"]
            summary = event.get("summary", "")
            event_url = event.get("url", "")

            start_local = event.get("start", {}).get("local", "")
            try:
                start_dt = dt.fromisoformat(start_local)
                # Skip events beyond the lookahead window; events are ordered
                # start_asc so we can stop paginating once we pass the cutoff.
                if start_dt.replace(tzinfo=timezone.utc) > cutoff:
                    past_cutoff = True
                    break
                date_str = start_dt.strftime("%A, %B %-d %Y at %-I:%M %p")
            except Exception:
                date_str = start_local

            venue = event.get("venue") or {}
            addr = venue.get("address") or {}
            venue_parts = [venue.get("name", ""), addr.get("address_1", ""),
                           addr.get("city", ""), addr.get("region", "")]
            venue_str = ", ".join(p for p in venue_parts if p) or "TBD"

            _raw_events.append({
                "title": title,
                "date_str": date_str,
                "venue_str": venue_str,
                "summary": summary,
                "event_url": event_url,
            })

        if past_cutoff:
            break

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

    if had_error:
        return None

    # Group by title — recurring events (same name) become one entry
    groups: dict[str, list] = defaultdict(list)
    for ev in _raw_events:
        groups[ev["title"]].append(ev)

    results = []
    for title, occurrences in groups.items():
        ev0 = occurrences[0]
        if len(occurrences) == 1:
            lines = [f"Title: {title}", f"Date: {ev0['date_str']}", f"Location: {ev0['venue_str']}"]
            if ev0["summary"]:
                lines.append(f"Description: {ev0['summary']}")
            if ev0["event_url"]:
                lines.append(f"URL: {ev0['event_url']}")
            log.info("  ✓ Eventbrite: %s (%s)", title, ev0["date_str"])
        else:
            # List every date within the lookahead window so the bot can answer
            # "when is the next open house after X?" for any date in the window.
            date_lines = [f"  - {ev['date_str']}" for ev in occurrences]
            lines = (
                [f"Title: {title}", f"Recurring event — {len(occurrences)} upcoming occurrences:"]
                + date_lines
                + [f"Location: {ev0['venue_str']}"]
            )
            if ev0["summary"]:
                lines.append(f"Description: {ev0['summary']}")
            if ev0["event_url"]:
                lines.append(f"URL: {ev0['event_url']}")
            log.info("  ✓ Eventbrite: %s (%d occurrences, next: %s)", title, len(occurrences), ev0["date_str"])
        results.append({"name": f"eventbrite/{title}", "content": "\n".join(lines)})

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
    """Save current sync manifest (atomic write — safe across permission boundaries)."""
    manifest = {d["id"]: {"name": d["name"], "modifiedTime": d["modifiedTime"]} for d in docs}
    path = Path(MANIFEST_PATH)
    tmp_path = path.parent / (path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as e:
        log.error("Failed to save manifest: %s", e)


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

    # Separate config docs (filename starts with '_') from knowledge base docs.
    # Check the basename so docs in subfolders like tools/_ignored also work.
    config_docs = [d for d in all_docs if d["name"].split("/")[-1].startswith("_")]
    kb_docs     = [d for d in all_docs if not d["name"].split("/")[-1].startswith("_")]

    # Process config docs first
    system_prompt_written = False
    for doc in config_docs:
        log.info("Config doc: %s", doc["name"])
        text = export_doc_as_text(service, doc["id"], doc["name"])
        if text:
            basename = doc["name"].split("/")[-1].lower()
            if basename == "_beebot-prompt":
                system_prompt_written = True
            handle_config_doc(doc["name"], text.strip())

    if not system_prompt_written:
        log.warning(
            "No '_beebot-prompt' doc found in Drive — bot will use built-in default system prompt. "
            "Create a Google Doc named '_beebot-prompt' in the Drive folder to customize it."
        )

    # Diff against previous run (KB docs only)
    old_manifest = load_manifest()
    diff = diff_manifest(old_manifest, kb_docs)
    first_run = not old_manifest

    sections = []
    failed = []
    synced = []
    external_errors = []  # tracks Eventbrite/WP failures for final summary

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
        if eb_events is None:
            external_errors.append("Eventbrite")
        elif not eb_events:
            log.info("Eventbrite: no upcoming events found")
        else:
            for event in eb_events:
                sections.append(
                    f"=== DOCUMENT: {event['name']} ===\n\n"
                    f"{event['content']}\n\n"
                    f"=== END: {event['name']} ==="
                )
                synced.append(event["name"])
    else:
        log.info("Eventbrite sync skipped — EVENTBRITE_PRIVATE_TOKEN or EVENTBRITE_ORG_ID not set")

    # ── Write knowledge base (atomic) ─────────────────────────────────────────
    output_path = Path(OUTPUT_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure the data directory is writable by non-root users (bot runs as appuser).
    # This succeeds when sync runs as root; silently ignored otherwise.
    try:
        os.chmod(output_path.parent, 0o777)
    except PermissionError:
        pass

    header = (
        f"# BeeBot Knowledge Base\n"
        f"# Generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"# Documents: {len(synced)}\n"
        f"# Source folder: {DRIVE_FOLDER_ID}\n\n"
    )

    tmp_path = output_path.parent / (output_path.name + ".tmp")
    try:
        tmp_path.write_text(header + "\n\n".join(sections), encoding="utf-8")
        os.replace(tmp_path, output_path)
    except PermissionError as e:
        log.error("Permission denied writing knowledge base — run sync via 'docker compose run --rm beebot-sync': %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("Failed to write knowledge base: %s", e)
        sys.exit(1)
    save_manifest(kb_docs)

    kb_size = output_path.stat().st_size
    log.info("Knowledge base written: %s (%d bytes, %d docs)", OUTPUT_PATH, kb_size, len(synced))

    # Warn when KB size approaches thresholds where full-context injection degrades quality.
    # ~80KB / ~20K tokens: flat-file approach still works but consider tiered loading.
    # ~200KB / ~50K tokens: quality impact likely; RAG or tiered KB strongly recommended.
    _KB_WARN_BYTES  = 80_000
    _KB_URGENT_BYTES = 200_000
    if kb_size >= _KB_URGENT_BYTES:
        log.warning(
            "KB SIZE CRITICAL: %d bytes (~%d tokens). Full-context injection will degrade answer "
            "quality at this size. Consider RAG (vector embeddings + semantic retrieval) or a "
            "tiered KB that loads only relevant sections per query. See architecture notes.",
            kb_size, kb_size // 4,
        )
    elif kb_size >= _KB_WARN_BYTES:
        log.warning(
            "KB SIZE WARNING: %d bytes (~%d tokens). Consider a tiered KB that keeps core docs "
            "(membership, hours, events) always loaded and injects tool manuals only when relevant "
            "keywords are detected in the query. Full-context injection still works but may miss "
            "details in large tool manual sets.",
            kb_size, kb_size // 4,
        )

    # Report changes
    if first_run:
        log.info("First sync — all docs loaded:")
        for name in synced:
            log.info("  ✓ %s", name)
    else:
        if not any([diff["added"], diff["changed"], diff["removed"]]):
            if external_errors:
                log.warning("Google Drive: no changes. External sources with errors: %s",
                            ", ".join(external_errors))
            else:
                log.info("No changes since last sync.")
        else:
            for name in diff["added"]:
                log.info("  ➕ NEW: %s", name)
            for name in diff["changed"]:
                log.info("  ✏️  UPDATED: %s", name)
            for name in diff["removed"]:
                log.info("  🗑️  REMOVED: %s", name)

    if external_errors:
        log.warning("Sync completed with errors from: %s — check logs above for details",
                    ", ".join(external_errors))

    for name in failed:
        log.warning("  ✗ FAILED: %s", name)

    if failed:
        log.warning("%d docs failed to export", len(failed))
        # Don't exit non-zero — partial KB is still useful


if __name__ == "__main__":
    run_sync()
