# IMAP/SMTP Mail MCP Server

A Python MCP server for IMAP mail accounts. It is especially useful when your mail account is not Gmail, Microsoft 365 or similar, since most AI assistants already offer ready-made integrations for those. It is intended for any AI assistant that needs an MCP server reachable from the internet.

## Overview

The server runs on a Raspberry Pi and is reached through a Cloudflare Tunnel, protected by Cloudflare Access (Service Token).

```
Langdock -> https://mcp.example.com/mcp
         -> Cloudflare Access (Service Token check)
         -> Cloudflare Tunnel (cloudflared on the Raspberry Pi)
         -> http://localhost:3000 (this server)
         -> IMAP / SMTP mail provider
```

### Design principles

- **No permanent delete.** `delete_mail` only moves mails to the trash.
- **Drafts first.** New mails and replies are saved as drafts by default.
- **Sending only on explicit request.** `send_mail` and `forward_mail` send immediately and should be used only after the user has approved.
- **Folder safety.** Special folders (Trash, Drafts, Sent) are found via SPECIAL-USE flags or known names. Nothing is created unexpectedly.
- **Attachments.** Forwarded mails keep their attachments (up to 20 MB in total).

### Tools

| Group | Tools |
|---|---|
| Test | `server_time`, `mail_check` |
| Folders | `list_folders`, `create_folder` |
| Reading | `list_mails`, `search_mails`, `read_mail` |
| Organising | `set_mail_status`, `move_mail`, `delete_mail` |
| Writing | `create_draft`, `create_reply_draft`, `create_forward_draft`, `forward_mail`, `send_mail` |

## Prerequisites

- Raspberry Pi with Raspberry Pi OS (64-bit recommended) and Python 3.9 or newer
- A mailbox with IMAP and SMTP access (for example Microsoft 365, Google Workspace, or a hosting provider)
- A domain managed in Cloudflare (free plan is sufficient)
- A Cloudflare account with Zero Trust (free plan is sufficient)
- A Langdock account with permission to add custom MCP servers

## 1. Prepare the Raspberry Pi

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-venv git
```

Clone the repository into the home directory of your user:

```bash
cd ~
git clone https://github.com/mrdrbernd/imap_mail-mcp-server.git mail-mcp
cd ~/mail-mcp
```

## 2. Create the Python environment

```bash
cd ~/mail-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins `mcp<2`. Version 2 of the MCP SDK renamed `FastMCP`, so keep this pin unless you adapt the code.

## 3. Configure the credentials

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Fill in your values:

```env
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
MAIL_USER=user@example.com
MAIL_PASSWORD=your-app-password
```

Never commit the `.env` file. It is listed in `.gitignore`.

## 4. Test the server locally

Start the server in the foreground:

```bash
cd ~/mail-mcp
source venv/bin/activate
python server.py
```

In a second terminal, run the test client (replace the URL if you use a different port):

```bash
cd ~/mail-mcp
source venv/bin/activate
python client_test.py http://localhost:3000/mcp
```

Stop the server with `Ctrl+C` when the test is done.

## 5. Run as a systemd service

Copy the service file and replace the placeholder `YOUR_USER` with your Linux user (for example `pi`):

```bash
sudo cp deploy/mail-mcp.service /etc/systemd/system/mail-mcp.service
sudo nano /etc/systemd/system/mail-mcp.service
```

Important lines in the unit file:

```ini
[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/mail-mcp
ExecStart=/home/YOUR_USER/mail-mcp/venv/bin/python /home/YOUR_USER/mail-mcp/server.py
Restart=on-failure
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mail-mcp
sudo systemctl status mail-mcp
journalctl -u mail-mcp -f
```

### Optional: daily restart

The repository contains a restart service and timer (default: 04:00 every day). Install them only if you want a regular restart:

```bash
sudo cp deploy/mail-mcp-restart.service deploy/mail-mcp-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-mcp-restart.timer
systemctl list-timers | grep mail-mcp
```

## 6. Cloudflare Tunnel

The server listens only on `localhost:3000`. Cloudflare Tunnel publishes it securely without opening ports on your router.

1. Install `cloudflared` on the Pi, following the Cloudflare documentation for Raspberry Pi (ARM64 or ARMv7, depending on your OS).
2. In the Cloudflare dashboard, go to **Zero Trust > Networks > Tunnels > Create a tunnel** and choose **Cloudflared**.
3. Name the tunnel and copy the install command shown. Run it on the Pi. The tunnel then runs as a service.
4. Add a **Public Hostname** to the tunnel:
   - **Subdomain / Domain:** `mcp` / `example.com` (result: `mcp.example.com`)
   - **Service:** `HTTP` and `localhost:3000`
   - **HTTP Host Header:** `localhost:3000`

> **Why the Host Header matters:** The MCP SDK rejects requests whose `Host` header is not `localhost`. Without this setting you get `421 Invalid Host header`.

## 7. Cloudflare Access with a Service Token

Access ensures that only requests with a valid token reach the server.

1. **Create a Service Token:** Zero Trust > Access > Service Auth > **Create Service Token**. Copy the **Client ID** and **Client Secret** immediately. The secret is shown only once.
2. **Create an Access application:** Zero Trust > Access > Applications > **Add an application** > **Self-hosted**.
   - **Application domain:** `mcp.example.com`, path empty
3. **Create a policy:** Action **Service Auth**, Include: **Service Token** > select the token you created.
4. Save the application.

### Verify access

Without headers, the request must be **blocked** (HTTP 403 or the Access login page):

```bash
curl -i -X POST https://mcp.example.com/mcp
```

With the Service Token headers, the request must reach the server:

```bash
curl -i -X POST https://mcp.example.com/mcp \
  -H "CF-Access-Client-Id: YOUR_CLIENT_ID" \
  -H "CF-Access-Client-Secret: YOUR_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

If the first request is not blocked, the Access policy is not active. Check the application and policy before going further.

## 8. Connect Langdock

1. In Langdock, add a new custom MCP integration (Integrations > add custom server).
2. **URL:** `https://mcp.example.com/mcp`
3. **Authentication:** None
4. **Custom headers** (plain text values, no braces):
   - `CF-Access-Client-Id` = your Client ID
   - `CF-Access-Client-Secret` = your Client Secret
5. Save and load the server functions. The tools from the table above should appear.
6. Recommended: enable confirmation for `send_mail` and `forward_mail`, and keep the write tools under review.
7. Test in a chat, for example: "Show me the 5 newest mails in the inbox."

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `421 Invalid Host header` | Tunnel Host Header not set | Set HTTP Host Header to `localhost:3000` in the public hostname |
| `502 Bad Gateway` from Cloudflare | Server not running or wrong port | `systemctl status mail-mcp`, check port 3000 |
| `403` or login page in Langdock | Service Token headers wrong or missing | Re-enter the headers exactly; check the policy includes the token |
| Request without headers is **not** blocked | Access policy missing or not saved | Check the application domain and the **Service Auth** policy |
| `IMAP error` in `mail_check` | Wrong host, user or password | Check `.env`; many providers need an app password |
| `Folder '...' does not exist` | Folder name differs | Run `list_folders` and use the exact name |
| `No folder found for 'Drafts'` | Provider uses another folder name | Create the folder on the server or add its name to `NAME_FALLBACKS` in `server.py` |
| Service does not start | Wrong paths or user in unit file | Check `User=` and paths; `journalctl -u mail-mcp -e` |

## Security notes

- Keep `.env` and the Service Token secret out of Git, chat logs and screenshots. Rotate them if they leak.
- Use an app password or a dedicated mailbox with restricted rights where possible.
- Keep the server bound to `localhost`. Only the Cloudflare Tunnel should reach it.
- Review every send action. The assistant should show recipient, subject and text before sending.
- Keep the Raspberry Pi updated (`sudo apt update && sudo apt upgrade`).

## License

No license has been chosen yet.
