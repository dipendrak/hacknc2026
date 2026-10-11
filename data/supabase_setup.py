"""Set up (or refresh) the Supabase project: schema, Nessie customers, settings, logins, phone links.

    .venv/bin/python -m data.supabase_setup            # apply everything; safe to re-run
    .venv/bin/python -m data.supabase_setup --check    # report what's there, change nothing

Reads DATABASE_URL, SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the root .env (never printed).
Re-running never overwrites a setting someone changed in the dashboard or resets a user's password.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values, load_dotenv

from api.settings import DEFAULTS, PER_CUSTOMER, SPEC

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
CUSTOMER_CACHE = ROOT / "data" / "cache" / "seeded"
EMAIL_DOMAIN = "pocketwatch.tech"


@dataclass(frozen=True)
class DemoLogin:
    name: str
    customer_id: str

    @property
    def email(self) -> str:
        return f"{self.name.lower()}@{EMAIL_DOMAIN}"

    @property
    def password(self) -> str:
        # Same demo passwords as the login screen's hint ("first name, then -aero").
        return f"{self.name.lower()}-aero"


def load_customers(cache_dir: Path = CUSTOMER_CACHE) -> list[dict]:
    """Nessie customers from the cached snapshots (the same files the API simulates from)."""
    customers = []
    for path in sorted(cache_dir.glob("*.json")):
        snap = json.loads(path.read_text())
        c = snap["customer"]
        last = c.get("last_name", "")
        seed = snap.get("seed_plan_hash") or (last.split()[-1] if last.startswith("Synthetic Seed") else None)
        customers.append({
            "customer_id": c["_id"], "display_name": c["first_name"], "nessie_first_name": c["first_name"],
            "nessie_last_name": last, "seed_plan_hash": seed, "nessie_origin": snap.get("base_url"),
            "nessie_account_ids": [a["account"]["_id"] for a in snap.get("accounts", []) if a.get("account", {}).get("_id")],
            "snapshot_as_of": snap.get("as_of"),
        })
    return customers


def personal_value(customer_id: str, key: str) -> float:
    """This customer's own value for a per-person setting: random within PER_CUSTOMER's realistic range, but
    seeded by the customer ID, so re-running the setup always gives the same person the same value."""
    lo, hi, step = PER_CUSTOMER[key]
    draw = int.from_bytes(hashlib.sha256(f"{customer_id}:{key}".encode()).digest()[:8], "big") / 2**64
    steps = round((hi - lo) / step)
    return round(lo + round(draw * steps) * step, 6)


def phone_links() -> dict[str, str]:
    raw = dotenv_values(ROOT / "agent" / ".env").get("CUSTOMER_PHONE_MAP") or "{}"
    try:
        return {phone: cid for phone, cid in json.loads(raw).items() if isinstance(cid, str)}
    except json.JSONDecodeError:
        return {}


class AuthAdmin:
    """Minimal Supabase Auth admin client (service key; server-side only)."""

    def __init__(self, url: str, service_key: str):
        self.client = httpx.Client(base_url=f"{url.rstrip('/')}/auth/v1", timeout=20,
                                   headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"})

    def users_by_email(self) -> dict[str, dict]:
        found, page = {}, 1
        while True:
            r = self.client.get("/admin/users", params={"page": page, "per_page": 200})
            r.raise_for_status()
            users = r.json().get("users", [])
            found.update({u["email"].lower(): u for u in users if u.get("email")})
            if len(users) < 200:
                return found
            page += 1

    def ensure(self, login: DemoLogin, existing: dict | None) -> tuple[str, str]:
        meta = {"app_metadata": {"customer_id": login.customer_id, "role": "demo"},
                "user_metadata": {"name": login.name}}
        if existing is None:
            r = self.client.post("/admin/users", json={"email": login.email, "password": login.password,
                                                       "email_confirm": True, **meta})
            r.raise_for_status()
            return r.json()["id"], "created"
        r = self.client.put(f"/admin/users/{existing['id']}", json=meta)  # never touches the password
        r.raise_for_status()
        return existing["id"], "updated"


def apply(check_only: bool = False) -> int:
    load_dotenv(ROOT / ".env")
    db_url, url, service_key = (os.getenv(k, "") for k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"))
    missing = [k for k, v in (("DATABASE_URL", db_url), ("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", service_key)) if not v]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}")
        return 1
    import psycopg

    customers = load_customers()
    logins = [DemoLogin(c["display_name"], c["customer_id"]) for c in customers]
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        if check_only:
            for table in ("customers", "profiles", "phone_links", "settings", "customer_settings", "runs", "transactions",
                          "transaction_units", "run_units"):
                exists = conn.execute("select to_regclass(%s)", (f"public.{table}",)).fetchone()[0]
                count = conn.execute(f"select count(*) from public.{table}").fetchone()[0] if exists else "-"
                print(f"  {table:13} {'ok' if exists else 'MISSING':8} rows: {count}")
            return 0

        for sql in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(sql.read_text())
            print(f"schema       applied {sql.name}")

        for c in customers:
            conn.execute(
                """insert into public.customers (customer_id, display_name, nessie_first_name, nessie_last_name,
                       seed_plan_hash, nessie_origin, nessie_account_ids, snapshot_as_of)
                   values (%(customer_id)s, %(display_name)s, %(nessie_first_name)s, %(nessie_last_name)s,
                       %(seed_plan_hash)s, %(nessie_origin)s, %(nessie_account_ids)s, %(snapshot_as_of)s)
                   on conflict (customer_id) do update set display_name = excluded.display_name,
                       nessie_first_name = excluded.nessie_first_name, nessie_last_name = excluded.nessie_last_name,
                       seed_plan_hash = excluded.seed_plan_hash, nessie_origin = excluded.nessie_origin,
                       nessie_account_ids = excluded.nessie_account_ids, snapshot_as_of = excluded.snapshot_as_of""", c)
        print(f"customers    {len(customers)} Nessie customers: {', '.join(c['display_name'] for c in customers)}")

        for name, (unit, description, lo, hi) in SPEC.items():
            # A value changed in the dashboard is kept; only the description and limits are refreshed.
            conn.execute(
                """insert into public.settings (key, value, unit, description, min_value, max_value)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (key) do update set unit = excluded.unit, description = excluded.description,
                       min_value = excluded.min_value, max_value = excluded.max_value""",
                (name, getattr(DEFAULTS, name), unit, description, lo, hi))
        print(f"settings     {len(SPEC)} adjustable settings")

        for c in customers:
            for setting in PER_CUSTOMER:
                # A value someone changed in the dashboard is kept.
                conn.execute("""insert into public.customer_settings (customer_id, key, value) values (%s, %s, %s)
                                on conflict (customer_id, key) do nothing""",
                             (c["customer_id"], setting, personal_value(c["customer_id"], setting)))
        print(f"per-person   {', '.join(PER_CUSTOMER)} for {len(customers)} customers")

        known = {c["customer_id"] for c in customers}
        links = {p: cid for p, cid in phone_links().items() if cid in known}
        for phone, cid in links.items():
            conn.execute("""insert into public.phone_links (phone, customer_id) values (%s, %s)
                            on conflict (phone) do update set customer_id = excluded.customer_id""", (phone, cid))
        print(f"phone_links  {len(links)} phone numbers from agent/.env")
        # Commit before talking to Supabase Auth: the schema references auth.users, and an open
        # transaction holding that lock would block the Auth service's own queries.
        conn.commit()

        admin = AuthAdmin(url, service_key)
        existing = admin.users_by_email()
        for login in logins:
            user_id, action = admin.ensure(login, existing.get(login.email))
            conn.execute("""insert into public.profiles (user_id, customer_id, display_name) values (%s, %s, %s)
                            on conflict (user_id) do update set customer_id = excluded.customer_id,
                                display_name = excluded.display_name""", (user_id, login.customer_id, login.name))
            print(f"login        {action:7} {login.email}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report tables and row counts without changing anything")
    sys.exit(apply(check_only=parser.parse_args().check))


if __name__ == "__main__":
    main()
