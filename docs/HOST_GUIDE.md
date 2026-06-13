# Hosting & Playing - Gwent Beta Restoration

A community preservation project for **Gwent 0.9.24.3.432** - the 2017/2018 open beta,
a version of Gwent very different from the current game and no longer available
anywhere. This is a non-commercial, fan-run effort to keep that version
playable. You need a **legal copy of Gwent 0.9.24.3.432** already installed - the
game itself is not distributed with this project.

There are two ways to play:

- **Join the community test server** - the easiest way to try it. The released
  launcher comes pre-pointed at the community server, so for most people it's:
  open the launcher, leave the host checkbox unticked, create an account, click play.
  The test server is provided as-is and may go down at any time - 
  it exists so people can try this version and, if they like it, run their own. 
  The url is gwentbetaunofficialserver.duckdns.org
- **Host your own** - run a server on your own PC or remote server for LAN or cross-network play. 

---

## Quick start (host a game on your PC for LAN)

1. Run **GwentServerHost.exe**.
2. In the window: confirm your Gwent folder (Browse if needed), note the
   **Server IP** shown, and click **Start Server**. It minimizes once running.
3. Open the **launcher** (GwentBetaLauncher), tick **"I'm hosting the server on
   this PC"** or enter 127.0.0.1, create an account, and play.
4. To let a friend join, share the **Server IP** the host window shows. They
   enter it in their launcher's **Server Address** field (and do NOT tick the
   host checkbox).

That's it for same-network play. For internet play, see "Playing over the
internet" below.

---

## Networking: who can reach your server?

Your server listens on your PC. Whether a friend can reach it depends on where
they are:

### Same network (same house / Wi-Fi)
Works out of the box. The host shares their **local IP** (e.g. `192.168.1.40`),
the friend types it in their launcher's Server Address field. The host's
firewall rules are added automatically by GwentServerHost. This is the simplest
way to play and needs no extra setup.

### Hosting over the internet
If you want friends on other networks to connect to a server on your own PC,
you'll need to **forward ports** on your router: forward TCP **443, 7777, 8445,
and 8447** to the host PC's local IP, then share your public IP. Note this
depends on your router and ISP (some ISPs use CGNAT, where port-forwarding isn't
possible). Same-network play above avoids all of this.

Otherwise, you can also setup a remote server, using a service like Oracle Free Tier, this is the most robust way to host, and the most complicated. Look in the docs folder for how to do this, it is what the test server uses.

---

## Everyone must use the SAME card data_definitions

The server and every connected player must share **identical**
`data_definitions` (the game's card data). If the server was set up from a
**modded** game (extra/changed cards) but a player runs a **vanilla** game (or a
different mod), cards won't line up:

- Decks/collections show **scrambled** cards (IDs shifted by the mod), or
- The client **fails to add** cards/decks it doesn't recognize.

For a clean multiplayer experience: the host should set up the server from a
**vanilla** 0.9.24.3 install, and all players should run **vanilla** too - or
everyone uses the *exact same* mod. This project's setup extracts definitions
from whatever client the host points at, so point it at the version you want
everyone to share.

If you want to modify cards, that is very possible to play with online too, just make sure both players have the same modded files, whether a dll melonloader mod, or a data_definitions file.

## Troubleshooting

**Client says "connection refused" / errno 111 / WinError 10061**
- Confirm the host's GwentServerHost is running and you entered the host's
  correct IP (not `127.0.0.1`, unless you ARE the host).
- Make sure both machines are on the same network (or that ports are forwarded
  if hosting over the internet).
- Quick reachability test from the client machine:
  `curl -k https://<host-ip>/` - a response (even a 404) means the host is
  reachable; "connection refused" means it isn't (wrong IP, not on the same
  network, or ports not reachable).