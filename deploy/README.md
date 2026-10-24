# Debian / GCP VM deployment

Architecture: browser → Nginx (HTTPS) → Gunicorn (127.0.0.1:5000) → pizza API.
These instructions assume a single Debian VM, a sudo-capable login, and a domain
pointing to its reserved external IP. Run the commands on the VM in Bash.
Gunicorn runs on Linux; keep `python app.py` for local Windows development.

## 1. Install and copy the application

```bash
sudo apt update
sudo apt install -y python3 python3-venv nginx certbot python3-certbot-nginx unzip
sudo useradd --system --user-group --home-dir /var/lib/luigi-ui --no-create-home --shell /usr/sbin/nologin luigi-ui
sudo install -d -m 755 /opt/luigi-ui
```

Upload the deployment ZIP to the VM, then extract it:

```bash
sudo unzip deployment.zip -d /opt/luigi-ui
cd /opt/luigi-ui
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements-production.txt
sudo chown -R root:root /opt/luigi-ui
sudo chmod -R go-w /opt/luigi-ui
```

The service account reads the code and writes only to its systemd-managed
`/var/lib/luigi-ui` state directory. Do not upload a Windows `.venv`, `.env`,
or local session files. `package_deployment.py` builds the deployment archive.

## 2. Configure secrets and API connection

```bash
sudo install -m 600 deploy/luigi-ui.env.example /etc/luigi-ui.env
python3 -c 'import secrets; print(secrets.token_hex(32))'
sudoedit /etc/luigi-ui.env
```

Replace `SECRET_KEY` with the generated value and retain it across restarts.
Set `API_BASE_URL` to the API address reachable **from the VM**. The default
works only if the API runs on this same VM on port 8000. Use a private network
or HTTPS for a remote API. The UI does not start or deploy the API.
The app does not automatically load `.env`; systemd loads `/etc/luigi-ui.env`.
Keep `COOKIE_SECURE=1` for HTTPS; HTTP-only browser sessions will not work with
secure cookies. Complete TLS setup before signing in.

## 3. Start Gunicorn under systemd

```bash
sudo install -m 644 deploy/luigi-ui.service /etc/systemd/system/luigi-ui.service
sudo systemctl daemon-reload
sudo systemctl enable --now luigi-ui
sudo systemctl status luigi-ui --no-pager
curl -I http://127.0.0.1:5000/login
```

Expect HTTP 200 from `/login`, which does not need the API. The service starts
two processes with four threads each, a starting point for modest traffic.
Tune `WEB_CONCURRENCY` and `GUNICORN_THREADS` against VM memory, traffic, and API
capacity. Threads help while requests wait for the API. The 60-second Gunicorn
timeout monitors worker liveness; it is not a strict per-request deadline for
threaded workers. API calls retain their own connection/read timeouts.

## 4. Enable Nginx and HTTPS

```bash
sudo install -m 644 deploy/luigi-ui.nginx /etc/nginx/sites-available/luigi-ui
sudoedit /etc/nginx/sites-available/luigi-ui
sudo ln -s /etc/nginx/sites-available/luigi-ui /etc/nginx/sites-enabled/luigi-ui
sudo nginx -t
sudo systemctl reload nginx
```

Replace `pizza.example.com` with your actual domain. Set its DNS A record to
the VM's reserved public IPv4 address. Add an AAAA record only if IPv6 is
configured. In GCP, allow ingress TCP 80 and 443 to this VM (and in any local
firewall). Limit SSH access appropriately. Do not open Gunicorn port 5000 or
the backend API port to the public internet for this setup.

Once DNS resolves and port 80 is reachable:

```bash
sudo certbot --nginx -d pizza.example.com --redirect
sudo certbot renew --dry-run
curl -I https://pizza.example.com/login
```

Use your real domain in both commands. Certbot configures certificates and
the HTTPS redirect. Verify the certificate renewal timer with
`systemctl list-timers --all | grep certbot`. Test menu loading, sign-in,
basket persistence, and staff access through HTTPS. Only submit real orders
when intended. If using a GCP HTTPS load balancer instead, adapt TLS and proxy
trust to that topology; this configuration assumes Nginx terminates TLS.

## Operations and limitations

```bash
sudo journalctl -u luigi-ui -n 100 --no-pager
sudo journalctl -u luigi-ui -f
sudo systemctl restart luigi-ui
```

After code updates, install changed dependencies and restart the service.
After environment changes, restart; a worker reload does not reload systemd's
environment file. After service-file changes, run `daemon-reload` then restart.
Keep `/var/lib/luigi-ui` across code updates. Treat its contents as credentials;
do not publish them or include them in ordinary deployment archives.

Filesystem sessions are shared by workers on this single VM, but are not a
multi-VM session solution. The current cache threshold is 1,000 entries;
eviction can end sessions early. Use a protected shared store such as Redis
before scaling across VMs or when stronger session durability is needed.
Concurrent requests for one session can overwrite session changes, and
checkout still needs API-side idempotency for reliable duplicate prevention.
Gunicorn does not resolve those existing application limitations. Keep OS and
Python dependencies patched and enforce login rate limits at the API.

References: [Flask Gunicorn deployment](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/),
[Gunicorn deployment](https://docs.gunicorn.org/en/stable/deploy.html).
