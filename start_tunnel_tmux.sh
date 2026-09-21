#!/bin/bash
SESSION_NAME="ssh-connect-session"

tmux has-session -t $SESSION_NAME 2>/dev/null

if [ $? != 0 ]; then
    echo "Creating new tmux session: $SESSION_NAME"
    tmux new-session -d -s $SESSION_NAME
    
    tmux send-keys -t $SESSION_NAME "python3 -u -c 'import urllib.request, time; exec(\"while True:\\n try:\\n  urllib.request.urlopen(\\\\'http://127.0.0.1:17850/\\\\', timeout=2)\\n  print(\\\\'[OK] Tunnel active\\\\')\\n except Exception as e:\\n  print(\\\\'[DOWN]\\\\', e)\\n time.sleep(30)\")'" C-m
    tmux split-window -v -t $SESSION_NAME
    tmux send-keys -t $SESSION_NAME "echo 'Tunnel monitor ready for repo D:/Projects/SSH-Connect'" C-m
fi

echo "Tmux session '$SESSION_NAME' is ready. Attach with: tmux attach -t $SESSION_NAME"
