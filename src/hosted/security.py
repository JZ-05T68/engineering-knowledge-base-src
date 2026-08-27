"""In-memory abuse controls and peer authority; no identity, HTTP, DB or network."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from threading import Lock
from time import monotonic
from typing import Literal

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network
RateCategory = Literal["agent", "source"]
MAX_FORWARDED_BYTES = 4096
MAX_FORWARDED_HOPS = 32
MAX_RATE_BUCKETS = 10_000


def _address(value: str) -> IPAddress:
    # Zone IDs are interface-local and must not create attacker-selected buckets.
    if "%" in value:
        raise ValueError("Scoped address is not a public client address")
    return ip_address(value)


def _key(address: IPAddress) -> str:
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _trusted(address: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    mapped = address.ipv4_mapped if isinstance(address, IPv6Address) else None
    return any(
        address in network or (mapped is not None and mapped in network) for network in networks
    )


def effective_client_ip(
    peer: str | None,
    forwarded: Sequence[bytes],
    trusted_proxies: Sequence[IPNetwork],
) -> str:
    """Trust XFF only behind an allowlisted immediate peer, scanning right to left.

    Invalid/absent peers share a fixed fallback bucket. Invalid/duplicate/oversized
    XFF or an entirely trusted chain falls back to the canonical direct peer.
    Forwarded and X-Real-IP are deliberately not inputs to this resolver.
    """
    try:
        direct = _address(peer) if peer is not None else None
    except ValueError:
        direct = None
    if direct is None:
        return "unknown-peer"
    fallback = _key(direct)
    if not _trusted(direct, trusted_proxies) or len(forwarded) != 1:
        return fallback
    raw = forwarded[0]
    if len(raw) > MAX_FORWARDED_BYTES or any(byte < 32 or byte > 126 for byte in raw):
        return fallback
    parts = raw.decode("ascii").split(",")
    if len(parts) > MAX_FORWARDED_HOPS:
        return fallback
    try:
        chain = [_address(part.strip()) for part in parts]
    except ValueError:
        return fallback
    for address in reversed(chain):
        if not _trusted(address, trusted_proxies):
            return _key(address)
    return fallback


@dataclass(slots=True)
class _Window:
    expires: float
    count: int = 0


class RateLimiter:
    """Per-category/client 60-second windows, anchored at the first request.

    No per-request history. Expired keys are removed on admission/prune. The
    active-key cap fails closed for new clients rather than evicting live quotas.
    A lock covers only clock/state arithmetic, never an application operation.
    """

    def __init__(
        self,
        agent_limit: int,
        source_limit: int,
        *,
        clock: Callable[[], float] = monotonic,
        max_buckets: int = MAX_RATE_BUCKETS,
    ) -> None:
        if min(agent_limit, source_limit, max_buckets) < 1:
            raise ValueError("Rate limits and state capacity must be positive")
        self._limits = {"agent": agent_limit, "source": source_limit}
        self._clock = clock
        self._max_buckets = max_buckets
        self._windows: OrderedDict[tuple[RateCategory, str], _Window] = OrderedDict()
        self._lock = Lock()

    def _expire(self, now: float) -> None:
        while self._windows and next(iter(self._windows.values())).expires <= now:
            self._windows.popitem(last=False)

    def admit(self, category: RateCategory, client: str) -> int | None:
        """Return None on admission, otherwise a safe integer Retry-After."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            key = (category, client)
            window = self._windows.get(key)
            if window is None:
                if len(self._windows) >= self._max_buckets:
                    expires = next(iter(self._windows.values())).expires
                    return max(1, math.ceil(expires - now))
                window = self._windows[key] = _Window(now + 60)
            if window.count >= self._limits[category]:
                return max(1, math.ceil(window.expires - now))
            window.count += 1
            return None

    def prune(self) -> None:
        """Release stale keys without a request or a background cleanup worker."""
        with self._lock:
            self._expire(self._clock())

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._windows)
