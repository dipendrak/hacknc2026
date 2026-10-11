-- Pocketwatch schema: Nessie customers, logins, phone links, adjustable settings, saved replays.
-- Applied by `python -m data.supabase_setup` (idempotent: safe to re-run).
--
-- Access model: the API connects with DATABASE_URL (the database owner), which bypasses row-level
-- security. RLS is on for every table so the browser, which only ever holds the publishable key and a
-- user's login token, can read nothing except what the policies below allow.

-- ---------------------------------------------------------------------------------------------------
-- Nessie customers (the synthetic bank profiles seeded into Capital One's Nessie API).
-- ---------------------------------------------------------------------------------------------------
create table if not exists public.customers (
  customer_id        text primary key,                 -- Nessie customer _id
  display_name       text not null,                    -- "Jordan"
  nessie_first_name  text not null,
  nessie_last_name   text not null,                    -- "Synthetic Seed e3ed4c8012ad"
  seed_plan_hash     text,                             -- which seeding run created this customer
  nessie_origin      text not null,                    -- e.g. https://prod-api.nessieisreal.com
  nessie_account_ids text[] not null default '{}',
  snapshot_as_of     date,                             -- date of the cached transaction snapshot
  source             text not null default 'nessie',
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- One login per customer. customer_id also lives in the user's app_metadata (which users can't edit),
-- so the API can read it straight from the login token without a lookup.
create table if not exists public.profiles (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  customer_id  text not null unique references public.customers (customer_id),
  display_name text not null,
  created_at   timestamptz not null default now()
);

-- Phone number -> customer, for the iMessage agent (replaces CUSTOMER_PHONE_MAP later). Server-only.
create table if not exists public.phone_links (
  phone       text primary key check (phone ~ '^\+[1-9][0-9]{6,14}$'),  -- E.164
  customer_id text not null references public.customers (customer_id),
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------------------------------
-- Adjustable assumptions (tax, interest rates, simulation size). The API reads these at runtime;
-- every value is range-checked here and again in api/settings.py.
-- ---------------------------------------------------------------------------------------------------
create table if not exists public.settings (
  key         text primary key,
  value       numeric not null,
  unit        text not null,
  description text not null,
  min_value   numeric not null,
  max_value   numeric not null,
  updated_at  timestamptz not null default now(),
  check (value between min_value and max_value)
);

create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists settings_touch on public.settings;
create trigger settings_touch before update on public.settings
  for each row execute function public.touch_updated_at();
drop trigger if exists customers_touch on public.customers;
create trigger customers_touch before update on public.customers
  for each row execute function public.touch_updated_at();

-- Saved replays (runs, transactions, transaction_units, run_units) are created by api/store.py's RunStore
-- (RUN_STORE=postgres) with the same columns as its SQLite file; 0003 makes sure they're locked down.

-- ---------------------------------------------------------------------------------------------------
-- Row-level security. No policy = no browser access (the API still has full access).
-- ---------------------------------------------------------------------------------------------------
alter table public.customers    enable row level security;
alter table public.profiles     enable row level security;
alter table public.phone_links  enable row level security;
alter table public.settings     enable row level security;

-- The signed-in user's customer, from the login token's app_metadata.
create or replace function public.current_customer_id() returns text
language sql stable set search_path = '' as $$
  select nullif(auth.jwt() -> 'app_metadata' ->> 'customer_id', '')
$$;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles
  for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists "read own customer" on public.customers;
create policy "read own customer" on public.customers
  for select to authenticated using (customer_id = (select public.current_customer_id()));

-- Assumptions aren't secret: anyone may read them (e.g. to show "assumes 22% tax").
drop policy if exists "read settings" on public.settings;
create policy "read settings" on public.settings
  for select to anon, authenticated using (true);
