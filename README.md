# Unofficial Gwent Beta Restoration

A project to restore offline and online functionality to Gwent's Beta version, currently focused on v0.9.24.3.432.

The code reroutes the calls the Gwent client makes through nginx and Python in order to replicate the now-defunct online functionality.

## To play
- **Windows:** simply download and run `GwentBetaLauncher.exe`.
- **Linux/Steam Deck:** simply download, extract and run `GwentBetaLauncher-Linux.tar.gz`.

See the [**Player Guide**](docs/PLAYER_GUIDE.md) for a walkthrough, and [**Features**](docs/FEATURES.md) for what's available.

## Host your own server

If you want to run your own server, [**SERVER_SETUP.md**](docs/SERVER_SETUP.md) is a step-by-step guide for hosting on a free cloud VPS, and the [**Host Guide**](docs/HOST_GUIDE.md) covers LAN and internet play.

## Building the launcher

- **Windows:** run `build_launcher.bat` (needs `pip install pyinstaller`) → `dist/GwentBetaLauncher.exe`
- **Linux / Steam Deck:** run `./build_launcher_linux.sh` (needs `pip install --user pyinstaller`) → `dist/GwentBetaLauncher-Linux`
