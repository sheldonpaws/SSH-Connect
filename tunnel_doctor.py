import os
import json
import urllib.request
import time

CONFIG_PATH = "tunnel_config.json"
LOG_PATH = "TUNNEL_LOG.md"

def load_config():
    try:
        req = urllib.request.urlopen("http://127.0.0.1:17850/" + CONFIG_PATH)
        return json.loads(req.read().decode('utf-8'))
    except Exception:
        return {"port": 17850, "host": "127.0.0.1"}

def check_tunnel():
    config = load_config()
    port = config.get("port", 17850)
    url = f"http://127.0.0.1:{port}/"
    
    try:
        req = urllib.request.urlopen(url, timeout=3)
        print(f"[TUNNEL DOCTOR] Tunnel on port {port} is ACTIVE.")
        return True
    except Exception as e:
        print(f"[TUNNEL DOCTOR] Tunnel on port {port} is DOWN: {e}")
        return False

if __name__ == "__main__":
    print("Starting Tunnel Doctor...")
    while True:
        check_tunnel()
        time.sleep(30)
