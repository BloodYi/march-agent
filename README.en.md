# March's Agent · 小三月的 Agent

[中文](README.md) · [Attribution](NOTICE.md)

March's Agent is an anime-themed web interface for the native Antigravity CLI (`agy`). It brings streaming conversations, agent activity, server files, and returned model information to desktop and mobile browsers. March 7th provides circular message avatars and animated status expressions, while Firefly and Castorice frame the conversation.

The application uses Python, FastAPI, and plain HTML/CSS/JavaScript. No frontend build is required. Model requests use your own authenticated `agy` account and its normal quota.

## Features

- Responsive desktop sidebar and mobile drawer, with a portrait composition designed around 19.8:9 displays that includes both characters.
- Switch between the original background and a scenic duo theme; adjust colors, shading, blur, and character decorations.
- March 7th expressions for idle, thinking, searching, working, waiting, paused, completed, and error states.
- Create, resume, rename, pin, and delete conversations. Server-side message storage survives refreshes; elapsed conversation time does not automatically delete history.
- Model list from `agy models`, account management, and quota information.
- Streaming replies, separate upstream-provided thinking, tool events, and stop controls. Content absent from the upstream response is not invented.
- A 15-second heartbeat during model/tool waits, explicit stream completion checks, and partial-reply preservation on disconnection. Interrupted requests are not replayed automatically.
- Request-scoped errors: native session summaries can carry an old failure. It becomes a historical notice only when native steps prove it predates the completed current turn; current or unverifiable failures remain errors.
- Exact returned model fields, independent of the selected preset. Values such as `fn`, `exp-a`, `exp-b`, and future variants are retained without a hardcoded routing table.
- Image/file uploads, server workspace selection, and downloads of generated files from allowed directories.
- SSE chat plus `/v1/chat/completions` and `/v1/models` compatibility endpoints. These endpoints share the site's session authentication.
- GitHub update notifications and deliberate installation from the sidebar, with environment preparation, backups, and startup verification.

## Deployment

These instructions target a Linux VPS with Python **3.10+**, Git, and a working authenticated `agy`. The verified combination is `agy 1.3.3` with desktop profile `2.22.0`.

### 1. Install and authenticate the CLI

Use the [official Antigravity installation guide](https://www.antigravity.google/docs/cli/install/). The Linux installer is:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
agy
```

Complete the CLI's first-launch login. Run the website and CLI as the same Linux user. No Google accounts or usable login credentials are included in this repository.

### 2. Install the website

On Ubuntu / Debian, install `git`, `python3`, `python3-venv`, and `curl` if needed.

```bash
git clone https://github.com/BloodYi/march-agent.git "$HOME/march-agent"
cd "$HOME/march-agent"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

Set your own `BASIC_AUTH_USER` and `BASIC_AUTH_PASS`. These are the **website credentials**, separate from your Google login. To access existing projects, set `AGY_ALLOWED_ROOTS` to their directories, for example `/home/your-user/projects:/srv/projects`.

```bash
bash start.sh
```

The default address is `127.0.0.1:26903`. Check the service with:

```bash
curl http://127.0.0.1:26903/healthz
```

### 3. Domain and Cloudflare Tunnel

Route your existing tunnel hostname to `http://127.0.0.1:26903` and access it through HTTPS. The login uses a Secure Cookie and requires HTTPS.

See [deploy/cloudflared.yml.example](deploy/cloudflared.yml.example). Replace the tunnel ID, credential path, and hostname with your own values, and merge the ingress rule into your existing configuration. Keep tunnel credentials outside Git.

### 4. Background service

The service template assumes installation at `~/march-agent`. Edit its two paths if you use another location.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/march-agent.service.example ~/.config/systemd/user/march-agent.service
systemctl --user daemon-reload
systemctl --user enable --now march-agent
systemctl --user status march-agent
```

An administrator can run `sudo loginctl enable-linger "$USER"` to keep the user service active after SSH logout.

## Web update checks

After signing in, use **检查更新 (Check updates)** at the bottom of the sidebar. On a phone, open the top-left menu first. The page checks on opening and every 30 minutes while visible, and displays a notification when a new version is available. Installation always requires a click.

Manual checks use a minimum 30-second interval; ordinary results are cached on the server for 15 minutes. **更新到最新版 (Update to latest)** installs the verified commit from the configured public GitHub repository's `main` branch. Version checks make no model requests and use no model quota.

These updates cover the website project, separately from the native `agy` executable. Updating the website does not upgrade `agy`, reauthorize Google accounts, or change models. Manage CLI upgrades separately using its official instructions.

Installation requires Linux, Git, `python3-venv`, a systemd user service, and a project directory writable by the website's user. Use the Git clone and service template above, with a single website process. Configure in `.env`:

```dotenv
AGY_WEB_UPDATE_ENABLED=1
AGY_UPDATE_REPO=BloodYi/march-agent
AGY_UPDATE_SERVICE=march-agent.service
```

The service name must match the systemd user service running this website. A public fork can change `AGY_UPDATE_REPO`, provided Git `origin` matches it. Set `AGY_WEB_UPDATE_ENABLED=0` to disable web installation. ZIP-only installations lack Git version metadata and show why one-click installation is unavailable.

The updater downloads source, creates a fresh Python environment, installs requirements, and checks startup before switching versions. The existing site stays active during preparation. An active reply prevents an update from starting, and new messages are paused during installation. Tracked local edits, conflicting custom files, or unrelated remote history stop the update and preserve local content.

Only project source is synchronized. `.env`, accounts, conversations, uploads, and the listening port remain local. Backups and job records are kept under `.march-agent-updater/` on the server; Git ignores them and they are never uploaded to GitHub. Switching versions briefly restarts the website. A failed startup check triggers recovery of the previous source and environment; failed recovery is reported for administrator attention. Backups are retained for administrators to manage according to storage and recovery needs.

Restarting invalidates the current website login. The update window can continue showing this job's progress. When finished, use **刷新页面 (Refresh page)** and sign in with the same credentials. The refresh button temporarily saves an unsent text draft. If GitHub is unavailable, a check reports failure rather than claiming the installation is current.

## Configuration

| Setting | Purpose |
| --- | --- |
| `HOST` / `PORT` | Defaults: `127.0.0.1` / `26903` |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | Your website credentials; required by the startup entry point |
| `AGY_BIN` | CLI executable name or absolute path |
| `AGY_UPSTREAM_PROFILE` | `desktop` for desktop compatibility; `native` for the original CLI profile |
| `AGY_DESKTOP_VERSION` | Desktop profile version; currently verified as `2.22.0` |
| `AGY_ALLOWED_ROOTS` | Folder picker and download boundaries; colon-separated on Linux |
| `AGY_TIMEOUT` | Per-turn timeout, default 1800 seconds; stops a turn without deleting history |
| `AGY_SANDBOX` | `1` enables the CLI sandbox; actual privileges depend on the Linux user and CLI |
| `AGY_DISABLE_MCP` / `AGY_DISABLED_MCP_SERVERS` | Disable all or selected MCP tool services |

See [.env.example](.env.example). Uploads default to `uploads/`, messages to `data/messages/`, and the conversation index to `.antigravity_conversations.json`. Git ignores these private runtime files.

## Medium and returned model information

A reproducible difference was observed with the same account and `gemini-3.8-flash-medium`: the native CLI profile returned `gemini-3.8-flash-n`, while the desktop profile returned `gemini-3.8-flash-exp-b`. The CLI genuinely selected Medium with a thinking budget of 4000. The upstream configuration differed by client identity.

The `desktop` option uses a temporary loopback forwarder for each turn to match the verified desktop HTTP User-Agent. The native CLI still handles requests, tools, and session storage. The forwarder preserves request bodies and response bytes, changing only the client header, and connects only to the configured official Google HTTPS host. It neither changes Medium to High nor rewrites returned model names.

Upstream experiments can change: **Medium is not guaranteed to always return exp-b**. The UI displays evidence for the current request, retains multiple values, and reports unavailable fields honestly. Historical evidence is separate from a fresh check. “Check current model” sends a short new request and uses normal account quota. Revalidate after client upgrades, or use `AGY_UPSTREAM_PROFILE=native` to restore the original profile.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/test.py
node tests/test_sse_client.mjs
```

Tests use a temporary project and user directory, read no account credentials, and make no online model calls. GitHub Actions runs the same checks on Python 3.10 and 3.12. Coverage includes returned model fields, request isolation, Medium arguments, thinking separation, forwarding errors, cancellation cleanup, authentication, file endpoints, update authorization, reply/update locking, runtime data preservation, and failed-update recovery.

## Attribution

Based on [ericmaster/antigravity-chat](https://github.com/ericmaster/antigravity-chat), with the original [MIT license](LICENSE) retained. This is an unofficial project. See [NOTICE.md](NOTICE.md) for character and artwork attribution.
