# AGENTS.md

This file provides guidance to AI coding agents working with code in this repository.

## What this is

Luxmon is lightweight monitoring for a small Proxmox VE cluster. Agents on each host push metrics every 15 s over HTTPS to one server, which stores them in SQLite, serves a password-protected dashboard, evaluates alerts and talks to Telegram. README.md (in Russian) is the user-facing spec of features, alerts, API and install steps — keep it in sync when behaviour changes.

Two parts, named in README and docstrings:
- **Alpha Luxmon** — the core: agent (`agent/luxmon-agent.py`) and server (`server/luxmon-server.py` + package `server/luxmon/`).
- **Omega Luxmon** — the web panel (`server/web/`), which talks to the core only through its HTTP API.

**Hard constraint: Python 3 standard library only, no third-party packages (tests included), no build step, no frontend framework or bundler.** Each of agent and server must stay ~20–40 MB RSS.

## Conventions

- All UI text, Telegram messages, code comments, README and git commit messages are in **Russian**. Commit style: short summary, often `Area: what changed` (e.g. `Безопасность: …`, `Панель: …`). The English UI is produced by translating the finished Russian page (see `i18n.js` below), so write UI strings in Russian.
- Never push directly to `main`: every change goes through a separate branch and a pull request.
- Comments explain *why* (edge cases, Proxmox/Ceph quirks), not what.
- Real configs (`server.json`, `agent.json`, `*.env`, `*.db`, `preview.html`) are gitignored; only `examples/` is tracked.

## Tests (mandatory)

**Run the full test suite before every commit and PR; all tests must pass.** Add or update tests in `tests/` when changing server behaviour. Tests use only `unittest`.

```bash
python3 -m unittest discover tests                 # everything, ~10 s
python3 -m unittest discover tests -k test_alerts  # one module (or class/method name substring)
cd tests && python3 -m unittest test_ingest.Ingest.test_name   # exact test; tests import `common` from their own directory
```

- `tests/common.py` must be imported before any `luxmon` module: the server reads its config at import time, so `common` writes a temp `server.json`, sets `LUXMON_CONFIG`/`LUXMON_TG_ENV` and puts `server/` on `sys.path`. It also provides `reset()` (empties all tables and in-memory alert/ingest state, captures notifications into a list), `Clock` (patches `time.time`) and `payload()` (a minimal agent push). New tables or module-level caches must be added to `reset()`.
- `test_http.py` starts the real server as a subprocess on a free port and talks to it over HTTP; it also calls the `token` CLI.
- On Windows two `test_http.HostTokens` tests are skipped (`needs_geteuid`): the `token` command calls `os.geteuid()`, which is Linux-only. On Linux nothing is skipped.
- CI (`.github/workflows/tests.yml`, GitHub Actions on Ubuntu, Python 3.11 and 3.13) runs on every PR and push to `main`: `py_compile` of all scripts, `bash -n` of the shell scripts, then the full suite. A PR with a red CI is not merged. There is no linter.
- `test_agent.py` loads `agent/luxmon-agent.py` via importlib (stubbing the Unix-only `resource` and `os.sysconf` on Windows) and checks its parsers on saved samples: it patches `agent.read` with a path→content dict and `run_json`/`subprocess.run`/`journal` with canned `pvesh`, `ceph`, `smartctl`, `apt-get`, `journalctl` output. When changing how the agent reads `/proc`, `/sys` or a command's output, add a sample there; module-level state (`C`, `SERIES`, `SSH_STATE`, `LOG_STATE`, `JOURNAL`, `DISK_OPS`) is reset in its `Base.setUp`.

## Running locally

```bash
# server (plain HTTP on :8095 unless tls_cert is set)
LUXMON_CONFIG=./dev-server.json python3 server/luxmon-server.py   # dev-server.json: at least a writable "db" (and "token" or issue host tokens)
LUXMON_CONFIG=./dev-server.json python3 server/luxmon-server.py token add dev1   # print a per-host agent token
LUXMON_CONFIG=./dev-server.json python3 server/luxmon-server.py passwd admin     # dashboard password; `role USER viewer|admin`

# agent
LUXMON_AGENT_CONFIG=./dev-agent.json python3 agent/luxmon-agent.py   # {"server":"http://127.0.0.1:8095","token":"…","host":"dev1","modules":["system"]}
```

Environment variables: `LUXMON_CONFIG` (server config, default `/etc/luxmon/server.json`), `LUXMON_TG_ENV` (Telegram env file, default `/etc/luxmon/telegram.env`; used by the server and `tg-notify.py`), `LUXMON_AGENT_CONFIG` (default `/etc/luxmon/agent.json`; agent and watchdog), watchdog-only `LUXMON_HEALTH_URL`, `LUXMON_CA`, `LUXMON_NOTIFY`. Inside `telegram.env`: `TELEGRAM_*` for alerts, `LUXMON_BOT_TOKEN`/`LUXMON_BOT_CHATS` for the command bot, optional `TELEGRAM_X509_STRICT=0`.

Production: the server runs as user `luxmon` (`deploy/luxmon-server-setup.sh` creates it and fixes ownership); the DB is in `/var/lib/luxmon`. `install.sh`, run on pve1 from the repo, pushes code/units/cert to CT 108 (server + agent), CT 101, nodes pve1–3 and VMs 104–106 via `qm guest exec`, and installs the watchdog on pve1. It never overwrites existing configs. Don't run it from here. It first runs `deploy/luxmon-migrate.py --dry-run` on every host, then, host by host, runs it for real right before deploying that host; the script moves an old Lumen install (`lumen-*` units, `/etc/lumen`, `/var/lib/lumen`) to Luxmon paths; `./install.sh --dry-run` only shows that plan. The migration must never overwrite existing Luxmon data and must check everything before its first change — `tests/test_migrate.py` covers this with a fake root and fake systemd.

## Architecture

### Agent (`agent/luxmon-agent.py`)
- One loop every `interval` s builds a payload: `metrics` (flat numbers from `system()`), `disks`, `info`, `ifaces`, `clock`, plus `cluster` and `series`.
- Optional modules are registered in `MODULES` (name → (fn, interval-config-key)) and run only when their own interval elapsed. A module's return value goes into `payload["cluster"][key]`; key is the module name for cluster-wide data (`pve`, `ceph`, `backups`, `patroni`, `pbs`), or `name:{host}` for per-host data (see `CLUSTER_KEY`). Adding a module = function + `MODULES` entry + `CLUSTER_KEY` entry if per-host + config default in `CFG`.
- `series(group, values)` emits extra time series under groups starting with `_`; the server stores them as pseudo-hosts (`_ceph`, `_etcd`, `_checks`, `_vm<id>`, `_pg_<host>`).
- Reliability: on push failure, cluster modules are re-run next cycle; SSH logins stay in `SSH_STATE["pending"]` and the journal cursor (`/var/lib/luxmon-agent/journal.cursor`) is only saved after a successful push. The server dedupes resent logins.
- The server's push reply may contain `checks`; this is how the dashboard's service-check list reaches the one host chosen as `checks_runner` (empty setting = first host that pushes).

### Server (`server/luxmon-server.py`, `server/luxmon/`)
`luxmon-server.py` is only the entry point: CLI commands (`passwd`, `role`, `token add|del|list`) and startup of three threads — HTTP server (`TLSServer` does the TLS handshake per request), `background()` (every 15 s: `ALERTS.evaluate()`, rollup/prune, morning/weekly summary) and `bot.bot_loop()`. Modules import each other with relative imports and share module-level singletons: `config.CFG`, `store.S`, `alerts.ALERTS`, `auth.SESSIONS`. Importing `config` reads the config file, so import order matters (see tests).

- **`config.py`**: `DEFAULTS`, `RULES_META`, `DEFAULT_SETTINGS`, constants. `RULES_META` is the single source for every alert threshold (default, label, unit, min, max); the settings UI is generated from it. New threshold = `RULES_META` entry + `settings.rules()[key]`. Precedence: `RULES_META` default < `server.json` `rules` < values saved from the dashboard (in `meta`).
- **`store.py`** (`S`): SQLite WAL, one connection behind a lock; `S.q()` for single statements, `S.tx(fn)` for transactions, `S.meta_get/meta_set` for JSON key/value in `meta` (settings, check list, sessions, rollup cursors). Tables: `hosts` (latest payload per host), `raw` (48 h) → `roll` (5-min avg/max, 30 d) → `hourly` (1 h avg/max, 365 d), `cluster` (latest blob per cluster key with source host — whichever agent pushed last wins, so cluster modules can run on several nodes), `events`, `alert_log`, `silence_log`, `logins`, `notes`, `agent_tokens`. Schema changes: `CREATE TABLE IF NOT EXISTS` / inline `ALTER TABLE … ADD COLUMN` in `Store.__init__`.
- **`ingest.py`**: turns a push into rows; SSH login/logout bookkeeping, unknown-IP and brute-force notifications, clock-skew estimate, first-time import of checks from an agent.
- **`alerts.py`** (`ALERTS`): `evaluate()` recomputes all conditions into `found = {key: (level, msg, hold_sec)}` each tick from `hosts`/`cluster`; the class handles hold time, clear delay (`clear_sec`), reminders, silences (scopes `all`, `host:<name>`, `ceph`, `patroni`, `checks`, `backups`, `ssh`, matched by alert-key prefix in `scope_matches`) and sending via `notify_cmd`. Alert keys are `kind:host[:sub]` — the prefix matters for grouping and silencing. One-shot events (reboots, guest start/stop, `/etc/passwd` change) go through `store.event()` + `notify`, not `found`.
- **`analytics.py`** (rollups, forecasts, anomalies, uptime), **`queries.py`** (read models for the API, notes), **`reports.py`** (status text, morning summary, weekly report), **`settings.py`** (effective rules/settings, check validation), **`bot.py`/`telegram.py`**, **`fmt.py`**.
- **Auth**: `passwd.py` (PBKDF2 hashes; `passwd`/`role` write to `server.json`), `auth.py` (sessions stored as sha256 of the cookie in `meta`, bound to the password fingerprint; `users()`/`role()` re-read `server.json` on each call so CLI changes apply without restart). No users → dashboard open. Roles: `admin` (default) and `viewer` (read-only).
- **Agent tokens** (`tokens.py`): each host can have its own token (`agent_tokens`, stored hashed). `push_identity()` returns `("host", name)` or `("shared", None)` for the legacy `token` from `server.json`; a host token can only push for its own host, and the shared token is refused for hosts that have their own.
- **HTTP** (`httpd.py`, `Handler`): `/api/push` — bearer token as above. Every other POST requires header `X-Luxmon: 1` (CSRF guard), a session and role `admin`. `GET /api/health` needs no login. `app.js`, `app.css`, `i18n.js` are served without login (no data in them).
- **CSP**: `static()` adds a per-request nonce to every `<script` tag of `index.html`/`login.html` and sends a strict CSP. So in `server/web/` **never use inline event handlers (`onclick=…`) or `javascript:` URLs** — use `data-open="…"` (delegated click listener in `app.js`) or `addEventListener`. Scripts load only from same origin; external resources only Google Fonts.

### Web panel (`server/web/`)
`index.html` is markup only; logic in `app.js` (vanilla JS: polls `/api/overview`, renders panels with template strings — always escape data with `esc()`; drawers/charts fetch `/api/history` and the other endpoints), styles in `app.css`. `i18n.js` loads first and holds `I18N_EN`: a Russian→English dictionary applied via `tr()` to the rendered page (language from `?lang=en|ru`, then `localStorage`, then browser). New Russian UI strings need an English entry there. Hash routes (`#/pve1`, `#/vm:105`, `#/ceph`, `#/settings`, …) open drawers; `?wall=1` is wall-display mode.

### Other pieces
- `server/tg-notify.py` — the `notify_cmd`: message on stdin → Telegram, using `telegram.env`.
- `deploy/luxmon-watchdog.py` — systemd timer on pve1; alerts to Telegram if the server's `/api/health` is down or stale (the server's CT 108 is under HA and can be frozen during planned node reboots).
- `deploy/server.crt` — the self-signed server cert agents pin via `ca_file`.
