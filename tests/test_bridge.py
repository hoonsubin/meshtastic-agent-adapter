"""
Tests for Meshtastic Bridge

Covers the API-server bridge path: config/env handling, byte-safe truncation,
OpenAI-compatible reply parsing, and the LoRa reply hop.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.config import (
    AgentConfig,
    BridgeConfig,
    Config,
    MeshtasticConfig,
    SerialConfig,
    expand_env_vars,
    expand_env_vars_in_obj,
)
from bridge.http_client import AgentClient, session_id_for
from bridge.text import truncate_bytes

API_KEY = "test-api-key-not-a-real-secret"


@pytest.fixture
def api_key_env(monkeypatch):
    """Provide the agent API key the way the systemd unit does."""
    monkeypatch.setenv("MESHTASTIC_AGENT_KEY", API_KEY)
    return API_KEY


def make_client(**kwargs) -> AgentClient:
    """AgentClient with test defaults."""
    params = {
        "url": "http://127.0.0.1:8642/v1/chat/completions",
        "api_key": API_KEY,
        "model": "hermes-agent",
        "system_prompt": "Reply briefly. Sender: {from_id}.",
        "max_reply_bytes": 200,
        "max_retries": 1,
    }
    params.update(kwargs)
    return AgentClient(**params)


def mock_response(status=200, json_body=None, text_body=""):
    """Build an AsyncMock that behaves like an aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {})
    resp.text = AsyncMock(return_value=text_body)
    return resp


def async_cm(response):
    """An async context manager yielding `response`, like aiohttp's post() does."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def fake_session(*responses):
    """
    Fake aiohttp.ClientSession.

    `post()` and `get()` are real (synchronous) callables returning async context
    managers, because that is the shape aiohttp actually has: an AsyncMock there
    would return a coroutine and the `async with` would blow up.
    """
    session = MagicMock()
    contexts = [async_cm(r) for r in responses]
    session.post = MagicMock(
        side_effect=contexts if len(contexts) > 1 else contexts * 8
    )
    session.get = MagicMock(return_value=contexts[0])
    session.close = AsyncMock()
    return session


class TestText:
    """Byte-safe truncation shared by every mesh write path."""

    def test_short_text_untouched(self):
        assert truncate_bytes("hello", 200) == "hello"

    def test_exact_fit_untouched(self):
        assert truncate_bytes("x" * 200, 200) == "x" * 200

    def test_truncates_to_limit(self):
        assert len(truncate_bytes("x" * 500, 200).encode("utf-8")) == 200

    def test_multibyte_never_split(self):
        # Each japanese char is 3 bytes, so a 200 byte budget fits 66 chars
        out = truncate_bytes("あ" * 100, 200)
        assert len(out.encode("utf-8")) <= 200
        assert out.startswith("あ" * 60)
        out.encode("utf-8").decode("utf-8")  # must stay decodable

    def test_truncation_is_marked(self):
        out = truncate_bytes("word " * 100, 200)
        assert out.endswith("...")
        assert len(out.encode("utf-8")) <= 200

    def test_truncates_on_word_boundary(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo"
        out = truncate_bytes(text, 40)
        assert out.endswith("...")
        assert len(out.encode("utf-8")) <= 40
        # everything kept must be a full word from the original
        assert text.startswith(out[:-3])

    def test_long_unbroken_token_still_uses_the_budget(self):
        out = truncate_bytes("z" * 500, 200, marker="...")
        assert len(out.encode("utf-8")) == 200
        assert out.startswith("z" * 100)

    def test_no_marker_when_disabled(self):
        out = truncate_bytes("word " * 100, 50, marker="")
        assert not out.endswith("...")
        assert len(out.encode("utf-8")) <= 50

    def test_tiny_limit_leaves_no_room_for_marker(self):
        assert truncate_bytes("hello world", 2) == "he"
        assert truncate_bytes("hello world", 0) == ""

    def test_non_positive_limit(self):
        assert truncate_bytes("hello", 0) == ""
        assert truncate_bytes("hello", -5) == ""

    def test_empty_text(self):
        assert truncate_bytes("", 200) == ""


class TestConfig:
    """Configuration loading, env-based secrets, validation."""

    def test_defaults(self):
        config = Config()
        assert config.serial.port == "/dev/ttyUSB0"
        assert config.serial.baud == 921600
        assert config.meshtastic.hop_limit == 3
        assert config.agent.timeout == 60
        assert config.agent.model == "hermes-agent"
        assert config.agent.api_key_env == "MESHTASTIC_AGENT_KEY"
        assert config.bridge.http_port == 8085

    def test_delivery_and_ignore_defaults(self):
        config = Config()
        assert config.meshtastic.want_ack is True
        assert config.meshtastic.ack_timeout == 8.0
        assert "📍" in config.agent.ignore_patterns

    def test_validation_rejects_non_list_allowlist(self, api_key_env):
        config = Config()
        config.meshtastic.allowed_nodes = "!68916e4c"
        with pytest.raises(ValueError, match="allowed_nodes"):
            config.validate()

    def test_is_allowed_with_empty_list_allows_everyone(self):
        assert Config().meshtastic.is_allowed("!anyoneatall")

    def test_is_allowed_filters_listed_nodes(self):
        config = Config()
        config.meshtastic.allowed_nodes = ["!68916e4c"]
        assert config.meshtastic.is_allowed("!68916E4C")
        assert not config.meshtastic.is_allowed("!deadbeef")

    def test_is_ignored_matches_position_share(self):
        config = Config()
        # The exact text a Meshtastic client sends when sharing a position
        assert config.agent.is_ignored("📍 Hoon Portable Mesh has shared their position")
        assert config.agent.is_ignored("HOON PORTABLE MESH HAS SHARED THEIR POSITION")
        assert not config.agent.is_ignored("Hello from my personal node!")

    def test_is_ignored_with_custom_patterns(self):
        config = Config()
        config.agent.ignore_patterns = ["beacon"]
        assert config.agent.is_ignored("BEACON report")
        assert not config.agent.is_ignored("📍 position")  # default no longer applies

    def test_is_ignored_handles_empty_text(self):
        assert Config().agent.is_ignored("") is False

    def test_no_secret_field_in_agent_config(self):
        # The API key must never live in the config file
        assert not hasattr(AgentConfig(), "secret")

    def test_api_key_read_from_env(self, api_key_env, monkeypatch):
        config = Config()
        assert config.api_key == API_KEY

        monkeypatch.setenv("MESHTASTIC_AGENT_KEY", "rotated-key")
        assert config.api_key == "rotated-key"

    def test_validation_fails_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("MESHTASTIC_AGENT_KEY", raising=False)
        with pytest.raises(ValueError, match="MESHTASTIC_AGENT_KEY"):
            Config().validate()

    def test_validation_passes_with_key(self, api_key_env):
        Config().validate()

    def test_validation_rejects_bad_values(self, api_key_env):
        config = Config()
        config.meshtastic.hop_limit = 0
        with pytest.raises(ValueError, match="hop_limit"):
            config.validate()

        config = Config()
        config.bridge.http_port = 99999
        with pytest.raises(ValueError, match="http_port"):
            config.validate()

        config = Config()
        config.agent.timeout = 0
        with pytest.raises(ValueError, match="timeout"):
            config.validate()

        config = Config()
        config.meshtastic.ack_timeout = -1
        with pytest.raises(ValueError, match="ack_timeout"):
            config.validate()

    def test_from_dict_reads_new_agent_shape(self, api_key_env):
        config = Config.from_dict(
            {
                "serial": {"port": "/dev/ttyUSB0", "baud": 921600},
                "meshtastic": {"hop_limit": 3, "max_message_length": 180},
                "agent": {
                    "url": "http://192.168.8.100:8642/v1/chat/completions",
                    "model": "hermes-agent",
                    "api_key_env": "MESHTASTIC_AGENT_KEY",
                    "timeout": 60,
                    "session_prefix": "meshtastic",
                },
                "bridge": {"host": "0.0.0.0", "http_port": 8085},
            }
        )
        assert config.meshtastic.max_message_length == 180
        assert config.agent.url.endswith("/v1/chat/completions")
        config.validate()

    def test_load_prefers_unit_config_path(self, tmp_path, api_key_env, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "serial:\n  port: /dev/ttyUSB9\n  baud: 921600\n"
            "meshtastic:\n  hop_limit: 3\n  max_message_length: 200\n"
            "agent:\n  url: http://127.0.0.1:8642/v1/chat/completions\n"
            "  model: hermes-agent\n  api_key_env: MESHTASTIC_AGENT_KEY\n"
            "  timeout: 60\n"
            "bridge:\n  host: 0.0.0.0\n  http_port: 8085\n"
        )
        monkeypatch.setenv("MESHTASTIC_BRIDGE_CONFIG", str(cfg))
        config = Config.load()
        assert config.serial.port == "/dev/ttyUSB9"

    def test_dataclasses_are_constructible(self):
        assert SerialConfig().baud == 921600
        assert MeshtasticConfig().max_message_length == 200
        assert BridgeConfig().http_port == 8085


class TestEnvExpansion:
    """
    ${VAR} substitution in config.yaml strings. The pattern is UPPER_SNAKE_CASE
    only, so it does not collide with the {max_bytes} / {from_id} .format()
    placeholders in agent.system_prompt.
    """

    def test_simple_substitution(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TEST_HOST", "192.168.42.1")
        assert expand_env_vars("http://${BRIDGE_TEST_HOST}:8642/v1") == (
            "http://192.168.42.1:8642/v1"
        )

    def test_multiple_vars_in_one_string(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TEST_HOST", "agent.lan")
        monkeypatch.setenv("BRIDGE_TEST_PORT", "9000")
        assert expand_env_vars("http://${BRIDGE_TEST_HOST}:${BRIDGE_TEST_PORT}/x") == (
            "http://agent.lan:9000/x"
        )

    def test_unset_var_stays_literal(self, monkeypatch):
        # Loud failure: a typo or missing env file must be visible in the
        # parsed config, not silently collapsed to "".
        monkeypatch.delenv("BRIDGE_TEST_UNSET", raising=False)
        assert expand_env_vars("http://${BRIDGE_TEST_UNSET}:8642/v1") == (
            "http://${BRIDGE_TEST_UNSET}:8642/v1"
        )

    def test_default_used_when_unset(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_TEST_MODEL", raising=False)
        assert expand_env_vars("${BRIDGE_TEST_MODEL:-hermes-agent}") == "hermes-agent"

    def test_set_value_overrides_default(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TEST_MODEL", "qwen3.7-plus")
        assert expand_env_vars("${BRIDGE_TEST_MODEL:-hermes-agent}") == "qwen3.7-plus"

    def test_empty_value_wins_over_default(self, monkeypatch):
        # Shell `:-` semantics: empty string IS set, so the default does NOT apply.
        monkeypatch.setenv("BRIDGE_TEST_MODEL", "")
        assert expand_env_vars("${BRIDGE_TEST_MODEL:-hermes-agent}") == ""

    def test_lowercase_name_not_matched(self, monkeypatch):
        # The regex requires UPPER_SNAKE_CASE so the {max_bytes} / {from_id}
        # .format() placeholders in agent.system_prompt are never eaten.
        monkeypatch.setenv("lowercase_var", "should-not-be-substituted")
        assert (
            expand_env_vars("cost is ${lowercase_var}, keep {max_bytes} and {from_id}.")
            == "cost is ${lowercase_var}, keep {max_bytes} and {from_id}."
        )

    def test_system_prompt_placeholders_preserved(self, monkeypatch):
        # The shipped default prompt must keep its Python .format() keys intact
        # even when the bridge is loaded from a YAML file that contains them.
        prompt = expand_env_vars(
            "Keep under {max_bytes} bytes. Sender {from_id}."
        )
        assert "{max_bytes}" in prompt
        assert "{from_id}" in prompt

    def test_recursive_walk_handles_dicts_lists_and_scalars(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TEST_HOST", "agent.lan")
        monkeypatch.setenv("BRIDGE_TEST_PORT", "8642")
        result = expand_env_vars_in_obj(
            {
                "a": "${BRIDGE_TEST_HOST}/api/x",
                "b": ["${BRIDGE_TEST_PORT}", 42, None, True],
                "c": {"nested": "${BRIDGE_TEST_HOST}/api/y"},
                "d": 7,
                "e": None,
            }
        )
        assert result["a"] == "agent.lan/api/x"
        assert result["b"] == ["8642", 42, None, True]
        assert result["c"]["nested"] == "agent.lan/api/y"
        assert result["d"] == 7
        assert result["e"] is None

    def test_load_substitutes_in_real_config_fields(self, tmp_path, api_key_env, monkeypatch):
        # End-to-end: write a config.yaml with ${VAR} placeholders, load it,
        # and confirm the parsed fields hold the substituted values.
        monkeypatch.setenv("BRIDGE_TEST_AGENT_HOST", "agent.lan")
        monkeypatch.setenv("BRIDGE_TEST_AGENT_PORT", "9000")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "serial:\n"
            "  port: /dev/ttyUSB0\n"
            "  baud: 921600\n"
            "meshtastic:\n"
            "  hop_limit: 3\n"
            "  max_message_length: 200\n"
            "  allowed_nodes: []\n"
            "agent:\n"
            "  url: http://${BRIDGE_TEST_AGENT_HOST}:${BRIDGE_TEST_AGENT_PORT}/v1/chat/completions\n"
            "  model: ${BRIDGE_TEST_AGENT_MODEL:-hermes-agent}\n"
            "  api_key_env: MESHTASTIC_AGENT_KEY\n"
            "  timeout: 60\n"
            "bridge:\n"
            "  host: 0.0.0.0\n"
            "  http_port: 8085\n"
        )
        config = Config.load(path=cfg)
        assert config.agent.url == "http://agent.lan:9000/v1/chat/completions"
        assert config.agent.model == "hermes-agent"  # unset, fell through to default

        monkeypatch.setenv("BRIDGE_TEST_AGENT_MODEL", "qwen3.7-plus")
        config2 = Config.load(path=cfg)
        assert config2.agent.model == "qwen3.7-plus"

    def test_load_keeps_unset_placeholder_literal(self, tmp_path, api_key_env, monkeypatch):
        # Loud failure: an unset ${VAR} survives in the parsed config, not "".
        monkeypatch.delenv("BRIDGE_TEST_HOST", raising=False)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "agent:\n"
            "  url: http://${BRIDGE_TEST_HOST}:8642/v1/chat/completions\n"
            "  api_key_env: MESHTASTIC_AGENT_KEY\n"
        )
        config = Config.load(path=cfg)
        assert "${BRIDGE_TEST_HOST}" in config.agent.url


class TestSessionId:
    """Per-node agent session ids (conversation continuity)."""

    def test_strips_punctuation(self):
        assert session_id_for("meshtastic", "!68916e4c") == "meshtastic-68916e4c"

    def test_empty_node_id(self):
        assert session_id_for("meshtastic", "") == ""

    def test_no_prefix(self):
        assert session_id_for("", "!68916e4c") == "68916e4c"


class TestAgentClient:
    """The OpenAI-compatible client: request shape, reply parsing, failure modes."""

    @pytest.mark.asyncio
    async def test_ask_returns_reply_and_sets_auth(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                reply = await client.ask(text="ping", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply == "pong"
        _, kwargs = session.post.call_args
        assert kwargs["headers"]["Authorization"] == f"Bearer {API_KEY}"
        assert kwargs["headers"]["X-Hermes-Session-Id"] == "meshtastic-68916e4c"
        assert "X-Hub-Signature-256" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_ask_sends_model_and_prompted_system_message(self):
        body = {"choices": [{"message": {"content": "ok"}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                await client.ask(text="hello", from_id="!68916e4c")
            finally:
                await client.stop()

        _, kwargs = session.post.call_args
        payload = kwargs["json"]
        assert payload["model"] == "hermes-agent"
        assert payload["messages"][0]["role"] == "system"
        assert "!68916e4c" in payload["messages"][0]["content"]
        # The budget is restated on the user message, next to the question
        assert payload["messages"][1]["role"] == "user"
        assert payload["messages"][1]["content"].startswith("hello")
        assert "reply budget: 200 bytes" in payload["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_default_system_prompt_gets_the_budget_and_sender(self):
        from bridge.config import DEFAULT_SYSTEM_PROMPT

        body = {"choices": [{"message": {"content": "ok"}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(system_prompt=DEFAULT_SYSTEM_PROMPT)
            await client.start()
            try:
                await client.ask(text="hello", from_id="!68916e4c")
            finally:
                await client.stop()

        _, kwargs = session.post.call_args
        system_content = kwargs["json"]["messages"][0]["content"]
        assert "under 200 bytes" in system_content
        assert "!68916e4c" in system_content
        assert "{max_bytes}" not in system_content

    @pytest.mark.asyncio
    async def test_prompt_with_unknown_placeholder_is_left_verbatim(self):
        body = {"choices": [{"message": {"content": "ok"}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(system_prompt="Say {something_else}")
            await client.start()
            try:
                await client.ask(text="hello", from_id="!68916e4c")
            finally:
                await client.stop()

        _, kwargs = session.post.call_args
        assert kwargs["json"]["messages"][0]["content"] == "Say {something_else}"

    @pytest.mark.asyncio
    async def test_ask_truncates_long_reply_after_a_shorten_attempt(self):
        """Both attempts overshoot: the reply is cut, and the log says so."""
        body = {"choices": [{"message": {"content": "x" * 900}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(max_reply_bytes=200)
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply is not None
        assert len(reply.encode("utf-8")) == 200
        assert reply.endswith("...")
        # one first attempt plus one rewrite request
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_ask_uses_the_shortened_reply_when_it_fits(self):
        long_body = {"choices": [{"message": {"content": "x" * 900}}]}
        short_body = {"choices": [{"message": {"content": "short answer"}}]}

        session = fake_session(
            mock_response(json_body=long_body), mock_response(json_body=short_body)
        )
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(max_reply_bytes=200)
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply == "short answer"
        assert session.post.call_count == 2

        # The rewrite request replays the exchange so it works without sessions
        _, kwargs = session.post.call_args
        messages = kwargs["json"]["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
        assert messages[2]["content"] == "x" * 900
        assert "under 200 bytes" in messages[3]["content"]

    @pytest.mark.asyncio
    async def test_shorten_retry_can_be_disabled(self):
        body = {"choices": [{"message": {"content": "x" * 900}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(max_reply_bytes=200, shorten_retry=False)
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert session.post.call_count == 1
        assert len(reply.encode("utf-8")) == 200

    @pytest.mark.asyncio
    async def test_reply_within_budget_is_not_rewritten(self):
        body = {"choices": [{"message": {"content": "pong"}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(max_reply_bytes=200)
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply == "pong"
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_ask_returns_none_on_auth_failure_without_retrying(self):
        session = fake_session(
            mock_response(status=401, text_body='{"error": "Invalid API key"}')
        )
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply is None
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_ask_retries_once_then_gives_up_on_5xx(self):
        session = fake_session(
            mock_response(status=503, text_body="upstream unavailable")
        )
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client(max_retries=1)
            client._retry_delay = 0  # keep the test fast
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply is None
        assert session.post.call_count == 2  # first attempt plus one retry

    @pytest.mark.asyncio
    async def test_ask_returns_none_on_malformed_response(self):
        with patch("bridge.http_client.aiohttp.ClientSession") as session_cls:
            session = AsyncMock()
            session_cls.return_value = session
            session.post.return_value.__aenter__.return_value = mock_response(
                json_body={"unexpected": "shape"}
            )

            client = make_client()
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply is None

    @pytest.mark.asyncio
    async def test_ask_returns_none_when_empty_reply(self):
        body = {"choices": [{"message": {"content": "   "}}]}

        session = fake_session(mock_response(json_body=body))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                reply = await client.ask(text="hi", from_id="!68916e4c")
            finally:
                await client.stop()

        assert reply is None

    @pytest.mark.asyncio
    async def test_ask_without_start_returns_none(self):
        client = make_client()
        assert await client.ask(text="hi", from_id="!68916e4c") is None

    @pytest.mark.asyncio
    async def test_check_health_uses_base_url_without_auth(self):
        session = fake_session(mock_response(status=200))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                healthy = await client.check_health()
            finally:
                await client.stop()

        assert healthy is True
        url = session.get.call_args[0][0]
        assert url == "http://127.0.0.1:8642/health"

    @pytest.mark.asyncio
    async def test_check_health_false_on_error_status(self):
        session = fake_session(mock_response(status=500))
        with patch("bridge.http_client.aiohttp.ClientSession", return_value=session):

            client = make_client()
            await client.start()
            try:
                healthy = await client.check_health()
            finally:
                await client.stop()

        assert healthy is False


class TestReplyHop:
    """Inbound LoRa message -> agent -> reply back over LoRa."""

    def make_bridge(self, api_key_env):
        from bridge.main import MeshtasticBridge

        config = Config()
        config.validate()
        bridge = MeshtasticBridge(config)
        radio = AsyncMock()
        # The bridge's own node: only packets addressed here are accepted
        radio.my_node_id = "!02e72ba8"
        radio.my_node_num = 48704424
        bridge.radio = radio
        bridge.agent_client = AsyncMock()
        return bridge

    @pytest.mark.asyncio
    async def test_reply_is_sent_back_to_sender(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.agent_client.ask.return_value = "pong"
        bridge.radio.send_text.return_value = True

        await bridge._forward_to_agent("!68916e4c", "ping", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_awaited_once_with(
            text="ping", from_id="!68916e4c"
        )
        bridge.radio.send_text.assert_awaited_once_with(
            text="pong",
            destination="!68916e4c",
            channel=0,
            want_ack=True,
            ack_timeout=8.0,
        )

    @pytest.mark.asyncio
    async def test_automated_notification_is_ignored(self, api_key_env):
        """A position share must not cost an agent turn or a LoRa reply."""
        bridge = self.make_bridge(api_key_env)

        await bridge._forward_to_agent(
            "!68916e4c",
            "📍 Hoon Portable Mesh has shared their position",
            0,
            "!02e72ba8",
        )

        bridge.agent_client.ask.assert_not_awaited()
        bridge.radio.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_message_still_reaches_the_agent(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.agent_client.ask.return_value = "hi"
        bridge.radio.send_text.return_value = True

        await bridge._forward_to_agent("!68916e4c", "hello there", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_awaited_once_with(
            text="hello there", from_id="!68916e4c"
        )

    @pytest.mark.asyncio
    async def test_no_reply_means_no_transmission(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.agent_client.ask.return_value = None

        await bridge._forward_to_agent("!68916e4c", "ping", 0, "!02e72ba8")

        bridge.radio.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_send_is_logged_not_raised(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.agent_client.ask.return_value = "pong"
        bridge.radio.send_text.return_value = False

        await bridge._forward_to_agent("!68916e4c", "ping", 2, "!02e72ba8")

        bridge.radio.send_text.assert_awaited_once_with(
            text="pong",
            destination="!68916e4c",
            channel=2,
            want_ack=True,
            ack_timeout=8.0,
        )

    @pytest.mark.asyncio
    async def test_receive_callback_schedules_forwarding(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge._loop = asyncio.get_running_loop()
        bridge._forward_to_agent = AsyncMock()

        bridge._on_receive(
            {"from_id": "!68916e4c", "text": "hello", "channel": 0, "to_id": "!02e72ba8"}
        )
        await asyncio.sleep(0.05)

        bridge._forward_to_agent.assert_awaited_once_with(
            "!68916e4c", "hello", 0, "!02e72ba8"
        )

    @pytest.mark.asyncio
    async def test_receive_callback_ignores_non_text_packets(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge._loop = asyncio.get_running_loop()
        bridge._forward_to_agent = AsyncMock()

        bridge._on_receive({"from_id": "!68916e4c", "text": "", "channel": 0})
        await asyncio.sleep(0.05)

        bridge._forward_to_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_health_reports_serial_agent_and_rx_liveness(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.radio.is_connected = True
        bridge.agent_client.check_health.return_value = True

        health = await bridge._get_health()

        assert health["serial"] is True
        assert health["agent_reachable"] is True
        assert health["inbound_packets"] == 0
        # Never heard anything yet: unknown, not zero seconds
        assert health["last_inbound_seconds"] is None

    @pytest.mark.asyncio
    async def test_health_ages_since_last_packet(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.radio.is_connected = True
        bridge.agent_client.check_health.return_value = True

        # Any packet counts, not just text: this is the deaf-radio signal
        bridge._on_packet({"decoded": {"portnum": "TELEMETRY_APP"}})
        health = await bridge._get_health()

        assert health["inbound_packets"] == 1
        assert health["last_inbound_seconds"] is not None
        assert health["last_inbound_seconds"] < 5


class TestHTTPServer:
    """Agent-initiated sends through the bridge HTTP API."""

    @pytest.mark.asyncio
    async def test_send_endpoint(self):
        from bridge.http_server import BridgeHTTPServer
        from aiohttp.test_utils import TestClient, TestServer

        send_fn = AsyncMock(return_value=True)
        server = BridgeHTTPServer("127.0.0.1", 8085, send_fn)
        await server.start()

        try:
            async with TestClient(TestServer(server._app)) as client:
                resp = await client.post(
                    "/send",
                    json={
                        "destination_id": "!68916e4c",
                        "message": "Test message",
                        "channel": 0,
                    },
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "sent"
                send_fn.assert_called_once()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_send_endpoint_truncates_bytes(self):
        from bridge.http_server import BridgeHTTPServer
        from aiohttp.test_utils import TestClient, TestServer

        captured = {}

        async def send_fn(destination_id, message, channel):
            captured["message"] = message
            return True

        server = BridgeHTTPServer("127.0.0.1", 8085, send_fn)
        await server.start()

        try:
            async with TestClient(TestServer(server._app)) as client:
                resp = await client.post(
                    "/send",
                    json={"destination_id": "^all", "message": "あ" * 100},
                )
                assert resp.status == 200
        finally:
            await server.stop()

        assert len(captured["message"].encode("utf-8")) <= 200
        captured["message"].encode("utf-8").decode("utf-8")

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        from bridge.http_server import BridgeHTTPServer
        from aiohttp.test_utils import TestClient, TestServer

        send_fn = AsyncMock()
        health_fn = AsyncMock(return_value={"serial": True, "agent_reachable": True})
        server = BridgeHTTPServer("127.0.0.1", 8085, send_fn, health_fn)
        await server.start()

        try:
            async with TestClient(TestServer(server._app)) as client:
                resp = await client.get("/health")
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert "uptime" in data
                assert data["agent_reachable"] is True
        finally:
            await server.stop()


class TestDeliveryConfirmation:
    """Routing ACK/NAK handling: the signal that a handset never received a reply."""

    def make_radio(self):
        from bridge.radio import MeshtasticRadio
        from bridge.transport import SerialTransport

        return MeshtasticRadio(transport=SerialTransport(port="/dev/ttyUSB0"))

    def test_routing_ack_resolves_pending_packet(self):
        radio = self.make_radio()
        radio._pending_acks[4242] = None

        radio._handle_receive(
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "requestId": 4242,
                    "routing": {"errorReason": "NONE"},
                }
            },
            None,
        )

        assert radio._pending_acks[4242] == "NONE"

    def test_routing_nak_records_reason(self):
        radio = self.make_radio()
        radio._pending_acks[7] = None

        radio._handle_receive(
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "requestId": 7,
                    "routing": {"errorReason": "TIMEOUT"},
                }
            },
            None,
        )

        assert radio._pending_acks[7] == "TIMEOUT"

    def test_ack_without_error_reason_counts_as_delivered(self):
        radio = self.make_radio()
        radio._pending_acks[9] = None

        radio._handle_receive(
            {"decoded": {"portnum": "ROUTING_APP", "requestId": 9, "routing": {}}},
            None,
        )

        assert radio._pending_acks[9] == "NONE"

    def test_unrelated_response_is_ignored(self):
        radio = self.make_radio()
        radio._pending_acks[1] = None

        radio._handle_receive(
            {"decoded": {"portnum": "ROUTING_APP", "requestId": 999, "routing": {}}},
            None,
        )

        assert radio._pending_acks[1] is None

    def test_all_packets_fire_on_packet_but_only_text_fires_on_receive(self):
        radio = self.make_radio()
        seen_packets = []
        seen_text = []
        radio.on_packet = seen_packets.append
        radio.on_receive = seen_text.append

        radio._handle_receive({"decoded": {"portnum": "TELEMETRY_APP"}}, None)
        radio._handle_receive(
            {
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"},
                "fromId": "!abc",
                "channel": 0,
            },
            None,
        )

        assert len(seen_packets) == 2
        assert len(seen_text) == 1
        assert seen_text[0]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_confirm_delivery_returns_on_ack(self):
        radio = self.make_radio()
        task = asyncio.create_task(radio._confirm_delivery(55, "!68916e4c", 2.0))
        await asyncio.sleep(0.05)
        radio._pending_acks[55] = "NONE"

        await asyncio.wait_for(task, timeout=2)

        assert 55 not in radio._pending_acks

    @pytest.mark.asyncio
    async def test_confirm_delivery_warns_and_cleans_up_on_timeout(self):
        radio = self.make_radio()

        started = asyncio.get_running_loop().time()
        await radio._confirm_delivery(56, "!68916e4c", 0.3)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.3
        assert elapsed < 2
        assert 56 not in radio._pending_acks

    @pytest.mark.asyncio
    async def test_confirm_delivery_with_zero_timeout_does_not_hang(self):
        radio = self.make_radio()
        await asyncio.wait_for(radio._confirm_delivery(57, "!68916e4c", 0.0), timeout=1)
        assert 57 not in radio._pending_acks

    @pytest.mark.asyncio
    async def test_send_text_requests_ack_only_for_unicast(self):
        """Broadcasts must not wait for an ack; unicasts must."""
        radio = self.make_radio()
        sent = {}

        class FakeIface:
            def sendText(self, text, **kwargs):
                sent.update(kwargs)
                return type("P", (), {"id": 123})()

        radio._interface = FakeIface()
        radio._connected = True

        await radio.send_text("hi", destination=None, channel=0, ack_timeout=0.0)
        assert sent["wantAck"] is False
        assert sent["destinationId"] == "^all"

        await radio.send_text("hi", destination="!68916e4c", ack_timeout=0.0)
        assert sent["wantAck"] is True

    @pytest.mark.asyncio
    async def test_send_text_respects_want_ack_false(self):
        radio = self.make_radio()
        sent = {}

        class FakeIface:
            def sendText(self, text, **kwargs):
                sent.update(kwargs)
                return type("P", (), {"id": 1})()

        radio._interface = FakeIface()
        radio._connected = True

        await radio.send_text("hi", destination="!68916e4c", want_ack=False)
        assert sent["wantAck"] is False


class TestNodeIdHelpers:
    """Node id normalization and broadcast detection (the DM-only filter's basis)."""

    def test_normalize_variants(self):
        from bridge.config import normalize_node_id

        assert normalize_node_id("!68916e4c") == "!68916e4c"
        assert normalize_node_id("68916E4C") == "!68916e4c"
        assert normalize_node_id("  68916e4c  ") == "!68916e4c"
        assert normalize_node_id(1754361420) == "!68916e4c"
        assert normalize_node_id("1754361420") == "!68916e4c"

    def test_normalize_rejects_junk(self):
        from bridge.config import normalize_node_id

        assert normalize_node_id(None) == ""
        assert normalize_node_id("") == ""
        assert normalize_node_id("!") == ""
        assert normalize_node_id("   ") == ""

    def test_broadcast_detection(self):
        from bridge.config import is_broadcast_id

        assert is_broadcast_id("^all")          # what the library reports
        assert is_broadcast_id("!ffffffff")     # 0xFFFFFFFF on the wire
        assert is_broadcast_id("4294967295")
        assert not is_broadcast_id("!68916e4c")
        assert not is_broadcast_id(None)


class TestDirectMessagePolicy:
    """DM-only default plus the sender allowlist."""

    def make_bridge(self, api_key_env, **overrides):
        from bridge.main import MeshtasticBridge

        config = Config()
        for key, value in overrides.items():
            setattr(config.meshtastic, key, value)
        config.validate()

        bridge = MeshtasticBridge(config)
        radio = AsyncMock()
        radio.my_node_id = "!02e72ba8"
        radio.my_node_num = 48704424
        radio.send_text.return_value = True
        bridge.radio = radio
        bridge.agent_client = AsyncMock()
        bridge.agent_client.ask.return_value = "ok"
        return bridge

    def test_dm_only_is_the_default(self):
        assert Config().meshtastic.direct_messages_only is True
        assert Config().meshtastic.allowed_nodes == []

    @pytest.mark.asyncio
    async def test_channel_broadcast_is_dropped(self, api_key_env):
        bridge = self.make_bridge(api_key_env)

        await bridge._forward_to_agent("!68916e4c", "hello everyone", 0, "^all")

        bridge.agent_client.ask.assert_not_awaited()
        bridge.radio.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_addressed_to_another_node_is_dropped(self, api_key_env):
        bridge = self.make_bridge(api_key_env)

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "!deadbeef")

        bridge.agent_client.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_message_without_destination_is_dropped(self, api_key_env):
        bridge = self.make_bridge(api_key_env)

        await bridge._forward_to_agent("!68916e4c", "hello", 0, None)

        bridge.agent_client.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_message_to_us_is_answered(self, api_key_env):
        bridge = self.make_bridge(api_key_env)

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_awaited_once_with(
            text="hello", from_id="!68916e4c"
        )
        bridge.radio.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_answered_when_filter_disabled(self, api_key_env):
        bridge = self.make_bridge(api_key_env, direct_messages_only=False)

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "^all")

        bridge.agent_client.ask.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fails_closed_when_own_node_id_is_unknown(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.radio.my_node_id = None

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sender_outside_allowlist_is_dropped(self, api_key_env):
        bridge = self.make_bridge(api_key_env, allowed_nodes=["!someoneelse"])

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlist_accepts_case_and_missing_bang(self, api_key_env):
        bridge = self.make_bridge(api_key_env, allowed_nodes=["68916E4C"])

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "!02e72ba8")

        bridge.agent_client.ask.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allowlist_does_not_bypass_the_dm_filter(self, api_key_env):
        bridge = self.make_bridge(api_key_env, allowed_nodes=["!68916e4c"])

        await bridge._forward_to_agent("!68916e4c", "hello", 0, "^all")

        bridge.agent_client.ask.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejection_log_is_rate_limited(self, api_key_env, caplog):
        import logging

        bridge = self.make_bridge(api_key_env)

        with caplog.at_level(logging.INFO):
            await bridge._forward_to_agent("!68916e4c", "hello", 0, "^all")
            await bridge._forward_to_agent("!68916e4c", "hello again", 0, "^all")

        dropped = [
            r.getMessage() for r in caplog.records if "Dropped channel message" in r.getMessage()
        ]
        assert len(dropped) == 1
        assert "^all" in dropped[0]

    @pytest.mark.asyncio
    async def test_own_packets_are_counted_separately_from_received(self, api_key_env):
        bridge = self.make_bridge(api_key_env)
        bridge.radio.is_connected = True
        bridge.agent_client.check_health.return_value = True

        # A packet the radio itself sent: proves TX, says nothing about RX
        bridge._on_packet({"from": 48704424, "decoded": {"portnum": "TELEMETRY_APP"}})
        health = await bridge._get_health()
        assert health["own_packets"] == 1
        assert health["inbound_packets"] == 0
        assert health["last_inbound_seconds"] is None

        # A packet from another node: the real RX signal
        bridge._on_packet({"from": 123456, "decoded": {"portnum": "TELEMETRY_APP"}})
        health = await bridge._get_health()
        assert health["own_packets"] == 1
        assert health["inbound_packets"] == 1
        assert health["last_inbound_seconds"] is not None


class TestTransports:
    """The seam that lets a new connection method be added in one place."""

    def test_serial_is_registered(self):
        from bridge.transport import TRANSPORTS, SerialTransport

        assert TRANSPORTS["serial"] is SerialTransport

    def test_build_transport_reads_the_serial_config(self):
        from bridge.transport import SerialTransport, build_transport

        config = Config()
        config.serial.port = "/dev/ttyTEST"
        config.serial.baud = 115200

        transport = build_transport(config)

        assert isinstance(transport, SerialTransport)
        assert transport.port == "/dev/ttyTEST"
        assert transport.baud == 115200
        assert transport.describe() == "serial /dev/ttyTEST @ 115200 baud"

    def test_unknown_connection_lists_registered_transports(self, api_key_env):
        config = Config()
        config.meshtastic.connection = "bluetooth"

        with pytest.raises(ValueError, match="available: serial"):
            config.validate()

    def test_prerequisites_report_missing_device(self):
        from bridge.transport import SerialTransport

        ok, detail = SerialTransport(port="/dev/definitely-not-here").prerequisites_met()

        assert ok is False
        assert "does not exist" in detail

    def test_prerequisites_report_present_device(self, tmp_path):
        from bridge.transport import SerialTransport

        device = tmp_path / "fake-tty"
        device.write_text("")

        ok, detail = SerialTransport(port=str(device)).prerequisites_met()

        assert ok is True
        assert "present" in detail

    def test_new_transport_is_a_class_plus_one_registry_entry(self, api_key_env):
        """Adding a connection method touches nothing outside this seam."""
        from bridge.transport import TRANSPORTS, Transport, build_transport

        class FakeTransport(Transport):
            name = "fake"

            def __init__(self, address: str = "unset"):
                self.address = address

            @classmethod
            def from_config(cls, config):
                return cls(f"addr-for-{config.meshtastic.connection}")

            def describe(self) -> str:
                return f"fake {self.address}"

            def prerequisites_met(self):
                return True, "always ready"

            def open(self):
                return "fake-interface"

            def close(self, interface) -> None:
                pass

        TRANSPORTS[FakeTransport.name] = FakeTransport
        try:
            config = Config()
            config.meshtastic.connection = "fake"
            config.validate()

            transport = build_transport(config)

            assert isinstance(transport, FakeTransport)
            assert transport.describe() == "fake addr-for-fake"
        finally:
            TRANSPORTS.pop(FakeTransport.name, None)

    @pytest.mark.asyncio
    async def test_radio_only_needs_describe_prepare_open_close(self):
        """MeshtasticRadio is transport-agnostic: it never sees a serial port."""
        from bridge.radio import MeshtasticRadio
        from bridge.transport import Transport

        calls = []

        class FakeTransport(Transport):
            name = "fake"

            @classmethod
            def from_config(cls, config):
                return cls()

            def describe(self) -> str:
                return "fake node"

            def prerequisites_met(self):
                return True, "ok"

            def prepare(self) -> None:
                calls.append("prepare")

            def open(self):
                calls.append("open")
                my_info = type("MyInfo", (), {"my_node_num": 123})()
                return type("Iface", (), {"myInfo": my_info})()

            def close(self, interface) -> None:
                calls.append("close")

        radio = MeshtasticRadio(transport=FakeTransport())

        assert await radio.connect() is True
        assert calls == ["prepare", "open"]
        assert radio.my_node_id == "!0000007b"

        await radio.disconnect()
        assert calls[-1] == "close"
        assert radio.is_connected is False


class TestCheckCommand:
    """``meshtastic-bridge --check``: verify a deployment without opening the radio."""

    def _config(self, tmp_path) -> Config:
        config = Config()
        device = tmp_path / "fake-tty"
        device.write_text("")
        config.serial.port = str(device)
        return config

    @pytest.mark.asyncio
    async def test_check_passes_when_everything_is_in_place(self, api_key_env, tmp_path, capsys):
        from bridge.main import check

        with patch("bridge.main.AgentClient") as client_cls:
            client_cls.return_value = AsyncMock()
            client_cls.return_value.check_health = AsyncMock(return_value=True)

            ok = await check(self._config(tmp_path), tmp_path / "config.yaml")

        out = capsys.readouterr().out
        assert ok is True
        assert "RESULT: PASS" in out
        assert "connection   : serial" in out
        assert "agent /health answered" in out

    @pytest.mark.asyncio
    async def test_check_fails_on_missing_device(self, api_key_env, tmp_path, capsys):
        from bridge.main import check

        config = self._config(tmp_path)
        config.serial.port = str(tmp_path / "not-a-device")

        with patch("bridge.main.AgentClient") as client_cls:
            client_cls.return_value = AsyncMock()
            client_cls.return_value.check_health = AsyncMock(return_value=True)

            ok = await check(config)

        out = capsys.readouterr().out
        assert ok is False
        assert "does not exist" in out
        assert "RESULT: FAIL" in out

    @pytest.mark.asyncio
    async def test_check_fails_when_agent_is_unreachable(self, api_key_env, tmp_path, capsys):
        from bridge.main import check

        with patch("bridge.main.AgentClient") as client_cls:
            client_cls.return_value = AsyncMock()
            client_cls.return_value.check_health = AsyncMock(return_value=False)

            ok = await check(self._config(tmp_path))

        out = capsys.readouterr().out
        assert ok is False
        assert "did not answer" in out

    @pytest.mark.asyncio
    async def test_check_warns_when_no_allowlist_is_set(self, api_key_env, tmp_path, capsys):
        from bridge.main import check

        with patch("bridge.main.AgentClient") as client_cls:
            client_cls.return_value = AsyncMock()
            client_cls.return_value.check_health = AsyncMock(return_value=True)

            await check(self._config(tmp_path))

        out = capsys.readouterr().out
        assert "allowed_nodes=any" in out
        assert "WARN no allowed_nodes set" in out

    def test_cli_check_returns_one_for_an_unregistered_connection(self, api_key_env, tmp_path, capsys):
        from bridge.main import cli

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "meshtastic:\n  connection: bluetooth\n"
            "serial:\n  port: /dev/ttyUSB0\n  baud: 921600\n"
            "agent:\n  url: http://127.0.0.1:8642/v1/chat/completions\n"
            "  api_key_env: MESHTASTIC_AGENT_KEY\n"
        )

        code = cli(["--check", "--config", str(config_path)])

        out = capsys.readouterr().out
        assert code == 1
        assert "not registered" in out
        assert "available: serial" in out

    def test_cli_check_uses_the_named_config_file(self, api_key_env, tmp_path, capsys):
        from bridge.main import cli

        config_path = tmp_path / "config.yaml"
        device = tmp_path / "fake-tty"
        device.write_text("")
        config_path.write_text(
            "meshtastic:\n  connection: serial\n  allowed_nodes:\n    - \"!68916e4c\"\n"
            f"serial:\n  port: {device}\n  baud: 921600\n"
            "agent:\n  url: http://127.0.0.1:8642/v1/chat/completions\n"
            "  api_key_env: MESHTASTIC_AGENT_KEY\n"
        )

        with patch("bridge.main.AgentClient") as client_cls:
            client_cls.return_value = AsyncMock()
            client_cls.return_value.check_health = AsyncMock(return_value=True)

            code = cli(["--check", "--config", str(config_path)])

        out = capsys.readouterr().out
        assert code == 0
        assert str(config_path) in out
        assert "allowed_nodes=1" in out
