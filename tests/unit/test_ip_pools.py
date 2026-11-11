"""The test-run IP pool claim: concurrent runs get disjoint pools; a closed run frees its own."""

from ipaddress import IPv4Network
from pathlib import Path

import pytest

from tests.ip_pools import CLAIMABLE_OCTETS, claim_run_pool


def test_concurrent_runs_claim_disjoint_pools(tmp_path: Path) -> None:
    first, first_handle = claim_run_pool(tmp_path)
    second, second_handle = claim_run_pool(tmp_path)

    assert first != second
    assert not first.overlaps(second)
    first_handle.close()
    second_handle.close()


def test_a_finished_run_frees_its_pool(tmp_path: Path) -> None:
    pool, handle = claim_run_pool(tmp_path)
    handle.close()

    again, again_handle = claim_run_pool(tmp_path)
    assert again == pool
    again_handle.close()


def test_never_the_default_pool_or_localhost() -> None:
    assert 42 not in CLAIMABLE_OCTETS
    assert {0, 1}.isdisjoint(CLAIMABLE_OCTETS)
    assert len(set(CLAIMABLE_OCTETS)) == len(CLAIMABLE_OCTETS)


def test_all_pools_claimed_is_an_error(tmp_path: Path) -> None:
    pool, handle = claim_run_pool(tmp_path, octets=(7,))
    assert pool == IPv4Network("127.7.0.0/16")
    with pytest.raises(RuntimeError, match="claimed by a live run"):
        claim_run_pool(tmp_path, octets=(7,))
    handle.close()
