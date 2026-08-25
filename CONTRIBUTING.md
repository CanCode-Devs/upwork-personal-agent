# Contributing

Thanks for helping improve Upwork Personal Agent.

## Setup (Docker)

Follow the Quick start in the [README](README.md). Use `AUTONOMY_MODE=manual` while developing so Connects are not spent by accident.

After Python changes, restart the app: `docker compose restart app` (no `--reload`).

## Setup (without Docker)

Use this only if you already have Python 3.12+ and can install the packages in `requirements.txt` yourself.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp profiles/example.yaml profiles/default.yaml
mkdir -p data
python -m app.cli.warmup_embeddings
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do not add or change dependencies in a PR without saying so in the description. Prefer Docker for a match with production.

## Changes

- Keep secrets out of git (`.env`, `data/`, `profiles/default.yaml`, OAuth tokens).
- Verify dashboard UI in the browser when you change templates or routes.
- Do not add a path that submits proposals without an explicit approve action.

## Pull requests

Open a PR against `main` with a short description of why the change exists and how you tested it.
