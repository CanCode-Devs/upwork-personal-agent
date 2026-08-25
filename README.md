# Upwork Job Watcher

Public project from [CanCode Devs](https://github.com/CanCode-Devs). A Docker-first local agent that searches Upwork (official MCP), scores jobs against your rules and history, drafts proposals from portfolio context, and submits according to **autonomy mode**.

Released under the [MIT License](LICENSE). You may use, copy, modify, and distribute this software.

## Features

- Poll Upwork for matching jobs
- Score against Settings (budget floors, min score, verified payment, exclude keywords)
- Draft a cover letter, highlights, screening answers, and escrow milestones
- Review in a local dashboard before Connects are spent (`manual` mode)
- Optional auto-submit above a score threshold

## Requirements

- Docker and Docker Compose
- An [OpenAI API key](https://platform.openai.com/api-keys) (for drafting)
- An Upwork freelancer account (OAuth via the official MCP)

The dashboard binds to `127.0.0.1:8000` by default. Keep it local; it can spend Connects when you approve a proposal.

## Quick start

```bash
git clone https://github.com/CanCode-Devs/upwork-proposals.git
cd upwork-proposals
cp .env.example .env
# Set OPENAI_API_KEY, DASHBOARD_PASSWORD, and SESSION_SECRET in .env
mkdir -p data/huggingface profiles
docker compose up --build
```

First start downloads `all-MiniLM-L6-v2` into `./data/huggingface`. Later starts reuse that folder.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` from `.env` (defaults: `admin` / `change-me`).

### Upwork login (once)

OAuth needs a browser on this machine. With Compose running, open the inbox and click **Connect Upwork**. Tokens are stored in `./data/upwork_oauth.json` (gitignored).

CLI alternative:

```bash
docker compose exec app python -m app.cli.upwork_login
```

## Configuration

Copy `.env.example` to `.env` and set at least:

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Drafts cover letters |
| `DASHBOARD_PASSWORD` | Login for the local UI |
| `SESSION_SECRET` | Cookie signing; use a long random string |
| `AUTONOMY_MODE` | `manual`, `auto_above_threshold`, or `fully_auto` |
| `AUTO_SUBMIT_THRESHOLD` | Score needed for auto-submit (default 85) |
| `MIN_SCORE` | Jobs below this are skipped |
| `MIN_FIXED` / `MIN_HOURLY` | Budget floors |

Skills, voice, and search queries live in `profiles/default.yaml`. Edit that file for your own profile.

Uvicorn is started **without** `--reload`. After Python changes:

```bash
docker compose restart app
```

HTML and CSS under `app/` are bind-mounted and update without a restart.

## Autonomy

Set in `.env` (`AUTONOMY_MODE`) or on the Preferences page:

- `manual` — inbox review; you approve before Connects are spent
- `auto_above_threshold` — auto-submit when score >= `AUTO_SUBMIT_THRESHOLD` (default 85)
- `fully_auto` — auto-submit everything at or above min score

## Daily use

1. Inbox shows scored jobs plus a short reason
2. Edit the letter, **Regenerate** if needed, then **Approve & submit**, or **Reject** with a reason
3. Log hired / ignored / shortlisted after the fact so scoring learns
4. Preferences: autonomy, rate floors, strict/soft rules
5. **Poll now** runs a discovery cycle immediately

Keep the machine awake; Compose uses `restart: unless-stopped`.

## Layout

- `Dockerfile` / `docker-compose.yml` — app + embedding warmup
- `app/agent.py` — poll orchestrator
- `app/tools/` — memory, discovery, execution
- `app/embeddings.py` — local vector store (sentence-transformers)
- `app/web` — dashboard
- `profiles/default.yaml` — skills, voice, queries

## Hugging Face cache

| Host | Container |
|------|-----------|
| `./data/huggingface` | `/models/huggingface` (`HF_HOME`) |

The `warmup` service exits immediately if `.all-minilm-l6-v2.ready` exists in that folder.

## Security

Do not commit `.env`, `./data/`, or Upwork OAuth tokens. Approving a proposal spends Connects on your Upwork account. Prefer `manual` until you trust scoring and drafts.

## License

[MIT](LICENSE) © 2026 CanCode Devs. Free to use, modify, and redistribute, including commercially, with the copyright notice retained.

## Contributing

Issues and pull requests are welcome on this repository. Keep secrets out of diffs. Do not add live-submit helpers that skip the dashboard approval flow.
