# Upwork Personal Agent

A **local** agent for **one** Upwork freelancer on **their own** account. It watches for jobs that match you, skips the ones that fail your rules, drafts a proposal from *your* portfolio and history, and waits for you before it spends Connects.

Upwork treats freelancer accounts as personal. You may not share a login, Connects, or proposals across a team. This app is built that way on purpose: one OAuth login, one dashboard, one person reviewing drafts. It is not a multi-seat agency inbox.

It is not a cloud SaaS and not an auto-apply bot unless you turn that on. You run it with Docker on your machine. Tokens, letters, and examples stay in `./data/` on disk.

Built by [CanCode Devs](https://github.com/CanCode-Devs). [MIT License](LICENSE).

## What you get

**Discovery**
- Poll Upwork through the official MCP (OAuth in the browser, once)
- Search from your profile queries; **Poll now** when you do not want to wait
- Sync portfolio, contracts, and certificates from Upwork for proof in drafts

**Scoring**
- Rank jobs against skills, client metrics, and Settings floors (budget, verified payment, min score)
- Hard-skip jobs that hit exclude keywords or strict rules
- Learn from outcomes you log (hired, ignored, shortlisted)

**Drafting**
- Cover letter, Upwork screening answers, posting “please include” items, and escrow milestones for fixed-price jobs
- Highlights capped at the closest real work (not invented case studies)
- **Proposal** page: hook, structure, never-say list, milestone template, and your own job-post → letter examples
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
# Set OPENAI_API_KEY, DASHBOARD_PASSWORD, and SESSION_SECRET in .env
mkdir -p data/huggingface profiles
cp profiles/example.yaml profiles/default.yaml
# Edit profiles/default.yaml: your name, skills, search queries
docker compose up --build
```

If `profiles/default.yaml` is missing, the app copies `profiles/example.yaml` on boot. First start downloads `all-MiniLM-L6-v2` into `./data/huggingface`. Later starts reuse that folder.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` from `.env` (defaults: `admin` / `change-me`).

Then:

1. **Settings** — budget floors, exclude keywords, autonomy
2. **Proposal** — opening hook, letter structure, never-say list, optional style examples
3. **Connect Upwork** from the inbox

### Upwork login (once)

OAuth needs a browser on this machine. With Compose running, open the inbox and click **Connect Upwork**. Tokens are stored in `./data/upwork_oauth.json` (gitignored).

CLI alternative:

```bash
docker compose exec app python -m app.cli.upwork_login
```

## Configuration

Copy `.env.example` to `.env` and set at least `OPENAI_API_KEY`, `DASHBOARD_PASSWORD`, and `SESSION_SECRET`.

| Layer | Owns | When it applies |
|-------|------|-----------------|
| `.env` | OpenAI, poll interval, dashboard login, `PROFILE_PATH`, `APP_NAME` / `APP_TAGLINE`, `SEED_DEMO_PORTFOLIO`, `EMBEDDING_MODEL` | Always |
| `.env` → SQLite (once) | Scoring floors, autonomy | Seeded on first boot; **Settings** owns them after that |
| `profiles/default.yaml` | Name, title, rate, skills, search queries, exclude keywords, voice | Identity and search. Voice reaches the writer |
| **Proposal page** | Hook, tone, structure, must/never lists, extra instructions, milestones, screening, few-shot examples | Every new draft or **Regenerate**. Stored in `./data/` |
| Settings rules | Hard skips (budget/tech/client); `proposal_style` lines in the writer prompt | Active rules |
| `profiles/scoring.yaml` | Optional numeric scoring matrix | If the file exists; otherwise code defaults. Copy from `scoring.example.yaml` |

`SEARCH_QUERIES` in `.env` is only used when the profile YAML has no `search_queries`.

Uvicorn is started **without** `--reload`. After Python changes:

```bash
docker compose restart app
```

HTML and CSS under `app/` are bind-mounted and update without a restart.

## Autonomy

Set in `.env` (`AUTONOMY_MODE`) or on the Settings page:

- `manual` — inbox review; you approve before Connects are spent
- `auto_above_threshold` — auto-submit when score >= `AUTO_SUBMIT_THRESHOLD` (default 85)
- `fully_auto` — auto-submit everything at or above min score

## Daily use

1. Inbox shows scored jobs plus a short reason
2. Edit the letter, **Regenerate** if needed, then **Approve & submit**, or **Reject** with a reason
3. Log hired / ignored / shortlisted after the fact so scoring learns
4. Settings: autonomy, rate floors, strict/soft rules
5. Proposal: voice and examples for the next draft
6. **Poll now** runs a discovery cycle immediately

Keep the machine awake; Compose uses `restart: unless-stopped`.

## Layout

- `Dockerfile` / `docker-compose.yml` — app + embedding warmup
- `app/agent.py` — poll orchestrator
- `app/tools/` — memory, discovery, execution
- `app/embeddings.py` — local vector store (sentence-transformers)
- `app/web` — dashboard (Inbox, Portfolio, Proposal, Settings)
- `profiles/example.yaml` — generic starter profile (committed)
- `profiles/default.yaml` — your profile (gitignored; copy from example)
- `profiles/scoring.example.yaml` — scoring matrix template

## Hugging Face cache

| Host | Container |
|------|-----------|
| `./data/huggingface` | `/models/huggingface` (`HF_HOME`) |

The `warmup` service exits immediately if `.all-minilm-l6-v2.ready` exists in that folder.

## Security

Do not commit `.env`, `profiles/default.yaml`, `./data/` (includes Proposal examples and OAuth tokens). Approving a proposal spends Connects on **your** Upwork account. Do not share that login or this dashboard with others; Upwork accounts are personal. Prefer `manual` until you trust scoring and drafts. Leave `SEED_DEMO_PORTFOLIO=false` unless you want the bundled demo case studies.

## Future plans

These are not in the current release. Drafting today uses the OpenAI API (`OPENAI_MODEL` / optional `OPENAI_BASE_URL`); embeddings use a local Hugging Face MiniLM model.

- **Multiple LLM providers**, including local runtimes such as [vLLM](https://docs.vllm.ai/) and [Ollama](https://ollama.com/), plus other hosted APIs — pick a provider per environment without rewriting the writer
- **Multiple embedding backends**: Hugging Face models, OpenAI embeddings, and other APIs, so scoring and example matching are not tied to one MiniLM checkpoint
- **Cleaner UI**: denser inbox, clearer job review, and a less form-heavy Proposal / Settings flow

Contributions that move any of this forward are welcome. Keep the approve-before-submit path; do not add live-submit shortcuts.

## License

[MIT](LICENSE) © 2026 CanCode Devs. Free to use, modify, and redistribute, including commercially, with the copyright notice retained.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests are welcome. Keep secrets out of diffs. Do not add live-submit helpers that skip the dashboard approval flow.
