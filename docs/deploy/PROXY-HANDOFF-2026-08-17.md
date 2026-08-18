# Handoff to the proxy repo — 2026-08-17

Two things changed underneath the proxy when `services` deployed to the
ap-south-1 dev environment, and one thing will block the proxy's own first
migration. None of it requires a proxy code change.

Measured against the live dev database on 2026-08-17. As of that moment the proxy
had **not** migrated: the only schemas present were `public` and `svc` — no
`profile`, `report` or `shared`.

---

## 1. `migrator_backend` cannot create roles yet — this will stop your first deploy

```sql
ALTER ROLE migrator_backend CREATEROLE;
```

Requires the RDS master, and there is no route to Postgres except from the
host — `aws ssm start-session --target i-0d277703b778392ef`, then `sudo bash`.
`docs/deploy/grant-public-schema.sh` in the services repo is a working template;
change the role name.

**Why it is needed.** Your 0012/0013 create `imageshield_proxy` and add
`app_backend` / `app_worker` to it. Creating a role requires the `CREATEROLE`
attribute, which the cluster bootstrap did not grant. Without it the runner stops
at `permission denied to create role`.

**Not a general shortfall — measured state:**

| role | `public` USAGE | `public` CREATE | `CREATEROLE` |
|---|---|---|---|
| `migrator_backend` | ✅ | ✅ | ❌ **this one** |
| `migrator_services` | ✅ | ✅ | ✅ |
| `app_backend` | ❌ | ❌ | ❌ |
| `app_worker` | ❌ | ❌ | ❌ |

So you already have the schema grants — only the role attribute is missing. The
services repo needed both; `DEPLOY-DEV.md` §4 now records the whole story.

`app_backend` and `app_worker` showing no `public` privileges is expected and
correct: they read the `svc` views through membership (see §2), not `public`
directly.

---

## 2. Migration 0017 completed a grant chain that had never reached you

**The defect.** Migration 0016 created `imageshield_proxy_ro` and granted it
`SELECT` on the four `svc` contract views. That role is `NOLOGIN` by design — a
grant target, not an identity — and **nothing had ever been granted membership in
it.** So the contract those views describe reached nobody.

It was invisible until this deploy because the proxy previously connected as the
database owner, which can read everything. Ownership and a correct grant chain
look identical from there. In dev the proxy connects as `app_backend`, which
inherits nothing from ownership, so the same gap would have presented as your
report screen failing on its first read and your `/readyz` never going green —
and it would have presented as *"the `svc` views are missing"*, which is the
wrong place to look.

**Fixed by services migration 0017.** Current membership, verified:

```
imageshield_proxy_ro members: app_backend, app_worker, migrator_services
```

**Why it had to be ours to run.** Granting membership requires `ADMIN OPTION` on
the role. `imageshield_proxy_ro` was created by the services migration runner
(0016), so that runner holds it implicitly and yours does not. You could not have
completed this chain from your side however much you wanted to.

**Why three role names and not one.** The tidy form is a single
`GRANT imageshield_proxy_ro TO imageshield_proxy` — your umbrella role, which
your 0012/0013 make `imageshield_app`, `app_backend` and `app_worker` members of,
so one grant would reach every login role you have now or add later. But
`imageshield_proxy` only exists after **your** 0001 has applied, and the deploy
order is services-first (we create `svc` before your migrator runs, because your
`/readyz` cannot pass without the four views). On a virgin database the tidy form
would grant nothing at all and fail silently. So 0017 enumerates all three names
and skips any that are absent — `imageshield_proxy` is currently skipped with a
`NOTICE`, and will be granted the next time 0017 runs after your 0001.

**Re-running 0017 after your migrations land is harmless and worth doing** — it is
idempotent, and it is what picks up `imageshield_proxy`.

### ⚠️ 0017's down leg is a coordinated deploy, not a solo rollback

Reverting it revokes the membership and therefore removes your **only** path to
the four `svc` views. Your report screen and your `/readyz` fail immediately
afterwards. Same status 0016's down leg carries, for the same reason. It does not
drop any role — `imageshield_proxy` belongs to your migrations, `app_backend` and
`app_worker` to the cluster bootstrap, and dropping another owner's login role to
reverse a `SELECT` would take your `api` and `worker` offline to undo a grant.

---

## 3. The four `svc` views are live and shape-checked

All four exist and match the contract, verified by the services `/readyz`
endpoint, which returns 503 unless every expected column is present with the
expected type:

```
/readyz → 200 {"status":"ready","version":"0.1.0","db":"ok","problems":[]}
```

That check reads `pg_catalog` rather than `information_schema`, deliberately —
`information_schema` is privilege-filtered, so a role without a grant on `svc`
sees zero columns and a perfectly good contract reports as four missing views.

Columns may be **added** freely; the check asserts expected-subset-of-actual.
Removals and retypes are the breaking changes and both fail readiness in the repo
that owns the views, rather than failing your reader at runtime after a deploy has
already gone out.

`PROXY_INTEGRATION.md` §6 and `SCHEMA.md` §2c remain authoritative for the
column list.

---

## 4. Nothing here needs a proxy code change

Item 1 is one SQL statement run from the host. Items 2 and 3 are already done.
The only thing to know is the down-leg coupling in §2 and that
`ALTER ROLE migrator_backend CREATEROLE` has to precede your first migration.
