"""
Tests for bridge.environment — the single source of truth for ${VAR} expansion
and .env-file management used by deploy/install.sh.

This replaces the old deploy/lib/env_capture.sh bash library and its
subprocess-based tests. Covers:

  * expand_env_vars          (runtime substitution, re-exported by bridge.config)
  * seed_env_file            (seed .env from .env.example, idempotent)
  * capture_from_shell       (pull host vars from the environment)
  * set_agent_key            (write/preserve/placeholder the agent key)
  * the CLI (python3 bridge/environment.py ...)
  * install.sh wiring        (thin calls, no env_capture.sh)
  * config.yaml <-> CAPTURED_VARS cross-check
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from bridge.environment import (
    CAPTURED_VARS,
    KEY_VAR,
    capture_from_shell,
    expand_env_vars,
    seed_env_file,
    set_agent_key,
    upsert_env_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE = REPO_ROOT / ".env.example"
INSTALL = REPO_ROOT / "deploy" / "install.sh"
MODULE = REPO_ROOT / "bridge" / "environment.py"
CONFIG = REPO_ROOT / "config.yaml"


def _read(path) -> dict:
    """Parse a .env-style file into {KEY: value}, ignoring blanks/comments."""
    p = Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class TestSeedEnvFile:
    def test_seed_creates_file_from_template(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        seed_env_file(env_file, EXAMPLE)
        env = _read(env_file)
        assert env["AGENT_HOST"] == "192.168.0.100"
        assert env["AGENT_PORT"] == "8642"

    def test_seed_is_idempotent(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CUSTOM=keep-me\n")
        seed_env_file(env_file, EXAMPLE)
        assert _read(env_file)["CUSTOM"] == "keep-me"


class TestCaptureFromShell:
    def test_capture_writes_shell_values(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        seed_env_file(env_file, EXAMPLE)
        capture_from_shell(
            env_file,
            environ={"AGENT_HOST": "myhost.lan", "AGENT_PORT": "9000", "AGENT_MODEL": "qwen3"},
        )
        env = _read(env_file)
        assert env["AGENT_HOST"] == "myhost.lan"
        assert env["AGENT_PORT"] == "9000"
        assert env["AGENT_MODEL"] == "qwen3"

    def test_capture_preserves_operator_edits(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=edited.lan\nAGENT_PORT=8642\n")
        capture_from_shell(env_file, environ={"AGENT_PORT": "9999"})
        env = _read(env_file)
        assert env["AGENT_HOST"] == "edited.lan"
        assert env["AGENT_PORT"] == "9999"

    def test_capture_shell_override_wins(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=edited.lan\n")
        capture_from_shell(env_file, environ={"AGENT_HOST": "new.lan"})
        assert _read(env_file)["AGENT_HOST"] == "new.lan"

    def test_capture_noop_when_no_vars(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=stable.lan\n")
        before = env_file.read_text()
        capture_from_shell(env_file, environ={})
        assert env_file.read_text() == before

    def test_capture_empty_treated_as_unset(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=real.lan\n")
        capture_from_shell(env_file, environ={"AGENT_HOST": ""})
        assert _read(env_file)["AGENT_HOST"] == "real.lan"


class TestSetAgentKey:
    def test_set_key_writes_and_dedupes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MESHTASTIC_AGENT_KEY=old-key\n")
        set_agent_key(env_file, environ={"MESHTASTIC_AGENT_KEY": "new-key"})
        assert _read(env_file)["MESHTASTIC_AGENT_KEY"] == "new-key"
        lines = [
            ln for ln in env_file.read_text().splitlines()
            if ln.startswith("MESHTASTIC_AGENT_KEY=")
        ]
        assert len(lines) == 1

    def test_set_key_preserves_existing(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("MESHTASTIC_AGENT_KEY=keep-me\n")
        set_agent_key(env_file, environ={})
        assert _read(env_file)["MESHTASTIC_AGENT_KEY"] == "keep-me"

    def test_set_key_writes_placeholder(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=192.168.0.100\n")
        set_agent_key(env_file, environ={})
        assert "MESHTASTIC_AGENT_KEY=<paste-key-here>" in env_file.read_text()


class TestUpsert:
    def test_upsert_dedupes(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("AGENT_HOST=one\nAGENT_PORT=8642\n")
        upsert_env_file(env_file, "AGENT_HOST", "two")
        lines = [
            ln for ln in env_file.read_text().splitlines()
            if ln.startswith("AGENT_HOST=")
        ]
        assert lines == ["AGENT_HOST=two"]


class TestExpandEnvVars:
    def test_uses_passed_environ(self) -> None:
        assert expand_env_vars("http://${HOST}:${PORT}/x", {"HOST": "h", "PORT": "9"}) == (
            "http://h:9/x"
        )

    def test_unset_with_default(self) -> None:
        assert expand_env_vars("${MODEL:-hermes-agent}", {}) == "hermes-agent"


class TestCli:
    def test_cli_expand(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE), "expand"],
            input="http://${CLI_HOST}:8642/x",
            capture_output=True,
            text=True,
            env={**os.environ, "CLI_HOST": "cli.lan"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "http://cli.lan:8642/x"

    def test_cli_seed_capture_set_key(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        result = subprocess.run(
            [sys.executable, str(MODULE), "seed", "--file", str(env_file), "--template", str(EXAMPLE)],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        assert result.returncode == 0, result.stderr
        assert env_file.exists()

        env = {**os.environ, "AGENT_HOST": "x.lan", "MESHTASTIC_AGENT_KEY": "sek"}
        result = subprocess.run(
            [sys.executable, str(MODULE), "capture", "--file", str(env_file)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        result = subprocess.run(
            [sys.executable, str(MODULE), "set-key", "--file", str(env_file)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr

        parsed = _read(env_file)
        assert parsed["AGENT_HOST"] == "x.lan"
        assert parsed["MESHTASTIC_AGENT_KEY"] == "sek"


class TestInstallShWiring:
    def test_install_sh_uses_environment_module(self) -> None:
        text = INSTALL.read_text()
        assert "bridge/environment.py" in text
        assert "deploy/lib/env_capture.sh" not in text

    def test_install_sh_calls_each_subcommand(self) -> None:
        text = INSTALL.read_text()
        for sub in ("env_tool seed", "env_tool capture", "env_tool set-key", "env_tool expand"):
            assert sub in text

    def test_install_sh_does_not_re_inline_substitution(self) -> None:
        text = INSTALL.read_text()
        assert "re.sub" not in text
        assert "bridge_env_capture_from_shell" not in text


class TestConfigAgreement:
    def test_config_yaml_placeholders_are_captured(self) -> None:
        text = CONFIG.read_text()
        placeholders = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
        captured = set(CAPTURED_VARS) | {KEY_VAR}
        missing = placeholders - captured
        assert not missing, (
            f"config.yaml references ${{{', '.join(sorted(missing))}}} but "
            "bridge.environment does not list it in CAPTURED_VARS/KEY_VAR"
        )

    def test_captured_vars_are_upper_snake_case(self) -> None:
        for name in CAPTURED_VARS:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name
