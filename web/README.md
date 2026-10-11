# Pocketwatch web app

Next.js (App Router), React, TypeScript and Tailwind v4, styled as a Frutiger Aero / Windows XP desktop.

```bash
npm install
cp env.example .env.local   # API_BASE_URL and the two NEXT_PUBLIC_SUPABASE_* values
npm run dev                 # http://localhost:3000
```

`npm test` and `npm run typecheck` check the app. The image cards sent over iMessage are screenshots taken with
Playwright's Chromium, so run `npx playwright install chromium` once.

## Pages

- `/login`: XP-style sign-in (Supabase Auth; falls back to local demo accounts when Supabase isn't configured).
- `/`: the desktop. `?view=overview` asks a question, `scenarios` holds saved drafts, `replays` lists your recent
  answers from every channel, `preferences` changes the wallpaper and accent.
- `/r/{run_id}`: a replay: the verdict, chart, week-by-week view and the transactions behind it. Open to anyone with
  the link, so texted links work on a phone.
- `/api/stats/{run_id}` and `/api/chart/{run_id}`: the PNG image cards.

Browser requests go through the same-origin `/api/backend` proxy, which only forwards the API's known routes.

## Where things live

- `lib/summary.ts`: the plain-English wording, kept identical to `api/pipeline.py` by the shared tests in
  `tests/summary-cases.json`.
- `lib/auth.ts`: sign-in. `components/auth/`: the login screen.
- `components/workspace/`: the desktop shell, workspace views, replay page and image cards.
- `app/globals.css`: all styles. `public/images/`: wallpapers, icons, logo and the Nessie cursor.
