from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

import maas_notebook_utils as notebook_utils


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def rendered_log(timestamp: str, record: dict) -> str:
    return f"### {timestamp} ###\n{json.dumps(record, indent=2)}\n\n"


class OpenShiftIdentityTests(unittest.TestCase):
    def test_get_and_show_identity(self):
        with patch.object(
            notebook_utils.subprocess,
            "check_output",
            side_effect=["alice\n", "sha256~secret-token\n"],
        ) as check_output:
            username, token = notebook_utils.get_openshift_identity()

        self.assertEqual(("alice", "sha256~secret-token"), (username, token))
        self.assertEqual(2, check_output.call_count)

        with patch("sys.stdout", new_callable=StringIO) as output:
            notebook_utils.show_openshift_identity(username, token)
        self.assertIn("alice", output.getvalue())
        self.assertIn("sha256~secret-", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())


class ResponsePresentationTests(unittest.TestCase):
    def test_api_key_response_is_masked_and_returns_values(self):
        response = FakeResponse(
            200,
            {
                "key": "sk-oai-abcdefghijklmnop",
                "id": "key-id",
                "subscription": "premium",
                "expiresAt": "tomorrow",
            },
        )
        with patch("sys.stdout", new_callable=StringIO) as output:
            result = notebook_utils.show_api_key_response(response)

        self.assertEqual(("sk-oai-abcdefghijklmnop", "key-id"), result)
        self.assertIn("sk-oai-abcdefg…", output.getvalue())
        self.assertNotIn("abcdefghijklmnop", output.getvalue())

    def test_successful_inference_shows_result_and_both_exchanges(self):
        response = FakeResponse(
            200,
            {
                "model": "demo-model",
                "usage": {"total_tokens": 5},
                "choices": [{"message": {"content": "hello"}}],
            },
        )
        exchanges = [
            {"timestampText": "2026-01-01T00:00:00.000Z", "record": {"message": "entitlement checked"}},
            {"timestampText": "2026-01-01T00:00:01.000Z", "record": {"message": "CloudEvent received"}},
        ]
        with (
            patch.object(
                notebook_utils,
                "_read_request_logs",
                return_value=(exchanges, None),
            ) as read_logs,
            patch("sys.stdout", new_callable=StringIO) as output,
        ):
            notebook_utils.show_inference_response(
                response,
                username="alice",
                model="demo-model",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        self.assertIn('"content": "hello"', output.getvalue())
        self.assertIn("entitlement checked", output.getvalue())
        self.assertIn("CloudEvent received", output.getvalue())
        self.assertTrue(read_logs.call_args.kwargs["expects_usage_event"])

    def test_denied_inference_shows_only_entitlement(self):
        response = FakeResponse(429, {}, "denied")
        exchanges = [
            {"timestampText": "2026-01-01T00:00:00.000Z", "record": {"message": "entitlement checked"}}
        ]
        with (
            patch.object(
                notebook_utils,
                "_read_request_logs",
                return_value=(exchanges, None),
            ) as read_logs,
            patch("sys.stdout", new_callable=StringIO) as output,
        ):
            notebook_utils.show_inference_response(
                response,
                username="alice",
                model="demo-model",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        self.assertIn("Request denied", output.getvalue())
        self.assertIn("entitlement checked", output.getvalue())
        self.assertFalse(read_logs.call_args.kwargs["expects_usage_event"])


class SimulatorLogTests(unittest.TestCase):
    def setUp(self):
        self.started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.entitlement = {
            "message": "entitlement checked",
            "received": {"customer": "alice", "model": "demo-model"},
        }
        self.event = {
            "message": "CloudEvent received",
            "received": {
                "body": {
                    "subject": "alice",
                    "data": {"user": "alice", "model": "demo-model"},
                }
            },
        }
        self.raw_logs = rendered_log(
            "2026-01-01T00:00:00.000Z", self.entitlement
        ) + rendered_log("2026-01-01T00:00:01.000Z", self.event)

    def test_selects_entitlement_and_event_for_allowed_request(self):
        selected = notebook_utils._select_request_exchanges(
            self.raw_logs,
            started_at=self.started_at,
            username="alice",
            model="demo-model",
            expects_usage_event=True,
        )
        self.assertEqual(2, len(selected))
        self.assertEqual("entitlement checked", selected[0]["record"]["message"])
        self.assertEqual("CloudEvent received", selected[1]["record"]["message"])

    def test_selects_only_entitlement_for_denied_request(self):
        selected = notebook_utils._select_request_exchanges(
            self.raw_logs,
            started_at=self.started_at,
            username="alice",
            model="demo-model",
            expects_usage_event=False,
        )
        self.assertEqual(1, len(selected))

    def test_log_command_error_is_returned_for_presentation(self):
        error = subprocess.CalledProcessError(
            1, ["oc", "logs"], output="forbidden"
        )
        with patch.object(
            notebook_utils.subprocess,
            "check_output",
            side_effect=error,
        ):
            selected, message = notebook_utils._read_request_logs(
                started_at=self.started_at,
                username="alice",
                model="demo-model",
                expects_usage_event=True,
            )

        self.assertEqual([], selected)
        self.assertEqual("forbidden", message)

    def test_log_polling_times_out_without_matching_records(self):
        with (
            patch.object(notebook_utils.subprocess, "check_output", return_value=""),
            patch.object(notebook_utils.time, "monotonic", side_effect=[0.0, 6.0]),
            patch.object(notebook_utils.time, "sleep") as sleep,
        ):
            selected, message = notebook_utils._read_request_logs(
                started_at=self.started_at,
                username="alice",
                model="demo-model",
                expects_usage_event=True,
            )

        self.assertEqual([], selected)
        self.assertIsNone(message)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
