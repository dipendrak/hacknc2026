# Pocketwatch iMessage agent

```bash
npm ci
cp .env.example .env      # Fill Photon credentials and your sender mapping locally.
npm run dev              # watches src/, restarts on change
```

Flow: inbound iMessage → typing indicator → `POST {API_BASE_URL}/ask` →
reply with prose → send the replay URL. One HTTP call, no Python, no auth —
just a phone → `customer_id` map (`CUSTOMER_PHONE_MAP`) in `.env`. After the answer it sends the stats and chart
image cards from the web app.

If `/ask` errors or times out, the agent sends a human fallback message
instead of going silent (`src/index.ts`, `handleAsk`).

`ENABLE_REACTION_RERUN=true` turns on the optional thumbs-up re-run: react
👍 to a result to re-ask with "assume I go ahead with it" applied. Off by
default.

`npm run typecheck` — no credentials needed, just validates the code.

For the seeded local setup, use `API_BASE_URL=http://127.0.0.1:8002`.
For replay links opened on a phone, the API's `REPLAY_BASE_URL` must point to
the frontend URL reachable from that phone. The agent prefers the absolute
URL returned by the API, so changing only the agent's replay origin will not
override an API response containing a localhost URL.

Texting `test1` to `test5` runs a fixed demo question against the five fixture customers without calling Gemini.
