# FranticSearch — Code review & infrastructure expansion guide

Review of `main` at `0adf43c` (2026-10-20), focused on scalability and best practices, plus a staged guide for growing the infrastructure. [PLAN.md](../PLAN.md) stays the source of truth for product decisions; this document covers *how the system holds up and how to grow it*. Items here are recommendations, not decisions; record the ones you pick in PLAN.md.

## Summary

> **Status (2026-10-21):** S1, S2, S4 fixed in PR #19; P1–P3, P6, P8 in PR #20; O3 in PR #25. The backlog table at the end has a Status column. Measurements below are from before those fixes.

- **The core design is sound and scales further than the deployment does.** The ledger with its invariant check, the re-entrant transactions, the migration runner and the per-route ownership checks are the right foundations, and none of them needs to change to grow.
- **Deployment model** (PLAN.md § Decisions): FOSS, self-hosted per person or friend group, never open sign-up. "Scaling" here means three things: bigger collections, a friend group of up to a few dozen accounts, and making it easy for *other* people to run their own instance. It does not mean one big shared service.
- **The laptop + reverse SSH tunnel setup stays.** The operational gaps worth closing inside it: the Flask dev server serves the public site, and backups exist only on the laptop's disk (§ Stage 1).
- **Three code paths get slow as collections grow:** the pull list / buy list (~2 s), history (~1 s), and the deck list / overview (~0.7–0.8 s), measured with ~8,000 stock lines and ~48,000 movements per user. All three have contained fixes (§ P1–P3).
- **One security bug:** an open redirect through `next=` (§ S1). It's a small fix.
- **The Scryfall card cache is 99.9 % of the database.** It is rewritten in full every day and makes up nearly all of each 49 MB backup, for less than 1 MB of user data (§ P6, P7).
- **SQLite is the right database for this model, permanently.** One file is also a feature for self-hosters: trivial to back up, move and restore.
- **For FOSS distribution:** the repo has no licence file yet, and the deploy units hard-code this machine's paths (§ Stage 3).

## How this was measured

- Read all of `franticsearch/` (≈ 4,100 lines of Python), the migrations, `deploy/`, `Dockerfile`, `compose.yaml`; `pytest` passes (98 tests).
- Timed pages with Flask's test client (median of 3, warm cache) on two copies of the live DB:
  - **live**: 118,893 printings, 328 stock lines, 328 movements, 2 decks, 2 users
  - **scaled**: the live DB plus 20 synthetic users, each with 10 binders, 8,000 stock lines, 25 commander decks × 100 slots, and 48,000 movements (total: 160k stock lines, 960k movements, 50k slots). Ledger check: 0 mismatches.
- Profiled `deckplan.pull_plan` with cProfile and checked query plans with `EXPLAIN QUERY PLAN`.

| Page | live | scaled (one user's view) | Cause at scale |
|---|---:|---:|---|
| `/` overview | 4 ms | **709 ms** | `totals()` movement count scans the whole ledger (P3) |
| `/collection` | 11 ms | 100 ms | fine |
| `/locations/<id>` (800 lines) | 66 ms | 207 ms | Python filtering + rendering; fine |
| `/card/<key>` | 4 ms | 33 ms | fine |
| `/decks` | 9 ms | **795 ms** | `deckplan.state()` per deck → `decks.slots()` correlated subqueries (P2) |
| `/decks/<id>` | 21 ms | 93 ms | fine |
| `/pull` (all decks) | 20 ms | **1,969 ms** | O(shortfalls × pool) loop + P2 + per-shortfall queries (P1) |
| `/buy` | 26 ms | **1,878 ms** | calls `pull_plan` (P1) |
| `/history` | 8 ms | **1,024 ms** | owner filter can't use an index: full ledger scan (P3) |
| `/history?location=` | 10 ms | 52 ms | fine (indexed) |
| `/add?q=goblin` | 25 ms | 55 ms | fine |
| `/add?type=creature&c=G` | 279 ms | 305 ms | JSON `LIKE` over all printings, independent of users (P5) |
| `python -m franticsearch check` | — | 2.5 s | full projection; fine off the request path (P4) |

## Findings

Severity: **High** = fix before growing; **Medium** = fix when its trigger appears; **Low** = hygiene.

### Security & correctness

**S1 — Open redirect via `next` (High, small).** ✅ Done in PR #19. `_back()` (`app.py:192`) and `login()` (`app.py:1022`) accept any `next` that starts with `/` and not `//`. Werkzeug drops control characters when it builds the `Location` header, so `next=/%09/evil.com` becomes `Location: //evil.com` (checked: `POST /stock/acquire` with `next="/\t/evil.com"` → `302 //evil.com`). A link to `…/franticsearch/login?next=/%09/evil.com` sends the user to another site right after a real login, which is useful for phishing.
- Fix: one `_safe_next(value, default)` helper used by both. Reject values containing a backslash or any character ≤ `0x20`. Also require `urlsplit(value)` to have no scheme or netloc and the path to start with a single `/`. Add a test with `/\t/x`, `/\\x`, `//x`, `https://x`.

**S2 — Secret key race on first boot with several workers (Low).** ✅ Done in PR #19. `create_app` (`app.py:53`) generates a key if `meta` has none and upserts it. Two gunicorn workers starting at once on an empty DB each keep their own key in memory, so sessions only validate on the worker that issued them. Fix: `INSERT OR IGNORE`, then re-read the stored value. Or require `FS_SECRET_KEY` in production.

**S3 — Backups contain the session secret and password hashes (Medium, before off-machine backups).** `meta.secret_key` lets anyone holding a backup forge sessions. Before backups leave the machine, either encrypt them (e.g. `age` to a public key) or set `FS_SECRET_KEY` from the environment so the DB copy holds no secret.

**S4 — Double-submit of an import gives a 500 (Low).** ✅ Done in PR #19. `_check_new()` runs outside the transaction (`importer.py:345` / `:388`), and the `UNIQUE(owner_id, sha256)` violation inside it raises `sqlite3.IntegrityError`, which the routes don't catch. Fix: move the check inside the transaction and turn `IntegrityError` into `ImportFailed`.

**S5 — Production runs the Werkzeug development server (High, ops).** ✅ Done in PR #26. `serve` calls `app.run()` (`__main__.py:963`), which is not meant for exposure. It has no worker supervision, no request timeouts, and debug-oriented error handling. `gunicorn` is already in the `serve` extra and the Dockerfile uses it, and it works behind the tunnel unchanged (same `127.0.0.1:6602`). See § Stage 1.

**S6 — Session cookie not `Secure` on the public HTTPS site (Medium).** `FS_SECURE_COOKIE` is off because the same process also serves plain HTTP on the LAN. Two ways out:
- Run the live instance with `FS_SECURE_COOKIE=1` and use a separate dev instance (another port and DB copy) for plain-HTTP work.
- Or use the public HTTPS URL from the LAN too.

Either way, add HSTS in nginx.

Positives (keep doing these): every SQL value is parameterised; the dynamic parts (`ORDER BY`, format keys, colour columns, MV operators) are whitelisted; CSRF and login sit in one hook; login takes about the same time whether or not the user exists; share tokens are 144-bit; and every id-taking route has an other-user 404 test.

### Scalability & performance

**P1 — Pull plan is quadratic in the alternatives step (High once decks grow).** ✅ Done in PR #20. `deckplan.pull_plan` (`deckplan.py:179`) scans the *whole free pool* for every shortfall to find other printings of the card. With 1,155 shortfalls × 8,000 pool rows, that loop is 0.76 s of the 1.65 s. Each shortfall also runs its own `_deck_location` and "in other decks" query (2,300 queries in total).
- Fix: build `pool_by_oracle = {oracle_id: [rows]}` once, next to `by_pf`. Fetch "in other decks" for all shortfalls in one query, grouped by (printing, finish). Take the deck location from the deck row you already have.
- `/buy` calls `pull_plan`, so it gets the same speed-up.

**P2 — `deckplan.state()` pays for columns it never uses (High once there are many decks).** ✅ Done in PR #20. `state()` uses `decks.slots()`, whose four correlated subqueries per slot (`in_deck`, `free`, `free_where`, `owned_any`) cost ~30 ms per 100-slot deck. `/decks`, `/pull` and `/buy` call `state()` once per deck: 25 decks ≈ 0.75 s before doing anything else.
- Fix: a lean `slots_basic()` (slots joined to printings, with no subqueries) for `state()`. Keep the rich query for the deck page and the slot editor only. If the deck page needs those counts at scale, compute them with one grouped query per owner (`GROUP BY printing_id, finish`) and join in Python.

**P3 — History and the overview's movement count scan the whole ledger (High at ~1M movements).** ✅ Done in PR #20. `queries.history` filters on `COALESCE(fl.owner_id, tl.owner_id) = ?`, and `totals()` uses `EXISTS (… l.id IN (m.from_location_id, m.to_location_id))`. SQLite can't use an index for either (`EXPLAIN`: `SCAN m`). Results: 1.0 s and 0.7 s at 960k movements, and the cost grows linearly with *everyone's* history, not just yours.
- Fix (recommended): **migration 006 adds `movements.owner_id`**, backfilled from the location, with an index on `(owner_id, id)`. Then `history` becomes `WHERE m.owner_id = ? ORDER BY m.id DESC LIMIT 100` (an index range scan), and `ledger._record` sets it. This fits the model because a movement never crosses owners (the ledger refuses that).
  - Constraint for the future: keep that true when trades arrive. Model a trade as two one-sided rows, one out of A and one into B, each with its own owner (§ Feature notes).
- Cheaper alternative with no migration: filter on `from_location_id IN (owner's locations) OR to_location_id IN (…)`, which can use the two existing indexes through SQLite's OR-optimisation. Less robust as the location count grows.
- Also: drop the movement count from the overview, or replace it with `MAX(id)`-style cheap stats.

**P4 — `ledger.check()` is O(all movements) (Low).** 2.5 s at 960k. Fine for the CLI and the System page button; keep it off normal page loads. Worth running nightly after the backup and logging the result (§ O2).

**P5 — Colour and type searches scan all printings (Low).** `CardSearch.where` uses `colors LIKE '%"G"%'` and `type_line LIKE '%word%'`, which take ~280 ms regardless of users. This is acceptable now. If it matters (e.g. with Scryfall query syntax), add integer bitmask columns `colors_mask` / `identity_mask` (W=1, U=2, …), filled at sync, and use bit tests: "include G" = `mask & 16 = 16`, "at most" = `mask & ~allowed = 0`.

**P6 — The daily Scryfall sync rewrites every printing (Medium).** ✅ Done in PR #20. `_UPSERT_SQL` updates all 119k rows unconditionally. Each update fires the FTS trigger (delete + insert into `printings_fts`), so every day the WAL and FTS index churn by roughly the size of the card table (~200 MB), and writes are serialised in 60 chunks for ~25 s.
- Fix: add a change guard to the upsert, `… DO UPDATE SET … WHERE (printings.c1, printings.c2, …) IS NOT (excluded.c1, excluded.c2, …)`. Checked on SQLite 3.53: unchanged rows fire no trigger.
- After a sync, run `PRAGMA wal_checkpoint(TRUNCATE)` and `PRAGMA optimize`. Optionally run `INSERT INTO printings_fts(printings_fts) VALUES('optimize')` weekly.

**P7 — The card cache dominates storage and backups (Medium).** `printings` + FTS + indexes ≈ 233 MB of 238 MB. User tables are < 1 MB. Each nightly backup is 49 MB, almost all of it data you can re-download in 25 s. This is what makes off-machine backups feel heavy. Options (a decision for you):
1. **User-data backups** (small change): back up as now, then in the temp copy delete `printings` rows that no `stock` / `movements` / `deck_slots` / `import_unresolved` row references, rebuild FTS, and `VACUUM`. A backup shrinks to well under 1 MB plus referenced printings, and restore runs a sync afterwards. FKs stay intact because referenced printings are kept.
2. **Split the cache into its own file** (`cards.db`, `ATTACH`ed). Cleaner long-term, but SQLite can't enforce FKs across attached databases, so `printing_id` integrity would rely on `ledger.validate_key` (which already checks). It's also a larger migration.
- Recommendation: option 1 now (it unblocks cheap off-machine backups), and keep the full weekly backup.

**P8 — No planner statistics (Low).** ✅ Done in PR #20. `sqlite_stat1` doesn't exist; `ANALYZE` has never run. Run `PRAGMA optimize` after migrations and syncs, or on connection close, so the planner keeps good choices as data skews (e.g. one user with 50k lines).

**P9 — Per-movement overhead in the ledger (Low).** `_record` runs ~8 statements per line: key validation, owner × 2, type × 2, archived, current qty, upsert, insert. A 10k-line import is still ~1–2 s inside one transaction, which is fine. If imports grow, look up location metadata once per batch.

**P10 — Repeated card-key expressions (Low, cleanliness + speed).** `COALESCE(p.oracle_id, p.scryfall_id)` and `oracle_id = ? OR (oracle_id IS NULL AND scryfall_id = ?)` appear across `queries.py`, `decks.py` and `deckplan.py`. A virtual generated column, `ALTER TABLE printings ADD COLUMN card_key TEXT GENERATED ALWAYS AS (COALESCE(oracle_id, scryfall_id)) VIRTUAL` plus an index, makes these one indexed equality and removes the duplication.

What already scales well: stock lookups (the PK starts with `location_id`), per-user collection paging, FTS name search, WAL with a 30 s busy timeout, short write transactions, and confirm functions that recompute plans (no stale-form over-moves under concurrency).

### Operations

**O1 — Backups live on the same disk as the data (High).** Downtime while the laptop sleeps is an accepted trade-off of self-hosting on a laptop. Data loss is not: a dead, lost or stolen laptop loses everything, backups included. The fix is off-machine copies, not moving the app. The server at the other end of the tunnel is a natural target, since the SSH key already exists (§ Stage 1).

**O2 — No health check (Medium).** A 502 is diagnosed by hand. Add a public `GET /healthz` (`SELECT 1`, schema version, sync status age; no user data) to `PUBLIC_ENDPOINTS`.
- With a laptop that sleeps, "down" is normal, so don't page on it.
- Do alert on what actually needs attention: a nightly `check` mismatch, a failed backup, a sync stuck in `error` for more than 2 days. A systemd `OnFailure=` unit that sends a desktop notification or an email is enough.

**O3 — Logging is unconfigured and background errors vanish (Medium).** ✅ Done in PR #25. Nothing calls `logging.basicConfig`. The auto-sync loop swallows every exception from `needs_sync` (`app.py:300`), and `_run_sync_in_background` does the same (`app.py:283`). Configure logging once in `serve` / `wsgi` (to stderr, so journald or Docker collect it). Use `log.exception` in both loops. Add an access log (gunicorn's `--access-logfile -`).

**O4 — Background sync lives in the web process (Medium; blocks multiple workers).** `_start_auto_sync` starts a thread in every process that calls `create_app`. Under gunicorn with more than one worker that means N concurrent syncs, and `_sync_lock` is per-process. Make the sync a systemd timer (`python -m franticsearch sync`, daily), like the backup. Set `FS_AUTO_SYNC=0` on the live service and keep it on for dev. If the System page's "sync now" stays, guard it with a DB-level lock (a `meta` row with a timestamp) instead of a thread lock.

**O5 — Migrations run in every worker at startup (Low; matters with several workers).** `create_app` calls `db.migrate`. Two workers can race: `executescript("BEGIN; …")` takes the write lock lazily, after both have read `user_version`. Run `python -m franticsearch migrate` as `ExecStartPre=` (or a container entrypoint step), and keep the in-app call only for dev.

**O6 — The live site serves whatever branch is checked out (Medium).** Checking out a feature branch to work on it changes production. Deploy from a separate checkout or worktree (e.g. `~/franticsearch-live`, on `main` or a tag), or `pip install` a tagged release into a dedicated venv. Restart only after `pytest` passes there.

**O7 — No CI (Medium, cheap).** Add a GitHub Actions workflow: Python 3.11 and 3.13, `pip install -e ".[dev]"`, `pytest`, and `ruff check`. The suite has no network access and takes ~12 s, so CI costs nothing and catches broken merges.

**O8 — No lock file (Low).** Dependencies are ranges (`flask>=3.1,<4`, …). Add a lock (`uv lock` or `pip-compile` → `requirements.lock`) and install from it in Docker and in the deploy venv so deployments are reproducible.

**O9 — Container hardening (Low; Docker is unverified).** `compose.yaml` publishes `6602:6602` on all interfaces. Behind nginx it should be `127.0.0.1:6602:6602`. Run as a non-root user, add a `HEALTHCHECK` against `/healthz`, set `FS_AUTO_SYNC=0` with a sync timer or cron sidecar (O4), and verify `docker compose up --build` once before relying on it.

### Code health & best practices

**C1 — `app.py` is one 1,130-line function of routes (Medium as features land).** Split `_register_routes` into Blueprints by area: `stock`, `locations`, `imports`, `decks` (+ pull/return/buy/export), `sharing`, `accounts`, `system`. Move the shared helpers (`_my_*`, `_back`, `_form_key`, `_done`) into `web/common.py`. URLs and templates stay the same. Blueprint endpoint names change (`decks.deck_view`), so do it in one pass with a `url_for` grep.

**C2 — Make the ownership rule mechanical (Medium).** Ownership checks are a convention plus a hand-maintained test list (`test_other_users_ids_are_not_found`). Add a meta-test that walks `app.url_map` and fails if a rule with an `<int:…>` converter (or a known id form field) isn't covered by that test. Forgetting a check then fails CI instead of relying on review.

**C3 — `LedgerError` is the app-wide validation error (Low).** Users, locations, decks and imports all raise `LedgerError`. Introduce `DomainError(ValueError)` with `LedgerError(DomainError)` and `ImportFailed(DomainError)`, and catch `DomainError` in routes. Only the name changes, but new modules (trades, prices) then get a natural home.

**C4 — Linting and typing (Low).** Add `ruff` (pyflakes + isort + bugbear rules) to `pyproject.toml` and CI. The code is already type-annotated, and `mypy --strict` on `ledger.py`, `deckplan.py` and `db.py` would be cheap insurance for the core.

**C5 — Duplicated constants (Low).** Port `6602` appears in `__main__.py` (twice) and `backup.restore`; the DB default path appears in `app.py` and `__main__.py`. Put them in one `config.py` that reads env vars (`FS_DB`, `FS_PORT`, …), used by both.

**C6 — Performance regression guard (Low).** Keep a `tools/scale_fixture.py` (like the one used for this review) that inflates a DB copy, plus a `tools/bench.py` that times the pages above. Run it before merging changes to `deckplan`, `queries` or `ledger`. No CI timing assertions; they're flaky.

## Expansion guide

**Model:** one instance per person or friend group, run by one of them, on whatever hardware they have (a laptop, a home server, a small VPS). Accounts are admin-created, there's no open sign-up, and instances may later talk to each other (§ Stage 4). Each stage lists its *trigger*, the steps, and what it buys. Every stage is cheap and keeps the next one possible.

### Stage 0 — Today

Laptop → Flask dev server `:6602` → autossh reverse tunnel → nginx on `theorycorner.com` → `/franticsearch/`. One SQLite file, nightly local backups, 2 users.

This shape is a good fit for the model: no inbound ports at home, the data stays on hardware you own, and the public box only proxies. Downtime while the laptop sleeps is accepted.

### Stage 1 — Harden in place (recommended next)

**Trigger:** now. The site is public and holds real data. Keep the laptop + tunnel layout and close the gaps inside it:

```
browser ─HTTPS─▶ nginx (theorycorner.com) ─▶ 127.0.0.1:16602 ═tunnel═▶ laptop 127.0.0.1:6602
                                                                        gunicorn (1 worker × 8 threads)
                                                                        data/franticsearch.db
laptop user timers: sync (daily) · backup (nightly) · check (nightly) · offsite copy (after backup)
```

1. **Fix S1** (open redirect) and add `/healthz` (O2), on a branch with tests.
2. **Real WSGI server (S5)** ✅ done in PR #26: `franticsearch.service` runs `.venv/bin/gunicorn -c deploy/gunicorn.conf.py franticsearch.wsgi:app` (settings in the file, because systemd reads `%` in unit files). The port, the tunnel and nginx don't change. Keep `python -m franticsearch serve` for development.
3. **Background jobs out of the web process (O4, O5):**
   - `ExecStartPre=… migrate`.
   - A `franticsearch-sync.timer` (daily, `Persistent=true` like the backup timer) and `FS_AUTO_SYNC=0` in the service.
   - A nightly `check`, with the `OnFailure=` notification from O2.
4. **Off-machine backups (O1, S3):**
   - After each nightly backup, encrypt it with `age` to a public key (the private key lives somewhere other than the laptop), then `rsync` it to the server over the existing SSH key. A second copy on a USB disk or cheap object storage is a bonus.
   - The server only ever sees ciphertext, so losing it leaks nothing.
   - Do P7 option 1 first and each copy is tiny.
   - **Test a restore** from the off-machine copy once.
5. **Deploy from a separate checkout (O6):** e.g. `git worktree add ~/franticsearch-live main` and point the units' `WorkingDirectory` / `ExecStart` there, with its own venv and `data/` (or `FS_DB` pointing at the current DB). Working on a branch then no longer changes the live site. Deploy = `git pull` in the worktree + restart.
6. **Cookies and headers (S6):** `FS_SECURE_COOKIE=1` on the live service; HSTS and `client_max_body_size 32m` in nginx.
7. Optional: `loginctl enable-linger` if the site should stay up while logged out (it still stops during sleep).

Buys: a production WSGI server, data that survives losing the laptop, a dev workflow that can't break the live site, and silent failures made visible. No new hosts and no new moving parts.

### Stage 2 — Bigger collections, bigger friend groups

**Trigger:** any user with more than ~5k stock lines or ~20 decks, a group past ~5 active accounts, or any page above ~300 ms in the access log.

Steps, in order of payoff:
1. P1 + P2 (pull plan and lean deck state): fixes `/pull`, `/buy` and `/decks`.
2. P3 (`movements.owner_id`, migration 006): fixes `/history` and `/`.
3. P6 (sync change guard) and P8 (`PRAGMA optimize`).
4. O3 (logging), C2 (ownership meta-test), C1 (Blueprints) before the route count grows further.
5. Optional: P10 (`card_key` column), P5 (colour masks) if search gets heavier.

Capacity notes:
- **SQLite is enough for this model, permanently.** WAL serves concurrent readers without blocking, and a click writes a few rows. A friend group, even dozens of people, writes orders of magnitude less than one SQLite file handles. After P1–P3, page cost depends on *your own* collection, not the instance's total.
- The only long writer is the sync, which is short per chunk and much lighter after P6.
- **Stay at one gunicorn worker** and scale with threads (8–16). This avoids every multi-process concern (O4, O5, S2) and keeps the laptop's footprint small. The bottleneck at this size is SQLite I/O, not Python's GIL, and threads overlap it.
- A laptop or Raspberry-Pi-class box is enough hardware. Memory is dominated by SQLite's page cache; the card cache is ~240 MB on disk.

### Stage 3 — Easy for others to self-host (FOSS packaging)

**Trigger:** before telling anyone else to run it. This is where most of the "expansion" work actually is for a self-hosted project. Today the repo is shaped around one specific install.

- **Licence.** There is no `LICENSE` file, so legally nobody else may run or modify it yet. Pick one (e.g. AGPL-3.0 if you want modified hosted versions to share their source, MIT/Apache-2.0 if not) and set `license` in `pyproject.toml`. This is your call.
- **Scryfall terms.** Each instance downloads the ~80 MB bulk file daily, which is what Scryfall asks for (bulk data instead of API calls). Keep the descriptive User-Agent, and say in the README that card data and images are Scryfall's / Wizards' (Fan Content Policy).
- **No machine-specific paths.**
  - `deploy/systemd/*` hard-code `%h/hobbies/dev/python/FranticSearch` and `theorycorner.com`. Ship them as templates (`franticsearch.service.in`, or `EnvironmentFile=` with `FS_HOME`, `FS_TUNNEL_HOST`).
  - Move this instance's specifics into a `deploy/examples/` note.
  - One `config.py` for all env vars (C5), documented in the README.
- **First-run setup.** The login page already knows when no users exist; let it say exactly which command creates the first admin. Or allow creating the first admin from the browser *only* when the users table is empty and the request comes from localhost.
- **Supported install paths**, each tested once end to end:
  1. pip + systemd (today's path, templated), for people with a Linux box;
  2. Docker / compose (verify it, then apply O9). This is the path most self-hosters expect;
  3. reverse-proxy recipes: nginx (yours), Caddy (automatic HTTPS, the easiest for newcomers), and **the tunnel pattern** as a documented option for people without a public IP. Besides autossh to a VPS, mention Tailscale Funnel / Cloudflare Tunnel as alternatives that need no server.
- **Releases and upgrades.**
  - Tag versions and bump `__version__`.
  - Keep a `CHANGELOG.md` that flags migrations.
  - Upgrades = new code + restart; migrations run in `ExecStartPre` / the entrypoint, and take an automatic backup first.
  - Never edit applied migrations (already a rule): other people's databases depend on it.
- **CI (O7) and a lock file (O8)** matter more once strangers install it: a broken `main` breaks their upgrades, not just yours.
- **Backup/restore docs** for self-hosters: where the DB is, what the timer does, how to copy backups off the machine (the Stage 1 recipe, generalised).

### Stage 4 — Federation (exploratory)

**Trigger:** friends on *different* instances want to see each other's shared binders or trade. Not decided; these are notes so earlier work doesn't block it.

Why it's tractable: card identity is already global (Scryfall printing ids and oracle ids), and sharing is already modelled per location (`private` / `users` / `link`). Federation is mostly "share links that another server can read".

Suggested steps, smallest first:
1. **Read-only, pull-based sharing.**
   - Add a JSON variant of the share link (`/s/<token>.json`: printing ids, finishes, quantities; no owner-private fields, same rules as the shared HTML view).
   - Another instance's user pastes a friend's link once. Their server fetches it on a schedule and caches it in a `remote_locations` / `remote_stock` table.
   - Card pages then show "alex@their-instance: 2 in Trade binder" next to local users' shared copies (`queries.shared_stock`).
   - This needs no new trust model: the token *is* the permission, as today, and revoking the link (new token) revokes it.
2. **Instance identity, if trades come.** Each instance gets a keypair (Ed25519, stored like `secret_key`, and covered by S3's backup encryption) and a `peers` table (URL, public key, display name, added by the admin).
   - Server-to-server requests are signed (HTTP Message Signatures, RFC 9421).
   - Keep it a small bespoke protocol over HTTPS. Full ActivityPub is far more than a collection manager needs.
3. **Cross-instance trades.** A trade is a proposal stored on both sides. Each instance writes only its own one-sided ledger rows (`trade_out` on A, `trade_in` on B), consistent with P3's single-owner movements. Messages go through an **outbox with retries**.

Infrastructure implications:
- **Peers are often offline** (a sleeping laptop is normal). Everything must be asynchronous and pull-or-retry: caches with "last seen" timestamps, an outbox table drained by a timer, and idempotent message ids. Never call a peer synchronously from a page request. The no-JS rule fits: states show up on the next page load.
- **Reachability.** An instance needs a stable public HTTPS URL for others to pull from. The tunnel pattern provides that. Instances that only want to *read* others' links need no inbound reachability.
- **Privacy.** Export only what a location's owner explicitly shared, and never usernames of accounts that didn't opt in. Remote data is untrusted input: validate printing ids against the local `printings` table and cap sizes.
- **Scope check.** Steps 1 and 2 add two or three tables and one timer. SQLite remains fine.

## Feature notes (infrastructure impact of PLAN.md § What's next)

- **Prices.**
  - Don't add price columns to `printings`: prices change daily, so the whole table would churn again (undoing P6) and FTS triggers would fire.
  - Use a separate `prices(scryfall_id PK, usd, usd_foil, usd_etched, eur, eur_foil, tix, updated_at)` table, replaced wholesale during sync, with no triggers.
  - Price *history* grows by ~119k rows a day (~43M a year). Store history only for printings someone owns or wants, or weekly snapshots.
  - Exclude prices from backups (they can be re-downloaded).
- **Trades between users.**
  - Keep every movement single-owner (§ P3): a trade is a `trades` row (proposer, counterparty, state, timestamps) plus, on confirmation, one batch with `trade_out` rows in A's ledger and `trade_in` rows in B's.
  - Undo needs both parties, or is disallowed.
  - This needs the reason `CHECK` widened (a table rebuild migration, as in 005).
  - Without JS, "notifications" are a badge in the nav from a cheap indexed count; no websockets.
  - Design local trades so the same `trades` row and one-sided ledger rows work when the other party is on another instance (§ Stage 4, step 3).
- **Scryfall query syntax.** Parse into the existing `CardSearch` fields rather than generating SQL directly, so the whitelisting stays in one place. Colour, type and `mv` operators will want P5's mask columns and an index on `cmc`.
- **Nested locations.** `parent_id` plus a recursive CTE for totals. Indexed, this is cheap in SQLite. Keep deck locations as leaves.
- **Undo for imports.** Needs the import record, queue and hash unwound in the same transaction as the compensating movements; no infrastructure impact.
- **Card images.** Hotlinking from Scryfall's CDN follows their guidelines and costs nothing. Caching images locally would add tens of GB for all printings; if ever needed (offline use), cache only owned and wanted printings, lazily, with a size cap.

## Prioritised backlog

| # | Item | Severity | Effort | Stage | Status |
|---|---|---|---|---|---|
| S1 | Safe `next` redirects + tests | High | XS | 1 | done #19 |
| O1/S3 | Encrypted off-machine backups (to the server over SSH) + restore drill | High | S | 1 |  |
| S5 | gunicorn instead of the dev server (same port, tunnel unchanged) | High | XS | 1 | done #26 |
| O6 | Live instance from a separate worktree | Medium | XS | 1 |  |
| O4/O5 | Sync, migrate and check as timers / `ExecStartPre` | Medium | S | 1 |  |
| O2/S6 | `/healthz`, failure notifications, Secure cookie, HSTS | Medium | S | 1 |  |
| P7 | Small user-data backups (option 1) | Medium | S | 1 |  |
| P1/P2 | Fast pull plan + lean deck state | High at scale | S | 2 | done #20 |
| P3 | `movements.owner_id` (migration 006) | High at scale | S | 2 | done #20 |
| P6/P8 | Sync change guard, `PRAGMA optimize`, checkpoint | Medium | XS | 2 | done #20 |
| O3 | Logging config, no silent `except` | Medium | XS | 2 | done #25 |
| C1/C2 | Blueprints, ownership meta-test | Medium | M | 2 |  |
| S4, S2 | Import double-submit, secret race | Low | XS | 2 | done #19 |
| — | Licence file (your choice of licence) | High for FOSS | XS | 3 |  |
| C5/O7/O8/O9 | Config module, templated units, CI, lock file, verified Docker | Medium | M | 3 |  |
| — | Install docs: pip+systemd, compose, Caddy/nginx, tunnel options | Medium | S | 3 |  |
| — | JSON share links + remote shared stock (read-only federation) | Exploratory | M | 4 |  |
| P5/P10 | Colour masks, `card_key` column | Low | S | when search grows |  |
| C3/C4/C6 | Error base class, ruff/mypy, bench tools | Low | S | any time |  |

Effort: XS < 1 h · S ≈ an evening · M ≈ a weekend.
