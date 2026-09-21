---
name: reverse-ssh-tunnel
description: "Diagnose and USE reverse SSH tunnels from the server side, including access to Windows PC folders exposed by the SSH-Connect desktop client over port 17850."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [ssh, tunnel, port-forwarding, networking, sshd, windows, pc, folders, reverse-access]
    category: devops
---

# Reverse SSH Tunnel Diagnostics & Windows-PC Folder Access

Serve the server-side host when the user reports a reverse tunnel (`ssh -R`), a
port-forwarding bridge to their machine, or "I see my SSH connections but never
any forwarded ports." The goal: confirm whether the tunnel is healthy, whether
forwarded ports are actually bound, why they are missing — and, when it is up,
how to reach files on the Windows PC that runs the SSH-Connect client.

## When to Use

- User says their reverse tunnel / remote forward "doesn't work", "I don't see
  my folders", or "the forwarded port never appears".
- User asks whether you can see their tunnel from the Linux host.
- Any task where you must tell a local listener apart from a tunnel-forwarded port.
- **User references files on their machine** ("вот файлы проекта", any mention of their local project)
  — ALWAYS run Step 0 first. Never assume files are local on the VPS.
- User asks to read/list/write a file on the Windows PC (a Windows path like
  `C:\Users\...` or `D:\...`).

## Proactive tunnel check (do NOT wait for the user to speak)

The user will NOT always announce that the tunnel is up — they often open it
before a work session and expect you to notice. Check autonomously:

- **At the start of a session** (first turn / when you begin a task involving
their projects), run the readiness probe below. If the tunnel answers, state it
proactively in your reply, e.g.:
  "Я вижу, ты открыл доступ к проекту <name>. Что будем делать в проекте?"
- During a long working session with a tunnel-reachable project, re-probe
periodically (a few turns apart or whenever you're about to read/write a
project file) — the user may swap or re-open the tunnel without announcing it.
- Root mount set can change between sessions (see Pitfalls), so ALWAYS re-read
  the fresh `/` listing rather than trusting an earlier one.

Readiness probe (one line, definitive up/down verdict):
```bash
curl -s --max-time 5 http://127.0.0.1:17850/; echo " exit=$?"
```
`exit=0`+JSON = tunnel up — list the mounts and name the project the user has
open. `exit=7` = tunnel down — do not fabricate files, just report it's closed.

## Accessing Windows PC folders (reverse_access.json)

The SSH-Connect desktop app may expose a Windows folder over the tunnel. The
**manifest** file that maps a Windows folder to a tunnel URL is:

```bash
cat "$HOME/.hermes/reverse_access.json"
```

If `$HOME` differs for the connected user (e.g. you ran as root for a non-root
user), the file lives under THAT user's home.

- The manifest value for a folder is NEVER the base `/`: it is the full URL to
  that folder, matching the desktop app's current multi-folder layout. Always
  preserve the `/slug/` suffix verbatim.

   Format: a JSON object — folder path (Windows) -> tunnel base URL:

```json
{"C:/Users/Me/Desktop": "http://127.0.0.1:17850/desktop/",
 "D:/Projects/Proj": "http://127.0.0.1:17850/proj/"}
```

- URL port is **always 17850** when the tunnel is up; each shared folder has its
  own slug subpath (`/desktop/`, `/proj/`). On the VPS folders are reached as
  `http://127.0.0.1:17850/<slug>/...`.
- A manifest key without `http` URL, or the whole file empty `{}` or missing,
  means the tunnel is NOT running. Never fabricate files.

### Map a Windows path to a URL

1. Take the requested Windows path, e.g. `C:\Users\Me\Desktop\reports\a.txt`.
2. Find the manifest entry whose folder key is a **prefix** of it (case-insensitive,
   forward slashes). Take that entry's URL **as-is** — it already contains the
   `/slug/` subpath, do NOT strip it.
3. Strip the matching folder-prefix from the Windows path, keep the remainder,
   and append it to the base URL with a leading `/`. The server does NOT
   translate Windows paths — the URL only carries the relative part.

The server HTTP API:

| Method | Path       | Meaning                                |
|--------|------------|------------------------------------------|
| GET    | `/`        | List root dir (JSON `entries`)          |
| GET    | `/dir/`    | List a directory                         |
| GET    | `/file`    | Download file (raw bytes)                |
| PUT    | `/file`    | Write file (body, creates dirs)         |
| DELETE | `/path`    | Delete file or directory                 |
| MKCOL  | `/dir`     | Create directory                         |

Directory listing returns JSON: `{"ok": true, "path": "/", "type": "dir",
"entries": [{"name": "reports", "type": "dir", "url": "/reports/"}, ...]}`.
Names may contain Cyrillic/spaces — use percent-encoding (`curl -G
--data-urlencode`, `python3 -c 'urllib.parse.quote'`) or follow the pre-encoded
`url` values from listings.

Examples (`/slug/` comes from the manifest; `/` lists all mounts):

```bash
curl -s http://127.0.0.1:17850/                            # list mounts
curl -s http://127.0.0.1:17850/desktop/                    # list a folder
curl -s http://127.0.0.1:17850/desktop/reports/a.txt       # read file
curl -s -X PUT --data-binary @x.tmp     http://127.0.0.1:17850/proj/new.txt                    # write file
curl -s -X MKCOL http://127.0.0.1:17850/proj/newdir        # create dir
curl -s -X DELETE http://127.0.0.1:17850/proj/old.txt      # delete
```

## Procedure

### Step 0: ALWAYS FIRST — confirm tunnel and resolve manifest

Before touching ANY file the user references, run:

```bash
cat "$HOME/.hermes/reverse_access.json"
ss -ltn | grep 17850
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:17850/
```

1. The JSON maps each Windows folder to its **full** tunnel URL including slug.
2. Use that slug-URL as the base for all subsequent curl/requests calls.
3. If the manifest is empty `{}` or the port is not listening — STOP and tell the user.
4. If the manifest changed since last session (new slug, new path), adapt all file paths.

This step exists because the manifest base path **can change** between sessions if the
user reconfigured SSH-Connect. Missing this step means failed 404s and wasted cycles.

### Step 1–6: Diagnostics and forwarding

1. **List listeners and established connections.**
   `ss -tlnp` shows what is bound; `ss -tnp | grep <peer-ip>` shows active SSH connections. Establish which IP is the dialing client.

2. **Identify every listener BEFORE calling it the tunnel.** For each port of interest, resolve the owner: `readlink /proc/<pid>/exe` and `tr '\0' ' ' < /proc/<pid>/cmdline`. Probe HTTP ports with `curl -sS -m 4 http://127.0.0.1:<port>/ -D-`. Local services (dashboards, nginx, model servers) sit on ordinary-looking ports and are NOT the tunnel.

3. **Forwarded ports never bind → remote forwarding is disabled on the server.** Establish `:22` sessions while the user's `-R` ports stay absent is the signature. The client request is being silently declined.

4. **Check the sshd config.**
   `grep -iE "AllowTcpForwarding|GatewayPorts" /etc/ssh/sshd_config` and `/etc/ssh/sshd_config.d/*`. Commented-out lines mean the DEFAULT applies.

5. **Fix**: set `AllowTcpForwarding yes` (and `GatewayPorts yes` only if access must come from outside the host), then `sudo systemctl restart ssh`, then re-run step 1 to confirm the forwarded port now binds.

6. **Ask for confirmation before touching sshd.** Enabling port forwarding widens the server's attack surface; do not modify it on spec. If the user does not confirm, report the diagnosis and stop.

## Writing files back to the Windows PC

When the user asks to push code/HTML/config changes to their machine, PUT via the tunnel:

```bash
curl -s -X PUT --data-binary @/tmp/local/file.html http://127.0.0.1:17850/<slug>/path/file.html
```

Or with Python requests:

```python
import requests, urllib.parse
with open('/tmp/local/file.html', 'rb') as f:
    content = f.read()
encoded = urllib.parse.quote('/<slug>/path/file.html', safe='/')
r = requests.put(f'http://127.0.0.1:17850{encoded}', data=content)
print(r.json())
```

After PUT, verify with GET and spot-check content (especially Cyrillic — check `.content`
raw bytes + `.decode('utf-8')` if text looks like mojibake).

**Clean up the /tmp copy after a successful PUT.** Files downloaded to /tmp for
editing are transient working copies — once the write is verified on the Windows
PC, remove the /tmp copy (`os.remove` / `rm`) so we leave no residue on the VPS
and don't waste disk. Cleanup is part of the PUT workflow, not optional.

**For editing EXISTING source in place** (Python `content.replace(...)` before PUT):

- `str.replace` is a no-op that returns a copy UNCHANGED when the needle is not
  found — it never errors. A multi-replace edit can therefore commit PARTIALLY.
  Assert every intended edit actually happened (`assert old not in content`
  / `print(marker)` per replacement counting to zero) before you PUT; if one did
  not apply, re-GET the fresh file and examine the real current shape (the
  method/line may have an extra guard you did not anticipate) instead of
  guessing a second needle.
- Re-fetch the file from the tunnel before a second patch pass. A stale `/tmp`
  copy from the first attempt will not reflect the source's actual layout and
  its needles may no longer match what is on disk.

## Pitfalls

- **Manifest base path changes silently.** SSH-Connect may change the slug between sessions
  (e.g. from `/` to `/fangunion/`). If a file returns 404, re-read the manifest first.
- Ubuntu sshd defaults `AllowTcpForwarding` to **no** — a commented line is NOT "yes". A `-R` request then fails silently: no error surfaces on the server, the port never binds. Diagnose the config before blaming the client program.
- `GatewayPorts` defaults to no, so even with forwarding enabled ports bind only to 127.0.0.1. That is fine for a bridge on the same host; enable it only for external access.
- The dialing client's source IP can change (it connects out from wherever it is). A new peer IP on `:22` is still the same tunnel — do not report it as lost or as a different tunnel.
- Multiple ESTAB connections to `:22` are separate SSH channels/fan-out sessions of one tunneled workflow, NOT the forwarded port. Do not present them as "the tunnel port".
- **The root mount set is NOT stable across re-opens.** SSH-Connect serves whichever folder the user last pointed the tunnel at; stopping and re-opening can swap the whole tree (e.g. `/` listed `fangunion`+`ai-dream-agents` one session, then only `barklay-ofice` the next). Re-`curl http://127.0.0.1:17850/` every session and re-read the listing before assuming a previously-seen project is still reachable — do not trust last session's mounts.
- **Tunnel down fingerprint:** a `curl` to `127.0.0.1:17850` returning exit code 7 ("failed to connect") means the tunnel is currently down. Cover it with a one-liner `curl -s --max-time 5 ...; echo exit $?` to give the user a definitive up/down verdict instead of an ambiguous empty body.
- **A drop mid-PUT leaves the file unchanged — report nothing as done on a failed write.** The user often closes/re-opens the SSH-Connect client mid-session ("забыл открыть доступ"); a PUT that hits a dropped tunnel fails with ConnectionRefused (exit 11/7) and NOTHING is written — the on-disk file stays the old version. Re-probe (`curl ...; echo exit $?`) before you PUT and again before you claim success; never tell the user a change landed on a refused PUT.
- **Do not trust a /tmp-prepared file across a tunnel drop.** Content you staged while the tunnel was down can be cleaned from /tmp before access returns. When the tunnel comes back, re-fetch the source fresh and re-apply a self-contained patch (assert each needle before PUT) rather than PUTting a saved /tmp copy whose needles may no longer match.
- Local listeners on common ports (8000 dashboard, 3001 nginx, 17850 agent, 41343 model server) masquerade as tunnel forwards. Always resolve the owning process before concluding. Port **17850** IS the SSH-Connect reverse tunnel when the reverse_access.json manifest points there.
 - Cyrillic file content may appear as mojibake via Python requests without proper encoding.
 Verify with `.content` (raw bytes) + `.decode('utf-8')` when text looks garbled.

 ## Working evidence (for the user)

Report, concretely: which IP dials in, how many ESTAB `:22` sessions, which ports are genuinely bound and by what process, and the specific sshd directive that blocks forwarding. Distinguish "tunnel is up" from "forwarded ports are up" — the former does not imply the latter.


## IMPORTANT: do NOT touch sshd when the tunnel already works

The SSH-Connect client bridges over paramiko `request_port_forward` — the same
mechanism as `ssh -R`, so a forwarded port that is BOUND means remote forwarding
is effectively enabled and nothing needs changing in `/etc/ssh/sshd_config`.

Before ever proposing `AllowTcpForwarding`, run:

```bash
ss -ltn | grep 17850
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:17850/
```

If `ss` shows `127.0.0.1:17850 LISTEN` AND curl returns `200`, the tunnel is up
and working. Do NOT propose sshd changes, do NOT ask to restart sshd (that would
drop the live SSH connection and kill the tunnel). Read the folders directly via
`http://127.0.0.1:17850/`. A commented-out `AllowTcpForwarding` line means the
ssh default **yes** applies — it is not evidence of a problem.
