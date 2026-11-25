"""The resolver's bundled definition: built here under a content-addressed tag, never pulled."""

import shutil
from pathlib import Path

import pytest

from sandbox.compose import resolver
from tests.fakes_compose import load_compose_yaml


def test_the_image_is_built_from_the_bundled_directory_and_never_pulled() -> None:
    service = load_compose_yaml(resolver.COMPOSE_FILE)["services"]["resolver"]

    assert service["build"] == {"context": "."}  # the directory the compose file is in
    assert (resolver.COMPOSE_FILE.parent / "Dockerfile").is_file()
    assert service["pull_policy"] == "never"
    assert service["image"].startswith("${SANDBOX_RESOLVER_IMAGE")
    assert resolver.resolver_environment("test")["SANDBOX_RESOLVER_IMAGE"] == (
        resolver.resolver_image()
    )


def test_the_image_tag_follows_the_content_of_the_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = resolver.resolver_image()
    copy = tmp_path / "resolver"
    shutil.copytree(resolver.RESOLVER_DIR, copy)
    monkeypatch.setattr(resolver, "RESOLVER_DIR", copy)

    assert resolver.resolver_image() == bundled  # same content, wherever it is installed
    with (copy / "dnsmasq.conf").open("a") as conf:
        conf.write("# edited\n")
    edited = resolver.resolver_image()

    assert edited != bundled
    assert edited.startswith("sandbox-resolver:")


def test_the_resolver_forwards_only_the_domain_it_is_started_for() -> None:
    service = load_compose_yaml(resolver.COMPOSE_FILE)["services"]["resolver"]

    assert service["command"] == ["--server=/${SANDBOX_RESOLVER_DOMAIN:?set by sandbox}/127.0.0.11"]
    assert resolver.resolver_environment("sandbox.example")["SANDBOX_RESOLVER_DOMAIN"] == (
        "sandbox.example"
    )
    settings = [
        line
        for line in (resolver.RESOLVER_DIR / "dnsmasq.conf").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    # As proven by the prototype (the spec's Implementation Decisions): no other upstream,
    # caching off, a 1-second answer TTL. The last two are the hardening's: an unprivileged
    # port for a process that is not root, and no pid file on a read-only filesystem.
    assert settings == [
        "no-resolv",
        "no-hosts",
        "cache-size=0",
        "max-ttl=1",
        "bind-dynamic",
        "except-interface=lo",
        "port=5353",
        "pid-file",
    ]


def test_the_resolver_is_locked_down() -> None:
    service = load_compose_yaml(resolver.COMPOSE_FILE)["services"]["resolver"]

    # Every capability dropped. Binding its port needs none: it listens on an unprivileged
    # one, so nothing is added back.
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert "port=5353" in (resolver.RESOLVER_DIR / "dnsmasq.conf").read_text().splitlines()
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["read_only"] is True
    # Its configuration is in the image: nothing of the machine is mounted, the Docker
    # socket least of all.
    assert "volumes" not in service
    assert "tmpfs" not in service
    assert "privileged" not in service
    assert "network_mode" not in service
    # Not root: a numeric user, so it holds whatever the image's passwd file says.
    assert service["user"] == "65534:65534"


def test_the_resolver_is_published_on_the_machines_loopback_only_on_a_port_that_is_not_53() -> None:
    service = load_compose_yaml(resolver.COMPOSE_FILE)["services"]["resolver"]

    assert resolver.RESOLVER_ADDRESS == "127.0.0.1"
    assert resolver.RESOLVER_PORT == 15353
    assert resolver.resolver_environment("test")["SANDBOX_RESOLVER_PORT"] == "15353"
    assert service["ports"] == [
        "127.0.0.1:${SANDBOX_RESOLVER_PORT:?set by sandbox}:5353/udp",
        "127.0.0.1:${SANDBOX_RESOLVER_PORT:?set by sandbox}:5353/tcp",
    ]


def test_a_name_docker_does_not_know_has_no_upstream_to_go_to() -> None:
    service = load_compose_yaml(resolver.COMPOSE_FILE)["services"]["resolver"]

    # Docker's DNS forwards what it cannot answer to the container's DNS servers: the one
    # given is the container's own loopback, where the resolver does not listen.
    assert service["dns"] == ["127.0.0.1"]
    conf = (resolver.RESOLVER_DIR / "dnsmasq.conf").read_text().splitlines()
    assert "no-resolv" in conf
    assert "except-interface=lo" in conf
    assert not [line for line in conf if line.startswith("server=")]
