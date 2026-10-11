# antigravity-chat

> Historical upstream notes. Port examples have been adjusted to this repository's default. Use [the current README](../README.md) for deployment.
> 原项目的历史说明；端口示例已调整为本仓库默认值。实际部署请看[当前 README](../README.md)。

A **lightweight web chat UI for the real antigravity (`agy`) CLI**. Simple, responsive, single-file FastAPI backend + single static HTML page.

## What it does

- Serves a minimal single-page chat at `/`.
- `POST /api/chat` — SSE stream of assistant replies using `agy` print mode with the prompt attached to `-p=` so CLI flags are parsed correctly.
- **Token-Level Streaming**: NDJSON stream parsing delivers word-by-word token deltas in real-time.
- **Stream Controls**: The UI exposes live status, preserves tool/usage badges, and can stop an in-flight response.
- **Stateful Conversations**: Preserves session state and conversation IDs across turns and browser refreshes (`localStorage` & `.antigravity_conversations.json`).
- **Tool & Usage Metrics**: Visual badges for active tool calls and token usage statistics (`input_tokens` / `output_tokens`).
- `GET /api/models` — live model list from `agy models` (populates the picker).
- **Bonus OpenAI-compatible endpoints** (`/v1/chat/completions`, `/v1/models`) so other OpenAI-protocol tools can reuse it — point their `base_url` at this port.

## Run

```bash
./start.sh                 # → http://0.0.0.0:26903
```

## Config (env vars)

| Var | Default | Description |
|-----|---------|-------------|
| `PORT` | `26903` | HTTP listen port |
| `AGY_BIN` | `agy` | CLI binary |
| `AGY_TIMEOUT` | `1800` | Max seconds per turn |
| `AGY_SANDBOX` | `0` | Set `1` to run agy in `--sandbox` (recommended if exposed publicly) |
| `AGY_OUTPUT_FORMAT` | `stream-json` | Output format for agy (`stream-json` or `text`) |
| `AGY_DISABLE_MCP` | `false` | Disable every MCP server for chat subprocesses when set to `true` |
| `AGY_DISABLED_MCP_SERVERS` | empty | Optional comma-separated MCP server names excluded from chat subprocesses |
| `AGY_RUNTIME_HOME` | `./scratch/.agy-home` | Ephemeral HOME used for the filtered agy subprocess config |
| `AGY_PERSIST_CONVERSATIONS` | `true` | Enable conversation persistence across turns |
| `AGY_CONVERSATIONS_FILE` | `.antigravity_conversations.json` | JSON file storing user conversation mappings |
| `AGY_SCRATCH` | `./scratch` | Neutral cwd for agy (keeps unrelated repos out of context) |

## Systemd Service (Optional)

You can optionally run `antigravity-chat` as a persistent background service managed by `systemd`.

An example service file is provided in [`antigravity-chat.service.example`](antigravity-chat.service.example).

### Setup as a User Service

1. Copy the example service file to your systemd user directory:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp antigravity-chat.service.example ~/.config/systemd/user/antigravity-chat.service
   ```

2. Edit `~/.config/systemd/user/antigravity-chat.service` to match your installation paths (`WorkingDirectory`, `ExecStart`, `Environment`).

3. Enable and start the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now antigravity-chat
   ```

4. Service management commands:
   ```bash
   systemctl --user status antigravity-chat   # Check status
   systemctl --user restart antigravity-chat  # Restart service
   systemctl --user stop antigravity-chat     # Stop service
   journalctl --user -u antigravity-chat -f   # View live logs
   ```

## Notes / limits

- Real-time token streaming is enabled when `AGY_OUTPUT_FORMAT=stream-json`.
- Per-turn latency is whatever `agy` takes (subprocess + model). Pick a `(Low)` Flash model for maximum speed.
- Uses your antigravity subscription/quota — no API keys needed.

## License

This project is open source and available under the [MIT License](LICENSE).
