# Contributing

Thanks for helping improve Upwork Job Watcher.

## Setup

Follow the Quick start in the [README](README.md). Use `AUTONOMY_MODE=manual` while developing so Connects are not spent by accident.

## Changes

- Keep secrets out of git (`.env`, `data/`, OAuth tokens).
- After Python changes, restart the app: `docker compose restart app` (no `--reload`).
- Verify dashboard UI in the browser when you change templates or routes.
- Do not add a path that submits proposals without an explicit approve action.

## Pull requests

Open a PR against `main` with a short description of why the change exists and how you tested it.
