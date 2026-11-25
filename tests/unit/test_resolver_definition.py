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
    # caching off, a 1-second answer TTL.
    assert settings == [
        "no-resolv",
        "no-hosts",
        "cache-size=0",
        "max-ttl=1",
        "bind-dynamic",
        "except-interface=lo",
    ]
