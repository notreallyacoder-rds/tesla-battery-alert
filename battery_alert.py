
import json,os,sys,time
try: import requests
except: import subprocess; subprocess.check_call([sys.executable,"-m","pip","install","requests","--break-system-packages","--quiet"]); import requests
THRESHOLD=30; STATE_FILE="battery_state.json"; TOKENS_FILE="tesla_tokens.json"
BASE_URL="https://fleet-api.prd.na.vn.cloud.tesla.com"; TOKEN_URL="https://auth.tesla.com/oauth2/v3/token"
SCOPES="openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"
WAKE_RETRIES=8; WAKE_DELAY=5
