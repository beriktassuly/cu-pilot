"""Bounded reuse of immutable public ATA derivations; never account-state caching."""

from __future__ import annotations

from functools import lru_cache

from solders.pubkey import Pubkey


@lru_cache(maxsize=4096)
def canonical_ata(
    recipient: str, mint: str, token_program: str, ata_program: str
) -> tuple[str, int]:
    """Cache only immutable public derivations, keyed by every seed and program.

    Account existence, ownership and freshness never enter this cache. Call
    cache_clear() and record cache_info() to compare cold and warm runs.
    """
    address, bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(recipient)),
            bytes(Pubkey.from_string(token_program)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ata_program),
    )
    return str(address), bump
