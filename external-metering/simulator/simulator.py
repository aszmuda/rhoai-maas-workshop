#!/usr/bin/env python3
"""Small OpenMeter-compatible HTTP service for external-metering testing."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlsplit


MAX_EVENT_BYTES = 1024 * 1024
INITIAL_BALANCE = 1.0
USAGE_CHARGE = 0.5
ENTITLEMENT_PATH = re.compile(
    r"^/api/v1/customers/([^/]+)/entitlements/([^/]+)/value$"
)
REQUIRED_CLOUD_EVENT_FIELDS = ("specversion", "id", "source", "type")
LOG_LOCK = Lock()


def emit_log(message: str, **fields: object) -> None:
    """Write one timestamped, human-readable log record to stdout."""
    timestamp = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    payload = json.dumps(
        {"message": message, **fields},
        indent=2,
        sort_keys=True,
    )
    with LOG_LOCK:
        print(f"### {timestamp} ###\n{payload}\n", flush=True)


def emit_exchange_log(
    message: str,
    *,
    received: object,
    sent: object,
    **fields: object,
) -> None:
    """Write one human-readable request/response exchange to stdout."""
    emit_log(
        message,
        received=received,
        sent=sent,
        **fields,
    )


@dataclass(frozen=True)
class Settings:
    port: int = 8080

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        try:
            port = int(values.get("PORT", "8080"))
        except ValueError as error:
            raise ValueError("PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")

        return cls(port=port)


class UsageBalances:
    """Thread-safe, in-memory usage balances keyed by customer ID."""

    def __init__(
        self,
        initial_balance: float = INITIAL_BALANCE,
        usage_charge: float = USAGE_CHARGE,
    ) -> None:
        self.initial_balance = initial_balance
        self.usage_charge = usage_charge
        self._balances: dict[str, float] = {}
        self._lock = Lock()

    def entitlement(self, customer_id: str) -> tuple[dict[str, object], float | None]:
        """Return entitlement state and reset an exhausted balance after denial."""
        with self._lock:
            balance = self._balances.setdefault(customer_id, self.initial_balance)
            has_access = balance > 0.0
            response = {
                "hasAccess": has_access,
                "balance": balance,
                "usage": self.initial_balance - balance,
                "overage": 0.0,
            }
            reset_to = None
            if not has_access:
                self._balances[customer_id] = self.initial_balance
                reset_to = self.initial_balance
            return response, reset_to

    def record_usage(self, customer_id: str) -> tuple[float, float]:
        """Apply one fixed usage charge and return the before/after balances."""
        with self._lock:
            balance_before = self._balances.setdefault(
                customer_id, self.initial_balance
            )
            balance_after = max(0.0, balance_before - self.usage_charge)
            self._balances[customer_id] = balance_after
            return balance_before, balance_after


def event_customer_id(event: Mapping[str, object]) -> str | None:
    """Extract the customer identity emitted by the external-metering plugin."""
    subject = event.get("subject")
    if isinstance(subject, str) and subject:
        return subject
    data = event.get("data")
    if isinstance(data, dict):
        user = data.get("user")
        if isinstance(user, str) and user:
            return user
    return None


def create_handler(
    balances: UsageBalances | None = None,
) -> type[BaseHTTPRequestHandler]:
    usage_balances = balances if balances is not None else UsageBalances()

    class SimulatorHandler(BaseHTTPRequestHandler):
        server_version = "ExternalMeteringSimulator/1.0"

        def _write_json(self, status: int, body: object) -> None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_empty(self, status: int, allow: str | None = None) -> None:
            self.send_response(status)
            if allow:
                self.send_header("Allow", allow)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _entitlement_match(self) -> re.Match[str] | None:
            return ENTITLEMENT_PATH.fullmatch(urlsplit(self.path).path)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._write_json(200, {"status": "ok"})
                return
            if parsed.path == "/api/v1/events":
                self._write_empty(405, "POST")
                return

            match = self._entitlement_match()
            if match is None:
                self._write_json(404, {"error": "not found"})
                return

            customer_id = unquote(match.group(1))
            feature_key = unquote(match.group(2))
            model = parse_qs(parsed.query).get("model", [""])[0]
            response, reset_to = usage_balances.entitlement(customer_id)
            emit_exchange_log(
                "entitlement checked",
                balanceResetTo=reset_to,
                received={
                    "method": "GET",
                    "path": parsed.path,
                    "customer": customer_id,
                    "featureKey": feature_key,
                    "model": model,
                },
                sent={"status": 200, "body": response},
            )
            self._write_json(200, response)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if self._entitlement_match() is not None:
                self._write_empty(405, "GET")
                return
            if parsed.path != "/api/v1/events":
                self._write_json(404, {"error": "not found"})
                return

            content_length = self.headers.get("Content-Length")
            if content_length is None:
                self._write_json(400, {"error": "Content-Length is required"})
                return
            try:
                body_length = int(content_length)
            except ValueError:
                self._write_json(400, {"error": "invalid Content-Length"})
                return
            if body_length < 0:
                self._write_json(400, {"error": "invalid Content-Length"})
                return
            if body_length > MAX_EVENT_BYTES:
                self._write_json(413, {"error": "event exceeds 1 MiB"})
                return

            try:
                event = json.loads(self.rfile.read(body_length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._write_json(400, {"error": "body must be valid JSON"})
                return
            if not isinstance(event, dict):
                self._write_json(400, {"error": "CloudEvent must be a JSON object"})
                return

            missing = [
                field
                for field in REQUIRED_CLOUD_EVENT_FIELDS
                if not event.get(field)
            ]
            if missing:
                self._write_json(
                    400,
                    {"error": "missing required CloudEvent fields", "fields": missing},
                )
                return

            customer_id = event_customer_id(event)
            if customer_id is None:
                self._write_json(
                    400,
                    {"error": "CloudEvent subject or data.user is required"},
                )
                return

            balance_before, balance_after = usage_balances.record_usage(customer_id)

            emit_exchange_log(
                "CloudEvent received",
                customer=customer_id,
                charge=usage_balances.usage_charge,
                balanceBefore=balance_before,
                balanceAfter=balance_after,
                received={
                    "method": "POST",
                    "path": parsed.path,
                    "body": event,
                },
                sent={"status": 204},
            )
            self._write_empty(204)

        def _method_not_allowed(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/api/v1/events":
                self._write_empty(405, "POST")
            elif self._entitlement_match() is not None or parsed.path == "/healthz":
                self._write_empty(405, "GET")
            else:
                self._write_json(404, {"error": "not found"})

        do_DELETE = _method_not_allowed
        do_HEAD = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_PUT = _method_not_allowed

        def log_message(self, format: str, *args: object) -> None:
            if self.command == "GET" and urlsplit(self.path).path == "/healthz":
                return
            emit_log(
                "http request",
                client=self.client_address[0],
                request=format % args,
            )

    return SimulatorHandler


def main() -> None:
    settings = Settings.from_environment()
    server = ThreadingHTTPServer(
        ("0.0.0.0", settings.port), create_handler()
    )
    server.daemon_threads = True
    emit_log(
        "external metering simulator started",
        port=settings.port,
        initialBalance=INITIAL_BALANCE,
        usageCharge=USAGE_CHARGE,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
