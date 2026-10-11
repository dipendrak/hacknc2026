# Approved money-storage design

On 2026-10-09 the user explicitly opted for **integer USD cents in Nessie for
our controlled synthetic seed**, decoded to **USD dollars in the app**. This
preserves the originally approved amounts; it does not approve truncation.

Nessie's production integer fields truncate fractional numeric inputs and reject
decimal strings. The cents convention is ours: Nessie itself does not know that
9450 means $94.50. Do not use its displayed values as dollar figures for these
accounts or mix them with ordinary dollar-based Nessie accounts.

| Boundary | Unit | Example |
|---|---|---|
| Approved seed plan | USD dollars | 94.50 |
| Seed POST and remote readback | Integer USD cents | 9450 |
| Raw cache, `/txn`, saved evidence | Integer USD cents for tagged accounts | 9450 |
| Fitted model, decision, simulation, claims, prose | USD dollars | 94.50 |

`data/money.py` uses `Decimal` to encode/decode money. Genuine sub-cent inputs
are rejected. For the original generated plan only, a binary-float residue
within one float step of a cent value (175 × 1.4 originally produced
244.99999999999997) is canonicalized to that cent. The fixture generator now
uses decimal multiplication. This does not change a financial cent.

The remote nickname prefix `Synthetic USD cents ` explicitly identifies this
convention. Ingest writes each account bundle's `monetary_unit: usd_cents`.
Ordinary Nessie accounts and older snapshots default to `usd`. The fitter
decodes each bundle separately, rejects unknown units or a cents marker without
metadata, and leaves raw records/IDs unchanged. It does not infer units from the
size of a number. The simulation core's contracts remain unchanged.

The earlier partial dollar account is retained with nickname prefix
`Superseded dollar seed ` and `include_in_fit: false`. Its account, deposits,
and purchase are retained for audit; they are excluded from the active model.
A new checking account replaces it because the production AccountUpdate route
cannot set a new balance. Customers and merchants are reused. The journal
archives all legacy IDs and durably resumes migration without duplicate POSTs.

For Dev C and Dev D:

- `/model`, `/ask`, `/run`, model and delta in `/run/{id}/inputs` use dollars.
- `/txn/{id}` returns the unmodified raw record. Read `X-Monetary-Unit`:
  `usd_cents` means divide its monetary fields by 100 for dollar display;
  `usd` means use them directly. Transaction IDs always remain the original IDs.
- `/run/{id}/inputs` adds `amount_units: {model: "usd", delta: "usd",
  evidence: {transaction_id: "usd_cents" | "usd"}}`. Use this persisted mapping
  when rendering evidence for a saved run, even after cache replacement.
- SQLite preserves raw evidence and units together. Older saved runs without
  unit rows use dollars. Conflicting raw records or units for an archived ID
  reject a save rather than silently changing financial evidence.

The seeding journal and IDs are in `data/cache/seeded/reports/`. Bank creation/ingestion
happens outside requests; `/ask` still runs offline from the cache.
