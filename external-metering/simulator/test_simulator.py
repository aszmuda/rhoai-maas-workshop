from __future__ import annotations

import json
import re
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import StringIO
from typing import Any
from unittest.mock import patch

from simulator import Settings, UsageBalances, create_handler, emit_log


LOG_HEADER = re.compile(
    r"^### \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ###$"
)


def parse_log_record(rendered: str) -> dict[str, Any]:
    header, payload = rendered.split("\n", 1)
    if LOG_HEADER.fullmatch(header) is None:
        raise AssertionError(f"invalid log header: {header!r}")
    if not payload.endswith("\n"):
        raise AssertionError("log record must end with a newline divider")
    return json.loads(payload)


@contextmanager
def running_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler())
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port: int, method: str, path: str, body: bytes | None = None):
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, payload


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(8080, Settings.from_environment({}).port)

    def test_port_override_and_validation(self):
        self.assertEqual(9090, Settings.from_environment({"PORT": "9090"}).port)
        with self.assertRaisesRegex(ValueError, "PORT must be an integer"):
            Settings.from_environment({"PORT": "invalid"})
        with self.assertRaisesRegex(ValueError, "PORT must be between"):
            Settings.from_environment({"PORT": "0"})


class UsageBalancesTests(unittest.TestCase):
    def test_concurrent_charges_never_make_balance_negative(self):
        balances = UsageBalances()
        barrier = threading.Barrier(12)

        def charge() -> None:
            barrier.wait()
            balances.record_usage("alice")

        threads = [threading.Thread(target=charge) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        entitlement, reset_to = balances.entitlement("alice")
        self.assertFalse(entitlement["hasAccess"])
        self.assertEqual(0.0, entitlement["balance"])
        self.assertEqual(1.0, reset_to)


class LogFormattingTests(unittest.TestCase):
    def test_log_has_timestamp_header_indented_json_and_blank_line(self):
        with patch("sys.stdout", new_callable=StringIO) as output:
            emit_log("example", nested={"value": 1})

        rendered = output.getvalue()
        header, _ = rendered.split("\n", 1)
        self.assertRegex(header, LOG_HEADER)
        self.assertIn('\n  "nested": {', rendered)
        self.assertIn('\n    "value": 1', rendered)
        self.assertTrue(rendered.endswith("}\n\n"))
        self.assertEqual("example", parse_log_record(rendered)["message"])


class SimulatorHandlerTests(unittest.TestCase):
    @staticmethod
    def entitlement(port: int, customer: str, model: str = "gpt-oss-20b"):
        path = (
            f"/api/v1/customers/{customer}/entitlements/"
            f"inference-tokens/value?model={model}"
        )
        status, _, payload = request(port, "GET", path)
        return status, json.loads(payload)

    @staticmethod
    def usage_event(customer: str | None, event_id: str = "evt-1") -> dict[str, Any]:
        data: dict[str, Any] = {"model": "gpt-oss-20b", "total_tokens": 42}
        event: dict[str, Any] = {
            "specversion": "1.0",
            "id": event_id,
            "source": "maas-gateway",
            "type": "inference.tokens.used",
            "data": data,
        }
        if customer is not None:
            event["subject"] = customer
            data["user"] = customer
        return event

    @staticmethod
    def post_usage(port: int, event: dict):
        return request(
            port, "POST", "/api/v1/events", json.dumps(event).encode("utf-8")
        )

    def test_third_invocation_is_denied_and_resets_for_the_fourth(self):
        with running_server() as port, patch("builtins.print") as output:
            first_status, first = self.entitlement(port, "alice")
            first_event_status, _, _ = self.post_usage(
                port, self.usage_event("alice", "evt-1")
            )
            second_status, second = self.entitlement(port, "alice")
            second_event_status, _, _ = self.post_usage(
                port, self.usage_event("alice", "evt-2")
            )
            third_status, third = self.entitlement(port, "alice")
            fourth_status, fourth = self.entitlement(port, "alice")

        self.assertEqual(200, first_status)
        self.assertEqual(204, first_event_status)
        self.assertEqual(
            {"hasAccess": True, "balance": 1.0, "usage": 0.0, "overage": 0.0},
            first,
        )
        self.assertEqual(200, second_status)
        self.assertEqual(204, second_event_status)
        self.assertEqual(
            {"hasAccess": True, "balance": 0.5, "usage": 0.5, "overage": 0.0},
            second,
        )
        self.assertEqual(200, third_status)
        self.assertEqual(
            {"hasAccess": False, "balance": 0.0, "usage": 1.0, "overage": 0.0},
            third,
        )
        self.assertEqual(200, fourth_status)
        self.assertEqual(
            {"hasAccess": True, "balance": 1.0, "usage": 0.0, "overage": 0.0},
            fourth,
        )

        entitlement_logs = [
            parse_log_record(call.args[0])
            for call in output.call_args_list
            if "entitlement checked" in call.args[0]
        ]
        self.assertIsNone(entitlement_logs[0]["balanceResetTo"])
        self.assertEqual(1.0, entitlement_logs[2]["balanceResetTo"])

    def test_balances_are_isolated_by_customer(self):
        with running_server() as port, patch("builtins.print"):
            self.post_usage(port, self.usage_event("alice"))
            _, alice = self.entitlement(port, "alice")
            _, bob = self.entitlement(port, "bob")

        self.assertEqual(0.5, alice["balance"])
        self.assertEqual(1.0, bob["balance"])

    def test_customer_and_feature_are_url_decoded_in_log(self):
        with running_server() as port, patch("builtins.print") as output:
            status, _, _ = request(
                port,
                "GET",
                "/api/v1/customers/alice%40example.com/entitlements/inference%20tokens/value",
            )
        self.assertEqual(200, status)
        entitlement_output = next(
            call.args[0]
            for call in output.call_args_list
            if "entitlement checked" in call.args[0]
        )
        self.assertIn("\n", entitlement_output)
        self.assertIn('    "customer": "alice@example.com"', entitlement_output)
        entitlement_log = parse_log_record(entitlement_output)
        self.assertEqual(
            "alice@example.com", entitlement_log["received"]["customer"]
        )
        self.assertEqual(
            "inference tokens", entitlement_log["received"]["featureKey"]
        )
        self.assertEqual("GET", entitlement_log["received"]["method"])
        self.assertEqual(200, entitlement_log["sent"]["status"])
        self.assertTrue(entitlement_log["sent"]["body"]["hasAccess"])

    def test_accepts_charges_and_logs_cloud_event(self):
        event = self.usage_event("alice")
        with running_server() as port, patch("builtins.print") as output:
            status, _, payload = self.post_usage(port, event)
        self.assertEqual(204, status)
        self.assertEqual(b"", payload)
        event_output = next(
            call.args[0]
            for call in output.call_args_list
            if "CloudEvent received" in call.args[0]
        )
        self.assertIn("\n", event_output)
        self.assertIn('      "total_tokens": 42', event_output)
        event_log = parse_log_record(event_output)
        self.assertEqual(event, event_log["received"]["body"])
        self.assertEqual("POST", event_log["received"]["method"])
        self.assertEqual(204, event_log["sent"]["status"])
        self.assertEqual("alice", event_log["customer"])
        self.assertEqual(0.5, event_log["charge"])
        self.assertEqual(1.0, event_log["balanceBefore"])
        self.assertEqual(0.5, event_log["balanceAfter"])

    def test_event_data_user_is_used_when_subject_is_missing(self):
        event = self.usage_event(None)
        event["data"]["user"] = "alice"
        with running_server() as port, patch("builtins.print"):
            status, _, _ = self.post_usage(port, event)
            _, entitlement = self.entitlement(port, "alice")

        self.assertEqual(204, status)
        self.assertEqual(0.5, entitlement["balance"])

    def test_rejects_malformed_incomplete_or_unattributed_events(self):
        with running_server() as port, patch("builtins.print"):
            malformed_status, _, _ = request(
                port, "POST", "/api/v1/events", b"not-json"
            )
            incomplete_status, _, incomplete_payload = request(
                port, "POST", "/api/v1/events", b'{"specversion":"1.0"}'
            )
            identity_status, _, identity_payload = self.post_usage(
                port, self.usage_event(None)
            )
        self.assertEqual(400, malformed_status)
        self.assertEqual(400, incomplete_status)
        self.assertIn("fields", json.loads(incomplete_payload))
        self.assertEqual(400, identity_status)
        self.assertIn("subject or data.user", json.loads(identity_payload)["error"])

    def test_health_unknown_path_and_wrong_method(self):
        with running_server() as port, patch("builtins.print"):
            health_status, _, health_body = request(port, "GET", "/healthz")
            missing_status, _, _ = request(port, "GET", "/missing")
            method_status, headers, _ = request(port, "GET", "/api/v1/events")
        self.assertEqual(200, health_status)
        self.assertEqual({"status": "ok"}, json.loads(health_body))
        self.assertEqual(404, missing_status)
        self.assertEqual(405, method_status)
        self.assertEqual("POST", headers["Allow"])

    def test_successful_health_probe_is_quiet_but_wrong_method_is_logged(self):
        with running_server() as port, patch("builtins.print") as output:
            health_status, _, health_body = request(port, "GET", "/healthz")
            self.assertEqual(200, health_status)
            self.assertEqual({"status": "ok"}, json.loads(health_body))
            output.assert_not_called()

            method_status, headers, _ = request(port, "PUT", "/healthz")

        self.assertEqual(405, method_status)
        self.assertEqual("GET", headers["Allow"])
        self.assertEqual(1, output.call_count)
        access_log = parse_log_record(output.call_args.args[0])
        self.assertEqual("http request", access_log["message"])
        self.assertIn("PUT /healthz", access_log["request"])


if __name__ == "__main__":
    unittest.main()
