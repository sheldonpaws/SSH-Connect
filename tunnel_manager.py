import os
import json
import socket
import urllib.request

CONFIG_PATH = "tunnel_config.json"
LOG_PATH = "TUNNEL_LOG.md"
START_PORT = 17850
END_PORT = 17900

def update_tunnel_port(new_port):
    config = {"port": new_port, "host": "127.0.0.1", "active": True}
    data_bytes = json.dumps(config, indent=2).encode('utf-8')
    
    # Записываем обратно через PUT в репозиторий
    req = urllib.request.Request("http://127.0.0.1:17850/" + CONFIG_PATH, data=data_bytes, method='PUT')
    urllib.request.urlopen(req)
    print(f"[TUNNEL MANAGER] Port updated to {new_port} and saved to Windows repo.")

if __name__ == "__main__":
    current_port = START_PORT
    try:
        req = urllib.request.urlopen("http://127.0.0.1:17850/" + CONFIG_PATH)
        current_port = json.loads(req.read().decode('utf-8')).get("port", START_PORT)
    except Exception:
        pass
            
    url = f"http://127.0.0.1:{current_port}/"
    try:
        urllib.request.urlopen(url, timeout=2)
        print(f"[TUNNEL MANAGER] Current port {current_port} is working fine.")
    except Exception:
        print(f"[TUNNEL MANAGER] Current port {current_port} is dead. Scanning for a new port...")
        for p in range(START_PORT, END_PORT):
            test_url = f"http://127.0.0.1:{p}/"
            try:
                urllib.request.urlopen(test_url, timeout=1)
                update_tunnel_port(p)
                break
            except Exception:
                continue
