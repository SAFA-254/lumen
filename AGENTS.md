# AGENTS.md

This file provides guidance to AI coding agents working with code in this repository.

## What this is

Lumen is lightweight monitoring for a small Proxmox VE cluster. Agents on each host push metrics every 15 s over HTTPS to one server, which stores them in SQLite, serves a password-protected dashboard, evaluates alerts and talks to Telegram. README.md (in Russian) is the user-facing spec of features and alerts — keep it in sync when behaviour changes.

**Hard constraint: Python 3 standard library only, no third-party packages, no build step, no frontend framework.** The dashboard is a single hand-written HTML file. Each of agent and server must stay ~20–40 MB RSS.

## Conventions

- All UI text, Telegram messages, code comments, README and git commit messages are in **Russian**. Commit style: short summary, often `Area: what changed` (e.g. `Безопасность: …`, `Панель: …`).
- Never push directly to `main`: every change goes through a separate branch and a pull request.
- Comments explain *why* (edge cases, Proxmox/Ceph quirks), not what.
- Real configs (`server.json`, `agent.json`, `*.env`, `*.db`, `preview.html`) are gitignored; only `examples/` is tracked.

## Before opening a PR

Before `gh pr create`, run a Codex review of the branch against `main` (`codex review --base main` or the codex plugin script). Fix P1/P2 findings, re-run the review, and only then open the PR. Briefly describe in the PR what Codex found and what was fixed.

## Running / testing

There is no test suite, linter, or CI. Verify changes by running the pieces locally:

```bash
# server (plain HTTP on :8095 by default; config path and DB come from env/config)
LUMEN_CONFIG=./dev-server.json python3 server/lumen-server.py      # dev-server.json needs at least "token" and a writable "db"
python3 server/lumen-server.py passwd admin                          # set dashboard password (writes to config, no restart)

# agent pointing at it
LUMEN_AGENT_CONFIG=./dev-agent.json python3 agent/lumen-agent.py     # {"server":"http://127.0.0.1:8095","token":"…","modules":["system"]}

python3 -m py_compile agent/lumen-agent.py server/lumen-server.py server/tg-notify.py deploy/lumen-watchdog.py
```

The agent reads Linux `/proc`, `/sys`, `journalctl`, `pvesh`, `ceph`, `smartctl` etc., so most modules only work on the real hosts (not on Windows). `GET /api/health` is unauthenticated and returns the age of the last push.

Deployment to the production cluster is `install.sh`, run on pve1 from `/root/lumen`: it pushes code/units/cert to CT 108 (server + agent), CT 101, nodes 192.168.3.3–5, and VMs 104–106 via `qm guest exec`, and installs the watchdog on pve1. It never overwrites existing configs. Don't run it from here.

## Architecture

### Agent (`agent/lumen-agent.py`)
- One loop every `interval` s builds a payload: `metrics` (flat numbers from `system()`), `disks`, `info`, `ifaces`, `clock`, plus `cluster` and `series`.
- Optional modules are registered in `MODULES` (name → (fn, interval-config-key)) and run only when their own interval elapsed. A module's return value goes into `payload["cluster"][key]`; key is the module name for cluster-wide data (`pve`, `ceph`, `backups`, `patroni`, `pbs`), or `name:{host}` for per-host data (see `CLUSTER_KEY`). Adding a module = function + `MODULES` entry + `CLUSTER_KEY` entry if per-host + config default in `CFG`.
- `series(group, values)` emits extra time series under groups starting with `_` (e.g. per-guest/OSD graphs); the server stores them as pseudo-hosts.
- Reliability: on push failure, cluster modules are re-run next cycle; SSH logins stay in `SSH_STATE["pending"]` and the journal cursor (`/var/lib/lumen-agent/journal.cursor`) is only saved after a successful push. The server dedupes resent logins.
- The server's push reply may contain `checks`; this is how the dashboard's service-check list reaches the one host chosen as `checks_runner`.

### Server (`server/lumen-server.py`)
Single file, sections marked with `# ----…---- name` comments. Threads: HTTP server (`ThreadingHTTPServer`, TLS handshake done per-request in `TLSServer`), `background()` (every 15 s: `ALERTS.evaluate()`, rollup/prune, morning/weekly summary), `bot_loop()` (Telegram command bot long-polling).

- **Storage (`Store`, global `S`)**: SQLite WAL, one connection guarded by a lock; `S.q()` for reads, `S.tx(fn)` for transactions. Tables: `hosts` (latest payload JSON per host), `raw` (48 h points), `roll` (5-min avg/max, 30 days), `cluster` (latest blob per cluster key, with source host — whichever agent pushed most recently wins, so modules can run on several nodes for redundancy), `events`, `alert_log`, `silence_log`, `logins`, `meta` (key/value JSON: dashboard settings and thresholds, check list, sessions keyed by token hash). Dashboard users/password hashes live in `server.json` (`users`), not the DB. Schema migrations are inline `ALTER TABLE … ADD COLUMN` in `Store.__init__`.
- **Ingest (`ingest`)**: turns a push into rows; also handles SSH login/logout bookkeeping, unknown-IP SSH notifications and first-time import of checks from an agent.
- **Alerts (`Alerts`, global `ALERTS`)**: `evaluate()` recomputes all conditions into `found = {key: (level, msg, hold_sec)}` each tick from `hosts`/`cluster` data; the class handles hold time, clear delay (`clear_sec`), reminders, silences (scopes `all`, `host:<name>`, `ceph`, `patroni`, `checks`, `backups`, `ssh`, matched by alert-key prefix in `scope_matches`) and notification via `notify_cmd`. Alert keys are `kind:host[:sub]` — the prefix matters for grouping and silencing. One-shot events (reboots, guest start/stop, `/etc/passwd` change) go through `event()` + `notify` directly, not through `found`.
- **Thresholds**: `RULES_META` is the single source for every alert threshold (default, label, unit, min, max) — the settings UI is generated from it. New threshold = add a `RULES_META` entry and read it via `rules()[key]`. `server.json` `rules` override defaults; dashboard-saved values in `meta` override those.
- **HTTP (`Handler`)**: `/api/push` uses bearer token (constant-time compare). Every other POST requires header `X-Lumen: 1` (CSRF guard) plus a session cookie. Sessions are bound to the password hash fingerprint, so changing a password kills that user's sessions. If no users exist, the dashboard is open.
- **CSP**: `static()` injects a per-request nonce into every literal `<script>` tag of the HTML. Consequently in `server/web/*.html` **never use inline event handlers (`onclick=…`) or `javascript:` URLs** — use `data-open="…"` (handled by a delegated click listener) or `addEventListener`. Inline styles are allowed; external resources only from Google Fonts.

### Dashboard (`server/web/index.html`)
Vanilla JS in one `<script>`: polls `/api/overview` and renders panels via template strings (always escape data with `esc()`); drawers/charts fetch `/api/history`, `/api/ssh`, `/api/uptime`, `/api/alerts/history`, `/api/settings`. Hash routes (`#/pve1`, `#/vm:105`, `#/ceph`, `#/settings`, …) open drawers; `?wall=1` is wall-display mode.

### Other pieces
- `server/tg-notify.py` — the `notify_cmd`: reads message from stdin, sends via bot token/chat from `/etc/lumen/telegram.env`. The command bot uses a *separate* token (`LUMEN_BOT_TOKEN`) because the main bot is already polled by another program.
- `deploy/lumen-watchdog.py` — runs on pve1 by systemd timer, alerts to Telegram if Lumen's `/api/health` is down or stale (Lumen runs in HA CT 108 that may be frozen during planned node reboots).
- `deploy/server.crt` — the self-signed server cert agents pin via `ca_file`.
