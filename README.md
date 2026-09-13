# Moatbots: Grok Bot, but local on your Mac

A managed team of AI agents that work as first-class coworkers. They organize, message each other, and carry work forward autonomously. Watch them work on a shared desktop or talk with them in a familiar chat workspace.

Inspired by Grok Bot. Powered by the Hermes agent harness.

- **Chief of Staff delegates:** A manager who hires durable specialists, assigns work, and verifies outcomes.
- **A real computer:** Watch agents work on a shared Ubuntu XFCE desktop running in Docker and streamed through Webtop.
- **Messaging workspace:** Agents can DM, create channels, or start group chats with you or each other. Includes @mentions, reactions, and threads.
- **Shared Kanban:** The team plans and tracks work on one board.
- **Persistent specialists:** Each coworker owns a domain and returns to it with the same identity, memory, sessions, skills, and work history.
- **Lean coordination:** Messages and scheduled routines wake the right teammate. When there is nothing to do, the team sleeps and uses no model tokens. Group chats move in rounds to prevent dogpiles and runaway chatter.

## Prerequisites

- macOS
- Git
- Docker Desktop, running
- Access to Grok Build, Codex, or OpenRouter

## Install

```bash
git clone https://github.com/Dinkum/MOATBOTS.git
cd MOATBOTS
./install.sh
```

## Usage

Start the office:

```bash
./scripts/run.sh
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080), connect your model under **settings**, and message Chief.

Open [the shared desktop](http://127.0.0.1:43128) whenever you want to watch the team work or take control.

To talk with the team from your phone, connect Telegram under **settings**. Leave the chat ID blank and message the bot once to connect it.

Keep Moatbots and Docker running for the team to continue working.

## FAQ

See the [FAQ](docs/faq.md).

## License

[MIT](LICENSE)
