"""Loopback IP leases: one /24 per set from the pool, one IP per hostname, sticky."""

import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

import pytest

from sandbox.errors import SandboxError
from sandbox.leases import LeaseStore

POOL = IPv4Network("127.42.0.0/16")
SETS_PER_WORKER = 15


def store(tmp_path: Path, pool: IPv4Network = POOL) -> LeaseStore:
    return LeaseStore(tmp_path / "leases.json", tmp_path / "locks" / "leases.lock", pool)


def test_a_set_leases_one_block_and_each_hostname_gets_an_ip_in_it(tmp_path: Path) -> None:
    lease = asyncio.run(
        store(tmp_path).lease("jira-123", ["redis.jira-123.test", "mysql.jira-123.test"])
    )

    assert lease.block == IPv4Network("127.42.0.0/24")
    # Hostnames in sorted order take .1 upwards.
    assert lease.ips == {
        "mysql.jira-123.test": IPv4Address("127.42.0.1"),
        "redis.jira-123.test": IPv4Address("127.42.0.2"),
    }


def test_different_sets_get_different_blocks(tmp_path: Path) -> None:
    leases = store(tmp_path)

    first = asyncio.run(leases.lease("jira-123", ["mysql.jira-123.test"]))
    second = asyncio.run(leases.lease("jira-456", ["mysql.jira-456.test"]))

    assert first.block != second.block
    assert second.ips == {"mysql.jira-456.test": IPv4Address("127.42.1.1")}


def test_leases_are_sticky_across_processes_and_new_hostnames_take_free_ips(
    tmp_path: Path,
) -> None:
    asyncio.run(store(tmp_path).lease("other", ["a.other.test"]))
    first = asyncio.run(store(tmp_path).lease("jira-123", ["mysql.jira-123.test"]))

    # A fresh store (as a later `up` process would build) sees the same lease.
    again = asyncio.run(
        store(tmp_path).lease("jira-123", ["api.jira-123.test", "mysql.jira-123.test"])
    )

    assert again.block == first.block == IPv4Network("127.42.1.0/24")
    assert again.ips["mysql.jira-123.test"] == IPv4Address("127.42.1.1")
    assert again.ips["api.jira-123.test"] == IPv4Address("127.42.1.2")


def test_a_hostname_dropped_from_the_set_keeps_its_ip(tmp_path: Path) -> None:
    leases = store(tmp_path)
    asyncio.run(leases.lease("s", ["a.s.test", "b.s.test"]))

    asyncio.run(leases.lease("s", ["b.s.test"]))
    lease = asyncio.run(leases.lease("s", ["a.s.test", "b.s.test", "c.s.test"]))

    assert lease.ips == {
        "a.s.test": IPv4Address("127.42.0.1"),
        "b.s.test": IPv4Address("127.42.0.2"),
        "c.s.test": IPv4Address("127.42.0.3"),
    }


def test_released_block_is_handed_out_again(tmp_path: Path) -> None:
    leases = store(tmp_path)
    asyncio.run(leases.lease("one", ["x.one.test"]))
    asyncio.run(leases.lease("two", ["x.two.test"]))

    asyncio.run(leases.release("one"))
    third = asyncio.run(leases.lease("three", ["x.three.test"]))

    assert third.block == IPv4Network("127.42.0.0/24")
    assert leases.read("one") is None


def test_lookup_without_leasing(tmp_path: Path) -> None:
    leases = store(tmp_path)
    assert leases.read("s") is None

    asyncio.run(leases.lease("s", ["a.s.test"]))

    found = leases.read("s")
    assert found is not None
    assert found.ips == {"a.s.test": IPv4Address("127.42.0.1")}


def test_exhausted_pool_fails_clearly(tmp_path: Path) -> None:
    leases = store(tmp_path, IPv4Network("127.42.7.0/24"))
    asyncio.run(leases.lease("one", ["x.one.test"]))

    with pytest.raises(SandboxError, match=r"no free /24 left in 127\.42\.7\.0/24"):
        asyncio.run(leases.lease("two", ["x.two.test"]))


def test_more_than_254_hostnames_fails_clearly(tmp_path: Path) -> None:
    hostnames = [f"h{i}.s.test" for i in range(255)]

    with pytest.raises(SandboxError, match="254"):
        asyncio.run(store(tmp_path).lease("s", hostnames))


def _lease_many(directory: str, worker: int, start_at: float) -> list[tuple[str, list[str]]]:
    # Every worker starts at the same moment, so their read-modify-writes overlap.
    time.sleep(max(0.0, start_at - time.time()))
    results: list[tuple[str, list[str]]] = []
    for j in range(SETS_PER_WORKER):
        name = f"w{worker}-{j}"
        lease = asyncio.run(
            store(Path(directory)).lease(name, [f"h{i}.{name}.test" for i in range(3)])
        )
        results.append((str(lease.block), [str(ip) for ip in lease.ips.values()]))
    return results


def test_concurrent_processes_never_share_a_block(tmp_path: Path) -> None:
    workers = 6
    start_at = time.time() + 2
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        per_worker = list(
            pool.map(_lease_many, [str(tmp_path)] * workers, range(workers), [start_at] * workers)
        )

    results = [result for worker in per_worker for result in worker]
    blocks = [block for block, _ in results]
    ips = [ip for _, set_ips in results for ip in set_ips]
    assert len(set(blocks)) == workers * SETS_PER_WORKER
    assert len(set(ips)) == len(ips)
    # Every process's lease survived: none overwrote another's write.
    names = [f"w{w}-{j}" for w in range(workers) for j in range(SETS_PER_WORKER)]
    assert all(store(tmp_path).read(name) is not None for name in names)


def test_a_corrupt_lease_file_fails_clearly(tmp_path: Path) -> None:
    (tmp_path / "leases.json").write_text("{not json")

    with pytest.raises(SandboxError, match=r"leases\.json"):
        asyncio.run(store(tmp_path).lease("s", ["a.s.test"]))
