---
title: Langfuse server setup
nav_order: 2
---

# Langfuse server setup

This page covers hosting your own [Langfuse](https://langfuse.com) v3
instance so one or more `cai` clients (installed via `install.sh`) can
report agent traces into a single project. Examples assume the public
hostname `langfuse.your-domain.com` — substitute your own.

## 1. DNS

Point a record at the server's public IP:

```
langfuse.your-domain.com   A    <server public IP>
```

Wait until it propagates before step 4 — automatic TLS issuance will
fail if the hostname doesn't yet resolve.

```bash
dig +short langfuse.your-domain.com
```

## 2. Firewall

Open the ports your reverse proxy needs (typically `:80` and `:443`)
and **block inbound `:3000`** — the Langfuse compose binds it to all
interfaces, but the proxy reaches it on `localhost` and you don't
want it exposed publicly.

## 3. Bring up the Langfuse stack

Use the [official Langfuse self-host docker-compose](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
verbatim. It defines six services: `langfuse-web`, `langfuse-worker`,
`postgres`, `clickhouse`, `redis`, `minio`.

```bash
mkdir -p /opt/langfuse && cd /opt/langfuse
wget https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml
```

### Generate strong secrets and a bootstrap project

Append a `.env` file next to the compose file. The `LANGFUSE_INIT_*`
values let Langfuse provision your org, project, admin user, and API
keys on first boot — no clicking through the UI to bootstrap.

```bash
cat > .env <<EOF
NEXTAUTH_URL=https://langfuse.your-domain.com
NEXTAUTH_SECRET=$(openssl rand -base64 32)
SALT=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 32)
CLICKHOUSE_PASSWORD=$(openssl rand -hex 32)
REDIS_AUTH=$(openssl rand -hex 32)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 32)

LANGFUSE_INIT_ORG_ID=cai
LANGFUSE_INIT_ORG_NAME=cai
LANGFUSE_INIT_PROJECT_ID=cai
LANGFUSE_INIT_PROJECT_NAME=cai
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_USER_EMAIL=admin@your-domain.com
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -base64 24)
EOF
chmod 600 .env
```

Save the `pk-lf-…` and `sk-lf-…` values — every cai client install
needs them.

Start the stack:

```bash
docker compose up -d
```

Wait ~30s for the migrations to run. `docker compose logs langfuse-web`
should end with `Ready in …`.

## 4. Reverse proxy with TLS

Front Langfuse with whatever reverse proxy you already use (Caddy,
nginx, Traefik, HAProxy…). Two requirements:

- Terminate TLS for `langfuse.your-domain.com` with a valid
  certificate.
- Forward all traffic to `http://localhost:3000` (or the docker host
  IP, if the proxy runs on a different machine).

Verify once it's up:

```bash
curl -I https://langfuse.your-domain.com/api/public/health
# HTTP/2 200
```

The UI is now at `https://langfuse.your-domain.com`. Log in with the
email/password from `LANGFUSE_INIT_USER_*` to confirm everything
bootstrapped.

## 5. Configure cai clients

On every machine running `cai`:

```bash
bash install.sh
```

When prompted, supply:

- **Base URL**: `https://langfuse.your-domain.com`
- **Public key**: the `pk-lf-…` from step 3
- **Secret key**: the `sk-lf-…` from step 3

Each client writes those into its own `.env`; `cai-refine` runs ship
traces to the central project automatically.

## Known quirk

`LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` defaults to `http://localhost:9090`
in the Langfuse compose. That only matters for browser-side media
playback in the Langfuse playground — irrelevant for `cai-refine`
tracing. You can ignore it unless you start uploading media via the
Langfuse UI.
