#!/usr/bin/env python3
"""
Tesla Battery Alert (Fleet API) — runs in GitHub Actions every 30 minutes.

Sends a push notification via ntfy.sh ONLY when the battery crosses
below 30% while the car is parked and not charging.
Stays silent on repeat runs while it remains below threshold.
Resets when battery recovers above threshold.

Required GitHub Secrets:
    TESLA_CLIENT_ID     — from developer.tesla.com app registration
    TESLA_CLIENT_SECRET — from developer.tesla.com app registration
    TESLA_REFRESH_TOKEN — from setup_fleet.py (initial value; auto-refreshed via cache)
    NTFY_TOPIC          — your private ntfy.sh topic name
"""

import json
import os
import subprocess
import sys
import time

# ── Config ──────────────────────────────────────────────────────────────────
THRESHOLD    = 30
STATE_FILE   = "battery_state.json"
TOKENS_FILE  = "tesla_tokens.json"
BASE_URL     = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL    = "https://auth.tesla.com/oauth2/v3/token"
SCOPES       = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"
WAKE_RETRIES = 12      # 12 × 5 s = 60 s max wait for car to wake
WAKE_DELAY   = 5
# ────────────────────────────────────────────────────────────────────────────


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


# ── Token management ─────────────────────────────────────────────────────────

def load_tokens():
    """Load tokens from cache file; fall back to TESLA_REFRESH_TOKEN secret."""
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    # First run — no cache yet; use the secret as the initial refresh token
    return {"refresh_token": os.environ["TESLA_REFRESH_TOKEN"]}


def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def get_access_token(tokens):
    """Exchange refresh token for a fresh access token; save updated tokens."""
    client_id     = os.environ["TESLA_CLIENT_ID"]
    client_secret = os.environ["TESLA_CLIENT_SECRET"]

    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "scope":         SCOPES,
        },
        timeout=30,
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    save_tokens(new_tokens)
    return new_tokens["access_token"]


# ── Battery state ─────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"notified": False, "last_level": 100}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Notifications ─────────────────────────────────────────────────────────────

def send_push(topic, vehicle_name, battery_level):
    title   = "🔋 Tesla Battery Low"
    message = f"{vehicle_name} is at {battery_level}% — plug in soon!"

    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title":    title,
            "Priority": "high",
            "Tags":     "battery,car,warning",
        },
        timeout=10,
    )
    resp.raise_for_status()
    print(f"📲  Push sent: {message}")


# ── Vehicle helpers ───────────────────────────────────────────────────────────

def wake_vehicle(vehicle_id, headers):
    """Poll wake_up until the vehicle reports 'online'."""
    for attempt in range(1, WAKE_RETRIES + 1):
        resp = requests.post(
            f"{BASE_URL}/api/1/vehicles/{vehicle_id}/wake_up",
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            state = resp.json().get("response", {}).get("state", "")
            if state == "online":
                print(f"  Vehicle online after {attempt} attempt(s).")
                return True
        time.sleep(WAKE_DELAY)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ntfy_topic = os.environ["NTFY_TOPIC"]

    # ── Auth ────────────────────────────────────────────────────────────────
    tokens       = load_tokens()
    access_token = get_access_token(tokens)
    headers      = {"Authorization": f"Bearer {access_token}"}

    # ── Vehicle list ─────────────────────────────────────────────────────────
    resp = requests.get(f"{BASE_URL}/api/1/vehicles", headers=headers, timeout=15)
    resp.raise_for_status()
    vehicles = resp.json().get("response", [])

    if not vehicles:
        print("No Tesla vehicles found — nothing to check.")
        return

    vehicle      = vehicles[0]
    vehicle_id   = vehicle["id"]
    vehicle_name = vehicle.get("display_name", "Your Tesla")

    # ── Wake the car ─────────────────────────────────────────────────────────
    print(f"Waking {vehicle_name}...")
    if not wake_vehicle(vehicle_id, headers):
        print("Vehicle did not come online within 60 s — skipping this run.")
        return

    # ── Fetch live data ──────────────────────────────────────────────────────
    resp = requests.get(
        f"{BASE_URL}/api/1/vehicles/{vehicle_id}/vehicle_data",
        params={"endpoints": "charge_state;drive_state"},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()["response"]

    charge = data["charge_state"]
    drive  = data["drive_state"]

    battery_level  = charge["battery_level"]
    charging_state = charge["charging_state"]          # "Charging" / "Disconnected" / etc.
    speed          = drive.get("speed") or 0
    shift_state    = drive.get("shift_state") or "P"  # P / D / R / N

    is_driving  = speed > 0 or shift_state not in ("P", None, "")
    is_charging = charging_state.lower() in ("charging", "complete")

    print(
        f"{vehicle_name} — Battery: {battery_level}% | "
        f"Charging: {charging_state} | "
        f"Shift: {shift_state} | Speed: {speed}"
    )

    # ── State-based alert logic ──────────────────────────────────────────────
    state = load_state()

    # Battery recovered above threshold → reset so we notify next time
    if battery_level > THRESHOLD and state["notified"]:
        print(f"Battery recovered to {battery_level}% — resetting alert state.")
        state["notified"] = False

    # Alert only on the first crossing below threshold
    if (
        battery_level <= THRESHOLD
        and not is_driving
        and not is_charging
        and not state["notified"]
    ):
        send_push(ntfy_topic, vehicle_name, battery_level)
        state["notified"] = True

    elif battery_level <= THRESHOLD and state["notified"]:
        print(f"Still below threshold ({battery_level}%) — alert already sent, skipping.")

    elif battery_level <= THRESHOLD and is_charging:
        print(f"Battery at {battery_level}% but already {charging_state} — no alert.")

    elif battery_level <= THRESHOLD and is_driving:
        print(f"Battery at {battery_level}% but car is moving — skipping.")

    else:
        print(f"Battery OK ({battery_level}% > {THRESHOLD}%) — no alert needed.")

    state["last_level"] = battery_level
    save_state(state)


if __name__ == "__main__":
    main()
