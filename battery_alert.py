#!/usr/bin/env python3
"""
Tesla Battery Alert (Fleet API) — runs in GitHub Actions every 30 minutes.
Uses vehicle_charging_cmds scope only (vehicle_device_data not required).

Sends a push notification via ntfy.sh ONLY when the battery drops to/below
30% while the car is not charging. Silent on repeat runs until battery recovers.

Required GitHub Secrets:
    TESLA_CLIENT_ID     — from developer.tesla.com app registration
    TESLA_CLIENT_SECRET — from developer.tesla.com app registration
    TESLA_REFRESH_TOKEN — obtained via reauth_tesla_v2.py
    NTFY_TOPIC          — your private ntfy.sh topic name
"""

import json
import os
import subprocess
import sys
import time

# ── Config ─────────────────────────────────────────────────────────────────────
THRESHOLD   = 30
STATE_FILE  = "battery_state.json"
TOKENS_FILE = "tesla_tokens.json"
BASE_URL    = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL   = "https://auth.tesla.com/oauth2/v3/token"
SCOPES      = "openid offline_access vehicle_charging_cmds"
WAKE_RETRIES = 8
WAKE_DELAY   = 5
# ───────────────────────────────────────────────────────────────────────────────


def pip_install(package):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", package,
         "--quiet", "--break-system-packages"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


try:
    import requests
except ImportError:
    pip_install("requests")
    import requests


# ── Token management ───────────────────────────────────────────────────────────

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {"refresh_token": os.environ["TESLA_REFRESH_TOKEN"]}


def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def get_access_token(tokens):
    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     os.environ["TESLA_CLIENT_ID"],
            "client_secret": os.environ["TESLA_CLIENT_SECRET"],
            "refresh_token": tokens["refresh_token"],
            "scope":         SCOPES,
        },
        timeout=30,
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    save_tokens(new_tokens)
    return new_tokens["access_token"]


# ── Battery state ──────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"notified": False, "last_level": 100}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Notifications ──────────────────────────────────────────────────────────────

def send_push(topic, vehicle_name, battery_level):
    message = f"{vehicle_name} is at {battery_level}% — plug in soon!"
    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title":    "🔋 Tesla Battery Low",
            "Priority": "high",
            "Tags":     "battery,car,warning",
        },
        timeout=10,
    )
    resp.raise_for_status()
    print(f"📲 Push sent: {message}")


# ── Vehicle helpers ────────────────────────────────────────────────────────────

def get_vehicles(headers):
    """Try /vehicles first, then /products as fallback."""
    resp = requests.get(f"{BASE_URL}/api/1/vehicles", headers=headers, timeout=15)
    if resp.ok:
        vehicles = resp.json().get("response", [])
        if vehicles:
            return vehicles
    print(f"  /vehicles returned {resp.status_code} — trying /products...")

    resp2 = requests.get(f"{BASE_URL}/api/1/products", headers=headers, timeout=15)
    if resp2.ok:
        products = resp2.json().get("response", [])
        # Vehicles have 'vin' and numeric 'id'
        vehicles = [p for p in products if "vin" in p and "id" in p]
        if vehicles:
            return vehicles

    # Both failed — raise the original error for visibility
    resp.raise_for_status()
    return []


def wake_vehicle(vehicle_id, headers):
    """Attempt to wake the car. Returns True if online; False if forbidden/timeout."""
    for attempt in range(1, WAKE_RETRIES + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/api/1/vehicles/{vehicle_id}/wake_up",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 403:
                print("  wake_up: 403 (scope limitation) — will read last-known state.")
                return False
            if resp.ok:
                state = resp.json().get("response", {}).get("state", "")
                if state == "online":
                    print(f"  Vehicle online after {attempt} attempt(s).")
                    return True
        except Exception as e:
            print(f"  wake_up attempt {attempt} error: {e}")
        time.sleep(WAKE_DELAY)
    return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ntfy_topic = os.environ["NTFY_TOPIC"]

    # Auth
    tokens = load_tokens()
    access_token = get_access_token(tokens)
    headers = {"Authorization": f"Bearer {access_token}"}

    # List vehicles
    vehicles = get_vehicles(headers)
    if not vehicles:
        print("No Tesla vehicles found — nothing to check.")
        return

    vehicle      = vehicles[0]
    vehicle_id   = vehicle["id"]
    vehicle_name = vehicle.get("display_name", "Your Tesla")

    # Wake car (best-effort — won't fail the run if forbidden)
    print(f"Waking {vehicle_name}...")
    awake = wake_vehicle(vehicle_id, headers)
    if not awake:
        print("  Proceeding with last-known charge state.")

    # Fetch charge state (works with vehicle_charging_cmds scope)
    resp = requests.get(
        f"{BASE_URL}/api/1/vehicles/{vehicle_id}/charge_state",
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    charge = resp.json()["response"]

    battery_level  = charge["battery_level"]
    charging_state = charge["charging_state"]   # "Charging" / "Disconnected" / etc.
    is_charging    = charging_state.lower() in ("charging", "complete")

    print(
        f"{vehicle_name} — Battery: {battery_level}% | "
        f"Charging: {charging_state}"
    )

    # State-based alert logic
    state = load_state()

    if battery_level > THRESHOLD and state["notified"]:
        print(f"Battery recovered to {battery_level}% — resetting alert.")
        state["notified"] = False

    if battery_level <= THRESHOLD and not is_charging and not state["notified"]:
        send_push(ntfy_topic, vehicle_name, battery_level)
        state["notified"] = True
    elif battery_level <= THRESHOLD and state["notified"]:
        print(f"Still below threshold ({battery_level}%) — alert already sent.")
    elif battery_level <= THRESHOLD and is_charging:
        print(f"Battery at {battery_level}% but {charging_state} — no alert.")
    else:
        print(f"Battery OK ({battery_level}% > {THRESHOLD}%) — no alert needed.")

    state["last_level"] = battery_level
    save_state(state)


if __name__ == "__main__":
    main()
