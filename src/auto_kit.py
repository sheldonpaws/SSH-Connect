import json
import os

def create_tunnel_kit(folder, port=17850):
    """Автоматически создаёт мини-набор tunnel-agent-kit в выбранной пользователем папке."""
    try:
        kit_dir = os.path.join(folder, "tunnel-agent-kit")
        os.makedirs(kit_dir, exist_ok=True)
        
        # tunnel_config.json
        config_path = os.path.join(kit_dir, "tunnel_config.json")
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({"port": port, "host": "127.0.0.1", "active": True}, f, indent=2)
                
        # TUNNEL_LOG.md
        log_path = os.path.join(kit_dir, "TUNNEL_LOG.md")
        if not os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"# Лог обратных подключений\n- Порт: {port}\n- Статус: Активен\n")
                
        print(f"[TUNNEL KIT] Successfully created agent kit in {kit_dir}")
    except Exception as e:
        print(f"[TUNNEL KIT] Error creating kit: {e}")
