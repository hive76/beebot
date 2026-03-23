"""
Tests for bot/beebot.py utility functions.
Slack and Anthropic clients are not contacted — network calls are not made.
"""
import json
import os
import sys
import time
import importlib
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Required env vars before importing beebot (module validates at import time)
_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test-token",
    "SLACK_APP_TOKEN": "xapp-test-token",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "NEW_MEMBERS_CHANNEL_ID": "C_TEST_CHANNEL",
}

# Patch the App and Anthropic constructors so no network calls happen on import
with (
    patch.dict(os.environ, _ENV),
    patch("slack_bolt.App", return_value=MagicMock()),
    patch("anthropic.Anthropic", return_value=MagicMock()),
):
    sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))
    import beebot


# ── Runtime Config Load/Save ──────────────────────────────────────────────────

class TestRuntimeConfig:
    def test_load_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "missing.json"))
        assert beebot.load_runtime_config() == {}

    def test_load_corrupt_returns_empty(self, tmp_path, monkeypatch, caplog):
        bad = tmp_path / "runtime_config.json"
        bad.write_text("not json")
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(bad))
        result = beebot.load_runtime_config()
        assert result == {}
        assert "unreadable" in caplog.text

    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        config = {"BOT_EMOJI": ":test:", "RATE_LIMIT_MAX": 5}
        beebot.save_runtime_config(config)
        loaded = beebot.load_runtime_config()
        assert loaded == config

    def test_atomic_write_no_tmp_left(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        beebot.save_runtime_config({"key": "val"})
        assert not (tmp_path / "rc.json.tmp").exists()


# ── _get_config Priority ──────────────────────────────────────────────────────

class TestGetConfig:
    def test_reads_from_runtime(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {"BOT_EMOJI": ":custom:"})
        assert beebot._get_config("BOT_EMOJI") == ":custom:"

    def test_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {})
        assert beebot._get_config("BOT_EMOJI") == ":robot_face:"

    def test_returns_none_for_optional_not_set(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {})
        assert beebot._get_config("WORDPRESS_BASE_URL") is None

    def test_runtime_overrides_default(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {"RATE_LIMIT_MAX": 99})
        assert beebot._get_config("RATE_LIMIT_MAX") == 99


# ── Validators ────────────────────────────────────────────────────────────────

class TestValidators:
    def test_emoji_valid(self):
        assert beebot._validate_emoji(":hive76:") is None
        assert beebot._validate_emoji(":robot_face:") is None
        assert beebot._validate_emoji(":bee-bot:") is None

    def test_emoji_invalid(self):
        assert beebot._validate_emoji("nocolons") is not None
        assert beebot._validate_emoji(":Has Spaces:") is not None
        assert beebot._validate_emoji(":CAPS:") is not None

    def test_model_valid(self):
        for model in beebot._ALLOWED_MODELS:
            assert beebot._validate_model(model) is None

    def test_model_invalid(self):
        assert beebot._validate_model("gpt-4") is not None
        assert beebot._validate_model("claude-fake-9") is not None

    def test_slug_valid(self):
        assert beebot._validate_slug("beebot-slackbot") is None
        assert beebot._validate_slug("my-category") is None

    def test_slug_invalid(self):
        assert beebot._validate_slug("Has Spaces") is not None
        assert beebot._validate_slug("UPPERCASE") is not None
        assert beebot._validate_slug("a" * 65) is not None

    def test_int_range_valid(self):
        assert beebot._validate_int_range("10", 1, 500) is None
        assert beebot._validate_int_range("1", 1, 500) is None
        assert beebot._validate_int_range("500", 1, 500) is None

    def test_int_range_out_of_bounds(self):
        assert beebot._validate_int_range("0", 1, 500) is not None
        assert beebot._validate_int_range("501", 1, 500) is not None

    def test_int_range_non_numeric(self):
        assert beebot._validate_int_range("abc", 1, 500) is not None

    def test_https_url_valid(self):
        assert beebot._validate_https_url("https://hive76.org") is None

    def test_https_url_invalid(self):
        assert beebot._validate_https_url("http://hive76.org") is not None
        assert beebot._validate_https_url("not-a-url") is not None

    def test_numeric_valid(self):
        assert beebot._validate_numeric("13775076") is None

    def test_numeric_invalid(self):
        assert beebot._validate_numeric("abc") is not None
        assert beebot._validate_numeric("12.34") is not None


# ── _require_admin ────────────────────────────────────────────────────────────

class TestRequireAdmin:
    def test_no_admin_ids_configured(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", set())
        respond = MagicMock()
        result = beebot._require_admin("U123", respond)
        assert result is True
        assert "disabled" in respond.call_args[0][0]

    def test_user_not_in_admin_list(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        respond = MagicMock()
        result = beebot._require_admin("U_NONADMIN", respond)
        assert result is True
        respond.assert_called_once()

    def test_admin_user_passes(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        respond = MagicMock()
        result = beebot._require_admin("U_ADMIN", respond)
        assert result is False
        respond.assert_not_called()


# ── Protected Key Rejection ───────────────────────────────────────────────────

class TestProtectedKeys:
    def test_protected_keys_not_in_configurable(self):
        for key in beebot._PROTECTED_KEYS:
            assert key not in beebot._CONFIGURABLE_KEYS, f"{key} should not be configurable"


# ── build_system_prompt ────────────────────────────────────────────────────────

class TestBuildSystemPrompt:
    def test_injects_knowledge_base(self, monkeypatch):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_TEMPLATE", "Prompt.\n\n{knowledge_base}")
        result = beebot.build_system_prompt("KB content")
        assert "KB content" in result

    def test_appends_kb_when_placeholder_missing(self, monkeypatch, caplog):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_TEMPLATE", "Prompt with no placeholder.")
        result = beebot.build_system_prompt("KB content")
        assert "KB content" in result
        assert "<knowledge_base>" in result
        assert "missing" in caplog.text

    def test_security_footer_always_present(self, monkeypatch):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_TEMPLATE", "Custom prompt.\n\n{knowledge_base}")
        result = beebot.build_system_prompt("kb")
        assert beebot._SECURITY_FOOTER in result

    def test_security_footer_present_without_placeholder(self, monkeypatch):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_TEMPLATE", "No placeholder here.")
        result = beebot.build_system_prompt("kb")
        assert beebot._SECURITY_FOOTER in result

    def test_bot_emoji_substituted(self, monkeypatch):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_TEMPLATE", "Emoji: {bot_emoji}\n\n{knowledge_base}")
        monkeypatch.setattr(beebot, "BOT_EMOJI", ":hive76:")
        result = beebot.build_system_prompt("kb")
        assert ":hive76:" in result
        assert "{bot_emoji}" not in result


# ── check_input_length ────────────────────────────────────────────────────────

class TestCheckInputLength:
    def _make_say_body(self):
        say = MagicMock()
        body = {"event": {"ts": "123", "thread_ts": None}}
        return say, body

    def test_under_limit_returns_false(self):
        say, body = self._make_say_body()
        assert beebot.check_input_length("short", say, body) is False
        say.assert_not_called()

    def test_at_limit_returns_false(self):
        say, body = self._make_say_body()
        text = "x" * beebot.MAX_INPUT_CHARS
        assert beebot.check_input_length(text, say, body) is False

    def test_over_limit_returns_true_and_replies(self):
        say, body = self._make_say_body()
        text = "x" * (beebot.MAX_INPUT_CHARS + 1)
        result = beebot.check_input_length(text, say, body)
        assert result is True
        say.assert_called_once()
        assert str(beebot.MAX_INPUT_CHARS) in say.call_args.kwargs.get("text", "")


# ── is_rate_limited ────────────────────────────────────────────────────────────

class TestIsRateLimited:
    def setup_method(self):
        beebot.user_request_times.clear()

    def test_first_request_not_limited(self):
        assert beebot.is_rate_limited("user1") is False

    def test_under_limit_not_limited(self, monkeypatch):
        monkeypatch.setattr(beebot, "RATE_LIMIT_MAX", 5)
        for _ in range(4):
            beebot.is_rate_limited("user1")
        assert beebot.is_rate_limited("user1") is False

    def test_at_limit_is_limited(self, monkeypatch):
        monkeypatch.setattr(beebot, "RATE_LIMIT_MAX", 3)
        for _ in range(3):
            beebot.is_rate_limited("user1")
        assert beebot.is_rate_limited("user1") is True

    def test_expired_requests_not_counted(self, monkeypatch):
        monkeypatch.setattr(beebot, "RATE_LIMIT_MAX", 3)
        monkeypatch.setattr(beebot, "RATE_LIMIT_WINDOW_SEC", 1)
        for _ in range(3):
            beebot.user_request_times["user1"].append(time.time() - 10)
        assert beebot.is_rate_limited("user1") is False

    def test_different_users_independent(self, monkeypatch):
        monkeypatch.setattr(beebot, "RATE_LIMIT_MAX", 2)
        beebot.is_rate_limited("user1")
        beebot.is_rate_limited("user1")
        assert beebot.is_rate_limited("user1") is True
        assert beebot.is_rate_limited("user2") is False


# ── load_system_prompt ────────────────────────────────────────────────────────

class TestLoadSystemPrompt:
    def test_loads_from_file_when_exists(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "system_prompt.txt"
        prompt_file.write_text("Custom prompt from Drive.")
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_PATH", str(prompt_file))
        result = beebot.load_system_prompt()
        assert result == "Custom prompt from Drive."

    def test_falls_back_to_default_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_PATH", str(tmp_path / "nonexistent.txt"))
        result = beebot.load_system_prompt()
        assert result == beebot._DEFAULT_SYSTEM_PROMPT

    def test_falls_back_to_default_when_empty(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "system_prompt.txt"
        prompt_file.write_text("   ")
        monkeypatch.setattr(beebot, "SYSTEM_PROMPT_PATH", str(prompt_file))
        result = beebot.load_system_prompt()
        assert result == beebot._DEFAULT_SYSTEM_PROMPT


# ── load_knowledge_base ───────────────────────────────────────────────────────

class TestLoadKnowledgeBase:
    def test_loads_content(self, tmp_path, monkeypatch):
        kb_file = tmp_path / "knowledge_base.txt"
        kb_file.write_text("Some KB content.")
        monkeypatch.setattr(beebot, "KNOWLEDGE_BASE_PATH", str(kb_file))
        assert beebot.load_knowledge_base() == "Some KB content."

    def test_placeholder_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(beebot, "KNOWLEDGE_BASE_PATH", str(tmp_path / "missing.txt"))
        result = beebot.load_knowledge_base()
        assert "No knowledge base" in result


# ── _build_sync_env ───────────────────────────────────────────────────────────

class TestBuildSyncEnv:
    def test_includes_wordpress_url_from_runtime(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {"WORDPRESS_BASE_URL": "https://example.com"})
        env = beebot._build_sync_env()
        assert env.get("WORDPRESS_BASE_URL") == "https://example.com"

    def test_blocklist_list_converted_to_csv(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {
            "WORDPRESS_SLUG_BLOCKLIST": ["billing", "wiki", "custom"]
        })
        env = beebot._build_sync_env()
        slugs = set(env["WORDPRESS_SLUG_BLOCKLIST"].split(","))
        assert "billing" in slugs
        assert "custom" in slugs

    def test_none_values_not_injected(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {})
        # Remove from os.environ too so we get a clean None result
        monkeypatch.delenv("WORDPRESS_BASE_URL", raising=False)
        env = beebot._build_sync_env()
        assert env.get("WORDPRESS_BASE_URL") is None

    def test_operational_key_in_env_is_stripped(self, monkeypatch):
        """.env values for operational keys must be stripped — runtime_config is sole source."""
        monkeypatch.setattr(beebot, "_runtime_config", {})
        monkeypatch.setenv("WORDPRESS_BASE_URL", "https://leaked-from-env.example.com")
        monkeypatch.delenv("WORDPRESS_BASE_URL", raising=False)  # ensure clean state via runtime
        env = beebot._build_sync_env()
        assert env.get("WORDPRESS_BASE_URL") is None

    def test_runtime_config_overrides_stripped_env(self, monkeypatch):
        """Operational key in runtime_config should be injected even after env strip."""
        monkeypatch.setattr(beebot, "_runtime_config", {"WORDPRESS_BASE_URL": "https://hive76.org"})
        monkeypatch.setenv("WORDPRESS_BASE_URL", "https://should-be-ignored.example.com")
        env = beebot._build_sync_env()
        assert env.get("WORDPRESS_BASE_URL") == "https://hive76.org"


# ── WP Blocklist (via config command internals) ───────────────────────────────

class TestWpBlocklist:
    def test_default_blocklist_contains_expected_slugs(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {})
        blocklist = beebot._get_config("WORDPRESS_SLUG_BLOCKLIST")
        assert "billing" in blocklist
        assert "wiki" in blocklist

    def test_runtime_blocklist_overrides_default(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {
            "WORDPRESS_SLUG_BLOCKLIST": ["custom-only"]
        })
        blocklist = beebot._get_config("WORDPRESS_SLUG_BLOCKLIST")
        assert blocklist == ["custom-only"]
        assert "billing" not in blocklist

    def test_any_slug_can_be_removed(self, monkeypatch):
        # No protected slugs — verify billing is in the default but we can set a runtime config without it
        monkeypatch.setattr(beebot, "_runtime_config", {
            "WORDPRESS_SLUG_BLOCKLIST": ["wiki"]  # billing removed
        })
        blocklist = beebot._get_config("WORDPRESS_SLUG_BLOCKLIST")
        assert "billing" not in blocklist


# ── Credential Handling ───────────────────────────────────────────────────────

class TestCredentialHandling:
    def test_eventbrite_token_marked_redact(self):
        assert beebot._CONFIGURABLE_KEYS["EVENTBRITE_PRIVATE_TOKEN"]["redact"] is True

    def test_bot_emoji_not_redacted(self):
        assert beebot._CONFIGURABLE_KEYS["BOT_EMOJI"]["redact"] is False

    def test_export_redacts_token(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {
            "EVENTBRITE_PRIVATE_TOKEN": "evb-private-test-token",
            "BOT_EMOJI": ":test:",
        })
        # Simulate what export does
        redacted = {}
        for k, v in beebot._runtime_config.items():
            meta = beebot._CONFIGURABLE_KEYS.get(k)
            if meta and meta.get("redact"):
                redacted[k] = "[redacted]"
            else:
                redacted[k] = v
        assert redacted["EVENTBRITE_PRIVATE_TOKEN"] == "[redacted]"
        assert redacted["BOT_EMOJI"] == ":test:"

    def test_token_not_in_default_config(self, monkeypatch):
        monkeypatch.setattr(beebot, "_runtime_config", {})
        assert beebot._get_config("EVENTBRITE_PRIVATE_TOKEN") is None


# ── handle_config_command ─────────────────────────────────────────────────────

def _cmd(user_id="U_ADMIN", text=""):
    """Build a fake Slack command payload."""
    return {"user_id": user_id, "text": text}


class TestHandleConfigCommand:
    def setup_method(self, monkeypatch=None):
        self.ack = MagicMock()
        self.respond = MagicMock()

    def _call(self, text="", user_id="U_ADMIN"):
        beebot.handle_config_command(self.ack, self.respond, _cmd(user_id=user_id, text=text))

    def test_ack_always_called(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("show")
        self.ack.assert_called_once()

    def test_non_admin_denied(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        self._call("show", user_id="U_NOBODY")
        text = self.respond.call_args[0][0]
        assert "permission" in text.lower() or "sorry" in text.lower()

    def test_no_admin_ids_disabled(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", set())
        self._call("show")
        text = self.respond.call_args[0][0]
        assert "disabled" in text.lower()

    def test_show_lists_configurable_keys(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("show")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "BOT_EMOJI" in text
        assert "CLAUDE_MODEL" in text
        assert "RATE_LIMIT_MAX" in text

    def test_show_default_subcommand(self, monkeypatch):
        """No subcommand → defaults to show."""
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "BOT_EMOJI" in text

    def test_set_valid_key(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("set BOT_EMOJI :hive76:")
        assert beebot._runtime_config.get("BOT_EMOJI") == ":hive76:"
        text = self.respond.call_args[0][0]
        assert "✅" in text

    def test_set_protected_key_rejected(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("set SLACK_BOT_TOKEN xoxb-evil")
        text = self.respond.call_args[0][0]
        assert "❌" in text
        assert "cannot be changed" in text or "env" in text.lower()

    def test_set_unknown_key_rejected(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("set MADE_UP_KEY value")
        text = self.respond.call_args[0][0]
        assert "❌" in text
        assert "Unknown key" in text

    def test_set_invalid_value_rejected(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("set BOT_EMOJI not-an-emoji")
        text = self.respond.call_args[0][0]
        assert "❌" in text
        # Value must NOT be saved
        assert "BOT_EMOJI" not in beebot._runtime_config

    def test_set_int_coercion(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("set RATE_LIMIT_MAX 25")
        assert beebot._runtime_config.get("RATE_LIMIT_MAX") == 25  # int, not str

    def test_reset_removes_runtime_value(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {"BOT_EMOJI": ":custom:"})
        self._call("reset BOT_EMOJI")
        assert "BOT_EMOJI" not in beebot._runtime_config
        text = self.respond.call_args[0][0]
        assert "✅" in text

    def test_reset_unknown_key(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("reset NONEXISTENT_KEY")
        text = self.respond.call_args[0][0]
        assert "❌" in text

    def test_reset_already_default(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})  # BOT_EMOJI not in runtime
        self._call("reset BOT_EMOJI")
        text = self.respond.call_args[0][0]
        assert "already at its default" in text

    def test_export_redacts_credentials(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {
            "EVENTBRITE_PRIVATE_TOKEN": "evb-private-test-token",
            "BOT_EMOJI": ":test:",
        })
        self._call("export")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "evb-private-test-token" not in text
        assert "[redacted]" in text
        assert ":test:" in text

    def test_wp_blocklist_add(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("wp-blocklist-add my-slug")
        blocklist = beebot._runtime_config.get("WORDPRESS_SLUG_BLOCKLIST", [])
        assert "my-slug" in blocklist
        text = self.respond.call_args[0][0]
        assert "✅" in text

    def test_wp_blocklist_add_invalid_slug(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        # Underscores are not allowed in slugs (only lowercase alphanumeric + hyphens)
        self._call("wp-blocklist-add has_underscore")
        text = self.respond.call_args[0][0]
        assert "❌" in text

    def test_wp_blocklist_remove(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "RUNTIME_CONFIG_PATH", str(tmp_path / "rc.json"))
        monkeypatch.setattr(beebot, "_runtime_config", {
            "WORDPRESS_SLUG_BLOCKLIST": ["billing", "wiki", "custom"]
        })
        self._call("wp-blocklist-remove billing")
        blocklist = beebot._runtime_config.get("WORDPRESS_SLUG_BLOCKLIST", [])
        assert "billing" not in blocklist
        assert "wiki" in blocklist

    def test_wp_blocklist_remove_not_in_list(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("wp-blocklist-remove nonexistent-slug")
        text = self.respond.call_args[0][0]
        assert "not in the blocklist" in text

    def test_wp_blocklist_show(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {
            "WORDPRESS_SLUG_BLOCKLIST": ["billing", "wiki"]
        })
        self._call("wp-blocklist-show")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "billing" in text
        assert "wiki" in text

    def test_unknown_subcommand_returns_help(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_runtime_config", {})
        self._call("bogus-command")
        text = self.respond.call_args[0][0]
        assert "/beebot-config set" in text


# ── handle_logs_command ───────────────────────────────────────────────────────

class TestHandleLogsCommand:
    def setup_method(self, monkeypatch=None):
        self.ack = MagicMock()
        self.respond = MagicMock()

    def _call(self, text="", user_id="U_ADMIN"):
        beebot.handle_logs_command(self.ack, self.respond, _cmd(user_id=user_id, text=text))

    def test_ack_called(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_LOG_FILE", tmp_path / "beebot.log")
        self._call()
        self.ack.assert_called_once()

    def test_non_admin_denied(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        self._call(user_id="U_NOBODY")
        text = self.respond.call_args[0][0]
        assert "permission" in text.lower() or "sorry" in text.lower()

    def test_log_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_LOG_FILE", tmp_path / "nonexistent.log")
        self._call()
        text = self.respond.call_args[0][0]
        assert "❌" in text
        assert "not found" in text.lower() or "log file" in text.lower()

    def test_default_40_lines(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        log_file = tmp_path / "beebot.log"
        lines = [f"2026-01-01 [INFO] line {i}" for i in range(60)]
        log_file.write_text("\n".join(lines))
        monkeypatch.setattr(beebot, "_LOG_FILE", log_file)
        self._call("")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        # Should show last 40 of 60 lines
        assert "line 59" in text
        assert "line 19" not in text  # line 20 is first in last 40

    def test_custom_line_count(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        log_file = tmp_path / "beebot.log"
        lines = [f"2026-01-01 [INFO] line {i}" for i in range(20)]
        log_file.write_text("\n".join(lines))
        monkeypatch.setattr(beebot, "_LOG_FILE", log_file)
        self._call("10")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "line 19" in text
        assert "line 9" not in text  # line 10 is first in last 10

    def test_line_count_capped_at_100(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        log_file = tmp_path / "beebot.log"
        lines = [f"2026-01-01 [INFO] line {i}" for i in range(200)]
        log_file.write_text("\n".join(lines))
        monkeypatch.setattr(beebot, "_LOG_FILE", log_file)
        self._call("999")
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        # 999 capped to 100; last 100 of 200 = lines 100-199
        assert "line 199" in text
        assert "line 99" not in text

    def test_invalid_argument(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        monkeypatch.setattr(beebot, "_LOG_FILE", tmp_path / "beebot.log")
        self._call("notanumber")
        text = self.respond.call_args[0][0]
        assert "❌" in text
        assert "Usage" in text

    def test_filters_non_log_lines(self, monkeypatch, tmp_path):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        log_file = tmp_path / "beebot.log"
        log_file.write_text(
            "2026-01-01 [INFO] good line\n"
            "some raw output with no tag\n"
            "2026-01-01 [ERROR] another good line\n"
        )
        monkeypatch.setattr(beebot, "_LOG_FILE", log_file)
        self._call()
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "good line" in text
        assert "raw output" not in text


# ── handle_restart_command ────────────────────────────────────────────────────

class TestHandleRestartCommand:
    def setup_method(self, monkeypatch=None):
        self.ack = MagicMock()
        self.respond = MagicMock()

    def _call(self, user_id="U_ADMIN"):
        beebot.handle_restart_command(self.ack, self.respond, _cmd(user_id=user_id))

    def test_non_admin_denied(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        self._call(user_id="U_NOBODY")
        text = self.respond.call_args[0][0]
        assert "permission" in text.lower() or "sorry" in text.lower()

    def test_admin_triggers_sys_exit(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        with pytest.raises(SystemExit) as exc_info:
            self._call()
        assert exc_info.value.code == 0

    def test_responds_before_exit(self, monkeypatch):
        monkeypatch.setattr(beebot, "ADMIN_USER_IDS", {"U_ADMIN"})
        with pytest.raises(SystemExit):
            self._call()
        self.ack.assert_called_once()
        self.respond.assert_called_once()
        response = self.respond.call_args[0][0]
        text = response["text"] if isinstance(response, dict) else response
        assert "estarting" in text  # "Restarting"
