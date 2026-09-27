"""A warm public-address cache must never authorize stale or substituted state."""

from __future__ import annotations

import pytest
from solders.pubkey import Pubkey

from examples.payouts.derivation_features import derive_features
from examples.payouts.model import (
    ATA_PROGRAM,
    TOKEN_PROGRAM,
    PayoutStateEnvelope,
    canonical_ata,
    recipient_ata,
)
from tests.test_payout_model import DEPLOYMENTS, MINT, key, state


@pytest.fixture(autouse=True)
def clear_derivations():
    canonical_ata.cache_clear()
    yield
    canonical_ata.cache_clear()


def uncached_ata(wallet, mint, token_program, ata_program):
    address, bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(wallet)),
            bytes(Pubkey.from_string(token_program)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ata_program),
    )
    return str(address), bump


def test_derivation_cache_isolates_every_seed_and_program():
    original = (key(500), MINT, TOKEN_PROGRAM, ATA_PROGRAM)
    variants = [original]
    for position in range(4):
        changed = list(original)
        changed[position] = key(600 + position)
        variants.append(tuple(changed))
    for inputs in variants:
        assert canonical_ata(*inputs) == uncached_ata(*inputs)
    info = canonical_ata.cache_info()
    assert info.currsize == info.misses == len(variants)
    assert len({canonical_ata(*inputs)[0] for inputs in variants}) == len(variants)
    assert recipient_ata(original[0], original[1]) == canonical_ata(*original)[0]
    assert canonical_ata.cache_info().hits > 0


def test_derivation_cache_is_bounded_and_eviction_preserves_results():
    first = (key(1_000_000), MINT, TOKEN_PROGRAM, ATA_PROGRAM)
    expected = canonical_ata(*first)
    maximum = canonical_ata.cache_info().maxsize
    assert maximum == 4096
    for index in range(maximum):
        canonical_ata(key(1_000_001 + index), MINT, TOKEN_PROGRAM, ATA_PROGRAM)
    before = canonical_ata.cache_info()
    assert before.currsize == maximum
    assert canonical_ata(*first) == expected
    after = canonical_ata.cache_info()
    assert after.misses == before.misses + 1
    assert after.currsize == maximum


def test_invalid_public_keys_are_not_cached():
    with pytest.raises(ValueError):
        canonical_ata("invalid", MINT, TOKEN_PROGRAM, ATA_PROGRAM)
    assert canonical_ata.cache_info().currsize == 0


def test_warm_derivations_do_not_cache_account_existence():
    present = state(20, count=8, missing=0)
    missing = state(20, count=8, missing=4)
    canonical_ata.cache_clear()
    cold = derive_features(present)
    warm = derive_features(present)
    changed = derive_features(missing)
    assert warm == cold
    assert canonical_ata.cache_info().hits == 16
    assert changed.ata_bumps == cold.ata_bumps
    assert changed.total_ata_attempts == cold.total_ata_attempts
    assert cold.missing == cold.missing_ata_attempts == 0
    assert changed.missing == 4
    assert changed.missing_ata_attempts == sum(256 - bump for bump in cold.ata_bumps[:4])


def test_cached_address_never_bypasses_snapshot_address_check():
    original = state(20)
    derive_features(original)
    accounts = list(original.recipient_accounts)
    accounts[0] = accounts[0].model_copy(update={"address": key(90)})
    changed = PayoutStateEnvelope.seal(**{**original.model_dump(), "recipient_accounts": accounts})
    assert changed.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_recipient_state"
    )
    with pytest.raises(ValueError, match="derived ATA differs"):
        derive_features(changed)


def test_warm_cache_preserves_state_freshness_and_token_risk():
    original = state(20)
    identity = original.snapshot_digest
    derive_features(original)
    assert original.snapshot_digest == identity
    assert original.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) is None
    assert original.risk(current_slot=209, deployment_bindings=DEPLOYMENTS) == "stale_state"
    accounts = list(original.recipient_accounts)
    accounts[0] = accounts[0].model_copy(update={"frozen": True})
    changed = PayoutStateEnvelope.seal(**{**original.model_dump(), "recipient_accounts": accounts})
    assert changed.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_recipient_state"
    )
