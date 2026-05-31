# Deploying pysize

Runs pysize as a systemd service (uvicorn on `127.0.0.1:8731`) behind one nginx
vhost that reverse-proxies to it. Any existing nginx sites are untouched —
nginx just gains one server block on a new subdomain.

## What it sets up

- A dedicated `pysize` system user (no login).
- Code in `/opt/pysize/app`, a uv-managed venv in `/opt/pysize/venv`.
- SQLite cache in `/var/lib/pysize/cache.sqlite` (survives restarts, bounded RAM).
- `pysize.service` — hardened systemd unit, auto-restart.
- nginx vhost with TLS (Let's Encrypt via certbot, webroot) and a per-IP rate limit.

## Prerequisites

1. **DNS first.** Point the subdomain's A/AAAA record at the server *before*
   running — certbot validates over HTTP and will fail otherwise.
2. **Push the code** to the git repo you reference in `vars.yml`.
3. Ansible on your machine; SSH access to the server (root, or a sudo user).

## Run it

```bash
cd ansible
cp vars.example.yml vars.yml            # then edit: domain, certbot_email
cp inventory.example.ini inventory.ini  # then edit: host, ssh user
ansible-playbook -i inventory.ini playbook.yml
```

`vars.yml` and `inventory.ini` are gitignored — this is a public repo, so your
per-instance values (domain, email, server IP) stay local and are never
committed. The `*.example.*` files are the tracked templates.

On the first run with no certificate yet, the playbook serves the app over HTTP,
obtains a cert via certbot, then redeploys the vhost with TLS — all in one run.
Renewal is handled by certbot's existing systemd timer; the vhost keeps serving
the ACME challenge path so renewals just work.

## Updating

Push to the repo, then re-run the playbook. It pulls the new revision and
restarts the service. Re-running is safe and idempotent (certbot is skipped once
the cert exists).

## Handy checks

```bash
systemctl status pysize
journalctl -u pysize -f
curl -s localhost:8731/api/size?pkg=flask   # straight to the app, bypassing nginx
```

## Notes / remaining hardening

- `proxy_read_timeout` is 60s so a cold resolve of a giant package (first-ever
  `torch`/`headroom-ai[all]`) doesn't get cut off. After that it's cached.
- The rate limit (`10r/s`, burst 20 per IP) is a basic guard; tune in `vars.yml`.
- There's no app-side cap on resolution size yet — nginx's timeout bounds it for
  now. Add one in `main.py` if you expect adversarial traffic.
