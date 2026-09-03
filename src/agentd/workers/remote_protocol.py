"""Small authenticated wire protocol used by remote workers.

The protocol intentionally has no command or shell escape hatch.  A message is
an authenticated JSON envelope preceded by a four-byte network-order length.
The length and every recursively contained value are bounded before any action
is considered.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import struct
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any, Protocol

from agentd.workers.errors import WorkerAuthenticationError, WorkerProtocolError

PROTOCOL_VERSION = 1
MAX_FRAME_SIZE = 1_048_576
MAX_STRING_SIZE = 16_384
MAX_COLLECTION_ITEMS = 1_024
MAX_NESTING_DEPTH = 20
MIN_PSK_SIZE = 32
DEFAULT_CLOCK_SKEW_SECONDS = 30.0


class MessageKind(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"


class RemoteAction(StrEnum):
    START = "start"
    STATUS = "status"
    OBSERVE = "observe"
    STEER = "steer"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"
    COLLECT = "collect"
    HEARTBEAT = "heartbeat"


ALLOWED_ACTIONS = frozenset(item.value for item in RemoteAction)


class Clock(Protocol):
    def __call__(self) -> float: ...


def _validate_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise WorkerProtocolError(f"{name} must be a string")
    if not value or len(value) > MAX_STRING_SIZE or "\x00" in value:
        raise WorkerProtocolError(f"{name} is empty or exceeds the string limit")
    return value


def _validate_value(value: object, *, depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise WorkerProtocolError("message nesting exceeds the protocol limit")
    if isinstance(value, str):
        if len(value) > MAX_STRING_SIZE or "\x00" in value:
            raise WorkerProtocolError("message string exceeds the protocol limit")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise WorkerProtocolError("message contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WorkerProtocolError("message list exceeds the protocol limit")
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise WorkerProtocolError("message object exceeds the protocol limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkerProtocolError("message object keys must be strings")
            if len(key) > MAX_STRING_SIZE or "\x00" in key:
                raise WorkerProtocolError("message object key exceeds the limit")
            _validate_value(item, depth=depth + 1)
        return
    raise WorkerProtocolError(f"message contains unsupported value {type(value)!r}")


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes used for HMAC and payload hashes."""

    _validate_value(dict(value))
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WorkerProtocolError("message is not canonical JSON") from error


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash a payload without exposing its contents in journal keys or logs."""

    return hashlib.sha256(canonical_json(payload)).hexdigest()


def operation_hash(
    action: str | RemoteAction,
    run_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Bind a response to the exact logical request it acknowledges."""

    return payload_hash(
        {
            "action": str(action),
            "run_id": run_id,
            "payload": dict(payload),
        }
    )


@dataclass(frozen=True, slots=True)
class Envelope:
    """A validated request/response envelope.

    ``auth`` is held separately so signing and verification can never
    accidentally include a secret or a mutable mapping in the signed bytes.
    """

    kind: MessageKind
    action: str
    request_id: str
    node_id: str
    session_epoch: str
    run_id: str
    sequence: int
    timestamp: float
    nonce: str
    payload: dict[str, Any]
    request_hash: str | None = None
    auth: str | None = None
    ok: bool | None = None
    error: str | None = None
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MessageKind):
            try:
                object.__setattr__(self, "kind", MessageKind(self.kind))
            except (TypeError, ValueError) as error:
                raise WorkerProtocolError("unknown message kind") from error
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise WorkerProtocolError("version must be an integer")
        if self.version != PROTOCOL_VERSION:
            raise WorkerProtocolError(f"unsupported protocol version {self.version}")
        if not isinstance(self.action, str) or self.action not in ALLOWED_ACTIONS:
            raise WorkerProtocolError(f"unknown worker action {self.action!r}")
        for name, value in (
            ("request_id", self.request_id),
            ("node_id", self.node_id),
            ("session_epoch", self.session_epoch),
            ("nonce", self.nonce),
        ):
            _validate_string(value, name)
        if len(self.nonce) < 16:
            raise WorkerProtocolError("nonce is too short")
        if not isinstance(self.run_id, str) or len(self.run_id) > MAX_STRING_SIZE:
            raise WorkerProtocolError("run_id is invalid")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise WorkerProtocolError("sequence must be an integer")
        if self.sequence < 0:
            raise WorkerProtocolError("sequence cannot be negative")
        timestamp_valid = False
        if isinstance(self.timestamp, int | float) and not isinstance(
            self.timestamp, bool
        ):
            try:
                timestamp_valid = isfinite(float(self.timestamp))
            except (OverflowError, ValueError):
                timestamp_valid = False
        if not timestamp_valid:
            raise WorkerProtocolError("timestamp must be finite")
        if not isinstance(self.payload, dict):
            raise WorkerProtocolError("payload must be an object")
        _validate_value(self.payload)
        if self.auth is not None:
            if not isinstance(self.auth, str) or len(self.auth) != 64:
                raise WorkerProtocolError("auth must be a SHA-256 hex digest")
            try:
                int(self.auth, 16)
            except ValueError as error:
                raise WorkerProtocolError(
                    "auth must be a SHA-256 hex digest"
                ) from error
        if self.kind is MessageKind.REQUEST:
            if (
                self.ok is not None
                or self.error is not None
                or self.request_hash is not None
            ):
                raise WorkerProtocolError("request cannot contain response fields")
        elif self.kind is MessageKind.RESPONSE:
            if (
                not isinstance(self.request_hash, str)
                or len(self.request_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.request_hash
                )
            ):
                raise WorkerProtocolError(
                    "response request_hash must be a SHA-256 hex digest"
                )
            if not isinstance(self.ok, bool):
                raise WorkerProtocolError("response requires a boolean ok field")
            if self.error is not None:
                _validate_string(self.error, "error")
            if self.ok and self.error is not None:
                raise WorkerProtocolError("successful response cannot contain error")
        elif self.kind is MessageKind.EVENT and (
            self.ok is not None
            or self.error is not None
            or self.request_hash is not None
        ):
            raise WorkerProtocolError("event cannot contain response fields")

    def unsigned_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": self.version,
            "kind": self.kind.value,
            "action": self.action,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "session_epoch": self.session_epoch,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "payload": self.payload,
        }
        if self.kind is MessageKind.RESPONSE:
            value["request_hash"] = self.request_hash
            value["ok"] = self.ok
            if self.error is not None:
                value["error"] = self.error
        return value

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned_dict()
        if self.auth is not None:
            value["auth"] = self.auth
        return value


_REQUEST_KEYS = frozenset(
    {
        "version",
        "kind",
        "action",
        "request_id",
        "node_id",
        "session_epoch",
        "run_id",
        "sequence",
        "timestamp",
        "nonce",
        "payload",
        "auth",
    }
)
_RESPONSE_KEYS = _REQUEST_KEYS | frozenset({"ok", "error", "request_hash"})


def envelope_from_dict(value: Mapping[str, Any]) -> Envelope:
    """Parse one envelope and reject unknown or missing fields."""

    if not isinstance(value, Mapping):
        raise WorkerProtocolError("envelope must be an object")
    raw = dict(value)
    _validate_value(raw)
    kind_value = raw.get("kind")
    try:
        kind = MessageKind(kind_value)
    except (TypeError, ValueError) as error:
        raise WorkerProtocolError("unknown message kind") from error
    allowed = _REQUEST_KEYS if kind is not MessageKind.RESPONSE else _RESPONSE_KEYS
    unknown = set(raw) - allowed
    if unknown:
        raise WorkerProtocolError("envelope contains unknown fields")
    required = allowed - {"auth", "error", "ok"}
    missing = required - set(raw)
    if missing:
        raise WorkerProtocolError("envelope is missing required fields")
    if kind is MessageKind.RESPONSE and "ok" not in raw:
        raise WorkerProtocolError("response is missing ok")
    if kind is not MessageKind.RESPONSE and ("ok" in raw or "error" in raw):
        raise WorkerProtocolError("non-response contains response fields")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise WorkerProtocolError("version must be an integer")
    return Envelope(
        kind=kind,
        action=raw["action"],
        request_id=raw["request_id"],
        node_id=raw["node_id"],
        session_epoch=raw["session_epoch"],
        run_id=raw["run_id"],
        sequence=raw["sequence"],
        timestamp=raw["timestamp"],
        nonce=raw["nonce"],
        payload=raw["payload"],
        request_hash=(
            raw.get("request_hash") if kind is MessageKind.RESPONSE else None
        ),
        auth=(raw["auth"] if raw.get("auth") is not None else None),
        ok=(raw.get("ok") if kind is MessageKind.RESPONSE else None),
        error=(raw["error"] if raw.get("error") is not None else None),
        version=version,
    )


def sign_envelope(envelope: Envelope, secret: bytes) -> Envelope:
    _validate_secret(secret)
    signature = hmac.new(
        secret,
        canonical_json(envelope.unsigned_dict()),
        hashlib.sha256,
    ).hexdigest()
    return Envelope(
        kind=envelope.kind,
        action=envelope.action,
        request_id=envelope.request_id,
        node_id=envelope.node_id,
        session_epoch=envelope.session_epoch,
        run_id=envelope.run_id,
        sequence=envelope.sequence,
        timestamp=envelope.timestamp,
        nonce=envelope.nonce,
        payload=dict(envelope.payload),
        request_hash=envelope.request_hash,
        auth=signature,
        ok=envelope.ok,
        error=envelope.error,
        version=envelope.version,
    )


def verify_signature(envelope: Envelope, secret: bytes) -> None:
    _validate_secret(secret)
    if envelope.auth is None:
        raise WorkerAuthenticationError("message authentication is missing")
    expected = sign_envelope(envelope, secret).auth
    if not hmac.compare_digest(envelope.auth, expected or ""):
        raise WorkerAuthenticationError("message authentication failed")


class ReplayGuard:
    """Bounded nonce/timestamp replay protection for one authenticated peer."""

    def __init__(
        self,
        *,
        skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
        clock: Clock = time.time,
        max_entries: int = 4_096,
    ) -> None:
        if not isfinite(skew_seconds) or skew_seconds <= 0:
            raise ValueError("skew_seconds must be finite and positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._skew = skew_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._seen: dict[tuple[str, str], float] = {}

    def accept(self, envelope: Envelope) -> None:
        now = float(self._clock())
        timestamp = float(envelope.timestamp)
        if abs(now - timestamp) > self._skew:
            raise WorkerAuthenticationError(
                "message timestamp is outside the clock window"
            )
        cutoff = now - self._skew
        self._seen = {
            key: seen_at for key, seen_at in self._seen.items() if seen_at >= cutoff
        }
        key = (envelope.node_id, envelope.nonce)
        if key in self._seen:
            raise WorkerAuthenticationError("message nonce was already used")
        if len(self._seen) >= self._max_entries:
            oldest = min(self._seen, key=self._seen.__getitem__)
            del self._seen[oldest]
        self._seen[key] = timestamp


class PSKAuthenticator:
    """Authenticate an envelope with a pre-shared key and replay guard."""

    def __init__(
        self,
        secret: bytes,
        *,
        skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
        clock: Clock = time.time,
        max_replay_entries: int = 4_096,
    ) -> None:
        _validate_secret(secret)
        self._secret = bytes(secret)
        self._replay = ReplayGuard(
            skew_seconds=skew_seconds,
            clock=clock,
            max_entries=max_replay_entries,
        )

    def sign(self, envelope: Envelope) -> Envelope:
        return sign_envelope(envelope, self._secret)

    def verify(self, envelope: Envelope) -> None:
        verify_signature(envelope, self._secret)
        self._replay.accept(envelope)


def _validate_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < MIN_PSK_SIZE:
        raise ValueError(f"PSK must contain at least {MIN_PSK_SIZE} bytes")


def make_request(
    *,
    action: str | RemoteAction,
    request_id: str,
    node_id: str,
    session_epoch: str,
    run_id: str = "",
    sequence: int = 0,
    payload: Mapping[str, Any] | None = None,
    secret: bytes,
    timestamp: float | None = None,
    nonce: str | None = None,
) -> Envelope:
    envelope = Envelope(
        kind=MessageKind.REQUEST,
        action=str(action),
        request_id=request_id,
        node_id=node_id,
        session_epoch=session_epoch,
        run_id=run_id,
        sequence=sequence,
        timestamp=float(time.time() if timestamp is None else timestamp),
        nonce=nonce or secrets.token_hex(16),
        payload=dict(payload or {}),
    )
    return sign_envelope(envelope, secret)


def make_response(
    request: Envelope,
    *,
    payload: Mapping[str, Any] | None = None,
    ok: bool = True,
    error: str | None = None,
    sequence: int = 0,
    secret: bytes,
    timestamp: float | None = None,
    nonce: str | None = None,
) -> Envelope:
    if not ok and not error:
        error = "worker operation failed"
    envelope = Envelope(
        kind=MessageKind.RESPONSE,
        action=request.action,
        request_id=request.request_id,
        node_id=request.node_id,
        session_epoch=request.session_epoch,
        run_id=request.run_id,
        sequence=sequence,
        timestamp=float(time.time() if timestamp is None else timestamp),
        nonce=nonce or secrets.token_hex(16),
        payload=dict(payload or {}),
        request_hash=operation_hash(
            request.action,
            request.run_id,
            request.payload,
        ),
        ok=ok,
        error=error,
    )
    return sign_envelope(envelope, secret)


def encode_envelope(envelope: Envelope) -> bytes:
    encoded = canonical_json(envelope.to_dict())
    if len(encoded) > MAX_FRAME_SIZE:
        raise WorkerProtocolError("encoded message exceeds the frame limit")
    return encoded


def decode_envelope(data: bytes) -> Envelope:
    if not isinstance(data, bytes) or len(data) > MAX_FRAME_SIZE:
        raise WorkerProtocolError("message exceeds the frame limit")
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise WorkerProtocolError("message is not valid UTF-8 JSON") from error
    return envelope_from_dict(value)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("message contains duplicate object keys")
        result[key] = value
    return result


def encode_frame(envelope: Envelope) -> bytes:
    payload = encode_envelope(envelope)
    return struct.pack("!I", len(payload)) + payload


async def write_frame(
    writer: asyncio.StreamWriter,
    envelope: Envelope,
    *,
    max_frame_size: int = MAX_FRAME_SIZE,
) -> None:
    frame = encode_frame(envelope)
    if max_frame_size <= 0 or len(frame) - 4 > max_frame_size:
        raise WorkerProtocolError("message exceeds the frame limit")
    writer.write(frame)
    await writer.drain()


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_frame_size: int = MAX_FRAME_SIZE,
) -> Envelope:
    if max_frame_size <= 0:
        raise ValueError("max_frame_size must be positive")
    header = await reader.readexactly(4)
    (length,) = struct.unpack("!I", header)
    if length == 0 or length > max_frame_size:
        raise WorkerProtocolError("frame length exceeds the protocol limit")
    payload = await reader.readexactly(length)
    return decode_envelope(payload)


def error_response(
    request: Envelope,
    error: Exception,
    *,
    sequence: int,
    secret: bytes,
) -> Envelope:
    """Return a safe response without serializing exception details or secrets."""

    detail = error if isinstance(error, WorkerProtocolError) else None
    message = (
        str(detail) if detail is not None and str(detail) else "operation rejected"
    )
    if len(message) > MAX_STRING_SIZE:
        message = message[:MAX_STRING_SIZE]
    return make_response(
        request,
        ok=False,
        error=message,
        sequence=sequence,
        secret=secret,
    )


__all__ = [
    "ALLOWED_ACTIONS",
    "DEFAULT_CLOCK_SKEW_SECONDS",
    "MAX_COLLECTION_ITEMS",
    "MAX_FRAME_SIZE",
    "MAX_NESTING_DEPTH",
    "MAX_STRING_SIZE",
    "PROTOCOL_VERSION",
    "Envelope",
    "MessageKind",
    "PSKAuthenticator",
    "RemoteAction",
    "ReplayGuard",
    "canonical_json",
    "decode_envelope",
    "encode_envelope",
    "encode_frame",
    "envelope_from_dict",
    "error_response",
    "make_request",
    "make_response",
    "operation_hash",
    "payload_hash",
    "read_frame",
    "sign_envelope",
    "verify_signature",
    "write_frame",
]
