#!/bin/bash
# Runs ON THE HOST via SSM. The master credential is fetched here and never
# leaves the instance — it is not in the SSM command text and not in any log.
#
# RUN AS ROOT: `sudo bash`, then paste. An interactive SSM session is `ssm-user`,
# which is not in the docker group, so `docker run` fails with "permission denied
# while trying to connect to the Docker daemon socket". A non-interactive
# send-command runs as root already, which is why it worked there and not here.
#
# NOTE there is deliberately no `set -e` here. Under an interactive shell any
# non-zero command would terminate the SSM session itself, which is how a docker
# permission error ended up looking like a disconnect.
#
# Why this is needed: the cluster bootstrap ran
#   REVOKE ALL ON SCHEMA public FROM PUBLIC
# and granted CREATE ON DATABASE to the two migrator roles. CREATE ON DATABASE
# permits CREATE SCHEMA; it does NOT permit creating a table inside `public`.
# Our migrations create their tables in `public`, so `migrator_services` needs
# CREATE on that schema. WITH GRANT OPTION because migration 0015 grants USAGE
# on `public` onward to the four module roles, and only a grantee holding the
# grant option may do that.
set -uo pipefail

# Disable history expansion. When this is pasted into an INTERACTIVE bash, the
# `!` in the master secret's `rds!db-...` name triggers history expansion inside
# double quotes and bash fails with `!db: event not found`, leaving MASTER_ARN
# empty. The next command then asks Secrets Manager for a zero-length SecretId
# and every step after it fails on empty input. The ARN below is single-quoted
# for the same reason; this line covers the case where someone re-quotes it.
set +H

REGION=ap-south-1
DB_HOST=imageshield-dev.cdk8oguayyeg.ap-south-1.rds.amazonaws.com
DB_NAME=imageshield

# Hardcoded, not looked up: the host's instance role holds GetSecretValue and
# DescribeSecret on `rds!db-*` but NOT ListSecrets, so a lookup here fails with
# AccessDenied. Resolved once from an operator session and pinned.
MASTER_ARN="arn:aws:secretsmanager:ap-south-1:225989356895:secret:rds!db-9ef90c0b-67fb-4c28-b944-21f449f273ba-83e3We"
echo "master secret: ${MASTER_ARN##*:}"

SECRET=$(aws secretsmanager get-secret-value --region "$REGION" \
  --secret-id "$MASTER_ARN" --query SecretString --output text)

MASTER_USER=$(echo "$SECRET" | python3 -c 'import sys,json;print(json.load(sys.stdin)["username"])')
export PGPASSWORD=$(echo "$SECRET" | python3 -c 'import sys,json;print(json.load(sys.stdin)["password"])')
unset SECRET
echo "master user: $MASTER_USER"

# psql is not on the ECS-optimized AMI; use the service image, which has psycopg.
IMAGE=225989356895.dkr.ecr.ap-south-1.amazonaws.com/imageshield/services:d93b3fa
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin 225989356895.dkr.ecr.ap-south-1.amazonaws.com >/dev/null 2>&1

# -i is REQUIRED. Without it docker does not attach stdin, the heredoc is
# discarded, `python -` reads EOF and exits 0 — so the script reports success
# having granted nothing. That failure is silent and was observed once.
#
# --platform linux/arm64 is also REQUIRED, despite the host being arm64
# (t4g.medium, verified `Architecture: arm64` via describe-instances). A manual
# `docker run` on this AMI resolves the manifest for linux/amd64 and fails with
# "no matching manifest for linux/amd64 in the manifest list entries"; the ECS
# agent passes the platform itself, which is why the migration task pulled the
# same image without complaint and this did not.
docker run --rm -i --platform linux/arm64 \
  -e PGPASSWORD \
  -e "MASTER_USER=$MASTER_USER" \
  -e "DB_HOST=$DB_HOST" \
  -e "DB_NAME=$DB_NAME" \
  --entrypoint python "$IMAGE" - <<'PY'
import os
import psycopg

url = (
    f"postgresql://{os.environ['MASTER_USER']}:{os.environ['PGPASSWORD']}"
    f"@{os.environ['DB_HOST']}:5432/{os.environ['DB_NAME']}?sslmode=require"
)
with psycopg.connect(url, autocommit=True) as conn:
    who = conn.execute("SELECT current_user, current_database()").fetchone()
    print("connected as:", who)

    owner = conn.execute(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='public'"
    ).fetchone()[0]
    print("public schema owner:", owner)

    for role in ("migrator_services",):
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone()
        if not exists:
            print(f"role {role} ABSENT — not granting")
            continue
        conn.execute(
            f'GRANT USAGE, CREATE ON SCHEMA public TO "{role}" WITH GRANT OPTION'
        )
        print(f"granted USAGE, CREATE ON SCHEMA public TO {role} WITH GRANT OPTION")

    # Prove it, rather than assuming the GRANT took.
    for priv in ("USAGE", "CREATE"):
        ok = conn.execute(
            "SELECT has_schema_privilege('migrator_services','public',%s)", (priv,)
        ).fetchone()[0]
        print(f"has_schema_privilege(migrator_services, public, {priv}) = {ok}")

    # CREATEROLE: 0001 creates `imageshield_app`, 0015 the four module roles and
    # 0016 `imageshield_proxy_ro`. The bootstrap granted CREATE ON DATABASE but
    # not this, so the runner reaches 0001 and stops at
    # "permission denied to create role".
    #
    # On Postgres 16 this is narrower than it reads: a CREATEROLE role may only
    # alter or drop roles it created (or holds ADMIN OPTION on), so this does not
    # make the migrator a superuser over the other repo's roles. It is also what
    # makes 0017 and 0018 work — membership requires ADMIN OPTION, which the
    # creator of a role holds implicitly.
    conn.execute('ALTER ROLE "migrator_services" CREATEROLE')
    has_createrole = conn.execute(
        "SELECT rolcreaterole FROM pg_roles WHERE rolname='migrator_services'"
    ).fetchone()[0]
    print(f"migrator_services rolcreaterole = {has_createrole}")
PY
echo "GRANT STEP COMPLETE"
