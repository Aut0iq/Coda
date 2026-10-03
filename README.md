<div align="center">

<img src="docs/banner.svg" alt="Coda — your own music server, set up from your phone" width="100%">

<br>

**English** · [Русский](README.ru.md)

[![server CI](https://github.com/aut0iq/Coda/actions/workflows/server.yml/badge.svg)](https://github.com/aut0iq/Coda/actions/workflows/server.yml)
![Android](https://img.shields.io/badge/platform-Android-3DDC84?logo=android&logoColor=white)
![Expo SDK 53](https://img.shields.io/badge/Expo-SDK%2053-000020?logo=expo&logoColor=white)
![Docker](https://img.shields.io/badge/server-Docker-2496ED?logo=docker&logoColor=white)
![Navidrome](https://img.shields.io/badge/works%20with-Navidrome-1d3b6e)
[![vici](https://img.shields.io/badge/pairs%20with-vici-e0b457)](https://github.com/aut0iq/vici)
![status](https://img.shields.io/badge/status-beta-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

**Coda** is an Android app that turns a bare Linux server into a personal music downloader for
[Navidrome](https://www.navidrome.org/). Enter the server's IP, login and password — the app connects over SSH,
installs Docker, brings everything up, and from then on you search an artist, tap an album and the tracks land
in your Navidrome library with proper tags and covers.

> **Pairs with [vici](https://github.com/aut0iq/vici).** Coda fills your Navidrome library; **vici** is the
> music player to listen to it (it works with Navidrome and any Subsonic API server). Use them together:
> Coda downloads, vici plays — and since both speak the Subsonic API, any other Subsonic client works too.

<div align="center">
<img src="docs/screens.png" alt="Coda screenshots: choosing where the music goes, installing over SSH, transfers to Navidrome, player credentials" width="100%">
</div>

> The app and its mini-app are currently in **Russian only**.

## Highlights

- **Set up from the phone** — IP, login, password. No terminal: Docker, containers, HTTPS certificate, the lot.
- **One server or two** — Navidrome next to the downloader, **or** on a different server: the downloader
  logs in to the Navidrome server over SSH on its own and drops the files into the folder you choose there.
- **You pick the folder** where the music is stored, in both layouts.
- **Smart downloading** — search through Deezer, audio via yt-dlp (YouTube → SoundCloud → cookies for 18+ →
  your proxies), exact matching by duration/title, tags and covers, skip duplicates and live versions
  (checked against your Navidrome), playlists per artist, a persistent queue that survives restarts.
- **Robust** — the install keeps running on the server if your phone sleeps or the connection drops, and the app
  reconnects; music is never lost if the Navidrome server is temporarily offline — it waits and is sent later.
- **Honest about blocks** — if the server is in a country where YouTube/Deezer/Docker registries are blocked or
  throttled (e.g. Russia), the app says so and suggests a VPN on the server or [zapret](https://github.com/bol-van/zapret).
- **Safe by default** — the SSH password is never stored, the API is protected by a token, host keys are pinned.

## How it works

<div align="center">
<img src="docs/architecture.svg" alt="Phone → download server → Navidrome server → player" width="100%">
</div>

1. **SSH, once.** The phone connects to the *download server*, uploads the installer and runs it. The installer
   puts Docker and three containers in place: `api` (the downloader), `caddy` (HTTPS), and — in the one-server
   layout — `navidrome`.
2. **HTTPS afterwards.** The app talks to the downloader's API with a token (the token was handed to the phone
   over the SSH session, never over the network). The working screen is a small web app served by the downloader.
3. **Two servers?** You also give the Navidrome server's SSH login once. The downloader generates its own key,
   places it in that server's `authorized_keys`, verifies key login, and from then on never needs the password.
   Files are sent over SFTP under a temporary name and renamed only after the size is verified, so Navidrome never
   sees half-written files; after success the local copy is deleted.
4. **Listen.** Point **[vici](https://github.com/aut0iq/vici)** (or any Subsonic client) at your Navidrome.

## Download

Grab the APK from the [**latest release**](https://github.com/aut0iq/Coda/releases/latest) — every release has a
build for each CPU architecture, plus a universal one:

| File | Choose it for |
|---|---|
| `coda-<version>-arm64-v8a.apk` | Almost every phone and tablet made after ~2017 — **start here** |
| `coda-<version>-armeabi-v7a.apk` | Older 32-bit ARM devices |
| `coda-<version>-x86_64.apk` | Android emulators on a PC, Chromebooks, x86 tablets |
| `coda-<version>-x86.apk` | Old 32-bit x86 devices and emulators |
| `coda-<version>-universal.apk` | Not sure which one? Works everywhere, ~2.5× bigger |

Open the file on the device and allow “install unknown apps” for the app you opened it from. `SHA256SUMS.txt` is
attached to verify the download. The APKs are signed with the standard Android debug key — fine for sideloading,
not for Google Play.

## Quick start

1. Install the APK (see above) — or build it yourself (see below).
2. Open **Coda**, tap *Connect server*, enter the IP / login / password of a fresh Linux server
   (tested on Ubuntu 24.04, other Debian-family systems should work; root or a user with `sudo`) and tap *Check server*.
3. Choose the layout:
   - leave **“Navidrome on this server”** ticked — Navidrome is installed next to the downloader
     (optionally set your own music folder), **or**
   - untick it and enter the Navidrome server's address, SSH login/password, the library folder on it, and
     Navidrome's own address and login.
4. Tap *Install*. When it says *Done*, open the server, search an artist, pick an album.
5. In the app menu you'll find the Navidrome address and credentials to enter into **vici**.

**Navidrome server requirements (two-server layout):** Navidrome is already running; SSH with password login
(at least during setup) and SFTP enabled (no shell needed); the SSH user can write to the folder that Navidrome
uses as its library. Installing Navidrome on that server from the app is not implemented yet.

## Build from source

Needs Node 22, JDK 17, the Android SDK (`ANDROID_HOME`), and [deno](https://deno.com) for the tests.

```bash
cd app
npm install                 # postinstall bundles the server package into src/bundle.generated.js
npm test                    # installer protocol, package round-trip (real bash)
npx expo prebuild --platform android --clean --no-install
cd android && ./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a
# → app/android/app/build/outputs/apk/release/app-release.apk
```

Use `x86_64` instead of `arm64-v8a` for an emulator on a PC. After changing anything in `server/`, run
`npm run bundle` in `app/` — the app uploads the copy stored in `src/bundle.generated.js`.

Server tests (no phone needed):

```bash
cd server/api && python -m unittest discover -s . -t . -p "test_*.py"     # 59 tests, incl. a real SSH/SFTP server
deno run --allow-read server/api/tests/ui_test.js                          # mini-app rendering
```

CI: `server` (tests, image to GHCR) on pushes to `server/**`; `apk` builds one APK per architecture plus a universal one
and publishes them to the release page when you push a `v*` tag (the tag must match the version in `app/package.json`).

## Repository layout

| Path | What |
|---|---|
| `app/` | Android app — Expo 53 / React Native 0.79. Native screens (servers, connect, install); the main UI is the mini-app served by your server. `modules/hub-ssh` is a small Kotlin SSH module on JSch (mwiede fork). |
| `server/api/` | The downloader: Deezer + yt-dlp + tags + dedupe + playlists, HTTP API, token auth, network-block detection, `remote.py` (transfer to another server). |
| `server/deploy/` | Installer package: `install.sh` (idempotent — also the updater), `preflight.sh`, `uninstall.sh`, compose and Caddy files for both layouts. |
| `docs/` | Banner, diagram, screenshots. |

Internal names (`music-hub`, `/opt/music-hub`, the `music-hub-api` image) come from the project's earlier name and
are kept so existing installs keep working.

## Status

Verified end-to-end on an Android emulator against clean Ubuntu 24.04 servers: install, update, uninstall,
connection loss in the middle of an install, one-server and two-server layouts (including an offline Navidrome
server and recovery), custom music folder, real downloads into Navidrome.

**Not verified yet:** HTTPS via Let's Encrypt (needs a public IP), installing Docker from scratch on a systemd
host, RHEL-family and arm64 servers, Navidrome behind a reverse proxy with a domain, the first GitHub Actions run.
In the two-server layout, requests from the downloader to Navidrome (dedupe, rescan, playlists) go straight to
Navidrome's address over HTTP(S) — not through the SSH tunnel — so that address must be reachable from the
download server.

## Security notes

- The SSH password lives only in memory during setup; the Navidrome server's password is sent through the open
  SSH session's stdin, not as a command argument and not over HTTP.
- Without HTTPS (ports 80/443 busy) the token travels in clear text; the app warns about it.
- 10 wrong tokens in 10 minutes block the address for 5 minutes.

## License

[MIT](LICENSE) © 2026 aut0iq

## Responsible use

Coda is a tool for building a personal library. You are responsible for the content you download and for
complying with the terms of the services involved and the laws that apply to you.

## Credits

[Navidrome](https://www.navidrome.org/) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [Caddy](https://caddyserver.com/) ·
[asyncssh](https://github.com/ronf/asyncssh) · [JSch (mwiede fork)](https://github.com/mwiede/jsch) ·
[Expo](https://expo.dev/) · and [**vici**](https://github.com/aut0iq/vici), the player that goes with it.
