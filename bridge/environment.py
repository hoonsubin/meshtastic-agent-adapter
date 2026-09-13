"""
Install-time environment handling for the Meshtastic bridge.

Two halves, both stdlib-only so ``deploy/install.sh`` can run them with the
system ``python3`` before the bridge venv exists:

  * ``${VAR}`` expansion — ``expand_env_vars`` is the single source of truth for
    the shell-style placeholder substitution (``${NAME}`` / ``${NAME:-default}``)
    used at runtime by ``bridge.config`` and by the installer's ``expand``
    subcommand.

  * ``.env`` file management — ``seed_env_file`` / ``capture_from_shell`` /
    ``set_agent_key`` build ``/etc/meshtastic-bridge/.env``. They replace the old
    bash library ``deploy/lib/env_capture.sh``.

Run the CLI as ``python3 bridge/environment.py <subcommand> ...``.
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# Matches ${UPPER_SNAKE_CASE} with an optional ${VAR:-default} fallback. The name
# has to start with an uppercase letter and contain only A-Z, 0-9 and underscore,
# which keeps it away from the .format() placeholders ({max_bytes}, {from_id}) in
# agent.system_prompt.
_ENV_VAR_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?::-(?P<default>[^{}]*))?\}"
)

# Single source of truth for the vars the installer may pull from the shell into
# the .env file. The agent key is handled separately by set_agent_key (it has
# placeholder + warning behaviour); everything else is captured generically.
CAPTURED_VARS = ("AGENT_HOST", "AGENT_PORT", "AGENT_MODEL", "SERIAL_PORT")
KEY_VAR = "MESHTASTIC_AGENT_KEY"


def expand_env_vars(text: str, environ=None) -> str:
    """
    Replace ${VAR_NAME} (or ${VAR_NAME:-default}) with the env value.

    - Unset variable, no default: left as the literal ``${VAR_NAME}`` so a typo
      or missing env file fails loudly later instead of silently becoming "".
    - Unset variable, with default: substituted with the default.
    - Set variable (including empty string), with default: substituted with the
      empty value (matches shell ``:-`` semantics — set-but-empty is a real
      value, not an unset state).
    """
    if not text:
        return text
    if environ is None:
        environ = os.environ

    def _sub(m: "re.Match[str]") -> str:
        name = m.group("name")
        default = m.group("default")
        value = environ.get(name)
        if value is None and default is not None:
            return default
        if value is None:
            return m.group(0)
        return value

    return _ENV_VAR_PATTERN.sub(_sub, text)


def _read_lines(path: Path) -> list:
    if not path.exists():
        return []
    return path.read_text().splitlines()


def _write_lines(path: Path, lines) -> None:
    """Atomically replace ``path`` with ``lines``, preserving its mode."""
    mode = 0o600
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines))
            if lines:
                f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def seed_env_file(path, template) -> str:
    """Copy ``template`` to ``path`` if it does not exist. Returns a status line."""
    env_file = Path(path)
    if env_file.exists():
        return ""
    tpl = Path(template)
    if tpl.is_file():
        shutil.copy2(tpl, env_file)
        os.chmod(env_file, 0o600)
        return f"  Seeded {env_file} from {tpl}"
    env_file.write_text(f"# Meshtastic Bridge env file. See {template} in the repo.\n")
    os.chmod(env_file, 0o600)
    return f"  WARNING: {template} not found; created minimal {env_file}"


def upsert_env_file(path, name: str, value: str) -> None:
    """Set ``name=value`` in ``path``, removing any prior line for ``name``."""
    env_file = Path(path)
    prefix = f"{name}="
    kept = [line for line in _read_lines(env_file) if not line.startswith(prefix)]
    kept.append(f"{name}={value}")
    _write_lines(env_file, kept)


def capture_from_shell(path, environ=None, names=CAPTURED_VARS) -> list:
    """Write non-empty shell vars from ``names`` into ``path``. Shell wins."""
    if environ is None:
        environ = os.environ
    env_file = Path(path)
    if not env_file.exists():
        return []
    written = []
    for name in names:
        value = environ.get(name, "")
        if value:  # empty string == unset, matching the old [ -n "$value" ] guard
            upsert_env_file(env_file, name, value)
            written.append(name)
    return written


def set_agent_key(path, environ=None) -> str:
    """Write/preserve/placeholder MESHTASTIC_AGENT_KEY. Returns a status line."""
    if environ is None:
        environ = os.environ
    key = environ.get(KEY_VAR, "")
    env_file = Path(path)

    if key:
        upsert_env_file(env_file, KEY_VAR, key)
        return f"  Wrote key from ${KEY_VAR} to {env_file}"

    has_key = any(ln.startswith(f"{KEY_VAR}=") for ln in _read_lines(env_file))
    if has_key:
        return f"  Existing key in {env_file} preserved"

    lines = _read_lines(env_file) + [
        "",
        "# Agent API key for the Hermes API server (Bearer token).",
        f"# {KEY_VAR}=<paste-key-here>",
    ]
    _write_lines(env_file, lines)
    return (
        f"  WARNING: ${KEY_VAR} was not provided. Placeholder written to {env_file}.\n"
        "           Set the key, then: sudo systemctl restart meshtastic-bridge"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="environment",
        description="Expand ${VAR} placeholders and manage the bridge .env file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("expand", help="Expand ${VAR} from stdin to stdout.")

    seed = sub.add_parser("seed", help="Seed the env file from a template if absent.")
    seed.add_argument("--file", required=True)
    seed.add_argument("--template", required=True)

    capture = sub.add_parser("capture", help="Write shell vars into the env file.")
    capture.add_argument("--file", required=True)

    set_key = sub.add_parser(
        "set-key", help="Write/preserve/placeholder the agent API key."
    )
    set_key.add_argument("--file", required=True)

    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "expand":
        sys.stdout.write(expand_env_vars(sys.stdin.read()))
        return 0
    if args.command == "seed":
        message = seed_env_file(args.file, args.template)
        if message:
            print(message)
        return 0
    if args.command == "capture":
        capture_from_shell(args.file)
        return 0
    if args.command == "set-key":
        print(set_agent_key(args.file))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
