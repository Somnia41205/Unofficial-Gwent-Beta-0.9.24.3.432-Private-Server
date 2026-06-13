# Playing on a Private Server

A quick guide to using the **Gwent Beta Restoration** launcher. If a friend
gave you the launcher and a server to play on, this, plus the game files, are all you need.

The launcher does everything for you — it sets up the connection,
finds the game, and starts it. You just pick a name, put in the server ip, and press Play.

---

## What you need to play
- The game files:
  - The complete vanilla game files: **Gwent v0.9.24.3.432**
  
- The launcher file:
  - **Windows:** `GwentBetaLauncher.exe`
  - **Linux / Steam Deck:** `GwentBetaLauncher-Linux.tar.gz`

---

## First time

### Windows

1. **Right-click `GwentBetaLauncher.exe` → Run as administrator.**
   (It needs admin once to set up the connection and a small background
   service. Windows may also prompt to install a Visual C++ component — let it.)
2. **Game folder:** Click **Browse** and point it at the folder containing `Gwent.exe`.
3. **Create an account:** on the **Create account** tab, type the username you
   want to play as. (Leave "Start with full collection…" checked unless you'd
   rather start from scratch.)
4. Click **Install & Play.**

That's it. The launcher configures everything, starts Gwent, and you'll land in
the game signed in as your new account.

### Linux / Steam Deck

1. On a Steam Deck, switch to **Desktop Mode** first.
2. Extract the archive and run it (no `chmod` needed — the archive keeps the
   file runnable):
   ```bash
   tar -xf GwentBetaLauncher-Linux.tar.gz
   ./GwentBetaLauncher-Linux
   ```
3. It will ask for your password (it needs it to set up the connection). The
   first run also installs wine if it's missing, so give it a few minutes.
4. From here it's the same as Windows: choose a game folder, create your account, enter the server url/ip, and press **Install & Play.**

---

## After the first time

Just open the launcher and press **PLAY**. It remembers your account and your
game install.

If you ever reinstall your PC or move to another computer, use the **Sign in**
tab and enter your **User ID** (see below) to get your account back on the server, but given you can start with all the cards and trinkets, easier just to make a new account.

---

## Playing

Create, or select a deck, maybe change your avatar and border, click start matchmaking and play.

(For the full list of what's available and what isn't, see `FEATURES.md`.)

---

## If something goes wrong

**"Run as administrator" / it won't set things up (Windows).**
Close it and start it again with right-click → **Run as administrator**. The
first-time setup can't work without it.

**The game won't connect / you get signed out.**
Close the game and run the launcher again - it reinstalls the connection setup
each time it launches, which fixes most connection issues. Make sure the host's
server is actually up.

**It can't find the game.**
Click **Browse** and point it at the folder that contains `Gwent.exe`, or leave
the folder blank to let the launcher download a fresh copy.