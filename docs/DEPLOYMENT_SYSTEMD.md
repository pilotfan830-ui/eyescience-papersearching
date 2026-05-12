# Deployment Notes for Python 3.10 and systemd

## Python 3.10 compatibility

The analytics module previously used `datetime.UTC`, which is only available in
Python 3.11+. It has been replaced with `datetime.timezone.utc`, so the project
now remains compatible with Python 3.10.

## Recommended startup commands

From the project root, either of these should work:

```bash
python serve.py
```

or

```bash
uvicorn backend.app.main:app --host 0.0.0.0 --port 421
```

The new root-level `serve.py` avoids package-path issues and is a good target
for `systemd`.

## Recommended systemd service

Example:

```ini
[Unit]
Description=Eye Science Article Search
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/eye-science-article-search/paper_search_tool-codex-0424ver
ExecStart=/opt/eye-science-article-search/paper_search_tool-codex-0424ver/.venv/bin/python /opt/eye-science-article-search/paper_search_tool-codex-0424ver/serve.py
Restart=always
RestartSec=3
Environment=PAPER_SEARCH_ADMIN_USERNAME=eyescience
Environment=PAPER_SEARCH_ADMIN_PASSWORD=es2026

[Install]
WantedBy=multi-user.target
```

If you prefer `uvicorn` directly, keep the same `WorkingDirectory` and use:

```ini
ExecStart=/opt/eye-science-article-search/paper_search_tool-codex-0424ver/.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 421
```

## After editing the service

```bash
sudo systemctl daemon-reload
sudo systemctl restart your-service-name
sudo systemctl status your-service-name
sudo journalctl -u your-service-name -n 100 --no-pager
```
