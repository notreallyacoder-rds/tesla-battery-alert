#!/usr/bin/env python3
"""
Tesla Battery Alert (Fleet API) — runs in GitHub Actions every 30 minutes.

Sends a push notification via ntfy.sh ONLY when the battery crosses
below 30% while the car is not charging.
Stays silent on repeat runs while it remains below threshold.
Resets when battery recovers above threshold.

Required GitHub Secrets:
    TESLA_CLIENT_ID      — from developer.tesla.com
    TESLA_CLIENT_SECRET  — from developer.tesla.com
    TESLA_REFRESH_TOKEN  — from OAuth flow
    TESLA_VEHICLE_ID     — numeric vehicle ID (3462864856672448)
    NTFY_TOPIC           — ntfy.sh topic name
"""
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "--break-system-packages", "--quiet"])
    import requests

THRESHOLD    = 30
STATE_FILE   = "battery_state.json"
TOKENS_FILE  = "tesla_tokens.json"
BASE_URL     = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_URL    = "https://auth.tesla.com/oauth2/v3/token"
SCOPES       = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"
WAKE_RETRIES = 8
WAKE_DELAY   = 5


# ── token helpers ───────────────────────────────────────────────────────────

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    rt = os.environ.get("TESLA_REFRESH_TOKEN", "")
    return {"refresh_token": rt}


def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def get_access_token(tokens):
    resp = requests.post(TOKEN_URL, json={
        "grant_type":    "refresh_token",
        "client_id":     os.environ["TESLA_CLIENT_ID"],
        "client_secret": os.environ["TESLA_CLIENT_SECRET"],
        "refresh_token": tokens["refresh_token"],
        "scope":         SCOPES,
    }, timeout=30)
    resp.raise_for_status()
    new_tokens = resp.json()
    save_tokens(new_tokens)
    return new_tokens["access_token"]


# ── state helpers ────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"notified": False, "last_level": 100}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── ntfy push ────────────────────────────────────────────────────────────────

def send_push(topic, vehicle_name, battery_level):
    msg = f"{vehicle_name} battery is at {battery_level}% — please plug in!"
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={
                "Title":    "🔋 Low Battery Alert",
                "Priority": "high",
                "Tags":     "battery,car",
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"Push notification sent: {msg}")
    except Exception as e:
        print(f"Failed to send push notification: {e}")


# ── vehicle helpers ──────────────────────────────────────────────────────────

def wake_vehicle(vehicle_id, headers):
    """Try to wake the vehicle; return True if online, False otherwise."""
    for attempt in range(1, WAKE_RETRIES + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/api/1/vehicles/{vehicle_id}/wake_up",
                headers=headers,
                timeout=15,
            )
            if resp.ok:
                state = resp.json().get("response", {}).get("state", "")
                print(f"  wake_up attempt {attempt}: state={state}")
                if state == "online":
                    print(f"  Vehicle online after {attempt} attempt(s).")
                    return True
        except Exception as e:
            print(f"  wake_up attempt {attempt} error: {e}")
        time.sleep(WAKE_DELAY)
    print("  Vehicle did not come online after all retries.")
    return False


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ntfy_topic = os.environ["NTFY_TOPIC"]
    vehicle_id = os.environ["TESLA_VEHICLE_ID"]

    tokens = load_tokens()
    access_token = get_access_token(tokens)
    headers = {"Authorization": f"Bearer {access_token}"}

    vehicle_name = "Your Tesla"

    print(f"Waking vehicle {vehicle_id}...")
    awake = wake_vehicle(vehicle_id, headers)
    if not awake:
        print("  Proceeding with last-known charge state (vehicle may be asleep).")

    print("Fetching vehicle data...")
    resp = requests.get(
        f"{BASE_URL}/api/1/vehicles/{vehicle_id}/vehicle_data",
        params={"endpoints": "charge_state"},
        headers=headers,
        timeout=20,
    )

    if not resp.ok:
        print(f"vehicle_data failed ({resp.status_code}): {resp.text}")
        sys.exit(1)

    charge          = resp.json()["response"]["charge_state"]
    battery_level   = charge["battery_level"]
    charging_state  = charge["charging_state"]
    is_charging     = charging_state.lower() in ("charging", "complete")

    print(f"{vehicle_name} — Battery: {battery_level}% | Charging: {charging_state}")

    state = load_state()

    if battery_level > THRESHOLD and state["notified"]:
        state["notified"] = False
        print(f"Battery above threshold ({THRESHOLD}%), resetting notification flag.")

    if battery_level <= THRESHOLD and not is_charging and not state["notified"]:
        send_push(ntfy_topic, vehicle_name, battery_level)
        state["notified"] = True
    elif battery_level <= THRESHOLD and is_charging:
        print(f"Battery at {battery_level}% but plugged in — no alert needed.")
    elif battery_level <= THRESHOLD and state["notified"]:
        print(f"Battery at {battery_level}% — alert already sent, not repeating.")
    else:
        print(f"Battery at {battery_level}% — above threshold, all good.")

    state["last_level"] = battery_level
    save_state(state)


if __name__ == "__main__":
    main()
