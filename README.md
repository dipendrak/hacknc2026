# Pocketwatch

**See what a purchase does to your next two years, before you make it.**

Text a question like *"Can I afford a $400 phone today?"* over iMessage, or ask it in the web app. Pocketwatch
learns your paycheck, bills and spending from your bank transactions (Capital One's Nessie API), simulates the next
two years 500 times with and without the decision, and answers in plain English: the odds you'd dip below $0, when
it would get tight, a safer time to do it, and a loan that would work. Every answer links to a replay with the chart
and the transactions behind it.

```
iMessage ─► agent/ ─┐
                    ├─► api/ ─► parse (Gemini fills fields, code checks every number)
web app ──► web/ ───┘          ─► simulate 500 futures (core/) ─► plain-English answer + saved replay
```

- **Gemini reads the question** into fields (income, purchases, loans, dates). Code checks that every number in the
  message is accounted for and does all the math, so a misread question is refused, never guessed. If Gemini is
  unavailable, rule-based parsers take over.
- **The simulation is deterministic** (fixed seed): the same question on the same data gives the same answer.
- **Gemini writes at most one sentence** of the answer (the biggest recurring bill), and a validator rejects it if it
  contains any number the simulation didn't produce.

## Project layout

| Folder | What's in it |
|---|---|
| `agent/` | iMessage agent (Photon Spectrum, TypeScript). Forwards texts to the API; replies with the answer, a replay link and image cards. |
| `api/` | FastAPI service: `/ask`, replays, customers. The wording of every answer (`pipeline.py`) and adjustable settings (`settings.py`). |
| `core/` | Simulation engine: cash-flow model, 500-path simulation, what's driving the risk, safer timing, financing, loans. |
| `llm/` | Question parsing (`parse_complex.py`: Gemini plus rules), Gemini's answer sentence (`render.py`), number validator (`validate.py`). |
| `data/` | Nessie tools (seed, ingest, fit) and `supabase_setup.py`. `cache/seeded/` holds the cached customers. |
| `web/` | Next.js app: Frutiger Aero desktop, XP-style sign-in, replay pages, image cards. |
| `supabase/` | Database schema (migrations). |
| `docs/` | `MONEY_UNITS.md`: how amounts are stored (integer cents in Nessie). |

## Setup

Requires Python 3.11+ and Node 20+.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
(cd web && npm install && npx playwright install chromium && cp env.example .env.local)
(cd agent && npm install && cp .env.example .env)
cp .env.example .env
```

Fill in each env file (all are gitignored):

| File | Needs |
|---|---|
| `.env` | `GEMINI_API_KEY`; Supabase `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `DATABASE_URL` (session pooler); `NESSIE_API_KEY` only to re-ingest |
| `web/.env.local` | `API_BASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` |
| `agent/.env` | Photon `PROJECT_ID` and `PROJECT_SECRET`, `API_BASE_URL`, `REPLAY_BASE_URL`, `CUSTOMER_PHONE_MAP` |

Then create the database tables, customers, settings and logins (safe to re-run):

```sh
.venv/bin/python -m data.supabase_setup          # add --check to see what's there
```

## Run

Three terminals, from the repo root:

```sh
# 1. API on port 8002
COUNTERFACTUAL_CACHE_DIR=data/cache/seeded \
COUNTERFACTUAL_DB_PATH=data/cache/seeded/frontend-verification.sqlite3 \
REPLAY_BASE_URL=http://127.0.0.1:3000 \
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8002

# 2. Web app at http://127.0.0.1:3000
cd web && npm run dev

# 3. iMessage agent
cd agent && npm run dev
```

**Sign in** as any demo customer, for example `jordan@pocketwatch.tech` / `jordan-aero` (also alex, blair, casey,
drew and ellis: the password is the first name, then `-aero`). Texting `test1` to `test5` gives an instant demo
answer without calling Gemini.

Settings:
- `COUNTERFACTUAL_PARSER` in `.env`: `auto` (Gemini, then rules; the default), `llm` (Gemini only) or `rules` (never
  calls Gemini).
- Tax share, loan rates, simulation size and horizon live in Supabase's `settings` table, with each person's own
  tax share and loan rates in `customer_settings`. The API picks up changes within a minute.

## Things to ask

- `Can I afford $400 today?`
- `thinking about a PS5 for $499 and 2 games at $70 each`
- `If I made $3000 off of a bonus, could I buy a $1999 iPhone 18 Pro`
- `I got a $500/month raise, can I swing $1,200/month rent starting in December?`
- `can I afford a $400k house with 20% down` (uses your own mortgage rate)
- `how much is my daily $6 coffee costing me a year?`
- `what subscriptions could I cut?`

## Tests

```sh
.venv/bin/python -m pytest -q
(cd web && npm test && npm run typecheck)
(cd agent && npm run typecheck)
```

`web/tests/summary-cases.json` is shared by the Python and web tests, so the text message and the replay page always
word an answer the same way.

## Nessie data (only to rebuild it)

The six demo customers already exist in Nessie and are cached in `data/cache/seeded/`. Amounts are stored in Nessie
as integer cents (see `docs/MONEY_UNITS.md`). To rebuild:

```sh
.venv/bin/python -m data.seed                    # dry run: shows the plan
.venv/bin/python -m data.seed --execute --ingest # create in Nessie and cache locally
.venv/bin/python -m data.audit --cache-dir data/cache/seeded --output-dir data/cache/seeded/models
```

Seeding never retries a request whose outcome is uncertain; `data/cache/seeded/reports/` holds its journal.
