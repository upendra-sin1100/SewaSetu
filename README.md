# Sewa Setu — Old Age Pension Portal

**Government of Purvanchal · Department of Social Welfare**
Developed by Netlink Infosolutions Pvt Ltd (2023). Vendor maintenance contract ended 31 Jan 2026.

---

## 🟢 Live Deployment

| URL | What it does |
|-----|-------------|
| http://52.202.28.56/ | Citizen portal — apply, track status, download ack PDF |
| http://52.202.28.56/admin | Admin dashboard — review / approve / reject applications |
| http://52.202.28.56/__gateway/ | SMS gateway console — view OTPs sent to any mobile number |

### Port & Service Map

| Docker service | Image | Host port → Container port | Notes |
|----------------|-------|-----------------------------|-------|
| `app` | `sewasetu-handout-app` (gunicorn/Flask) | **80 → 8000** | Main web process; Nginx or direct exposure |
| `db` | `postgres:15` | internal only (5432) | Never exposed to internet |
| `smsgw` | smsgw (Python/Flask) | internal only (8025) | Served through app at `/__gateway/` |
| `scheduler` | sewasetu-handout-scheduler | — | **NOT running** — deemed-approval cron is stopped; start manually if needed |

> **Admin credentials** are in `.env.production` (`SEWASETU_ADMIN_PASSWORD`). Username is always `admin`.
> **OTPs** for any test mobile number appear instantly at `/__gateway/` — no real SMS is sent.

---

## What This Is

A citizen-facing web portal for the Old Age Pension Scheme. Citizens apply online, upload a supporting document (JPG/PDF ≤ 5 MB), receive an OTP to verify their mobile number, and download a PDF acknowledgement. District administrators review applications, set approval/rejection status, and trigger deemed-approval under the RTPS Act when the 15-day SLA lapses.

### Architecture

```
Browser → Gunicorn (Flask) :8000
              ├── PostgreSQL :5432  (citizen & application data)
              ├── SMS Gateway :8025  (simulated; OTPs visible at /__gateway/)
              └── Cron scheduler  (deemed-approval job, runs nightly)
```

All four services run as Docker containers orchestrated by `docker-compose.yml`.

---

## Quick Start (Local / Dev)

### Prerequisites

- Docker Desktop ≥ 4.20 (Linux containers mode)
- Git

### Steps

```bash
git clone <repo-url>
cd sewasetu-handout

# Copy and edit secrets (never commit .env.production)
cp .env.production.example .env.production   # if example exists
# OR edit .env.production directly (already present in handover copy)

docker compose up --build
```

| Service | URL |
|---------|-----|
| Citizen portal | http://localhost:8000 |
| Admin dashboard | http://localhost:8000/admin |
| SMS Gateway (OTP viewer) | http://localhost:8000/__gateway/ |

> **OTPs**: The gateway is a simulator. Any OTP sent to a mobile number appears at `/__gateway/`. Use it during local testing — no real SMS is sent.

### Running Tests

Tests must run **inside the container** (no localhost:8000 dependency):

```bash
docker compose run --rm --no-deps app python -m pytest tests -q
```

Expected: **74 passed**.

---

## Environment Variables

Set in `.env.production` (never commit this file):

| Variable | Description |
|----------|-------------|
| `SEWASETU_SECRET_KEY` | Flask session signing key. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SEWASETU_ADMIN_PASSWORD` | Password for the `/admin` login (username: `admin`) |

---

## Project Structure

```
sewasetu-handout/
├── app/
│   ├── app.py              # Flask application (routes, PDF generation)
│   ├── rules.py            # Pure-Python business rules (SLA, age check)
│   ├── schema.sql          # Database schema
│   ├── config/app.ini      # Runtime config (SLA days, upload dir, …)
│   ├── templates/          # Jinja2 HTML templates
│   ├── static/
│   │   └── fonts/
│   │       ├── NotoSansBengali-Regular.ttf   # Unicode TTF for Assamese/Bengali names in PDF
│   │       └── OFL.txt                        # SIL Open Font Licence 1.1
│   ├── scripts/
│   │   └── deemed_approval.py  # Nightly cron: auto-approve overdue PENDING apps
│   └── tests/              # pytest test suite
├── smsgw/                  # Simulated SMS gateway (Python/Flask)
├── seed/seed.sql           # 10 000-row seed dataset (Assamese names, villages)
├── logs/                   # Production server logs Jan–Jun 2026
├── docker-compose.yml
├── .env.production         # Secrets — DO NOT COMMIT
├── BRIEF.md                # DC briefing notes
├── HANDOFF.md              # Full handover documentation
└── AI_LOG.md               # Agent correction log
```

---

## Deploying on AWS EC2 t3.small

### Why t3.small

| Resource | t3.small | This app's needs |
|----------|----------|-----------------|
| vCPUs | 2 | Gunicorn 2–4 workers fits comfortably |
| RAM | 2 GB | Flask + Postgres + smsgw ≈ 600 MB typical |
| Network | Up to 5 Gbps burst | PDF downloads are small; fine |
| Storage | EBS (attach 20–30 GB gp3) | Uploads + Postgres WAL + logs |

### Step-by-step

#### 1. Launch the instance

```
AMI:            Amazon Linux 2023 (or Ubuntu 22.04 LTS)
Instance type:  t3.small
Storage:        20 GB gp3 root + 20 GB gp3 data volume (mount at /data)
Security group: allow inbound TCP 80, 443 (Nginx), 22 (SSH, restricted to your IP)
                DO NOT expose 8000, 5432, or 8025 to the internet
Key pair:       use an existing key or create one
```

#### 2. Install Docker

```bash
# Amazon Linux 2023
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user   # log out and back in
docker --version

# docker compose plugin
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```

#### 3. Clone and configure

```bash
sudo mkdir -p /opt/sewasetu
sudo chown ec2-user: /opt/sewasetu
cd /opt/sewasetu
git clone <repo-url> .

# Create secrets
cat > .env.production <<EOF
SEWASETU_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
SEWASETU_ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
EOF
chmod 600 .env.production
```

#### 4. Mount the data volume (for uploads + Postgres data)

```bash
# Assuming /dev/xvdf is the 20 GB data volume
sudo mkfs.xfs /dev/xvdf
sudo mkdir -p /data
echo '/dev/xvdf /data xfs defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a

# Docker volumes will land here
sudo mkdir -p /data/docker
sudo tee /etc/docker/daemon.json <<EOF
{"data-root": "/data/docker"}
EOF
sudo systemctl restart docker
```

#### 5. Install Nginx as reverse proxy

```bash
sudo dnf install -y nginx
sudo tee /etc/nginx/conf.d/sewasetu.conf <<'EOF'
server {
    listen 80;
    server_name _;          # replace with your domain or EC2 public DNS

    client_max_body_size 6M;   # slightly above the app's 5 MB limit

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }
}
EOF
sudo nginx -t && sudo systemctl enable --now nginx
```

> **HTTPS**: Use [Certbot](https://certbot.eff.org/) with the Nginx plugin once you have a domain pointing at the instance.
> ```bash
> sudo dnf install -y certbot python3-certbot-nginx
> sudo certbot --nginx -d your.domain.example
> ```

#### 6. Start the application

```bash
cd /opt/sewasetu
docker compose up -d --build
docker compose ps      # all four services should show "Up"
docker compose logs -f app   # watch for startup errors
```

#### 7. Verify

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/
# → 200
```

Visit `http://<EC2-public-IP>/` in a browser (port 80 via Nginx).

#### 8. Set up auto-restart on reboot

```bash
sudo tee /etc/systemd/system/sewasetu.service <<EOF
[Unit]
Description=Sewa Setu Docker Compose
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/sewasetu
ExecStart=/usr/local/lib/docker/cli-plugins/docker-compose up -d
ExecStop=/usr/local/lib/docker/cli-plugins/docker-compose down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable sewasetu
```

#### 9. Monitor and maintain

```bash
# View logs
docker compose logs app --tail=100 -f

# Run tests in production image (safe — no DB writes)
docker compose run --rm --no-deps app python -m pytest tests -q

# Database backup
docker compose exec db pg_dump -U sewasetu sewasetu | gzip > /data/backup-$(date +%F).sql.gz

# Update the app
cd /opt/sewasetu
git pull
docker compose up -d --build
```

### Security checklist for EC2

- [ ] Security group: only ports 22, 80, 443 open inbound
- [ ] `.env.production` has `chmod 600` — not readable by other users
- [ ] `SEWASETU_SECRET_KEY` is ≥ 48 random bytes (use `secrets.token_urlsafe(48)`)
- [ ] `SEWASETU_ADMIN_PASSWORD` is a strong random password (not the handover default)
- [ ] HTTPS enabled via Certbot before going live
- [ ] EBS volume encrypted at rest (enable in EC2 console during launch)
- [ ] Postgres port 5432 not exposed in security group (internal Docker network only)
- [ ] Regular `pg_dump` backups to S3: `aws s3 cp /data/backup-*.sql.gz s3://your-bucket/sewasetu/`

---

## Key Bug Fixes Applied (post-handover)

| Fix | Description |
|-----|-------------|
| **Fix 4b** | Ack PDF re-download: Chrome's PDF viewer fetches the URL twice. Session token now uses `get()` not `pop()` + `Content-Disposition: inline`. |
| **Fix 5** | Upload limit was 100 KB PDF-only; form promised 5 MB JPG/PDF. Now accepts both, plain-language errors. |
| **Fix 6** | Assamese/Bengali names were byte-truncated by `sanitize()` and rendered as `?` in PDFs. Now character-based limit (100 chars = DB column); Noto Sans Bengali TTF embedded via `add_font()`; `_font_for()` helper auto-selects Helvetica for ASCII and NotoSansBengali for non-ASCII text. |

See `AI_LOG.md` for agent correction notes and `HANDOFF.md` for the full handover documentation.

---

## Port Reference

| Port | Service | Exposed to host |
|------|---------|----------------|
| 8000 | Gunicorn (Flask app) | Yes (via Nginx on 80/443 in production) |
| 5432 | PostgreSQL | No (internal Docker network only) |
| 8025 | SMS Gateway simulator | No (served through app at `/__gateway/`) |
