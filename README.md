# Upwork Personal Agent

A **local** agent for **one** Upwork freelancer on **their own** account. It watches for jobs that match you, skips the ones that fail your rules, and waits for you before it writes a letter or spends Connects.

Upwork treats freelancer accounts as personal. This app uses **one OAuth login** and one Connects balance. You can add dashboard seats (Admin / Reviewer) so hired operators review and send from this UI. They never log into Upwork; they share the token on this machine. Keep the host private (localhost or a VPN).

It is not a cloud SaaS and not an auto-apply bot unless you turn that on. You run it with Docker on your machine. Tokens, letters, and examples stay in `./data/` on disk.

Built by [CanCode Devs](https://github.com/CanCode-Devs). [MIT License](LICENSE).

## What you get

**Discovery**
- Poll Upwork through the official MCP (OAuth in the browser, once)
- Search from your profile queries; **Poll now** when you do not want to wait
- **Suggest search queries** on Settings: one cheap-model pass over your profile, Upwork history, and Portfolio case studies. Add a suggestion before polls use it
- Sync portfolio, contracts, and certificates from Upwork for proof in drafts

**Scoring**
- Two scores on Inbox and the job page: **Relevance** (fit to your skills and the posting) and **Client** (client quality from Upwork signals). Jobs must pass both `min_score` and `min_client_score` to stay in Pending
- Rank against skills, client metrics, and Settings floors (budget, verified payment, rating/hires/spend, max Connects)
- Hard-skip jobs that hit exclude keywords, strict rules, or Settings gates: US work authorization, W-2/no C2C, on-site/local, entry-level, job type (hourly/fixed), engagement (project vs role hire), blocked client countries
- Polls refresh **job activity** on known inbox jobs (proposal count, interviewing)
- Learn from outcomes synced from Upwork (hired, declined, messaged) plus notes you add
- Polls score only. They do not call the writer

**Drafting**
- Open a scored job and click **Write proposal** when you want a letter (larger model: `OPENAI_DRAFT_MODEL`)
- Cover letter, Upwork screening answers, posting “please include” items, and escrow milestones for fixed-price jobs
- Highlights capped at the closest real work (not invented case studies)
- **Proposal** page: hook, **project** vs **role hire** letter structures, never-say list, milestone template, few-shot examples, and **self-critique rounds** (0 skips the auto-review; 1 drafts, grades, and rewrites once if it fails)
- Job page: fill **unproven** screening / “please include” answers before submit, enter an **hourly quote** on hourly jobs, and paste or upload **attachments** when the post references files the tool cannot fetch
- **Regenerate** rewrites a pending draft; it does not submit

**Submit**
- Inbox on `127.0.0.1:8000`: edit, approve, or reject
- **Messages** tab: poll Upwork chats, suggest a reply, send with optional file attachments
- Default **manual** mode: nothing goes to Upwork until you click **Approve & submit**
- Optional auto-submit above a score threshold if you explicitly enable it

## Requirements

- Docker and Docker Compose
- An [OpenAI API key](https://platform.openai.com/api-keys) (for drafting)
- An Upwork freelancer account (OAuth via the official MCP)

The dashboard binds to `127.0.0.1:8000` by default. Keep it local; it can spend Connects when you approve a proposal.

## Quick start

```bash
git clone https://github.com/CanCode-Devs/upwork-personal-agent.git
cd upwork-personal-agent
cp .env.example .env
# Set OPENAI_API_KEY, DASHBOARD_USERNAME, DASHBOARD_PASSWORD, and SESSION_SECRET in .env
# Change DASHBOARD_PASSWORD from change-me; SESSION_SECRET must be a long random string
mkdir -p data/huggingface profiles
cp profiles/example.yaml profiles/default.yaml
# Edit profiles/default.yaml: your name, skills, search queries
docker compose up --build
```

If `profiles/default.yaml` is missing, the app copies `profiles/example.yaml` on boot. First start downloads `all-MiniLM-L6-v2` into `./data/huggingface`. Later starts reuse that folder.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in as the bootstrap **Admin** (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` from `.env`; defaults: `admin` / `change-me`). That account is created on every start. Extra operators are added later on **Users** (see [Admin and access control](#admin-and-access-control)).

Then:

1. **Settings** — budget floors, exclude keywords, autonomy
2. **Proposal** — opening hook, letter structure, never-say list, optional style examples
3. **Connect Upwork** from the inbox
4. **Users** (optional) — add a Reviewer if someone else will draft and submit from this machine

### Upwork login (once)

OAuth needs a browser on this machine. With Compose running, open the inbox and click **Connect Upwork**. Tokens are stored in `./data/upwork_oauth.json` (gitignored).

CLI alternative:

```bash
docker compose exec app python -m app.cli.upwork_login
```

Rewrite pending drafts after you change Proposal settings (does not submit):

```bash
docker compose exec app python -m app.cli.redraft_pending
```

## Configuration

Copy `.env.example` to `.env` and set at least `OPENAI_API_KEY`, `DASHBOARD_PASSWORD`, and `SESSION_SECRET`. `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` create the owner Admin (see [Admin and access control](#admin-and-access-control)).

| Layer | Owns | When it applies |
|-------|------|-----------------|
| `.env` | OpenAI (`OPENAI_MODEL` for chat and query suggestions, `OPENAI_DRAFT_MODEL` for letters; optional `OPENAI_BASE_URL` for compatible APIs), poll interval, dashboard login, `PROFILE_PATH`, `APP_NAME` / `APP_TAGLINE`, `SEED_DEMO_PORTFOLIO`, `EMBEDDING_MODEL` | Always |
| `.env` → SQLite (once) | Scoring floors (`min_score`, `min_client_score`, rate floors), autonomy | Seeded on first boot; **Settings** owns them after that |
| `profiles/default.yaml` | Name, title, rate, skills, search queries, exclude keywords, voice | Identity and search. Voice reaches the writer |
| **Proposal page** | Hook, tone, project vs role structure, critique rounds, must/never lists, extra instructions, milestones, screening, few-shot examples | Every new draft or **Regenerate**. Stored in `./data/` |
| Settings rules | Hard skips (budget/tech/client); `proposal_style` lines in the writer prompt | Active rules |
| `profiles/scoring.yaml` | Optional numeric scoring matrix | If the file exists; otherwise code defaults. Copy from `scoring.example.yaml` |

`SEARCH_QUERIES` in `.env` is only used when the profile YAML has no `search_queries`.

Uvicorn is started **without** `--reload`. After Python changes:

```bash
docker compose restart app
```

HTML and CSS under `app/` are bind-mounted and update without a restart.

## Autonomy

Polls never write or submit a letter. Use **Write proposal** then **Approve & submit** on the job page.

- `manual` — inbox review; you approve before Connects are spent
- `auto_above_threshold` — auto-submit when score >= `AUTO_SUBMIT_THRESHOLD` (default 85)
- `fully_auto` — auto-submit everything at or above min score

## Daily use

1. Inbox shows **Rel** and **Client** scores plus a short reason. Filter by Pending / Applied / Failed / Submitted / Rejected / Skipped; sort from the dropdown. **Needs draft** until you write a letter
2. Open a job, click **Write proposal** if you want to apply, fill unproven answers and the hourly quote if asked, edit, **Regenerate** if needed, then **Approve & submit**, or **Reject** with a reason
3. Polls update the outcome log from proposal status and messages. On applied jobs, add an outcome note so scoring and style learning get extra context
4. Settings: autonomy, rate floors, eligibility and search skips (job type, engagement, countries, Connects, US work auth / W-2 / on-site / entry-level), **Suggest search queries**
5. Proposal: voice, project vs role structure, critique rounds, and examples for the next draft
6. **Portfolio**: **Sync from Upwork** for imported history; add agent notes for off-platform work the writer can retrieve
7. **History** lists submitted, rejected, failed, and expired jobs
8. **Poll now** runs a discovery cycle immediately (score only; no letters)

Keep the machine awake; Compose uses `restart: unless-stopped`.

## Admin and access control

One Upwork freelancer account, one OAuth token, one Connects balance. Dashboard seats are local logins on this machine so hired operators can review and send without logging into Upwork.

### Bootstrap the owner Admin

1. In `.env`, set `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, and `SESSION_SECRET` **before** the first `docker compose up`. Do not leave `DASHBOARD_PASSWORD=change-me`.
2. On every start the app creates that username as **Admin** if it is missing, or re-applies Admin + active and syncs the password from `.env` if the hash differs.
3. Sign in at [http://127.0.0.1:8000](http://127.0.0.1:8000) with those credentials. This env account is the **owner**: you cannot demote or deactivate it from **Users**.

Extra operators are stored in SQLite (`./data/app.db`), not in `.env`. Changing `DASHBOARD_USERNAME` later creates a second owner Admin; it does not rename the old one.

### Add operators (Users page)

While signed in as Admin, open **Users**:

1. **Add operator** — username, password, role (`Reviewer` by default, or `Admin`)
2. Give them the username and password out of band; there is no invite email
3. Later: reset password, change role, or deactivate / reactivate

You cannot demote or deactivate the last remaining Admin. Deactivated users cannot sign in.

| Role | Can | Cannot |
|------|-----|--------|
| **Admin** | Inbox, History, Messages, write/edit/submit drafts, Poll now, Settings, Proposal writer, Portfolio, Connect Upwork, Users | — |
| **Reviewer** | Inbox, History, Messages, write/edit drafts, **Approve & submit**, reject, Poll now | Settings, Proposal page, Portfolio, Connect Upwork, Users |

The job log records which dashboard user edited, approved, or sent. Everyone shares `./data/upwork_oauth.json`. Keep the UI off the public internet.

## Layout

- `Dockerfile` / `docker-compose.yml` — app + embedding warmup
- `app/agent.py` — poll orchestrator
- `app/tools/` — memory, discovery, execution
- `app/embeddings.py` — local vector store (sentence-transformers)
- `app/web` — dashboard (Inbox, Messages, Portfolio, Proposal, Settings, History, Users)
- `profiles/example.yaml` — generic starter profile (committed)
- `profiles/default.yaml` — your profile (gitignored; copy from example)
- `profiles/scoring.example.yaml` — scoring matrix template

## Hugging Face cache

| Host | Container |
|------|-----------|
| `./data/huggingface` | `/models/huggingface` (`HF_HOME`) |

The `warmup` service exits immediately if `.all-minilm-l6-v2.ready` exists in that folder.

## Security

Do not commit `.env`, `profiles/default.yaml`, `./data/` (includes Proposal examples and OAuth tokens). Approving a proposal spends Connects on **your** Upwork account. Dashboard seats share that token; do not expose this UI on the public internet. Prefer `manual` until you trust scoring and drafts. Leave `SEED_DEMO_PORTFOLIO=false` unless you want the bundled demo case studies.

## Future plans

These are not in the current release. Chat replies and search-query suggestions use `OPENAI_MODEL` (default `gpt-4o-mini`). Cover letters use `OPENAI_DRAFT_MODEL` (default `gpt-4o`). Embeddings use a local Hugging Face MiniLM model.

- **Multiple LLM providers**, including local runtimes such as [vLLM](https://docs.vllm.ai/) and [Ollama](https://ollama.com/), plus other hosted APIs — pick a provider per environment without rewriting the writer
- **Multiple embedding backends**: Hugging Face models, OpenAI embeddings, and other APIs, so scoring and example matching are not tied to one MiniLM checkpoint
- **Cleaner UI**: denser inbox, clearer job review, and a less form-heavy Proposal / Settings flow

Contributions that move any of this forward are welcome. Keep the approve-before-submit path; do not add live-submit shortcuts.

## License

[MIT](LICENSE) © 2026 CanCode Devs. Free to use, modify, and redistribute, including commercially, with the copyright notice retained.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests are welcome. Keep secrets out of diffs. Do not add live-submit helpers that skip the dashboard approval flow.
