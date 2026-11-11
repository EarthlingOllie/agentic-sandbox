"""Per-set TLS: a ``*.<set>.test`` leaf certificate issued by the machine's mkcert CA.

One mkcert CA serves the whole machine (``sudo sandbox setup`` installs it with
``mkcert -install``, so the host trusts it). Each set that has a web app gets its own
wildcard leaf in ``<set dir>/tls/`` (``cert.pem``, ``key.pem``), issued on the first ``up``
and reused while it still verifies against the CA for the set's hostnames and is not about
to expire. Containers are given the CA's ``rootCA.pem`` only, never ``rootCA-key.pem``, so
nothing inside a set can mint certificates the host trusts.

mkcert is driven through its CLI (:class:`SubprocessMkcert`); unit tests substitute
``tests.fakes_certs.FakeMkcert``. Validity is checked in-process with a TLS handshake over
memory buffers (stdlib ``ssl``), which checks the chain, the dates, the hostnames and that
the key matches the certificate in one go.
"""

import asyncio
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final, Protocol

from sandbox.compose.network import TLD
from sandbox.errors import SandboxError

ROOT_CA_FILENAME: Final = "rootCA.pem"
TLS_DIRNAME: Final = "tls"
CERT_FILENAME: Final = "cert.pem"
KEY_FILENAME: Final = "key.pem"
# A certificate expiring sooner than this is reissued rather than reused.
REISSUE_MARGIN: Final = timedelta(days=7)
SETUP_HINT: Final = "run `sudo sandbox setup` (it installs the mkcert CA with `mkcert -install`)"


class Mkcert(Protocol):
    async def caroot(self) -> Path:
        """The directory holding the machine's CA (``mkcert -CAROOT``)."""
        ...

    async def issue(self, cert_file: Path, key_file: Path, names: Sequence[str]) -> None:
        """Issue a leaf certificate for ``names`` signed by the CA."""
        ...


class SubprocessMkcert:
    """Runs the real ``mkcert`` CLI (``$CAROOT`` selects the CA, as mkcert documents)."""

    async def _run(self, args: Sequence[str]) -> str:
        argv = ["mkcert", *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise SandboxError(
                f"mkcert is not installed or not on PATH; install it, then {SETUP_HINT}"
            ) from exc
        output, _ = await process.communicate()
        text = output.decode(errors="replace").strip()
        if process.returncode != 0:
            raise SandboxError(f"`{' '.join(argv)}` failed:\n{text}")
        return text

    async def caroot(self) -> Path:
        return Path(await self._run(["-CAROOT"]))

    async def issue(self, cert_file: Path, key_file: Path, names: Sequence[str]) -> None:
        await self._run(["-cert-file", str(cert_file), "-key-file", str(key_file), *names])


@dataclass(frozen=True)
class SetCertificate:
    directory: Path
    cert: Path
    key: Path
    # True when this `up` issued it; False when an earlier one's was still valid.
    issued: bool


def wildcard(set_name: str) -> str:
    return f"*.{set_name}.{TLD}"


async def root_ca(mkcert: Mkcert) -> Path:
    """The CA certificate containers trust, or fail naming the fix (setup not run)."""
    path = await mkcert.caroot() / ROOT_CA_FILENAME
    if not path.is_file():
        raise SandboxError(f"the mkcert CA certificate {path} is missing; {SETUP_HINT}")
    return path


async def ensure_certificate(
    mkcert: Mkcert, ca: Path, set_dir: Path, set_name: str, hostnames: Sequence[str]
) -> SetCertificate:
    """The set's leaf certificate: reused while valid for ``hostnames``, else (re)issued."""
    directory = set_dir / TLS_DIRNAME
    cert, key = directory / CERT_FILENAME, directory / KEY_FILENAME
    if certificate_valid(cert, key, ca, hostnames):
        return SetCertificate(directory, cert, key, issued=False)
    directory.mkdir(parents=True, exist_ok=True)
    await mkcert.issue(cert, key, [wildcard(set_name)])
    # The web app reads the key as whatever user its image runs as. It is a leaf for
    # `*.<set>.test` on loopback only; the CA key never leaves mkcert's CAROOT.
    key.chmod(0o644)
    if not certificate_valid(cert, key, ca, hostnames):
        raise SandboxError(
            f"mkcert issued {cert}, but it does not verify against {ca} for "
            f"{', '.join(hostnames)}; is $CAROOT the CA that setup installed?"
        )
    return SetCertificate(directory, cert, key, issued=True)


def certificate_valid(
    cert: Path, key: Path, ca: Path, hostnames: Sequence[str], now: float | None = None
) -> bool:
    """True if ``cert``/``key`` serve TLS that ``ca`` trusts for every hostname, and the
    certificate stays valid for at least :data:`REISSUE_MARGIN`."""
    if not hostnames or not (cert.is_file() and key.is_file()):
        return False
    deadline = (time.time() if now is None else now) + REISSUE_MARGIN.total_seconds()
    for hostname in hostnames:
        expires = _handshake(cert, key, ca, hostname)
        if expires is None or expires < deadline:
            return False
    return True


def _handshake(cert: Path, key: Path, ca: Path, hostname: str) -> float | None:
    """Run a TLS handshake in memory; the certificate's expiry if the client accepted it."""
    try:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.load_verify_locations(cafile=ca)
        to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_end = client.wrap_bio(to_client, to_server, server_hostname=hostname)
        server_end = server.wrap_bio(to_server, to_client, server_side=True)
        if not _complete(client_end, server_end):
            return None
        peer = client_end.getpeercert()
    except (ssl.SSLError, OSError, ValueError):
        return None
    not_after = peer.get("notAfter") if peer else None
    return ssl.cert_time_to_seconds(not_after) if isinstance(not_after, str) else None


def _complete(*ends: ssl.SSLObject) -> bool:
    """Step both ends until their handshakes finish (the buffers carry the bytes)."""
    pending = list(ends)
    for _ in range(10):
        for end in list(pending):
            try:
                end.do_handshake()
                pending.remove(end)
            except ssl.SSLWantReadError:
                continue
        if not pending:
            return True
    return False
