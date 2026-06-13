# Third-Party Components & Attribution

This project bundles the following third-party software. Each is distributed
under its own license, included alongside it.

## nginx
- License: BSD-2-Clause. See `Nginx/docs/LICENSE`.
- Bundled dependency licenses: `Nginx/docs/OpenSSL.LICENSE`,
  `Nginx/docs/PCRE.LICENCE`, `Nginx/docs/zlib.LICENSE`.
- `Nginx/nginx.exe` is the official Windows build, redistributed unmodified.

## comet (GOG Galaxy backend reimplementation)
- License: Apache-2.0. See `comet-main/LICENSE`.
- We include and build comet's `dummy-service`. Modified from upstream:
  `communication_wine.c` and the meson build (Apache-2.0 sec. 4(b)).

## Game data (CD PROJEKT RED)
- `rewards.json`, and related game content are property of CD PROJEKT RED and are NOT covered by this project's
  license. Included only so the restoration server can function.
