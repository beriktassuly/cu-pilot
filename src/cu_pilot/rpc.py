"""Bounded read/simulation RPC. No signing or submission methods exist here."""

import base64
import math
import threading
import time
from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from cu_pilot.schemas import MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES, StrictModel


class RpcError(RuntimeError):
    """Redacted transport/RPC failure; never includes credential-bearing URLs."""

    def __init__(
        self, message: str, *, code: str = "rpc_failure", evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = evidence


class SimulationEstimate(StrictModel):
    slot: int = Field(ge=0)
    units_consumed: int = Field(ge=0)
    loaded_accounts_bytes: int | None = Field(default=None, ge=0)
    compute_unit_limit: int = Field(gt=0, le=MAX_COMPUTE_UNITS)
    loaded_accounts_data_size_limit: int | None = Field(
        default=None, gt=0, le=MAX_LOADED_ACCOUNT_BYTES
    )
    elapsed_ms: float = Field(ge=0)
    source: Literal["simulation"] = "simulation"


class RpcClient:
    def __init__(
        self,
        endpoint: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15,
        attempts: int = 3,
        requests_per_second: float = 10,
        cancellation: threading.Event | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RPC endpoint must be an HTTP(S) URL")
        if type(attempts) is not int or attempts < 1 or attempts > 5:
            raise ValueError("RPC attempts must be between one and five")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RPC timeout must be positive and finite")
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("RPC rate must be positive and finite")
        self._endpoint = endpoint
        self._attempts = attempts
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self._cancel = cancellation or threading.Event()
        self._rate_interval = 1 / requests_per_second
        self._next_request = 0.0
        self._rate_lock = threading.Lock()
        self.call_count = 0
        self.retry_count = 0

    def _pause(self, seconds: float) -> None:
        if self._cancel.wait(max(0, seconds)):
            raise RpcError("RPC cancelled", code="cancelled")

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def _call(self, method: str, params: list[Any]) -> Any:
        for attempt in range(self._attempts):
            with self._rate_lock:
                self._pause(self._next_request - time.monotonic())
                self._next_request = time.monotonic() + self._rate_interval
            if attempt:
                self.retry_count += 1
            try:
                self.call_count += 1
                response = self._client.post(
                    self._endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                if response.status_code in {429, 502, 503, 504}:
                    if attempt + 1 < self._attempts:
                        self._pause(0.1 * 2**attempt)
                        continue
                response.raise_for_status()
                body = response.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 < self._attempts:
                    self._pause(0.1 * 2**attempt)
                    continue
                raise RpcError(
                    "RPC transport timeout or network failure", code="transport"
                ) from None
            except (httpx.HTTPError, ValueError):
                raise RpcError("RPC transport or response decoding failed") from None
            if not isinstance(body, dict) or body.get("id") != 1:
                raise RpcError("Invalid RPC response envelope")
            if body.get("error") is not None:
                error = body["error"]
                if isinstance(error, dict) and error.get("code") in {-32005, -32016}:
                    if attempt + 1 < self._attempts:
                        self._pause(0.1 * 2**attempt)
                        continue
                raise RpcError("RPC returned an error; check provider/version support")
            if "result" not in body:
                raise RpcError("RPC response is missing result")
            return body["result"]
        raise RpcError("RPC retry limit reached")

    def get_transaction(self, signature: str) -> dict[str, Any]:
        result = self._call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        )
        if not isinstance(result, dict):
            raise RpcError("Transaction is unavailable at finalized commitment")
        return result

    def get_slot(self) -> int:
        return _integer(self._call("getSlot", [{"commitment": "finalized"}]), "slot")

    def get_genesis_hash(self) -> str:
        from cu_pilot.parsing import validate_pubkey

        result = self._call("getGenesisHash", [])
        try:
            return validate_pubkey(result)
        except ValueError:
            raise RpcError("Invalid cluster genesis hash") from None

    def get_version(self) -> dict[str, Any]:
        result = self._call("getVersion", [])
        if not isinstance(result, dict) or not isinstance(result.get("solana-core"), str):
            raise RpcError("Invalid runtime version")
        return result

    def get_account_info(
        self, address: str, *, min_context_slot: int, commitment: str = "confirmed"
    ) -> dict[str, Any]:
        from cu_pilot.parsing import validate_pubkey

        validate_pubkey(address)
        _commitment(commitment)
        _integer(min_context_slot, "minContextSlot")
        result = self._call(
            "getAccountInfo",
            [
                address,
                {
                    "encoding": "base64",
                    "commitment": commitment,
                    "minContextSlot": min_context_slot,
                },
            ],
        )
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise RpcError("Invalid account response")
        if _integer(result["context"].get("slot"), "slot") < min_context_slot:
            raise RpcError("Account response is older than minimum context slot")
        return result

    def get_transaction_wire(self, signature: str, *, commitment: str = "finalized") -> Any:
        from cu_pilot.parsing import decode_base58

        if len(decode_base58(signature)) != 64:
            raise ValueError("invalid transaction signature")
        if commitment not in {"confirmed", "finalized"}:
            raise ValueError("execution commitment must be confirmed or finalized")
        return self._call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "base64",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        )

    def get_multiple_accounts(
        self, addresses: Sequence[str], *, min_context_slot: int, commitment: str = "confirmed"
    ) -> dict[str, Any]:
        from cu_pilot.parsing import validate_pubkey

        if not 1 <= len(addresses) <= 100:
            raise ValueError("getMultipleAccounts requires 1 to 100 addresses")
        for address in addresses:
            validate_pubkey(address)
        _integer(min_context_slot, "minContextSlot")
        _commitment(commitment)
        result = self._call(
            "getMultipleAccounts",
            [
                list(addresses),
                {
                    "encoding": "base64",
                    "commitment": commitment,
                    "minContextSlot": min_context_slot,
                },
            ],
        )
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise RpcError("Invalid multiple-account response")
        if _integer(result["context"].get("slot"), "slot") < min_context_slot:
            raise RpcError("Account evidence is older than minimum context slot")
        if not isinstance(result.get("value"), list) or len(result["value"]) != len(addresses):
            raise RpcError("Missing multiple-account evidence")
        return result

    def simulate(
        self,
        wire_base64: str,
        *,
        version: Literal["legacy", 0, 1] | None = None,
        margin: float = 0.1,
        min_context_slot: int | None = None,
        replace_recent_blockhash: bool = True,
        require_loaded_data: bool = False,
        commitment: str = "confirmed",
    ) -> SimulationEstimate:
        """Simulate caller-prepared wire bytes. Does not rewrite/sign any transaction.

        Caller must prepare adequate CU/data budgets first. For durable nonce messages,
        disable blockhash replacement. Failed or incomplete results raise RpcError.
        """
        if type(margin) is bool or not math.isfinite(margin) or not 0 <= margin <= 1:
            raise ValueError("Margin must be finite and between zero and one")
        if version is not None and (
            version != "legacy" and (type(version) is not int or version not in (0, 1))
        ):
            raise ValueError("Unsupported transaction version")
        try:
            decoded = base64.b64decode(wire_base64, validate=True)
        except ValueError:
            raise ValueError("Expected a base64 serialized transaction") from None
        if not decoded or len(decoded) > 4096:
            raise ValueError("Serialized transaction must contain 1–4096 bytes")
        detected_version = _wire_version(decoded)
        if version is not None and version != detected_version:
            raise ValueError("Declared version differs from serialized transaction version")
        version = detected_version
        if version != 1 and len(decoded) > 1232:
            raise ValueError("Legacy and v0 transactions cannot exceed 1232 bytes")
        _commitment(commitment)
        config: dict[str, Any] = {
            "encoding": "base64",
            "commitment": commitment,
            "sigVerify": False,
            "replaceRecentBlockhash": replace_recent_blockhash,
        }
        if min_context_slot is not None:
            _integer(min_context_slot, "minContextSlot")
            config["minContextSlot"] = min_context_slot
        started = time.perf_counter()
        result = self._call("simulateTransaction", [wire_base64, config])
        elapsed = (time.perf_counter() - started) * 1000
        if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
            raise RpcError("Simulation response is incomplete")
        context = result.get("context")
        if not isinstance(context, dict):
            raise RpcError("Simulation response is missing context")
        slot = _integer(context.get("slot"), "slot")
        if min_context_slot is not None and slot < min_context_slot:
            raise RpcError(
                "Simulation response is older than minimum context slot", code="stale_context"
            )
        value = result["value"]
        evidence: dict[str, Any] = {
            "success": "err" in value and value["err"] is None,
            "error": None if "err" in value and value["err"] is None else "transaction_error",
            "compute_units": _optional_integer(value.get("unitsConsumed")),
            "loaded_accounts_bytes": _optional_integer(value.get("loadedAccountsDataSize")),
            "slot": slot,
            "elapsed_ms": elapsed,
        }
        if "err" not in value or value["err"] is not None:
            # Do not persist arbitrary RPC logs/error strings (they may echo secrets).
            raise RpcError(
                "Simulation failed; no usable resource estimate",
                code="transaction_error",
                evidence=evidence,
            )
        if evidence["compute_units"] is None:
            raise RpcError(
                "Simulation requires valid unitsConsumed",
                code="missing_measurement",
                evidence=evidence,
            )
        units = _integer(value.get("unitsConsumed"), "unitsConsumed")
        data = value.get("loadedAccountsDataSize")
        if data is not None:
            if evidence["loaded_accounts_bytes"] is None:
                raise RpcError(
                    "Simulation requires valid loadedAccountsDataSize",
                    code="missing_measurement",
                    evidence=evidence,
                )
            data = evidence["loaded_accounts_bytes"]
        if (version == 1 or require_loaded_data) and data is None:
            raise RpcError(
                "Resource simulation requires loadedAccountsDataSize",
                code="missing_measurement",
                evidence=evidence,
            )
        factor = Decimal(1) + Decimal(str(margin))
        limit = max(1, int((Decimal(units) * factor).to_integral_value(rounding=ROUND_CEILING)))
        data_limit = None
        if data is not None:
            padded_data = int((Decimal(data) * factor).to_integral_value(rounding=ROUND_CEILING))
            # Reserve full 32-KiB pages, with at least one page of loaded-data capacity.
            data_limit = max(32768, ((padded_data + 32767) // 32768) * 32768)
        if limit > MAX_COMPUTE_UNITS or (
            data_limit is not None and data_limit > MAX_LOADED_ACCOUNT_BYTES
        ):
            raise RpcError(
                "Measured usage plus safety margin exceeds a resource limit",
                code="resource_cap_exceeded",
                evidence=evidence,
            )
        return SimulationEstimate(
            slot=slot,
            units_consumed=units,
            loaded_accounts_bytes=data,
            compute_unit_limit=limit,
            loaded_accounts_data_size_limit=data_limit,
            elapsed_ms=elapsed,
        )


def _integer(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise RpcError(f"RPC returned an invalid or missing {field}")
    return int(value)


def _optional_integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value < 2**64 else None


def _commitment(value: str) -> None:
    if value not in {"processed", "confirmed", "finalized"}:
        raise ValueError("unsupported commitment")


def _wire_version(data: bytes) -> Literal["legacy", 0, 1]:
    """Inspect the envelope only; the RPC validates the full transaction.

    V1 starts with 0x81 and has signatures at the end. Valid legacy/v0 packets
    fit fewer than 128 signatures, hence their shortvec count fits one byte.
    """
    if data[0] == 0x81:
        return 1
    count = data[0]
    if not 1 <= count < 128:
        raise ValueError("Unsupported or malformed transaction envelope")
    message_offset = 1 + 64 * count
    if message_offset >= len(data):
        raise ValueError("Transaction is truncated before its message")
    prefix = data[message_offset]
    if prefix == 0x80:
        return 0
    if prefix < 0x80:
        return "legacy"
    raise ValueError("Unsupported transaction message version")
