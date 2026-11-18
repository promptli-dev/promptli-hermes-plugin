<img src="assets/icon.png" alt="Promptli Comms" width="96" align="right">

# Promptli Comms for Hermes Agent

Give [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research) its own email address
and phone number. People email, text or call your agent and Hermes answers, each person in their own
session. Hermes can also email, text and phone people itself.

```
person ──email/SMS/call──▶ Promptli Comms ──Agent Link (WebSocket)──▶ Hermes gateway ──▶ Hermes session
                                                                      (promptli platform)   (one per person)
```

- **A real Hermes messaging platform.** Promptli plugs into the Hermes gateway like Telegram or Slack do:
  `promptli` shows up in `hermes gateway status`, `hermes gateway setup`, cron delivery and `hermes send`.
- **No tunnels or webhooks.** The gateway keeps one outbound WebSocket to Promptli and resumes after the
  last acknowledged event, so nothing is lost while your machine sleeps or the gateway restarts.
- **One Hermes session per person.** The same person by email and then by text lands in the same
  conversation. Group texts get one session per group.
- **Replies on the same channel.** Hermes's answer goes back as a threaded email reply, or as SMS
  without markdown. `NO_REPLY` and the gateway's own busy notices are never sent to a person.
- **Calls.** By default Promptli's AI voice assistant takes the call and asks Hermes whenever it needs
  an answer; `PROMPTLI_VOICE=agent` lets Hermes speak every word. Either way the caller is told they are
  talking to an AI, and a promised follow-up is texted to them after the call.
- **Promptli tools** come from the hosted MCP server, signed in with your browser through Hermes's own
  MCP OAuth. Every send, call or schedule asks you for approval first.

> **Security:** anyone who learns the agent's address or number can message it. Senders are checked
> before Hermes sees anything (`PROMPTLI_ALLOWED_USERS`, or `PROMPTLI_ALLOW_ALL_USERS=true` to let
> strangers in), their content is marked as untrusted, and their sessions only get web search and a
> todo list (`PROMPTLI_TOOLSETS`). Hermes can still be talked into using any tool those sessions have,
> so widen the toolsets only for people you trust.

## Install

You need [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started) installed.

```bash
hermes plugins install promptli-dev/promptli-hermes-plugin --enable
hermes promptli setup
hermes mcp login promptli
hermes gateway restart
```

Or use the installer, which runs the first command for you:

```bash
curl -fsSL https://raw.githubusercontent.com/promptli-dev/promptli-hermes-plugin/main/install.sh | sh
```

`hermes plugins install` prepares the plugin's Python packages (`promptli`, `promptli-gateway`) in
Hermes's own environment after asking for your consent (`--yes-deps` answers that up front).

`setup` shows the security warning, then asks for your agent's **identity-scoped** API key in a hidden
prompt. If you paste an organization key instead, setup uses it once to mint an identity-scoped key for
the agent you name and never stores the organization key. The key is saved in Hermes's own `.env` for the
active profile, the way every Hermes platform stores its credentials. Setup then asks who may reach the
agent, your home address for cron jobs, and the call mode. Finally it adds the hosted Promptli MCP server
(`trust: untrusted`), and `hermes mcp login promptli` signs you in with your browser.

Why a key and not a browser login for the gateway? Hermes's plugin rules say a plugin gets credentials
only through Hermes, and the Agent Link WebSocket needs a long-lived agent key. The Promptli tools (MCP)
do use a browser login, held and refreshed by Hermes.

## Commands

| Command | What it does |
| --- | --- |
| `hermes promptli setup` | Interactive setup: key, who may reach the agent, home address, calls, MCP tools |
| `hermes promptli status [--json]` | Agent, address, number, access and call settings |
| `hermes promptli doctor [--json]` | Key, channels, Agent Link, clock, access and MCP checks |
| `hermes promptli mcp` | Add the hosted Promptli MCP server (OAuth, approval on every send) |
| `hermes mcp login promptli` | Log in with your browser for the Promptli tools |
| `hermes gateway setup` | Promptli also appears in Hermes's own gateway setup menu |
| `hermes gateway run` / `restart` | Run the gateway; the `promptli` platform connects with it |

The skill `promptli-comms:promptli-comms` (`skill_view`) teaches Hermes the tools and the rules for calls.

How replies work:
- Hermes's final answer is sent back on the channel the person used.
- If Hermes answers `NO_REPLY`, nothing is sent.
- Messages that are not replies (cron `deliver=promptli`, `hermes send --to promptli:…`) only reach
  `PROMPTLI_HOME_CHANNEL` and `PROMPTLI_ALLOWED_USERS`.

## Configuration

Settings live in the active Hermes profile's `.env` (written by setup), or under
`gateway.platforms.promptli.extra` in `config.yaml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROMPTLI_API_KEY` | (required) | Identity-scoped key, `psk_live_…` or `psk_test_…` (test mode sends nothing) |
| `PROMPTLI_IDENTITY` | from the key | Agent handle |
| `PROMPTLI_ALLOWED_USERS` | empty | Emails and E.164 numbers that may reach the agent. These people may also run Hermes `/commands` by text |
| `PROMPTLI_ALLOW_ALL_USERS` | `false` | `true` lets anyone in (restricted sessions). With neither set, nobody gets in |
| `PROMPTLI_HOME_CHANNEL` | empty | Your email or number, for cron jobs and notifications |
| `PROMPTLI_VOICE` | `hosted` | `hosted`: Promptli's assistant talks, Hermes answers its questions. `agent`: Hermes speaks every word. `off` |
| `PROMPTLI_TOOLSETS` | `web,todo,no_mcp` | Toolsets for Promptli sessions; `inherit` uses `platform_toolsets.promptli` |
| `PROMPTLI_BASE_URL` | `https://api.comms.promptli.com` | For local or test stacks |

Rules the plugin enforces (a `pre_tool_call` hook):
- **Calling hours:** calls to Indian numbers (+91) only between 9 AM and 9 PM IST (TRAI), whether placed
  now or scheduled.
- **AI disclosure:** a call's `opening` must say the caller is an AI; voice turns tell Hermes to say so.
- **Strangers:** in a conversation someone else started, the outbound Promptli tools are refused unless
  that person is in `PROMPTLI_ALLOWED_USERS`.

What it talks to: only Promptli (`api.comms.promptli.com`, and its Agent Link WebSocket while the gateway
runs). It stores its Agent Link cursor, processed event ids and reply routes (which channel and message
each reply belongs to) in `<hermes home>/plugin-data/promptli-comms/state.db`. No telemetry, no shell
commands, no self-update.

## Development

```bash
uv sync
uv run pytest                     # unit tests (Hermes stand-ins, fake Promptli; nothing is sent)
uv run ruff check . && uv run ruff format --check . && uv run pyright
# against a real Hermes checkout (its own venv):
HERMES_AGENT_SRC=../hermes-agent ../hermes-agent/.venv/bin/python -m pytest -m hermes
hermes plugins validate . --install-deps   # the plugin catalog's gate
hermes plugins doctor . --ci
```

Install from a local checkout: `PROMPTLI_HERMES_SOURCE=file://$PWD sh install.sh`.

The runtime-independent pieces live in `promptli-gateway` (the
[promptli-comms](https://github.com/promptli-dev/promptli-comms) repo): framing, the local state store and
the call drivers. This repo adds the Hermes platform adapter (`adapter.py`), the bridge that runs call
turns through Hermes sessions (`bridge.py`), the guard hook, the CLI and the skill.

## License

MIT
