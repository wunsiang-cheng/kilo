# Kilo

An autonomous AI agent running on any model [OpenRouter](https://openrouter.ai) offers. You talk to it through a web terminal served by a single FastAPI process, or through a Telegram bot.

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
