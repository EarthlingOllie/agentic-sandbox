"""A web app over HTTPS against real Docker and mkcert: trusted from the host and the agent,
at its own hostname and at the fixture's extra host (``admin``).

mkcert runs against a CA of this test's own (``$CAROOT`` in a temp dir, never installed in
the system store), so the host side trusts it explicitly with ``--cacert``; inside the set
the agent trusts it through the mounted ``rootCA.pem``. ``--resolve`` stands in for the
hosts file (the integration config writes the hosts block to a temp file).
"""

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox.compose import project_name
from sandbox.compose.cli import SubprocessCompose
from sandbox.git import SubprocessGit
from tests.conftest import Env, invoke
from tests.integration.conftest import git, unique_set

pytestmark = pytest.mark.integration

for tool in ("docker", "mkcert", "curl"):
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed", allow_module_level=True)

FIXTURES = Path(__file__).parent.parent / "fixtures"
SET = unique_set("web")
PROJECT = project_name(SET)
HOST = f"web.{SET}.test"
# The fixture's extra host: same container, IP and certificate.
ADMIN = f"admin.{SET}.test"


def compose_down() -> None:
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "down", "--volumes", "--remove-orphans"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def caroot(env: Env, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = env.root / "caroot"
    monkeypatch.setenv("CAROOT", str(path))
    # Issuing anything creates the CA (without installing it anywhere).
    scratch = env.root / "bootstrap"
    scratch.mkdir()
    subprocess.run(
        [
            "mkcert",
            "-cert-file", str(scratch / "c.pem"),
            "-key-file", str(scratch / "k.pem"),
            "bootstrap.test",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    assert (path / "rootCA.pem").is_file()
    return path


@pytest.fixture
def web_repo(env: Env, caroot: Path) -> Iterator[Path]:
    path = env.cwd / "web"
    shutil.copytree(FIXTURES / "web-repo", path)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    compose_down()
    yield path
    compose_down()


def web_ip(output: str, host: str = HOST) -> str:
    match = re.search(rf"{re.escape(host)}  (\S+)  ", output)
    assert match, output
    return match.group(1)


def curl_from_host(ip: str, caroot: Path, host: str = HOST) -> dict[str, object]:
    output = subprocess.run(
        [
            "curl", "-sS", "--fail", "--noproxy", "*",
            "--cacert", str(caroot / "rootCA.pem"),
            "--resolve", f"{host}:443:{ip}",
            f"https://{host}/",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout  # fmt: skip
    return json.loads(output)


def in_agent(*command: str) -> str:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "exec", "-T", "agent", *command],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_web_app_is_served_over_trusted_https(env: Env, web_repo: Path, caroot: Path) -> None:
    result = invoke([SET, "up", "--repos", "web"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    ip = web_ip(result.stdout)
    clone = str(env.set_dir(SET) / "web")
    expected = {"user": 65534, "WORKTREE": clone, "APP_URL": f"https://{HOST}"}
    # From the host: the leased IP, port 443, the set's certificate (no -k).
    assert curl_from_host(ip, caroot) == expected
    # From the agent: the set hostname on the set network, trusting the mounted CA.
    assert json.loads(in_agent("curl", "-sS", "--fail", f"https://{HOST}/")) == expected
    # The extra host reaches the same app, over the same certificate, from both sides.
    assert f"{ADMIN}  {ip}  " in result.stdout
    assert curl_from_host(ip, caroot, ADMIN) == expected
    assert json.loads(in_agent("curl", "-sS", "--fail", f"https://{ADMIN}/")) == expected
    # The CA certificate is mounted; its key is not.
    assert "rootCA.pem" in in_agent("ls", "/sandbox/ca")
    assert "rootCA-key.pem" not in in_agent("ls", "/sandbox/ca")

    # A later `up` reuses the still-valid certificate.
    cert = env.set_dir(SET) / "tls" / "cert.pem"
    before = cert.read_bytes()
    assert invoke([SET, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    again = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())

    assert again.exit_code == 0, again.output
    assert cert.read_bytes() == before
    assert curl_from_host(web_ip(again.stdout), caroot) == expected


def test_web_app_is_served_under_a_non_default_domain(
    env: Env, web_repo: Path, caroot: Path
) -> None:
    """With ``domain: sandbox.test`` the app, its extra host and the set's certificate all
    move to ``<name>.<set>.sandbox.test``; the certificate issued for ``.test`` is replaced."""
    first = invoke([SET, "up", "--repos", "web"], SubprocessGit(), SubprocessCompose())
    assert first.exit_code == 0, first.output
    cert = env.set_dir(SET) / "tls" / "cert.pem"
    issued_for_test = cert.read_bytes()
    assert invoke([SET, "down"], SubprocessGit(), SubprocessCompose()).exit_code == 0
    with env.config_file.open("a") as config:
        config.write("domain: sandbox.test\n")
    host, admin = f"web.{SET}.sandbox.test", f"admin.{SET}.sandbox.test"

    result = invoke([SET, "up"], SubprocessGit(), SubprocessCompose())

    assert result.exit_code == 0, result.output
    ip = web_ip(result.stdout, host)
    expected = {
        "user": 65534,
        "WORKTREE": str(env.set_dir(SET) / "web"),
        "APP_URL": f"https://{host}",
    }
    # A new certificate, for the new domain: the handshakes below verify the hostname.
    assert cert.read_bytes() != issued_for_test
    # From the host, at the hostname under the configured domain (no -k).
    assert curl_from_host(ip, caroot, host) == expected
    assert curl_from_host(ip, caroot, admin) == expected
    # From the agent: the set network resolves the same names.
    assert json.loads(in_agent("curl", "-sS", "--fail", f"https://{host}/")) == expected
    assert json.loads(in_agent("curl", "-sS", "--fail", f"https://{admin}/")) == expected
    # The hosts block lists the set's names under the domain, and only those.
    hosts = env.hosts_file.read_text()
    assert f"{ip}\t{host}\n" in hosts
    assert f"{ip}\t{admin}\n" in hosts
    assert f"web.{SET}.test" not in hosts
