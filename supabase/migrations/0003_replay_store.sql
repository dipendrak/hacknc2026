-- Saved replays live in the tables api/store.py's RunStore uses (same columns as its SQLite file), so the API can
-- store them in Supabase with RUN_STORE=postgres. Earlier setups created an unused, differently-shaped `runs` and
-- `transactions`; those are replaced only while empty, so no saved replay is ever dropped.

do $$
begin
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'runs' and column_name = 'result')
     and not exists (select 1 from public.runs) then
    drop table public.runs;
  end if;
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'transactions' and column_name = 'record')
     and not exists (select 1 from public.transactions) then
    drop table public.transactions;
  end if;
end $$;

create table if not exists public.runs (
  run_id        text primary key,
  created_at    text not null,
  result_json   text not null,
  model_json    text not null,
  delta_json    text not null,
  response_json text not null,
  evidence_json text not null
);
create table if not exists public.transactions (txn_id text primary key, record_json text not null);
create table if not exists public.transaction_units (txn_id text primary key, monetary_unit text not null);
create table if not exists public.run_units (run_id text primary key, units_json text not null);
create index if not exists runs_created_at on public.runs (created_at desc);

-- Only the API (the database owner, via DATABASE_URL) may touch replays; no browser policies.
alter table public.runs              enable row level security;
alter table public.transactions      enable row level security;
alter table public.transaction_units enable row level security;
alter table public.run_units         enable row level security;
