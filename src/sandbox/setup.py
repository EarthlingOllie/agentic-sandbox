"""``sudo sandbox setup``: install, once, everything sandbox needs root for.

After it, the everyday ``sandbox`` runs unprivileged:

- **Hosts helper.** A root-owned copy of ``sandbox-hosts`` at ``/usr/local/bin``, run by the
  system's Python with ``-I -S`` from a root-owned bundle of the stdlib-only modules it needs
  (``/usr/local/lib/sandbox-hosts``), so nothing the user can write (a virtualenv,
  site-packages, ``PYTHONPATH``) runs as root. A NOPASSWD sudoers rule lets the invoking user
  run exactly that file (checked with ``visudo -c`` before it is put in place).
- **macOS loopback pool.** A launchd job that adds ``lo0`` aliases for every address of the
  pool's first ``--sets`` /24 blocks at boot (run once now as well). Linux needs none: the
  whole of 127.0.0.0/8 is local.
- **mkcert CA.** ``mkcert -install``, run as ``SUDO_USER`` (never as root), so the CA lands
  in that user's CAROOT and their browser trusts it.

Before installing anything it refuses if the configured IP range is already in use (an
interface address, or a hosts-file entry outside sandbox's own blocks). ``--uninstall``
removes the helper, the sudoers rule and the launchd job (and its aliases), and leaves the
mkcert CA, printing the ``mkcert -uninstall`` command instead. Every system path is taken
relative to :attr:`Host.root`, and every command goes through :attr:`Host.run`, so tests run
setup against a temp directory with a fake runner.
"""

import os
import plistlib
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Final

from sandbox.config import Config, load_config
from sandbox.errors import SandboxError
from sandbox.hosts_block import HELPER_PATH, SYSTEM_HOSTS_FILE, outside_blocks
from sandbox.leases import BLOCK_PREFIX
from sandbox.platforms import Host

SETUP_COMMAND: Final = "sudo sandbox setup"
DEFAULT_SETS: Final = 16
HELPER_LIB: Final = Path("/usr/local/lib/sandbox-hosts")
SUDOERS_PATH: Final = Path("/etc/sudoers.d/sandbox-hosts")
LAUNCHD_LABEL: Final = "sandbox.loopback-aliases"
LAUNCHD_PLIST: Final = Path(f"/Library/LaunchDaemons/{LAUNCHD_LABEL}.plist")
IFCONFIG: Final = "/sbin/ifconfig"
# The helper and everything it imports: standard library only (see sandbox.hosts_block).
HELPER_MODULES: Final = (
    "__init__.py",
    "errors.py",
    "names.py",
    "locking.py",
    "hosts_block.py",
    "hosts_helper.py",
)
_USER_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_INTERFACE_LINE: Final = re.compile(r"^(?:\d+:\s+)?([^\s:]+):?\s")
_INET: Final = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)")
_SHOWN_CONFLICTS: Final = 5


@dataclass(frozen=True)
class SetupResult:
    user: str
    lines: list[str]


async def install(host: Host, sets: int = DEFAULT_SETS) -> SetupResult:
    """Install the helper, its sudoers rule, the macOS alias pool and the mkcert CA."""
    user = _require_root(host)
    config = _user_config(host, user)
    blocks = _pool_blocks(config.ip_pool, sets)
    mkcert = _find_mkcert(host)
    await _refuse_used_range(host, config.ip_pool)

    await _check_python(host)
    _install_helper(host)
    await _install_sudoers(host, user)
    lines = [
        f"hosts helper: {HELPER_PATH} (root-owned), runnable by {user} without a password "
        f"(sudoers rule {SUDOERS_PATH}, this helper only)"
    ]
    if host.is_macos:
        await _install_alias_pool(host, config.ip_pool, blocks)
        lines.append(
            f"loopback: lo0 aliases for {len(blocks)} /24 blocks ({blocks[0]} to {blocks[-1]}), "
            f"added now and at boot by launchd job {LAUNCHD_PLIST}"
        )
    else:
        lines.append("loopback: no loopback setup needed on Linux (127.0.0.0/8 is local)")
    await _mkcert_install(host, user, mkcert)
    lines.append(f"mkcert: CA installed for {user} (`mkcert -install` as {user})")
    return SetupResult(user, lines)


async def uninstall(host: Host) -> SetupResult:
    """Remove the helper, its sudoers rule and the launchd job; keep the mkcert CA."""
    user = host.env.get("SUDO_USER", "")
    _require_euid_root(host)
    lines: list[str] = []
    for path in (HELPER_PATH, SUDOERS_PATH):
        if _remove(host.path(path)):
            lines.append(f"removed {path}")
    if host.path(HELPER_LIB).exists():
        shutil.rmtree(host.path(HELPER_LIB))
        lines.append(f"removed {HELPER_LIB}/")
    if host.is_macos:
        await host.run(["launchctl", "bootout", f"system/{LAUNCHD_LABEL}"])
        if _remove(host.path(LAUNCHD_PLIST)):
            lines.append(f"removed {LAUNCHD_PLIST}")
        config = _user_config(host, user) if _valid_user(user) else Config()
        aliased = _blocks_with_aliases(config.ip_pool, await _lo0_ips(host))
        if aliased:
            await _run_checked(host, ["/bin/sh", "-c", unalias_script(aliased)])
            lines.append(f"removed the lo0 aliases of {len(aliased)} /24 blocks")
    lines.append(
        "the mkcert CA was left installed (other tools may rely on it); "
        "to remove it, run as yourself: mkcert -uninstall"
    )
    return SetupResult(user, lines)


# --- checks ----------------------------------------------------------------------------------


def _require_euid_root(host: Host) -> None:
    if host.euid != 0:
        raise SandboxError(f"setup needs root: run `{SETUP_COMMAND}`")


def _valid_user(user: str) -> bool:
    return bool(user) and user != "root" and bool(_USER_NAME.fullmatch(user))


def _require_root(host: Host) -> str:
    """The invoking (non-root) user; setup itself must run as root."""
    _require_euid_root(host)
    user = host.env.get("SUDO_USER", "")
    if not _valid_user(user):
        raise SandboxError(
            "setup installs things for the user who runs it: run it with sudo from your own "
            f"account (`{SETUP_COMMAND}`), not from a root shell"
        )
    return user


def _user_config(host: Host, user: str) -> Config:
    """The invoking user's config (sudo resets HOME, so it is found through their account)."""
    named = host.env.get("SANDBOX_CONFIG")
    if named:
        return load_config(Path(named))
    home = host.homes(user)
    if home is None:
        raise SandboxError(f"user {user!r} has no home directory")
    path = home / ".config" / "sandbox" / "config.yaml"
    return load_config(path) if path.is_file() else Config()


def _pool_blocks(pool: IPv4Network, sets: int) -> list[IPv4Network]:
    available = list(pool.subnets(new_prefix=BLOCK_PREFIX))
    if sets < 1:
        raise SandboxError("--sets must be at least 1")
    if sets > len(available):
        raise SandboxError(
            f"--sets must be at most {len(available)}: ip_pool {pool} holds "
            f"{len(available)} /{BLOCK_PREFIX} blocks"
        )
    return available[:sets]


def _find_mkcert(host: Host) -> str:
    found = shutil.which("mkcert", path=host.env.get("PATH", ""))
    if found is None:
        raise SandboxError(
            "mkcert is not installed or not on PATH; install it (https://github.com/FiloSottile/"
            "mkcert), then rerun setup. If it is installed, sudo may have reset PATH: run "
            '`sudo env "PATH=$PATH" sandbox setup`'
        )
    return found


async def _check_python(host: Host) -> None:
    status, output = await host.run(
        [str(host.python), "-I", "-S", "-c", "import sys; sys.exit(sys.version_info < (3, 9))"]
    )
    if status != 0:
        raise SandboxError(
            f"the hosts helper runs under {host.python}, which must be Python 3.9 or newer "
            f"(install the system's python3){': ' + output if output else ''}"
        )


async def _refuse_used_range(host: Host, pool: IPv4Network) -> None:
    """Refuse if anything but sandbox already uses an address in ``pool``."""
    conflicts: list[str] = []
    ours = set[IPv4Address]()
    if host.is_macos and host.path(LAUNCHD_PLIST).exists():
        ours = await _lo0_ips(host)  # an earlier setup's aliases
    argv = ["ifconfig", "-a"] if host.is_macos else ["ip", "-4", "-o", "addr", "show"]
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"could not list interface addresses: `{' '.join(argv)}`: {output}")
    for ip, interface in interface_addresses(output):
        if ip in pool and ip not in ours:
            conflicts.append(f"{ip} ({interface})")
    try:
        hosts_text = host.path(SYSTEM_HOSTS_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        hosts_text = ""
    for line in outside_blocks(hosts_text):
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            ip = IPv4Address(fields[0])
        except ValueError:
            continue
        if ip in pool:
            conflicts.append(f"{ip} ({SYSTEM_HOSTS_FILE}: {' '.join(fields[1:])})")
    if conflicts:
        shown = ", ".join(conflicts[:_SHOWN_CONFLICTS])
        more = len(conflicts) - _SHOWN_CONFLICTS
        raise SandboxError(
            f"the configured IP range {pool} is already in use: {shown}"
            + (f" and {more} more" if more > 0 else "")
            + "\nsandbox will not take over addresses something else relies on: free them, "
            "or set another `ip_pool` in your config, then rerun setup"
        )


def interface_addresses(output: str) -> list[tuple[IPv4Address, str]]:
    """``(address, interface)`` pairs from ``ip -4 -o addr show`` or ``ifconfig -a`` output."""
    found: list[tuple[IPv4Address, str]] = []
    interface = "?"
    for line in output.splitlines():
        if line and not line[0].isspace():
            match = _INTERFACE_LINE.match(line)
            if match:
                interface = match.group(1)
        found += [(IPv4Address(ip), interface) for ip in _INET.findall(line)]
    return found


# --- installing ------------------------------------------------------------------------------


def _write(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` atomically, with ``mode`` (root-owned, as setup runs as root)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.sandbox-tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as file:
        file.write(data)
    temporary.chmod(mode)
    temporary.replace(path)


def _remove(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def helper_script(python: Path, lib: Path) -> str:
    return (
        f"#!{python} -IS\n"
        '"""sandbox-hosts: edits /etc/hosts for sandbox (installed by `sudo sandbox setup`)."""\n'
        "import sys\n"
        f"sys.path.insert(0, {str(lib)!r})\n"
        "from sandbox.hosts_helper import main\n"
        "main()\n"
    )


def _install_helper(host: Host) -> None:
    source = Path(__file__).parent
    lib = host.path(HELPER_LIB)
    if lib.exists():
        shutil.rmtree(lib)
    package = lib / "sandbox"
    package.mkdir(parents=True, mode=0o755)
    lib.chmod(0o755)
    package.chmod(0o755)
    for name in HELPER_MODULES:
        _write(package / name, (source / name).read_bytes(), 0o644)
    # The script names its bundle by its real (installed) location.
    _write(host.path(HELPER_PATH), helper_script(host.python, lib).encode(), 0o755)


def sudoers_rule(user: str) -> str:
    return (
        "# Installed by `sudo sandbox setup`: lets this user run the sandbox hosts helper\n"
        "# without a password. Remove with `sudo sandbox setup --uninstall`.\n"
        f"{user} ALL=(root) NOPASSWD: {HELPER_PATH}\n"
    )


async def _install_sudoers(host: Host, user: str) -> None:
    target = host.path(SUDOERS_PATH)
    # A name with a dot: sudo's includedir skips it, so a half-written rule is never live.
    candidate = target.with_name(f"{target.name}.new")
    _write(candidate, sudoers_rule(user).encode(), 0o440)
    status, output = await host.run(["visudo", "-cf", str(candidate)])
    if status != 0:
        candidate.unlink()
        raise SandboxError(f"visudo rejected the sudoers rule for {HELPER_PATH}: {output}")
    candidate.replace(target)


def _prefixes(blocks: Sequence[IPv4Network]) -> str:
    return " ".join(str(block.network_address).rsplit(".", 1)[0] for block in blocks)


def alias_script(blocks: Sequence[IPv4Network]) -> str:
    """A ``/bin/sh`` script adding an lo0 alias for every host address of ``blocks``."""
    return (
        f"for p in {_prefixes(blocks)} ; do h=1; while [ $h -le 254 ]; do "
        f'{IFCONFIG} lo0 alias "$p.$h" up; h=$((h + 1)); done; done'
    )


def unalias_script(blocks: Sequence[IPv4Network]) -> str:
    """A ``/bin/sh`` script removing every lo0 alias of ``blocks`` (missing ones are fine)."""
    return (
        f"for p in {_prefixes(blocks)} ; do h=1; while [ $h -le 254 ]; do "
        f'{IFCONFIG} lo0 -alias "$p.$h" 2>/dev/null; h=$((h + 1)); done; done; true'
    )


def launchd_plist(blocks: Sequence[IPv4Network]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": ["/bin/sh", "-c", alias_script(blocks)],
            "RunAtLoad": True,
        }
    )


async def _lo0_ips(host: Host) -> set[IPv4Address]:
    status, output = await host.run(["ifconfig", "lo0"])
    if status != 0:
        raise SandboxError(f"could not read the lo0 aliases: `ifconfig lo0` failed: {output}")
    return {IPv4Address(ip) for ip in _INET.findall(output)}


def _blocks_with_aliases(pool: IPv4Network, aliased: set[IPv4Address]) -> list[IPv4Network]:
    return [
        block
        for block in pool.subnets(new_prefix=BLOCK_PREFIX)
        if any(ip in block for ip in aliased)
    ]


async def _run_checked(host: Host, argv: list[str]) -> None:
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"`{' '.join(argv[:2])} ...` failed: {output}")


async def _install_alias_pool(host: Host, pool: IPv4Network, blocks: Sequence[IPv4Network]) -> None:
    stale = [
        block for block in _blocks_with_aliases(pool, await _lo0_ips(host)) if block not in blocks
    ]
    if stale:  # an earlier, larger pool: its extra blocks must not look leasable
        await _run_checked(host, ["/bin/sh", "-c", unalias_script(stale)])
    _write(host.path(LAUNCHD_PLIST), launchd_plist(blocks), 0o644)
    # launchd runs the job at every boot; run it now too, so the pool is live at once.
    await _run_checked(host, ["/bin/sh", "-c", alias_script(blocks)])


async def _mkcert_install(host: Host, user: str, mkcert: str) -> None:
    argv = ["sudo", "-u", user, "-H", "--", mkcert, "-install"]
    status, output = await host.run(argv)
    if status != 0:
        raise SandboxError(f"`mkcert -install` as {user} failed:\n{output}")
