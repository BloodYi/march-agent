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
- Request-scoped errors: native session summaries can carry an old failure. It becomes a historical notice only when native steps prove it predates the completed current turn; current or unverifiable failures remain errors.
- Exact returned model fields, independent of the selected preset. Values such as `fn`, `exp-a`, `exp-b`, and future variants are retained without a hardcoded routing table.
- Image/file uploads, server workspace selection, and downloads of generated files from allowed directories.
- SSE chat plus `/v1/chat/completions` and `/v1/models` compatibility endpoints. These endpoints share the site's session authentication.

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

An administrator can run `sudo loginctl enable-linger "$USER"` to keep the user service active after SSH logout. After updating, reinstall requirements and restart `march-agent`; retain `.env`, account storage, and conversation data.

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
```

Tests use a temporary project and user directory, read no account credentials, and make no online model calls. GitHub Actions runs the same checks on Python 3.10 and 3.12. Coverage includes returned model fields, request isolation, Medium arguments, thinking separation, forwarding errors, cancellation cleanup, authentication, and file endpoints.

## Attribution

Based on [ericmaster/antigravity-chat](https://github.com/ericmaster/antigravity-chat), with the original [MIT license](LICENSE) retained. This is an unofficial project. See [NOTICE.md](NOTICE.md) for character and artwork attribution.
