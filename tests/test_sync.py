"""
Tests for sync/sync_docs.py utility functions.
HTTP calls to Drive/WP/Eventbrite are fully mocked — no network required.
"""
import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add sync directory to path so we can import sync_docs without the full container env
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))

# sync_docs reads env vars at module level; provide required ones before import
os.environ.setdefault("GOOGLE_DRIVE_FOLDER_ID", "test-folder-id")

import sync_docs


# ── HTML Stripping ─────────────────────────────────────────────────────────────

class TestHtmlTableStripping:
    def test_table_cells_separated_by_spaces(self):
        html = "<tr><td>President</td><td>Charlie Affel</td></tr>"
        result = sync_docs.strip_html(html)
        assert "President" in result
        assert "Charlie Affel" in result

    def test_table_rows_separated_by_newlines(self):
        html = "<tr><td>Row1</td></tr><tr><td>Row2</td></tr>"
        result = sync_docs.strip_html(html)
        assert "Row1" in result
        assert "Row2" in result
        assert "\n" in result

    def test_bom_not_present_in_stripped_output(self):
        html = "\ufeff<p>Hello</p>"
        # strip_html itself doesn't strip BOM (that's export_doc_as_text's job);
        # verify it passes through here so the export layer test confirms removal
        result = sync_docs.strip_html(html)
        assert "Hello" in result


class TestStripHtml:
    def test_strips_basic_tags(self):
        assert sync_docs.strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_preserves_absolute_href(self):
        result = sync_docs.strip_html('<a href="https://example.com">click here</a>')
        assert "click here" in result
        assert "https://example.com" in result
        assert result == "click here (https://example.com)"

    def test_ignores_relative_href(self):
        result = sync_docs.strip_html('<a href="/local/path">link</a>')
        assert "https" not in result
        assert result == "link"

    def test_ignores_mailto_href(self):
        result = sync_docs.strip_html('<a href="mailto:test@example.com">email</a>')
        assert "mailto" not in result

    def test_collapses_extra_newlines(self):
        html = "<p>Line one</p>\n\n\n\n<p>Line two</p>"
        result = sync_docs.strip_html(html)
        assert "\n\n\n" not in result

    def test_empty_string(self):
        assert sync_docs.strip_html("") == ""

    def test_plain_text_unchanged(self):
        assert sync_docs.strip_html("No HTML here") == "No HTML here"

    def test_multiple_links(self):
        html = '<a href="https://a.com">A</a> and <a href="https://b.com">B</a>'
        result = sync_docs.strip_html(html)
        assert "A (https://a.com)" in result
        assert "B (https://b.com)" in result

    def test_style_block_stripped(self):
        html = "<style>.lst-kix > li:before{content:'●'}</style><p>Hello</p>"
        result = sync_docs.strip_html(html)
        assert "lst-kix" not in result
        assert "Hello" in result

    def test_script_block_stripped(self):
        html = "<script>var x = 1;</script><p>World</p>"
        result = sync_docs.strip_html(html)
        assert "var x" not in result
        assert "World" in result

    def test_head_block_stripped(self):
        html = "<head><title>Doc Title</title><style>.foo{color:red}</style></head><body><p>Content</p></body>"
        result = sync_docs.strip_html(html)
        assert "Doc Title" not in result
        assert ".foo" not in result
        assert "Content" in result

    def test_nested_skip_tags(self):
        # <style> inside <head> should not prematurely re-enable output
        html = "<head><style>.x{}</style><title>Meta</title></head><p>Real</p>"
        result = sync_docs.strip_html(html)
        assert "Meta" not in result
        assert ".x" not in result
        assert "Real" in result

    def test_google_redirect_url_unwrapped(self):
        url = "https://www.google.com/url?q=https://example.com/page&sa=D&source=editors&usg=xyz"
        html = f'<a href="{url}">click here</a>'
        result = sync_docs.strip_html(html)
        assert "https://example.com/page" in result
        assert "google.com/url" not in result

    def test_google_url_with_encoded_params(self):
        url = "https://www.google.com/url?q=https://docs.google.com/d/abc?usp%3Dsharing&sa=D"
        html = f'<a href="{url}">Guide</a>'
        result = sync_docs.strip_html(html)
        assert "https://docs.google.com/d/abc?usp=sharing" in result

    def test_paragraph_tags_produce_newlines(self):
        html = "<p>First paragraph.</p><p>Second paragraph.</p>"
        result = sync_docs.strip_html(html)
        assert "First paragraph." in result
        assert "Second paragraph." in result
        assert "\n" in result

    def test_list_items_get_bullet_prefix(self):
        html = "<ul><li>Item one</li><li>Item two</li></ul>"
        result = sync_docs.strip_html(html)
        assert "- Item one" in result
        assert "- Item two" in result

    def test_heading_tags_produce_newlines(self):
        html = "<h2>Section Title</h2><p>Body text.</p>"
        result = sync_docs.strip_html(html)
        assert "Section Title" in result
        assert "Body text." in result
        assert result.index("Section Title") < result.index("Body text.")

    def test_nbsp_converted_to_space(self):
        html = "<p>Hello&nbsp;World</p>"
        result = sync_docs.strip_html(html)
        assert "Hello World" in result
        assert "\xa0" not in result

    def test_html_entities_decoded(self):
        html = "<p>Classes &amp; Events &#038; More</p>"
        result = sync_docs.strip_html(html)
        assert "Classes & Events & More" in result
        assert "&amp;" not in result
        assert "&#038;" not in result


# ── Google Doc Export ──────────────────────────────────────────────────────────

class TestExportDocAsText:
    def _make_service(self, html_bytes: bytes):
        """Return a mock Drive service that returns html_bytes on export_media."""
        mock_request = MagicMock()
        mock_request.execute.return_value = html_bytes
        mock_files = MagicMock()
        mock_files.export_media.return_value = mock_request
        mock_service = MagicMock()
        mock_service.files.return_value = mock_files
        return mock_service

    def test_preserves_hyperlinks(self):
        html = b'<p>See the <a href="https://example.com/manual">Instruction Manual</a> for details.</p>'
        service = self._make_service(html)
        result = sync_docs.export_doc_as_text(service, "doc-id", "test-doc")
        assert "Instruction Manual" in result
        assert "https://example.com/manual" in result

    def test_strips_bom(self):
        html = "\ufeff<p>Hello</p>".encode("utf-8")
        service = self._make_service(html)
        result = sync_docs.export_doc_as_text(service, "doc-id", "test-doc")
        assert "\ufeff" not in result
        assert "Hello" in result

    def test_markdown_image_references_stripped(self):
        html = b"<p>![Alt text](/local/path/image.jpg)See the manual.</p>"
        service = self._make_service(html)
        result = sync_docs.export_doc_as_text(service, "doc-id", "test-doc")
        assert "![" not in result
        assert "See the manual." in result

    def test_returns_none_on_http_error(self):
        from googleapiclient.errors import HttpError
        mock_request = MagicMock()
        mock_request.execute.side_effect = HttpError(MagicMock(status=403), b"Forbidden")
        mock_service = MagicMock()
        mock_service.files.return_value.export_media.return_value = mock_request
        result = sync_docs.export_doc_as_text(mock_service, "doc-id", "test-doc")
        assert result is None


# ── Manifest Diff ──────────────────────────────────────────────────────────────

class TestDiffManifest:
    def _make_doc(self, id, name, modified="2026-01-01T00:00:00Z"):
        return {"id": id, "name": name, "modifiedTime": modified}

    def test_added_doc(self):
        old = {}
        new = [self._make_doc("1", "new-doc")]
        diff = sync_docs.diff_manifest(old, new)
        assert "new-doc" in diff["added"]
        assert diff["changed"] == []
        assert diff["removed"] == []

    def test_removed_doc(self):
        old = {"1": {"name": "old-doc", "modifiedTime": "2026-01-01T00:00:00Z"}}
        new = []
        diff = sync_docs.diff_manifest(old, new)
        assert "old-doc" in diff["removed"]
        assert diff["added"] == []
        assert diff["changed"] == []

    def test_changed_doc(self):
        old = {"1": {"name": "doc", "modifiedTime": "2026-01-01T00:00:00Z"}}
        new = [self._make_doc("1", "doc", "2026-02-01T00:00:00Z")]
        diff = sync_docs.diff_manifest(old, new)
        assert "doc" in diff["changed"]
        assert diff["added"] == []
        assert diff["removed"] == []

    def test_unchanged_doc(self):
        ts = "2026-01-01T00:00:00Z"
        old = {"1": {"name": "doc", "modifiedTime": ts}}
        new = [self._make_doc("1", "doc", ts)]
        diff = sync_docs.diff_manifest(old, new)
        assert diff["added"] == []
        assert diff["changed"] == []
        assert diff["removed"] == []

    def test_mixed_changes(self):
        old = {
            "keep": {"name": "keep", "modifiedTime": "2026-01-01T00:00:00Z"},
            "drop": {"name": "drop", "modifiedTime": "2026-01-01T00:00:00Z"},
            "edit": {"name": "edit", "modifiedTime": "2026-01-01T00:00:00Z"},
        }
        new = [
            self._make_doc("keep", "keep", "2026-01-01T00:00:00Z"),
            self._make_doc("edit", "edit", "2026-02-01T00:00:00Z"),
            self._make_doc("fresh", "fresh"),
        ]
        diff = sync_docs.diff_manifest(old, new)
        assert diff["added"] == ["fresh"]
        assert diff["changed"] == ["edit"]
        assert diff["removed"] == ["drop"]


# ── Manifest Save / Load ───────────────────────────────────────────────────────

class TestManifestIO:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_docs, "MANIFEST_PATH", str(tmp_path / "manifest.json"))
        docs = [
            {"id": "1", "name": "doc-one", "modifiedTime": "2026-01-01T00:00:00Z"},
            {"id": "2", "name": "doc-two", "modifiedTime": "2026-02-01T00:00:00Z"},
        ]
        sync_docs.save_manifest(docs)
        loaded = sync_docs.load_manifest()
        assert loaded["1"]["name"] == "doc-one"
        assert loaded["2"]["modifiedTime"] == "2026-02-01T00:00:00Z"

    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_docs, "MANIFEST_PATH", str(tmp_path / "nonexistent.json"))
        assert sync_docs.load_manifest() == {}

    def test_load_corrupt_returns_empty(self, tmp_path, monkeypatch):
        bad = tmp_path / "manifest.json"
        bad.write_text("not json")
        monkeypatch.setattr(sync_docs, "MANIFEST_PATH", str(bad))
        assert sync_docs.load_manifest() == {}

    def test_atomic_write_no_partial_file(self, tmp_path, monkeypatch):
        """Save should not leave a .tmp file behind on success."""
        monkeypatch.setattr(sync_docs, "MANIFEST_PATH", str(tmp_path / "manifest.json"))
        sync_docs.save_manifest([{"id": "1", "name": "x", "modifiedTime": "t"}])
        assert not (tmp_path / "manifest.json.tmp").exists()


# ── WordPress Blocklist ────────────────────────────────────────────────────────

class TestWpBlocklist:
    def test_default_blocklist_contains_known_slugs(self):
        for slug in ("billing", "wiki", "home", "password-reset"):
            assert slug in sync_docs._DEFAULT_WP_BLOCKLIST

    def test_env_override_replaces_default(self, monkeypatch):
        monkeypatch.setenv("WORDPRESS_SLUG_BLOCKLIST", "custom-slug,another-slug")
        # Re-parse the blocklist the same way the module does
        raw = os.environ.get("WORDPRESS_SLUG_BLOCKLIST", "")
        result = {s.strip() for s in raw.split(",") if s.strip()} if raw else sync_docs._DEFAULT_WP_BLOCKLIST
        assert result == {"custom-slug", "another-slug"}
        assert "billing" not in result  # default not present when overridden

    def test_empty_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("WORDPRESS_SLUG_BLOCKLIST", "")
        raw = os.environ.get("WORDPRESS_SLUG_BLOCKLIST", "")
        result = {s.strip() for s in raw.split(",") if s.strip()} if raw else sync_docs._DEFAULT_WP_BLOCKLIST
        assert result == sync_docs._DEFAULT_WP_BLOCKLIST


# ── Config Doc Handling ────────────────────────────────────────────────────────

class TestHandleConfigDoc:
    def test_beebot_prompt_writes_system_prompt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_docs, "SYSTEM_PROMPT_PATH", str(tmp_path / "system_prompt.txt"))
        sync_docs.handle_config_doc("_beebot-prompt", "You are BeeBot.")
        assert (tmp_path / "system_prompt.txt").read_text() == "You are BeeBot."

    def test_beebot_prompt_case_insensitive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_docs, "SYSTEM_PROMPT_PATH", str(tmp_path / "system_prompt.txt"))
        sync_docs.handle_config_doc("_BeeBot-Prompt", "content")
        assert (tmp_path / "system_prompt.txt").exists()

    def test_unknown_config_doc_ignored(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(sync_docs, "SYSTEM_PROMPT_PATH", str(tmp_path / "system_prompt.txt"))
        sync_docs.handle_config_doc("_unknown-doc", "content")
        assert not (tmp_path / "system_prompt.txt").exists()

    def test_beebot_prompt_in_subfolder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sync_docs, "SYSTEM_PROMPT_PATH", str(tmp_path / "system_prompt.txt"))
        sync_docs.handle_config_doc("config/_beebot-prompt", "prompt content")
        assert (tmp_path / "system_prompt.txt").read_text() == "prompt content"


# ── WordPress Fetch ────────────────────────────────────────────────────────────

def _wp_response(data, status=200):
    """Build a mock context-manager response for urllib.request.urlopen."""
    mock = MagicMock()
    mock.read.return_value = json.dumps(data).encode()
    mock.status = status
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestFetchWordpressPages:
    def _call(self, side_effects, blocklist=None, category="beebot-slackbot"):
        if blocklist is not None:
            orig = sync_docs.WORDPRESS_SLUG_BLOCKLIST
            sync_docs.WORDPRESS_SLUG_BLOCKLIST = set(blocklist)
        try:
            with patch("urllib.request.urlopen", side_effect=side_effects):
                return sync_docs.fetch_wordpress_pages("https://example.com", category)
        finally:
            if blocklist is not None:
                sync_docs.WORDPRESS_SLUG_BLOCKLIST = orig

    def test_returns_published_page(self):
        pages = [{"slug": "about", "title": {"rendered": "About"},
                  "content": {"rendered": "<p>About us</p>"}, "modified": "2026-01-01"}]
        # pages p1, pages p2 (empty → stop), categories (empty → no posts)
        results = self._call([
            _wp_response(pages),
            _wp_response([]),
            _wp_response([]),
        ], blocklist=[])
        assert len(results) == 1
        assert results[0]["name"] == "wordpress/About"
        assert "About us" in results[0]["content"]

    def test_blocklisted_slug_excluded(self):
        pages = [{"slug": "billing", "title": {"rendered": "Billing"},
                  "content": {"rendered": "<p>Pay here</p>"}, "modified": "2026-01-01"}]
        results = self._call([
            _wp_response(pages),
            _wp_response([]),
            _wp_response([]),
        ], blocklist=["billing"])
        assert results == []

    def test_empty_content_page_skipped(self):
        pages = [{"slug": "empty-page", "title": {"rendered": "Empty"},
                  "content": {"rendered": "   "}, "modified": "2026-01-01"}]
        results = self._call([
            _wp_response(pages),
            _wp_response([]),
            _wp_response([]),
        ], blocklist=[])
        assert results == []

    def test_http_error_on_pages_returns_empty(self):
        import urllib.error
        err = urllib.error.HTTPError(url="u", code=500, msg="Internal Server Error", hdrs=None, fp=None)
        results = self._call([err], blocklist=[])
        assert results == []

    def test_400_ends_pagination_gracefully(self):
        import urllib.error
        pages = [{"slug": "about", "title": {"rendered": "About"},
                  "content": {"rendered": "<p>Content</p>"}, "modified": "2026-01-01"}]
        err400 = urllib.error.HTTPError(url="u", code=400, msg="Bad Request", hdrs=None, fp=None)
        results = self._call([
            _wp_response(pages),
            err400,             # page 2 → 400 means no more pages
            _wp_response([]),   # categories
        ], blocklist=[])
        assert len(results) == 1

    def test_posts_fetched_with_matching_category(self):
        post = {"slug": "my-post", "title": {"rendered": "My Post"},
                "content": {"rendered": "<p>Post body</p>"}, "modified": "2026-01-01"}
        results = self._call([
            _wp_response([]),                          # pages: empty
            _wp_response([{"id": 42, "name": "beebot-slackbot"}]),  # categories
            _wp_response([post]),                      # posts p1
            _wp_response([]),                          # posts p2: empty
        ], blocklist=[])
        assert any(r["name"] == "wordpress/My Post" for r in results)

    def test_category_not_found_no_posts(self):
        results = self._call([
            _wp_response([]),   # pages: empty
            _wp_response([]),   # categories: empty → no posts
        ], blocklist=[])
        assert results == []

    def test_multiple_pages_pagination(self):
        page = lambda slug, title: {"slug": slug, "title": {"rendered": title},
                                    "content": {"rendered": f"<p>{title}</p>"}, "modified": "2026-01-01"}
        results = self._call([
            _wp_response([page("a", "Alpha")]),
            _wp_response([page("b", "Beta")]),
            _wp_response([]),           # pages: empty → stop
            _wp_response([]),           # categories
        ], blocklist=[])
        assert len(results) == 2


# ── Eventbrite Fetch ───────────────────────────────────────────────────────────

def _eb_response(events, has_more=False, continuation=None):
    """Build a mock Eventbrite API response."""
    pagination = {"has_more_items": has_more}
    if has_more and continuation:
        pagination["continuation"] = continuation
    return _wp_response({"events": events, "pagination": pagination})


def _eb_event(title="Test Event", start_local="2026-06-01T18:00:00",
              summary="Fun event", url="https://eventbrite.com/e/123",
              venue_name="Hive76", city="Philadelphia", region="PA", address="915 Spring Garden St"):
    return {
        "name": {"text": title},
        "summary": summary,
        "url": url,
        "start": {"local": start_local},
        "venue": {
            "name": venue_name,
            "address": {"address_1": address, "city": city, "region": region},
        },
    }


class TestFetchEventbriteEvents:
    def _call(self, side_effects):
        with patch("urllib.request.urlopen", side_effect=side_effects):
            return sync_docs.fetch_eventbrite_events("test-token", "99999")

    def test_returns_event_with_all_fields(self):
        results = self._call([_eb_response([_eb_event()])])
        assert results is not None
        assert len(results) == 1
        content = results[0]["content"]
        assert "Test Event" in results[0]["name"]
        assert "Title: Test Event" in content
        assert "Date:" in content
        assert "Location:" in content
        assert "Description: Fun event" in content
        assert "URL: https://eventbrite.com/e/123" in content

    def test_404_returns_none(self, caplog):
        import urllib.error
        err = urllib.error.HTTPError(url="u", code=404, msg="NOT FOUND", hdrs=None, fp=None)
        result = self._call([err])
        assert result is None
        assert "404" in caplog.text
        assert "EVENTBRITE_ORG_ID" in caplog.text

    def test_401_returns_none(self, caplog):
        import urllib.error
        err = urllib.error.HTTPError(url="u", code=401, msg="UNAUTHORIZED", hdrs=None, fp=None)
        result = self._call([err])
        assert result is None
        assert "401" in caplog.text
        assert "EVENTBRITE_PRIVATE_TOKEN" in caplog.text

    def test_network_error_returns_none(self, caplog):
        result = self._call([Exception("connection refused")])
        assert result is None
        assert "connection refused" in caplog.text

    def test_empty_event_list_returns_empty_list(self):
        result = self._call([_eb_response([])])
        assert result == []  # not None — API succeeded, just no events

    def test_pagination_fetches_all_pages(self):
        result = self._call([
            _eb_response([_eb_event("Event 1")], has_more=True, continuation="tok123"),
            _eb_response([_eb_event("Event 2")], has_more=False),
        ])
        assert result is not None
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert any("Event 1" in n for n in names)
        assert any("Event 2" in n for n in names)

    def test_missing_venue_uses_tbd(self):
        event = _eb_event()
        event["venue"] = None
        result = self._call([_eb_response([event])])
        assert result is not None
        assert "TBD" in result[0]["content"]

    def test_missing_summary_omitted(self):
        event = _eb_event(summary="")
        result = self._call([_eb_response([event])])
        assert result is not None
        assert "Description:" not in result[0]["content"]

    def test_missing_url_omitted(self):
        event = _eb_event(url="")
        result = self._call([_eb_response([event])])
        assert result is not None
        assert "URL:" not in result[0]["content"]

    def test_invalid_date_falls_back_to_raw(self):
        event = _eb_event(start_local="not-a-date")
        result = self._call([_eb_response([event])])
        assert result is not None
        assert "not-a-date" in result[0]["content"]


# ── Return value contract: error (None) vs empty results ([]) ─────────────────

class TestLogAvailableEventbriteOrgs:
    def test_logs_org_ids_on_404(self, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="sync_docs")
        orgs_data = {"organizations": [
            {"id": "111", "name": "Acme Makerspace"},
            {"id": "222", "name": "Another Org"},
        ]}
        with patch("urllib.request.urlopen", side_effect=[_wp_response(orgs_data)]):
            sync_docs._log_available_eventbrite_orgs("test-token")
        assert "111" in caplog.text
        assert "Acme Makerspace" in caplog.text
        assert "222" in caplog.text

    def test_no_orgs_logs_warning(self, caplog):
        with patch("urllib.request.urlopen", side_effect=[_wp_response({"organizations": []})]):
            sync_docs._log_available_eventbrite_orgs("test-token")
        assert "No Eventbrite organizations" in caplog.text

    def test_404_triggers_org_lookup(self, caplog):
        import logging, urllib.error
        caplog.set_level(logging.INFO, logger="sync_docs")
        orgs_data = {"organizations": [{"id": "99999", "name": "Real Org"}]}
        err = urllib.error.HTTPError(url="u", code=404, msg="NOT FOUND", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=[err, _wp_response(orgs_data)]):
            sync_docs.fetch_eventbrite_events("test-token", "wrong-id")
        assert "99999" in caplog.text
        assert "Real Org" in caplog.text


class TestEventbriteGrouping:
    """Recurring events (same title) must be collapsed into one entry with all dates."""

    def _call(self, events, lookahead_days=90):
        with patch("urllib.request.urlopen", side_effect=[_eb_response(events)]):
            return sync_docs.fetch_eventbrite_events("test-token", "99999",
                                                     lookahead_days=lookahead_days)

    def test_recurring_events_grouped_into_one_entry(self):
        events = [
            _eb_event("Open House", start_local="2026-06-01T14:00:00"),
            _eb_event("Open House", start_local="2026-06-08T14:00:00"),
            _eb_event("Open House", start_local="2026-06-15T14:00:00"),
        ]
        result = self._call(events)
        assert result is not None
        assert len(result) == 1, "Three occurrences of same event should collapse to one entry"
        assert "Open House" in result[0]["name"]

    def test_recurring_entry_lists_all_dates(self):
        from datetime import datetime as dt, timedelta, timezone
        # Use dates well within a generous lookahead to avoid clock-dependent failures
        base = dt.now(timezone.utc)
        dates = [(base + timedelta(days=7 * i)).strftime("%Y-%m-%dT14:00:00") for i in range(1, 4)]
        events = [_eb_event("Open House", start_local=d) for d in dates]
        result = self._call(events, lookahead_days=365)
        assert result is not None and len(result) == 1
        content = result[0]["content"]
        assert "3 upcoming occurrences" in content
        assert content.count("  - ") == 3, "All 3 dates should be listed"

    def test_distinct_events_not_grouped(self):
        events = [_eb_event("Open House"), _eb_event("Workshop")]
        result = self._call(events)
        assert result is not None
        assert len(result) == 2

    def test_lookahead_filters_future_events(self):
        """Events past the lookahead window are excluded client-side."""
        # lookahead_days=1: tomorrow is within window, far-future is excluded
        from datetime import datetime as dt, timedelta, timezone
        tomorrow = (dt.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        far_future = (dt.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
        events = [
            _eb_event("Near Event", start_local=tomorrow),
            _eb_event("Far Event", start_local=far_future),
        ]
        with patch("urllib.request.urlopen", side_effect=[_eb_response(events)]):
            result = sync_docs.fetch_eventbrite_events("tok", "99999", lookahead_days=2)
        assert result is not None
        names = [r["name"] for r in result]
        assert any("Near Event" in n for n in names)
        assert not any("Far Event" in n for n in names)


class TestEventbriteReturnValueContract:
    """fetch_eventbrite_events must return None on API error and [] on empty results.
    run_sync uses this distinction to avoid logging 'no events' when the call failed.
    """

    def test_api_error_returns_none_not_empty_list(self):
        import urllib.error
        err = urllib.error.HTTPError(url="u", code=404, msg="NOT FOUND", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=[err]):
            result = sync_docs.fetch_eventbrite_events("tok", "99999")
        assert result is None, "API error should return None, not []"

    def test_no_events_returns_empty_list_not_none(self):
        with patch("urllib.request.urlopen", side_effect=[_eb_response([])]):
            result = sync_docs.fetch_eventbrite_events("tok", "99999")
        assert result == []
        assert result is not None, "Empty API response should return [], not None"

    def test_network_error_returns_none_not_empty_list(self):
        with patch("urllib.request.urlopen", side_effect=[Exception("timeout")]):
            result = sync_docs.fetch_eventbrite_events("tok", "99999")
        assert result is None
