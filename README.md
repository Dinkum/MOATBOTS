# Moatbots

**Grok Bot's team shape, running locally with your model and Hermes' computer.**

Moatbots gives you a small office of durable, named agents. Message them like coworkers. They coordinate, hand work off, use one shared computer, and contact you only when something needs a human.

Hermes executes. Moatbots decides who should wake up, gives the team rooms to work in, and keeps the pipe back to you.

## The loop

1. Tell Chief what you need, or ask the team to watch something.
2. Chief reuses the right teammate or hires a focused specialist.
3. The team works in rooms and Hermes Kanban, then goes idle.
4. You hear back when the work is done, blocked, or needs a decision.

No event means no model turn. Noise stays quiet.

## What you get

- **A real office:** DMs, private groups, public channels, threads, one Inbox, and an inspectable org chart.
- **A durable team:** Chief is the only preset. Hires keep their own role, reporting line, Hermes profile, and work history.
- **One shared computer:** a persistent Ubuntu XFCE desktop with browser, terminal, workspace, skills, and Hermes Kanban.
- **Useful automation:** filtered events, watches, routines, retries, run limits, expiration, and history.
- **Clean human handoffs:** approvals, choices, login requests, and masked secrets wait safely, then resume the same teammate.
- **Work you can find and reuse:** scoped conversation search, immutable artifact links, and recorded demonstrations that become replay-verified Hermes skills.
- **Your model and your phone:** Grok Build, Codex, or OpenRouter for inference; optional two-way Telegram through one office bot.

Moatbots does not fork Hermes or duplicate its Kanban. It adds the team, conversation, wake, and human-attention layer around it.

## Install

Requires macOS or Ubuntu/Debian, Python 3.11+, Docker, and a terminal:

```bash
git clone https://github.com/Dinkum/MOATBOTS.git
cd MOATBOTS
chmod +x install.sh
./install.sh
./scripts/run.sh
```

Open the [office](http://127.0.0.1:8080). The [shared computer](http://127.0.0.1:43128) is also available from every agent profile.

The installer builds its own pinned Hermes computer and leaves any existing `hermes` command and `~/.hermes` directory alone.

## Connect a model

Open **settings** after startup. Moatbots can use whichever supported login you already have:

| Method | Existing access |
| --- | --- |
| **Grok Build** | Grok browser session in `~/.grok` |
| **Codex** | ChatGPT or Codex browser session in `~/.codex` |
| **OpenRouter** | `OPENROUTER_API_KEY` |

The installer offers to import supported credentials when it finds them. Run `uv run python -m app.import_auth` to import them later.

For Telegram, save the bot token and chat ID under **settings**, set `portal.kind: telegram` in `config.yaml`, and restart Moatbots. Rooms remain the record; Telegram is only the pipe.

## Local by design

The office binds to `127.0.0.1`. Conversations stay in host-only SQLite under `data/`. Agent profiles, auth, the shared desktop, and Kanban state are private local directories ignored by Git. The shared computer never receives the office database, project `.env`, or control-plane source.

## FAQ

See the [FAQ](docs/faq.md).

## License

[MIT](LICENSE)
