"""What an earlier version of sandbox installed on the machine, and its removal.

Before set hostnames were answered by the resolver, sandbox gave every set loopback
addresses and wrote its hostnames into ``/etc/hosts`` (see
``docs/adr/0001-set-hostnames-resolve-to-container-addresses.md``). ``sudo sandbox setup``
of that version installed, as root, things the present one never uses:

- a hosts helper at ``/usr/local/bin/sandbox-hosts`` with its module bundle and lock file;
- a sudoers rule letting the developer run that helper without a password;
- on macOS, a launchd job adding ``lo0`` aliases at boot, and the aliases themselves;

and every ``up`` left a ``# sandbox:<set>`` block in ``/etc/hosts``.

:func:`remove_leftovers` removes whatever of that is still there, so that nothing
privileged lingers after an upgrade; ``setup`` and ``setup --uninstall`` both call it. It is
the only code that knows these paths. A machine that never ran the earlier version is not
touched: no command runs and no file is rewritten.

Two things are left alone on purpose. A hosts block whose end line is missing is reported
and kept, since the lines after it may be the developer's own. And the aliases are taken
from the launchd job itself, the only record of which ones it added: when the job cannot be
read, or names anything but loopback /24 blocks, only the job is removed (nothing from it
reaches a root shell) and the aliases go at the next restart.
"""

import plistlib
import re
import shutil
from collections.abc import Sequence
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any, Final, cast

from sandbox.platforms import Host

HOSTS_FILE: Final = Path("/etc/hosts")
HELPER: Final = Path("/usr/local/bin/sandbox-hosts")
HELPER_BUNDLE: Final = Path("/usr/local/lib/sandbox-hosts")
HELPER_LOCK: Final = Path("/var/run/sandbox-hosts.lock")
SUDOERS_RULE: Final = Path("/etc/sudoers.d/sandbox-hosts")
ALIAS_JOB_LABEL: Final = "sandbox.loopback-aliases"
ALIAS_JOB: Final = Path(f"/Library/LaunchDaemons/{ALIAS_JOB_LABEL}.plist")
IFCONFIG: Final = "/sbin/ifconfig"
LOOPBACK: Final = IPv4Network("127.0.0.0/8")
WHY: Final = "(an earlier version of sandbox installed it; nothing uses it now)"

_BLOCK_START: Final = re.compile(r"# sandbox:(\S+)")
# The job's script: `for p in 127.42.0 127.42.1 ; do ...`, one `a.b.c` per aliased /24.
_ALIASED_PREFIXES: Final = re.compile(r"^for p in ([^;]*) ; do ")
_PREFIX: Final = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")


async def remove_leftovers(host: Host) -> list[str]:
    """Remove what the earlier version left on ``host``; one line per thing removed."""
    lines: list[str] = []
    for path in (HELPER, SUDOERS_RULE):
        if _remove(host.path(path)):
            lines.append(f"removed {path} {WHY}")
    if host.path(HELPER_BUNDLE).exists():
        shutil.rmtree(host.path(HELPER_BUNDLE))
        lines.append(f"removed {HELPER_BUNDLE}/ {WHY}")
    _remove(host.path(HELPER_LOCK))
    if host.is_macos and host.path(ALIAS_JOB).exists():
        lines += await _remove_alias_job(host)
    lines += _remove_hosts_blocks(host.path(HOSTS_FILE))
    return lines


def _remove(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# --- the macOS alias job -----------------------------------------------------------------


async def _remove_alias_job(host: Host) -> list[str]:
    job = host.path(ALIAS_JOB)
    blocks = aliased_blocks(job.read_bytes())
    # Not loaded is fine: the file is what brings the job back at boot.
    await host.run(["launchctl", "bootout", f"system/{ALIAS_JOB_LABEL}"])
    job.unlink()
    lines = [f"removed {ALIAS_JOB} {WHY}"]
    if not blocks:
        lines.append(
            "could not tell from that job which lo0 aliases it added; they are gone after "
            "the next restart"
        )
        return lines
    status, output = await host.run(["/bin/sh", "-c", unalias_script(blocks)])
    if status == 0:
        lines.append(f"removed the lo0 aliases of {len(blocks)} /24 blocks")
    else:
        lines.append(
            f"could not remove the lo0 aliases ({output or f'exit status {status}'}); they "
            "are gone after the next restart"
        )
    return lines


def aliased_blocks(job: bytes) -> list[IPv4Network]:
    """The loopback /24 blocks the launchd job aliases; empty unless every one of them is
    exactly that."""
    try:
        loaded: object = plistlib.loads(job)
    except Exception:  # plistlib raises whatever its parsers raise
        return []
    if not isinstance(loaded, dict):
        return []
    program: object = cast(dict[str, Any], loaded).get("ProgramArguments")
    if not isinstance(program, list):
        return []
    arguments = cast(list[object], program)
    if len(arguments) != 3 or not isinstance(arguments[2], str):
        return []
    match = _ALIASED_PREFIXES.match(arguments[2])
    if match is None:
        return []
    blocks: list[IPv4Network] = []
    for prefix in match.group(1).split():
        if not _PREFIX.fullmatch(prefix):
            return []
        try:
            block = IPv4Network(f"{prefix}.0/24")
        except ValueError:
            return []
        if not block.subnet_of(LOOPBACK):
            return []
        blocks.append(block)
    return blocks


def unalias_script(blocks: Sequence[IPv4Network]) -> str:
    """A ``/bin/sh`` script removing every lo0 alias of ``blocks`` (missing ones are fine)."""
    prefixes = " ".join(str(block.network_address).rsplit(".", 1)[0] for block in blocks)
    return (
        f"for p in {prefixes} ; do h=1; while [ $h -le 254 ]; do "
        f'{IFCONFIG} lo0 -alias "$p.$h" 2>/dev/null; h=$((h + 1)); done; done; true'
    )


# --- hosts blocks ------------------------------------------------------------------------


def _remove_hosts_blocks(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return []
    kept, removed, unterminated = without_blocks(text)
    lines: list[str] = []
    if removed:
        _replace_file(path, kept)
        lines.append(
            f"removed {removed} `# sandbox:` block{'' if removed == 1 else 's'} from "
            f"{HOSTS_FILE} (sets no longer write there)"
        )
    lines += [
        f"`# sandbox:{name}` in {HOSTS_FILE} has no `# end sandbox:{name}` line, so it was "
        "left alone; remove it by hand"
        for name in unterminated
    ]
    return lines


def without_blocks(text: str) -> tuple[str, int, list[str]]:
    """``text`` without its complete ``# sandbox:<set>`` blocks, how many there were, and
    the sets whose block has no end line (kept, with everything after it)."""
    lines = text.splitlines(keepends=True)
    stripped = [line.strip() for line in lines]
    kept: list[str] = []
    removed = 0
    unterminated: list[str] = []
    index = 0
    while index < len(lines):
        start = _BLOCK_START.fullmatch(stripped[index])
        if start is None:
            kept.append(lines[index])
            index += 1
            continue
        name = start.group(1)
        try:
            end = stripped.index(f"# end sandbox:{name}", index + 1)
        except ValueError:
            unterminated.append(name)
            kept += lines[index:]
            break
        removed += 1
        index = end + 1
    return "".join(kept), removed, unterminated


def _replace_file(path: Path, text: str) -> None:
    """Atomically replace ``path`` (keeping its mode), so nothing ever reads half a file.

    Falls back to writing in place where the file cannot be renamed over, e.g. a
    bind-mounted ``/etc/hosts`` inside a container.
    """
    temporary = path.with_name(f".{path.name}.sandbox-tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        shutil.copymode(path, temporary)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")
