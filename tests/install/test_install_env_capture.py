"""
Integration + regression checks for the installer's env-file handling.

The installer (deploy/install.sh) sources deploy/lib/env_capture.sh and uses
its three functions to manage /etc/meshtastic-bridge/.env:

  - bridge_env_seed:                seed the file from .env.example
  - bridge_env_capture_from_shell:  pull host-specific vars from the shell
  - bridge_env_append_key:          write MESHTASTIC_AGENT_KEY, dedup

Two classes of checks:

  1. Integration tests (TestInstallerEnvCapture): shell out to a real
     `bash -c` invocation of these functions in a tmp dir, with the same
     shape the installer uses. These cover the contract.

  2. Regression checks (TestInstallShReferencesLibrary): static assertions
     that install.sh still wires up the library. Catches a refactor that
     removes the source line or duplicates the logic inline again.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB = REPO_ROOT / "deploy" / "lib" / "env_capture.sh"
EXAMPLE = REPO_ROOT / ".env.example"
INSTALL = REPO_ROOT / "deploy" / "install.sh"


def _run_in_sandbox(env: dict, script: str) -> subprocess.CompletedProcess:
    """Run a bash snippet in a fresh tmp dir with the library sourced.

    The test mimics how install.sh calls the functions: in a child shell,
    with BRIDGE_ENV_FILE/BRIDGE_ENV_EXAMPLE pointed at a tmp file. We
    inherit the parent env, override with the per-test `env`, and pass
    the rest of the script as a string the child executes.
    """
    assert LIB.exists(), f"library missing: {LIB}"
    assert EXAMPLE.exists(), f"template missing: {EXAMPLE}"

    full_env = os.environ.copy()
    full_env.update(env)

    return subprocess.run(
        ["bash", "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """Point BRIDGE_ENV_FILE/BRIDGE_ENV_EXAMPLE at a tmp dir."""
    return {
        "BRIDGE_ENV_FILE": str(tmp_path / ".env"),
        "BRIDGE_ENV_EXAMPLE": str(EXAMPLE),
    }


def _read(env_file: str) -> dict:
    """Parse a .env-style file into {KEY: value}, ignoring blanks/comments."""
    out: dict = {}
    p = Path(env_file)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class TestInstallerEnvCapture:
    """
    Contract tests for the installer's env-file handling.

    These exercise the same `bash -c` boundary the installer uses, so they
    catch bugs that only manifest in child shells (e.g. array values
    exported via `export -f` not surviving into the subshell).
    """

    SOURCE = (
        f"source {LIB}"
    )

    def test_seed_creates_file_from_template(self, sandbox: dict) -> None:
        result = _run_in_sandbox(
            sandbox,
            f"{self.SOURCE}; bridge_env_seed",
        )
        assert result.returncode == 0, result.stderr
        env_file = sandbox["BRIDGE_ENV_FILE"]
        assert Path(env_file).exists()
        seeded = _read(env_file)
        # The shipped template contains these defaults
        assert seeded.get("AGENT_HOST") == "192.168.0.100"
        assert seeded.get("AGENT_PORT") == "8642"

    def test_seed_is_idempotent(self, sandbox: dict) -> None:
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("CUSTOM=keep-me\n")
        _run_in_sandbox(sandbox, f"{self.SOURCE}; bridge_env_seed")
        # Existing file is NOT clobbered
        assert _read(env_file).get("CUSTOM") == "keep-me"

    def test_capture_writes_shell_values_on_first_install(self, sandbox: dict) -> None:
        result = _run_in_sandbox(
            {**sandbox,
             "AGENT_HOST": "myhost.lan",
             "AGENT_PORT": "9000",
             "AGENT_MODEL": "qwen3",
             "MESHTASTIC_AGENT_KEY": "secretkey"},
            f"{self.SOURCE}; "
            "bridge_env_seed; "
            "bridge_env_capture_from_shell; "
            "bridge_env_append_key \"$MESHTASTIC_AGENT_KEY\"",
        )
        assert result.returncode == 0, result.stderr
        env = _read(sandbox["BRIDGE_ENV_FILE"])
        assert env["AGENT_HOST"] == "myhost.lan"
        assert env["AGENT_PORT"] == "9000"
        assert env["AGENT_MODEL"] == "qwen3"
        assert env["MESHTASTIC_AGENT_KEY"] == "secretkey"

    def test_capture_preserves_operator_edits(self, sandbox: dict) -> None:
        """If the operator already set AGENT_HOST in .env, a re-install
        without re-exporting it must keep the operator's value."""
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("AGENT_HOST=edited.lan\nAGENT_PORT=8642\n")
        result = _run_in_sandbox(
            {**sandbox, "AGENT_PORT": "9999"},  # override only AGENT_PORT
            f"{self.SOURCE}; "
            "bridge_env_seed; "  # no-op, file exists
            "bridge_env_capture_from_shell",
        )
        assert result.returncode == 0, result.stderr
        env = _read(env_file)
        assert env["AGENT_HOST"] == "edited.lan"  # operator's edit kept
        assert env["AGENT_PORT"] == "9999"  # shell override applied

    def test_capture_shell_override_wins_over_file(self, sandbox: dict) -> None:
        """If the operator both edited the file AND exported in the shell,
        the shell value wins. This is the regression case for the array
        bug: previously the loop body never ran in bash -c, so shell
        overrides were silently dropped."""
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("AGENT_HOST=edited.lan\n")
        result = _run_in_sandbox(
            {**sandbox, "AGENT_HOST": "new.lan"},
            f"{self.SOURCE}; bridge_env_capture_from_shell",
        )
        assert result.returncode == 0, result.stderr
        env = _read(env_file)
        assert env["AGENT_HOST"] == "new.lan"

    def test_capture_is_noop_when_shell_has_no_vars(self, sandbox: dict) -> None:
        """Re-install with nothing exported must not touch the file."""
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("AGENT_HOST=stable.lan\n")
        before = Path(env_file).read_text()
        result = _run_in_sandbox(
            sandbox,
            f"{self.SOURCE}; bridge_env_capture_from_shell",
        )
        assert result.returncode == 0, result.stderr
        assert Path(env_file).read_text() == before

    def test_capture_handles_empty_string_as_unset(self, sandbox: dict) -> None:
        """A shell var set to the empty string is treated as unset (matches
        the [ -n "$value" ] guard the library uses). This means an operator
        who exported AGENT_HOST='' on the command line will not clobber a
        real value already in the file."""
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("AGENT_HOST=real.lan\n")
        result = _run_in_sandbox(
            {**sandbox, "AGENT_HOST": ""},
            f"{self.SOURCE}; bridge_env_capture_from_shell",
        )
        assert result.returncode == 0, result.stderr
        assert _read(env_file)["AGENT_HOST"] == "real.lan"

    def test_append_key_dedupes_existing_key(self, sandbox: dict) -> None:
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("MESHTASTIC_AGENT_KEY=old-key\n")
        result = _run_in_sandbox(
            {**sandbox, "MESHTASTIC_AGENT_KEY": "new-key"},
            f"{self.SOURCE}; bridge_env_append_key \"$MESHTASTIC_AGENT_KEY\"",
        )
        assert result.returncode == 0, result.stderr
        env = _read(env_file)
        assert env["MESHTASTIC_AGENT_KEY"] == "new-key"
        # Exactly one MESHTASTIC_AGENT_KEY line in the file
        lines = [
            ln for ln in Path(env_file).read_text().splitlines()
            if ln.startswith("MESHTASTIC_AGENT_KEY=")
        ]
        assert len(lines) == 1

    def test_append_key_refuses_empty(self, sandbox: dict) -> None:
        """Appending an empty key is a no-op (the library returns 1). The
        installer handles this by leaving the placeholder line untouched."""
        env_file = sandbox["BRIDGE_ENV_FILE"]
        Path(env_file).write_text("placeholder content\n")
        result = _run_in_sandbox(
            sandbox,
            f"{self.SOURCE}; bridge_env_append_key \"\"; echo \"rc=$?\"",
        )
        assert "rc=1" in result.stdout
        assert "placeholder content" in Path(env_file).read_text()


class TestInstallShReferencesLibrary:
    """
    Regression checks: install.sh must source and use the library.
    A refactor that bypasses it (and re-inlines the logic) would re-introduce
    the bash -c array bug, so these static checks anchor the contract.
    """

    def test_install_sh_sources_the_library(self) -> None:
        text = INSTALL.read_text()
        assert 'source "$SCRIPT_DIR/lib/env_capture.sh"' in text, (
            "install.sh must source deploy/lib/env_capture.sh "
            "(regression: the inline logic was removed in favour of this library)"
        )

    def test_install_sh_calls_each_library_function(self) -> None:
        text = INSTALL.read_text()
        for fn in ("bridge_env_seed", "bridge_env_capture_from_shell", "bridge_env_append_key"):
            assert fn in text, f"install.sh must call {fn}"

    def test_install_sh_does_not_re_inline_the_old_capture_loop(self) -> None:
        """install.sh should call bridge_env_capture_from_shell from the
        library, not re-inline the same loop. If someone re-inlines the
        `for name in AGENT_HOST AGENT_PORT ...` block in install.sh, that
        logic will drift from the library and the integration tests will
        silently bypass the library contract.
        """
        text = INSTALL.read_text()
        assert "for name in AGENT_HOST AGENT_PORT" not in text, (
            "install.sh re-introduced an inline env-capture loop; "
            "remove it and rely on bridge_env_capture_from_shell"
        )

    def test_library_does_not_rely_on_exported_arrays(self) -> None:
        """Code-style preference: hardcode the captured variable names in the
        function body, do not iterate an exported array. Modern bash does
        propagate exported arrays alongside `export -f`'d functions, but
        older bash (4.2 and earlier) and edge cases around `set -a` /
        subshell boundaries have historically been inconsistent. The
        hardcoded form is portable, debuggable, and never the cause of a
        silent failure. If someone rewrites the loop over an array, this
        test fires."""
        text = LIB.read_text()
        assert "${_BRIDGE_CAPTURED_VARS[@]}" not in text, (
            "env_capture.sh must not iterate an exported array — use a "
            "hardcoded `for name in MESHTASTIC_AGENT_KEY AGENT_HOST ...` "
            "list for portability across bash versions"
        )

    def test_config_yaml_and_library_agree_on_variable_names(self) -> None:
        """If someone adds a new ${VAR} to config.yaml but forgets to update
        the library's hardcoded list, the var silently won't be captured.
        Cross-check the two to catch drift."""
        import re

        config_text = (REPO_ROOT / "config.yaml").read_text()
        # Placeholders are ${NAME} or ${NAME:-default}. Names must be UPPER_SNAKE_CASE.
        placeholders = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", config_text))

        lib_text = LIB.read_text()
        # The hardcoded list lives in the for-loop in bridge_env_capture_from_shell.
        # Extract names from the `for name in ...` line.
        m = re.search(r"for name in ([A-Z_ ]+); do", lib_text)
        assert m, "bridge_env_capture_from_shell must have a `for name in ...` loop"
        lib_names = set(m.group(1).split())

        # Every placeholder in config.yaml that is a "captured" var (i.e. read
        # from the installer's shell) must appear in the library. We allow
        # additional library names (e.g. MESHTASTIC_AGENT_KEY which the
        # config doesn't reference directly — only the api_key_env does).
        missing = placeholders - lib_names
        assert not missing, (
            f"config.yaml references ${{{', '.join(sorted(missing))}}} but the "
            "installer's env-capture library doesn't list it. Update "
            "deploy/lib/env_capture.sh so the operator can pass it on the "
            "command line."
        )