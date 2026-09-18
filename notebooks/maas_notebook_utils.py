"""Presentation helpers for the MaaS interaction notebook."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import urllib3


SIMULATOR_NAMESPACE = "external-metering"
SIMULATOR_WORKLOAD = "deployment/external-metering-simulator"
LOG_WAIT_SECONDS = 5


def disable_tls_warnings() -> None:
    """Hide warnings for the demo cluster's self-signed certificates."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def load_env(env_path: str | Path | None = None) -> Path | None:
    """Load environment variables from a .env file into os.environ.

    Searches in env_path, the current working directory, or the project root.
    Supports comments (#), optional quotes, and 'export ' prefixes.
    Existing os.environ variables take precedence unless empty.
    """
    candidates = (
        [Path(env_path).expanduser().resolve()]
        if env_path
        else [
            (Path.cwd() / ".env").resolve(),
            (Path.cwd().parent / ".env").resolve(),
            (Path(__file__).resolve().parent.parent / ".env").resolve(),
        ]
    )

    seen: set[Path] = set()
    unique_candidates = [p for p in candidates if not (p in seen or seen.add(p))]
    resolved_file = next((p for p in unique_candidates if p.is_file()), None)
    if not resolved_file:
        return None

    with open(resolved_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and (key not in os.environ or not os.environ[key]):
                os.environ[key] = val

    return resolved_file


def set_kubeconfig(kubeconfig_path: str | Path | None = None) -> str | None:
    """Set the KUBECONFIG environment variable for the process and child commands.

    Expands user home shortcuts ('~'), validates file existence, and sets
    os.environ['KUBECONFIG']. If kubeconfig_path is None or empty, unsets KUBECONFIG.
    """
    if not kubeconfig_path:
        os.environ.pop("KUBECONFIG", None)
        print("KUBECONFIG unset. Using default (~/.kube/config).")
        return None

    path = Path(kubeconfig_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Kubeconfig file not found at: {path}")

    os.environ["KUBECONFIG"] = str(path)
    print(f"Active KUBECONFIG set to: {path}")
    return str(path)


def get_openshift_server() -> str:
    """Return the active OpenShift cluster API URL."""
    return subprocess.check_output(
        ["oc", "whoami", "--show-server"], text=True
    ).strip()


def get_openshift_identity() -> tuple[str, str, str]:
    """Return the username, access token, and cluster server URL from the active oc session."""
    server = get_openshift_server()
    username = subprocess.check_output(["oc", "whoami"], text=True).strip()
    access_token = subprocess.check_output(
        ["oc", "whoami", "-t"], text=True
    ).strip()
    return username, access_token, server


def show_openshift_identity(
    username: str, access_token: str, server: str | None = None
) -> None:
    """Print a presentation-safe identity summary."""
    if server:
        print("Cluster:    ", server)
    print("User:       ", username)
    print("Token prefix:", access_token[:15] + "…")


def show_json_response(response: Any) -> None:
    """Validate and pretty-print a JSON HTTP response."""
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


def show_api_key_response(response: Any) -> tuple[str, str]:
    """Print a safe API-key summary and return the key and its ID."""
    response.raise_for_status()
    payload = response.json()
    api_key = payload["key"]
    api_key_id = payload["id"]

    print("Minted key id:     ", api_key_id)
    print("Key prefix:        ", api_key[:14] + "…")
    print("Bound subscription:", payload["subscription"])
    print("Expires at:        ", payload["expiresAt"])
    return api_key, api_key_id


def request_start_time() -> datetime:
    """Return a timestamp suitable for selecting this request's logs."""
    return datetime.now(timezone.utc) - timedelta(milliseconds=1)


def show_inference_response(
    response: Any,
    *,
    username: str,
    model: str,
    started_at: datetime,
) -> None:
    """Print the inference result and its matching simulator exchanges."""
    if response.status_code == 429:
        print(
            f"Request denied: user {username!r} has exhausted "
            "the simulated $1.00 balance."
        )
    elif response.ok:
        payload = response.json()
        print(
            json.dumps(
                {
                    "model": payload["model"],
                    "usage": payload["usage"],
                    "content": payload["choices"][0]["message"]["content"],
                },
                indent=2,
            )
        )
    else:
        print(f"Inference failed with HTTP {response.status_code}: {response.text}")

    expects_usage_event = response.ok
    request_logs, log_error = _read_request_logs(
        started_at=started_at,
        username=username,
        model=model,
        expects_usage_event=expects_usage_event,
    )
    _show_simulator_logs(request_logs, log_error, expects_usage_event)

    if not response.ok and response.status_code != 429:
        response.raise_for_status()


def show_revocation_response(response: Any) -> None:
    """Validate and print an API-key revocation summary."""
    response.raise_for_status()
    payload = response.json()
    print(
        json.dumps(
            {
                "id": payload["id"],
                "status": payload["status"],
                "expirationDate": payload["expirationDate"],
            },
            indent=2,
        )
    )


def _parse_simulator_logs(raw_logs: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chunk in raw_logs.split("### ")[1:]:
        timestamp_text, separator, payload_section = chunk.partition(" ###\n")
        if not separator:
            continue
        payload_text = payload_section.split("\n\n", 1)[0]
        try:
            record = json.loads(payload_text)
            timestamp = datetime.fromisoformat(
                timestamp_text.replace("Z", "+00:00")
            )
        except (json.JSONDecodeError, ValueError):
            continue
        records.append(
            {
                "timestamp": timestamp,
                "timestampText": timestamp_text,
                "record": record,
            }
        )
    return records


def _select_request_exchanges(
    raw_logs: str,
    *,
    started_at: datetime,
    username: str,
    model: str,
    expects_usage_event: bool,
) -> list[dict[str, Any]]:
    entitlement = None
    usage_event = None

    for log_entry in _parse_simulator_logs(raw_logs):
        if log_entry["timestamp"] < started_at:
            continue

        record = log_entry["record"]
        received = record.get("received", {})
        if (
            entitlement is None
            and record.get("message") == "entitlement checked"
            and received.get("customer") == username
            and received.get("model") == model
        ):
            entitlement = log_entry
            continue

        if entitlement is None or not expects_usage_event:
            continue

        event = received.get("body", {})
        if isinstance(event, dict):
            data = event.get("data", {})
            event_user = event.get("subject") or data.get("user")
        else:
            data = {}
            event_user = None

        if (
            usage_event is None
            and record.get("message") == "CloudEvent received"
            and event_user == username
            and data.get("model") == model
            and log_entry["timestamp"] >= entitlement["timestamp"]
        ):
            usage_event = log_entry

    selected = [entitlement] if entitlement is not None else []
    if usage_event is not None:
        selected.append(usage_event)
    return selected


def _read_request_logs(
    *,
    started_at: datetime,
    username: str,
    model: str,
    expects_usage_event: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    since_time = (
        started_at - timedelta(seconds=1)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    command = [
        "oc",
        "logs",
        SIMULATOR_WORKLOAD,
        "-n",
        SIMULATOR_NAMESPACE,
        "--since-time",
        since_time,
    ]
    deadline = time.monotonic() + LOG_WAIT_SECONDS

    while True:
        try:
            raw_logs = subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as error:
            active_cfg = os.environ.get("KUBECONFIG", "~/.kube/config")
            return [], f"{error.output.strip()} (KUBECONFIG: {active_cfg})"

        selected = _select_request_exchanges(
            raw_logs,
            started_at=started_at,
            username=username,
            model=model,
            expects_usage_event=expects_usage_event,
        )
        expected_count = 2 if expects_usage_event else 1
        if len(selected) >= expected_count or time.monotonic() >= deadline:
            return selected, None
        time.sleep(0.5)


def _show_simulator_logs(
    request_logs: list[dict[str, Any]],
    log_error: str | None,
    expects_usage_event: bool,
) -> None:
    print()
    print("External metering simulator logs for this request:")
    if log_error:
        print(f"Unable to read simulator logs: {log_error}")
        return
    if not request_logs:
        print(f"No matching simulator log records were found within {LOG_WAIT_SECONDS} seconds.")
        return

    for exchange in request_logs:
        print("### " + exchange["timestampText"] + " ###")
        print(json.dumps(exchange["record"], indent=2, sort_keys=True))
        print()
    if expects_usage_event and len(request_logs) < 2:
        print(
            "The entitlement log was found, but the usage-event log "
            f"did not arrive within {LOG_WAIT_SECONDS} seconds."
        )
