# Migrations

Numbered, paired, reversible SQL files (the Flashback convention):

```
0001_initial_schema.up.sql
0001_initial_schema.down.sql
```

Apply with `python scripts/migrate.py up`, revert with
`python scripts/migrate.py down [--steps N | --all]`. The runner keeps a
checksummed `schema_migrations` ledger — editing an applied migration is a
deploy-blocking error; write a new one instead.

The Phase 2 DDL (liveness, enrolments, providers, search, outbox, audit) will
be `0001`. No migrations exist yet in Phase 1.
