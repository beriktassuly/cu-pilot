"""Bounded read/simulation RPC. No signing or submission methods exist here."""

import base64
import math
import time
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from cu_pilot.schemas import MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES, StrictModel


class RpcError(RuntimeError):
    """Redacted transport/RPC failure; never includes credential-bearing URLs."""


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
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RPC endpoint must be an HTTP(S) URL")
        if attempts < 1 or attempts > 5:
            raise ValueError("RPC attempts must be between one and five")
        self._endpoint = endpoint
        self._attempts = attempts
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def __enter__(self) -> "RpcClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def _call(self, method: str, params: list[Any]) -> Any:
        for attempt in range(self._attempts):
            try:
                response = self._client.post(
                    self._endpoint,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                if response.status_code in {429, 502, 503, 504}:
                    if attempt + 1 < self._attempts:
                        time.sleep(0.1 * 2**attempt)
                        continue
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                raise RpcError("RPC transport or response decoding failed") from None
            if not isinstance(body, dict) or body.get("id") != 1:
                raise RpcError("Invalid RPC response envelope")
            if body.get("error") is not None:
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

    def simulate(
        self,
        wire_base64: str,
        *,
        version: Literal["legacy", 0, 1] | None = None,
        margin: float = 0.1,
        min_context_slot: int | None = None,
        replace_recent_blockhash: bool = True,
    ) -> SimulationEstimate:
        """Simulate caller-prepared wire bytes. Does not rewrite/sign any transaction.

        Caller must prepare adequate CU/data budgets first. For durable nonce messages,
        disable blockhash replacement. Failed or incomplete results raise RpcError.
        """
        if not math.isfinite(margin) or not 0 <= margin <= 1:
            raise ValueError("Margin must be finite and between zero and one")
        if version is not None and (type(version) is bool or version not in ("legacy", 0, 1)):
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
        config: dict[str, Any] = {
            "encoding": "base64",
            "commitment": "confirmed",
            "sigVerify": False,
            "replaceRecentBlockhash": replace_recent_blockhash,
        }
        if min_context_slot is not None:
            if min_context_slot < 0:
                raise ValueError("min_context_slot must be nonnegative")
            config["minContextSlot"] = min_context_slot
        started = time.perf_counter()
        result = self._call("simulateTransaction", [wire_base64, config])
        elapsed = (time.perf_counter() - started) * 1000
        if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
            raise RpcError("Simulation response is incomplete")
        value = result["value"]
        if "err" not in value or value["err"] is not None:
            raise RpcError("Simulation failed; no usable resource estimate")
        units = _integer(value.get("unitsConsumed"), "unitsConsumed")
        data = value.get("loadedAccountsDataSize")
        if data is not None:
            data = _integer(data, "loadedAccountsDataSize")
        if version == 1 and data is None:
            raise RpcError("v1 simulation requires loadedAccountsDataSize")
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
            raise RpcError("Measured usage plus safety margin exceeds a resource limit")
        context = result.get("context")
        if not isinstance(context, dict):
            raise RpcError("Simulation response is missing context")
        return SimulationEstimate(
            slot=_integer(context.get("slot"), "slot"),
            units_consumed=units,
            loaded_accounts_bytes=data,
            compute_unit_limit=limit,
            loaded_accounts_data_size_limit=data_limit,
            elapsed_ms=elapsed,
        )


def _integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise RpcError(f"RPC returned an invalid or missing {field}")
    return int(value)


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
