#!/usr/bin/env python3
"""
Tesla Weekly Battery Summary - runs every Friday at noon via GitHub Actions.
Reads battery_history.json and sends a summary push via ntfy.sh.
Required secret: NTFY_TOPIC
"""
import json, os, sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "--break-system-packages", "--quiet"])
    import requests

HISTORY_FILE = "battery_history.json"

def send_push(topic, title, message):
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "default", "Tags": "battery,car,weekly"},
            timeout=15,
        )
        r.raise_for_status()
        print("Summary sent.")
        print(message)
    except Exception as e:
        print(f"Failed to send summary: {e}")
        sys.exit(1)

def main():
    ntfy_topic = os.environ["NTFY_TOPIC"]

    if not os.path.exists(HISTORY_FILE):
        send_push(ntfy_topic, "Tesla Weekly Summary",
                  "No history yet - check back next Friday after the monitor has been running for a week.")
        return

    with open(HISTORY_FILE) as f:
        history = json.load(f)

    if not history:
        send_push(ntfy_topic, "Tesla Weekly Summary", "History file is empty - nothing to report yet.")
        return

    levels    = [e["level"] for e in history]
    avg_level = round(sum(levels) / len(levels))
    min_level = min(levels)
    max_level = max(levels)

    min_entry = min(history, key=lambda e: e["level"])
    min_ts    = datetime.strptime(min_entry["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    min_label = min_ts.strftime("%a %b %d at %I:%M %p UTC").replace(" 0", " ")

    low_count = sum(
        1 for e in history
        if e["level"] <= 30 and e["charging"].lower() not in ("charging", "complete")
    )

    charge_sessions = 0
    prev_charging = False
    for e in history:
        currently = e["charging"].lower() in ("charging", "complete")
        if currently and not prev_charging:
            charge_sessions += 1
        prev_charging = currently

    first_ts   = datetime.strptime(history[0]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    last_ts    = datetime.strptime(history[-1]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    date_range = f"{first_ts.strftime('%b %d')} - {last_ts.strftime('%b %d')}".replace(" 0", " ")

    low_str    = f"{low_count}x below 30% unplugged" if low_count else "Never below 30% unplugged"
    charge_str = f"{charge_sessions} charging session{'s' if charge_sessions != 1 else ''}"

    lines = [
        f"Week of {date_range} ({len(history)} checks)",
        "",
        f"Avg battery:  {avg_level}%",
        f"Range:        {min_level}% - {max_level}%",
        f"Low point:    {min_level}% on {min_label}",
        f"Low alerts:   {low_str}",
        f"Charging:     {charge_str}",
    ]
    message = "\n".join(lines)
    send_push(ntfy_topic, "Tesla Weekly Battery Summary", message)

if __name__ == "__main__":
    main()
