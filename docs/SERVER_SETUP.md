# Hosting Your Own Private Server

This guide gets you from a fresh cloud server to a working private
**Gwent Beta Restoration** server that you and your friends can play on.

There are three things to do:

1. Extract the card data definitions from your own copy of the game
   (`deploy/extract_data_definitions.py` — see step 2).
2. Put the server/deploy files on a machine with a public IP and start them.
3. Point the launcher at your server and share its IP with those you want to
   play with.

That's it. The launcher handles all of the messy client-side setup
(certificate, DNS redirect, game mod) automatically.

> **What's actually happening:** the game thinks it's talking to GOG's
> servers. Your server pretends to be GOG — nginx answers the `*.gog.com`
> domains on port 443 and forwards everything to `server.py`. Your friends'
> launchers redirect the game to your server.

---

## 1. Get a server

Any Ubuntu 22.04+ machine or even just your own pc or laptop with a public IP and port forwarding works. If you want a remote server, the free option that's more than enough is **Oracle Cloud's Always-Free ARM VM**:

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com).
2. **Compute → Create Instance** → image **Ubuntu 22.04**, shape
   **VM.Standard.A1.Flex** (Ampere ARM). Download your private and public key. Create it.
3. Note the **public IP**.

**Open these ports** so players can reach the server. Two firewalls sit in
front of an Oracle VM:

- **Oracle VCN Security List** (Networking → your VCN → subnet → Security
  Lists → Default): add Ingress rules for TCP **443**, **7777**, **8445**,
  and **8447** from `0.0.0.0/0`.
- **The VM's own firewall (iptables):** the setup script in step 2 opens these
  for you.

(On a non-Oracle VPS, just make sure 443, 7777, 8445, and 8447 are reachable.)

---

## 2. Install and start the server

First, **extract the card data definitions from your own game install.**
This project does not distribute CD PROJEKT RED's game data, so you generate
the `Data_definitions/` folder yourself from a copy of Gwent 0.9.24.3 you own.
On your local machine, from the `deploy/` folder:

```bash
python3 extract_data_definitions.py            # auto-detects common install paths
# or point it at your install:
python3 extract_data_definitions.py "D:\path\to\Gwent The Witcher Card Game"
```

This writes `deploy/Data_definitions/` (Templates.xml, Abilities.xml, etc.).

Then, from the `deploy/` folder of **your local copy of this repo**, upload the
server files. Note the `static/` folder and the `Data_definitions/` you just
generated — both are required:

```bash
scp setup_server.sh \
    server.py broker.py relay.py db.py \
    rewards.json nginx-gwent.conf \
    ubuntu@<YOUR_PUBLIC_IP>:~
scp -r static Data_definitions ubuntu@<YOUR_PUBLIC_IP>:~
```


SSH into the machine:

```bash
ssh -i <your_private_key> ubuntu@<YOUR_PUBLIC_IP>
```

On the server, run the setup script:

```bash
sudo bash setup_server.sh
```

It installs Python, nginx, and OpenSSL, generates the TLS certificate, opens
the firewall ports, writes the nginx config, and creates the `systemd`
services. Then put the files in place and start everything:

```bash
sudo cp server.py broker.py relay.py db.py \
        rewards.json /opt/gwent-server/
sudo cp -r static Data_definitions /opt/gwent-server/
sudo chown -R gwent:gwent /opt/gwent-server

sudo systemctl start gwent.target
sudo systemctl status gwent-server gwent-broker gwent-relay   # all "active (running)"?
```

> **nginx note:** `setup_server.sh` writes and enables its own nginx config
> automatically — you don't normally touch nginx yourself (the package is
> installed by the script; it is **not** bundled in this repo). If the
> auto-generated config doesn't work correctly or completely, the bundled
> `nginx-gwent.conf` is an alternative that may work — copy it into place and
> reload:
>
> ```bash
> sudo cp nginx-gwent.conf /etc/nginx/sites-available/gwent
> sudo nginx -t && sudo systemctl reload nginx
> ```

Quick check that the API is alive:

```bash
curl -k https://127.0.0.1/register -H "Content-Type: application/json" \
  -d '{"username":"TestUser"}'
```

A JSON response with an `"id"` means it's working. To watch what's happening
live (the most useful debugging tool):

```bash
sudo journalctl -u gwent-relay -f
```

---

## 3. Share the address

**Share** the resulting public ip with those you want to play. On first run it sets up the
certificate, the DNS redirect, the Galaxy spoof, and the game mod, then launches
Gwent pointed at your server. Each player picks a username on first launch and
is registered automatically.

(Players also need the Gwent 0.9.24.3.432 beta game files)

---

## Keeping it running

```bash
sudo systemctl restart gwent.target          # restart after changing a file

# Back up everything (users, decks, progress are in one SQLite file):
sqlite3 /opt/gwent-server/data/gwent.db \
  ".backup /opt/gwent-server/data/gwent_backup.db"

# See who's registered:
sqlite3 /opt/gwent-server/data/gwent.db \
  "SELECT id, username FROM users;"
```

---

## If something's wrong

**A service won't start** — read its log:
`sudo journalctl -u gwent-server -n 50` (or `-broker` / `-relay`). Usually a
missing data file (did you copy `rewards.json`, the `static/` folder, and
`Data_definitions/`?).

**Friends connect but matches never start** — port 7777 or 8445 is blocked.
Re-check both firewalls (Oracle Security List *and* the VM's iptables).

**Friend invites never arrive** — port 8447 is blocked. Same fix: open 8447 in
both firewalls (it carries game invitations between players).

**The launcher can't reach the server** — make sure the address in `server.txt`
was right *before* you built, and that `curl -k https://127.0.0.1/register`
works on the server itself. If it works locally but friends can't connect, the
problem is the network path (ports / DNS), not the server.

**Players get signed out** — that's almost always client-side; have them re-run
the launcher, which reinstalls the certificate and redirect.

---

## Ports, for reference

| Port | Open to internet? | What it's for |
|------|-------------------|---------------|
| 443  | Yes | TLS endpoint for the GOG domains (nginx → `server.py`) |
| 7777 | Yes | Live match relay |
| 8445 | Yes | Push-notification broker |
| 8447 | Yes | Friend / game-invite listener |
| 8443, 8444, 8446 | No (localhost) | Internal — leave closed |

---

Game content (cards, art, quests) belongs to CD PROJEKT RED - see
`NOTICE.md`. Run this for you and your friends, not as a service.
