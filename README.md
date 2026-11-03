# Kilo

An autonomous AI agent with an unrestricted shell, scheduled jobs and MCP integrations, running on any model [OpenRouter](https://openrouter.ai) offers. You talk to it through a web terminal served by a single FastAPI process, or through a Telegram bot.

> [!WARNING]
> Kilo runs shell commands with your user's full permissions, with no sandbox and no approval step. Anyone who logs in, or is allowed to talk to its Telegram bot, controls that shell. Run it on a machine or in a container you are willing to hand over.

## Features

- **Agent loop**: streams the model's replies and tool calls; sessions persist in SQLite and long conversations are compacted automatically.
- **Tools**: `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `shell`, `mcp`, `schedule`.
- **Files**: send any file from the web terminal (`/upload`, drag and drop, or paste) or from Telegram. It is saved in `uploads/` in the workspace, and the agent is told where it is, not given its contents.
- **Schedules**: one-off or cron jobs that continue a session when they fire, one per session (a one-off is deleted once it has run). Ask the agent to set them up; each schedule, and each change to one, runs only after you approve it at the prompt (or with the buttons in Telegram).
- **MCP**: connect stdio or streamable HTTP servers; their tools are offered to the agent alongside the built-in ones.
- **Telegram (optional)**: a second interface, for the users you list, in private chats and the groups you list.
- **LINE (optional)**: read and send LINE messages through a bundled MCP server.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and an [OpenRouter API key](https://openrouter.ai/settings/keys).

```bash
uv sync
```

```bash
cp .env.example .env
```

Set `OPENROUTER_API_KEY` in `.env`; the server will not start without it. Then start the server from the project folder:

```bash
uv run kilo-server
```

Open <http://127.0.0.1:8000> and log in with `KILO_USERNAME` and `KILO_PASSWORD`. Type `/help` at the prompt for the slash commands.

## Configuration

Settings come from the environment or from `.env` in the directory you start Kilo from. `.env.example` documents every option. The ones you are most likely to change:

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_MODEL`, `OPENROUTER_REASONING_EFFORT` | Model (any OpenRouter model id) and reasoning effort |
| `KILO_COMPACT_AT_TOKENS` | Conversation size (tokens) that triggers a summary; keep it well below the model's context length |
| `KILO_USERNAME`, `KILO_PASSWORD` | The web terminal's only user |
| `KILO_HOST`, `KILO_PORT` | Listen address |
| `KILO_WORKSPACE` | The agent's working directory |
| `KILO_DATA_DIR` | Databases, task logs, saved tool output, MCP logs |
| `KILO_TIMEZONE` | Default timezone for schedules |
| `KILO_UPLOAD_MAX_MB` | Largest file accepted from the web terminal or Telegram, in MB (default 500) |
| `KILO_TELEGRAM_BOT_TOKEN`, `KILO_TELEGRAM_ALLOWED_USERS`, `KILO_TELEGRAM_ALLOWED_CHATS` | The Telegram bot, who may use it, and in which groups (see below) |

The default password is public, so it works only from the local machine, and the server refuses to start on a non-local `KILO_HOST` with it. To serve beyond localhost, set your own password and put HTTPS in front.

## Files

Files you send are saved in the workspace under `uploads/<date>/` (the date in `KILO_TIMEZONE`). Their names are kept, except that characters a shell could act on become `_`, and a name already used that day gets `-2`, `-3` and so on. The model never receives a file's contents: the message it came with lists each file's path, type and size, and the agent reads the file with its tools when it needs to. For now `read_file` reads text files only.

- **Web terminal**: `/upload` opens a file chooser; you can also drop files on the page or paste them at the prompt. Each one uploads at once and waits at the prompt (`📎2` means two) until you send your next message, which may be empty. `/upload clear` forgets the files waiting; they stay in `uploads/`.
- **Telegram**: a file with a caption goes to the agent with the caption. A file without one is saved and goes with your next message, so you can send a photo and then say what to do with it. An album is one message. A Telegram bot can fetch files of up to 20 MB; send larger ones from the web terminal.

`KILO_UPLOAD_MAX_MB` (default 500) caps a file's size. If a proxy is in front of Kilo, raise its request size limit too (nginx's `client_max_body_size` is 1 MB by default). Nothing is deleted automatically: clear old day folders in `uploads/` yourself.

## MCP servers

Define servers in `.kilo_data/mcp_servers.json` (`KILO_MCP_CONFIG`). Header values may use `${NAME}` to read a secret from the environment or `.env`.

```json
{
  "mcpServers": {
    "local-tool": { "command": "uvx", "args": ["some-mcp-server"] },
    "remote-tool": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ${REMOTE_TOOL_TOKEN}" }
    }
  }
}
```

The agent can also add servers itself through its `mcp` tool.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and set `KILO_TELEGRAM_BOT_TOKEN` to its token.
2. Set `KILO_TELEGRAM_ALLOWED_USERS` to the numeric user ids allowed to use it, comma-separated. A message from anyone else is ignored and logged with the sender's id, so you can find your own id there.
3. Optionally, set `KILO_TELEGRAM_ALLOWED_CHATS` to the group ids (negative numbers) it may join, comma-separated. It leaves any other group it is added to, and logs that group's id.
4. Start `kilo-server`; the bot runs inside it, polling Telegram (no public address needed).

Each chat has a current session, shared with the web terminal: `/new` starts one, `/sessions` switches. A reply shows the tools it used and why, then its answer, as plain text. Messages sent during a reply wait for it; `/stop` stops it. Schedules proposed in a chat come with Approve / Reject buttons, and their runs report back to the chat that last used their session. Files are supported too (see [Files](#files)).

In a group, the bot answers commands, mentions of it and replies to its messages, from the listed users only; other members' messages never reach the agent. But everyone in the group sees what the bot says, so a group uses only sessions made in it, and `/sessions` there lists only those. Any listed user may approve the group's schedules. Telegram topics are not supported.

## LINE integration

Reads and sends messages through a dedicated Chrome running the LINE extension. The browser launcher is Linux-only.

1. Install the LINE extension from the Chrome Web Store in your own Chrome.
2. Run `uv run kilo-line-browser` and keep it running. Log in to LINE in the window it opens.
3. Add the server to `mcp_servers.json`:

   ```json
   { "mcpServers": { "line": { "command": "uv", "args": ["run", "kilo-line-mcp"] } } }
   ```

4. Edit `.kilo_data/line_accounts.json` (`KILO_LINE_ACCOUNTS`), which is created empty on first start and reloaded when it changes:

   ```json
   {
     "chats": ["Exact chat name"],
     "managers": ["Display name"],
     "colleagues": [],
     "bots": []
   }
   ```

Only listed chats can be read or written. An empty `chats` list opens none, and `["*"]` opens every chat. Role labels only match display names, which anyone can copy, so they grant no authority.

## Project layout

```
kilo/
├── agent/       Agent loop, conversation history
├── server/      FastAPI app and REST routes (/api/v1)
├── tools/       Built-in tools, task manager, MCP client
├── scheduling/  Cron parsing, scheduler, run launcher
├── storage/     SQLite persistence
├── telegram/    Telegram bot
├── line/        LINE browser launcher and MCP server
├── models/      Pydantic models
├── web/         Web terminal (static HTML, CSS, JS)
└── assembly.py  Builds and wires every long-lived component
```

The REST API is documented at `/docs` once you are logged in.
