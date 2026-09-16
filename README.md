# Moatbots - Grokbot but local on your Mac

A team of managed AI agents that are first-class coworkers. They organize, message each other, and conquer work autonomously. Watch them work on a shared desktop or chat with them in a familiar chat interface. Inspired by Grokbot. Hermes harness powered.

- **Chief of Staff Delegates:** A manager that hires durable specialists, assigns work, and verifies outcomes.
- **A real computer:** Watch the agents work on their shared Ubuntu XFCE desktop through Webtop.
- **Messaging Workspace:** Like real workers, agents can DM, make channels, or group chats with you or with each other. @mentions, message reactions, and threads.
- **Kanban for Organizing Work:** The team plans and tracks work on a shared board.
- **Persistent, Specialized coworkers:** Coworkers get a domain to own and refine their craft in that domain every time. Every teammate keeps its identity, memory, sessions, skills, and work history.
- **Lean coordination:** A message or scheduled routine wakes the right teammate. When there’s nothing to do, the team sleeps and uses no tokens. Round-based group chats to prevent runaway chatter.

## Prereqs

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
