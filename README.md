<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.png">
    <img alt="MovieClaw" src="docs/images/logo-light.png" width="360">
  </picture>
</p>

<h3 align="center">The all-in-one media server with an AI agent built in</h3>

<p align="center">
  Point MovieClaw at the folders where your media lives, and everything downstream just works:<br>
  the poster wall, series subscriptions, playback on every device.<br>
  One container replaces Jellyfin + Sonarr + Radarr + Prowlarr + Bazarr + Overseerr.<br>
  Everything stays on your own hardware.
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#features">Features</a> ·
  <a href="#boundaries">Boundaries</a> ·
  <a href="#control-it-from-anywhere">CLI</a> ·
  <a href="docs/design/">Design Docs</a> ·
  <a href="https://github.com/movieclaw/movieclaw/issues">Feedback</a>
</p>

<p align="center">
  <a href="https://github.com/movieclaw/movieclaw/releases"><img alt="Release" src="https://img.shields.io/github/v/release/movieclaw/movieclaw?label=release"></a>
  <a href="https://hub.docker.com/r/movieclaw/movieclaw"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/movieclaw/movieclaw"></a>
  <a href="https://hub.docker.com/r/movieclaw/movieclaw/tags"><img alt="Image Version" src="https://img.shields.io/docker/v/movieclaw/movieclaw/latest?label=docker%20image"></a>
  <img alt="Last Commit" src="https://img.shields.io/github/last-commit/movieclaw/movieclaw">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/movieclaw/movieclaw"></a>
</p>

<p align="center">
  <img src="docs/images/home-library.jpg" width="900" alt="MovieClaw library home page">
</p>

## Why MovieClaw

Strip everything away and there's only one thing you actually want: see a show, hit
**Subscribe**, and have it appear on your poster wall — downloaded, renamed, ready to
play — the moment a release drops. At home, that one workflow has traditionally meant six
containers: Jellyfin to serve the library, Sonarr and Radarr for subscriptions,
Prowlarr for indexers, Bazarr for subtitles, Overseerr so the family can request things —
and it's on you to keep all six agreeing about the same library.

MovieClaw collapses the whole pipeline into one container:

| The job | The usual stack | MovieClaw |
| --- | --- | --- |
| Playback, poster wall, watch progress | Jellyfin / Emby / Plex | Built in |
| Metadata | Whatever the server ships, plus tinyMediaManager for the hard cases | Built in, with TMDB and Douban as dual sources |
| Subscriptions and automatic downloads | Sonarr + Radarr | Built in |
| Indexers and tracker sites | Prowlarr / Jackett | Built in, 23 private trackers preconfigured |
| Subtitles | Bazarr | Built in, including PGS-to-SRT conversion |
| Family requests and permissions | Overseerr / Jellyseerr | Built-in member management |
| Running all of the above from a chat message | Nothing does this | Built-in AI assistant |
| Total | 6 containers, 6 configs | 1 container, 1 `data` directory |

All-in-one has its trade-offs — they're laid out honestly in [Boundaries](#boundaries).

## Screenshots

Four screens, in the order you'll actually use them: land on the home page, browse the
library, open a title, subscribe. Every screenshot is from a live instance, with real
metadata scraped from TMDB.

<table>
  <tr>
    <td width="50%"><img src="docs/images/library-movies.jpg" alt="Movie library"></td>
    <td width="50%"><img src="docs/images/series-detail.jpg" alt="Series detail"></td>
  </tr>
  <tr>
    <td><b>Library</b>: localized titles, posters, and years, with an A–Z index down the right edge for fast jumps</td>
    <td><b>Series detail</b>: episode stills, audio and subtitle track listings — missing episodes are greyed out, so gaps are obvious at a glance</td>
  </tr>
  <tr>
    <td><img src="docs/images/discover.jpg" alt="Discover page"></td>
    <td><img src="docs/images/subscriptions.jpg" alt="Subscriptions"></td>
  </tr>
  <tr>
    <td><b>Discover</b>: flip between TMDB and Douban charts — anything you see, you can subscribe to on the spot</td>
    <td><b>Subscriptions</b>: delivery progress for every show, plus a forecast of what may land over the next seven days</td>
  </tr>
</table>

The interface is liquid glass: the sidebar, inputs, and floating buttons refract whatever
background image you choose, with a touch of chromatic aberration at the edges. Swap the
background under **Settings → Appearance** and the refraction follows — consistently, on
every device connected to the same instance.

<p align="center">
  <img src="docs/images/glass.jpg" width="900" alt="Workbench with liquid glass panels refracting the background image">
</p>

Your phone gets the same interface, not a cut-down companion app. On iOS,
**Add to Home Screen** runs MovieClaw as a standalone app — no address bar, and the layout
respects the notch and home-indicator safe areas.

<p align="center">
  <img src="docs/images/mobile.jpg" width="620" alt="Library and series detail on a phone">
</p>

## Features

### Subscriptions & downloads

- Hit **Subscribe** on a show and forget about it: when a release appears, it's downloaded, renamed, and filed into your library automatically.
- Quality upgrades: describe your target quality as a rule and MovieClaw swaps in better releases as they appear. Replaced files go to a delayed-deletion recycle queue — and if you'd rather keep the 1080p REMUX *and* the 4K, turn on side-by-side versions.
- Torrent names aren't guessed at with regexes. Resolution, source, codec, subtitles, audio, and release group each get parsed into their own field:

  ```text
  三体.Three-Body.2023.S01E05.2160p.WEB-DL.H265.AAC.国语中字-OurTV
  ↓
  Series · Season 1 Episode 5 · 2160p · H.265 · WEB-DL · AAC
  Subtitles: Chinese    Audio: Mandarin    Release group: OurTV
  ```

  Even shorthand like "国语中字" (Mandarin audio, Chinese subs), which only exists on Chinese trackers, splits cleanly into separate subtitle and audio fields — courtesy of a small ML model shipped in the image, not a pile of regexes.

- Just joined a private tracker and need to protect your ratio? Turn on site protection: subscriptions steer around that site while manual search still works, so you can build up your ratio before opening it up.

### Media library

- You open the app to a poster wall. Synopses, ratings, cast, and episode stills are all stored locally — browse it all completely offline.
- It never demands renaming: point it at your existing directories and it works with them as-is. Letting MovieClaw organize your files is a separate opt-in, off by default — it doesn't touch your disks until you say so.
- When it isn't sure, it doesn't guess. Ambiguous items land in a *pending identification* queue with a plain-language reason — "3 equally plausible matches; not choosing for you" — and one confirmation resolves the whole group.
- Scraping is a matter of taste, so it's configurable: language, artwork, naming templates, NFO files, episode stills — all under **Settings → Scraping & Organizing**. Don't like an auto-picked poster? Replace it and lock it in.

### Playback

- Add the server address to Infuse and you're in: MovieClaw looks exactly like a Jellyfin server to third-party players, and watch progress syncs back.
- Or play straight in the browser — audio tracks, subtitles, and resume positions all there. Direct play whenever possible; transcoding only kicks in for codecs the browser can't handle, and it tells you the cost first.
- No hardware transcoding on your box? Hand it to a Mac: the remote transcode worker is a menu-bar app for Apple Silicon that encodes through VideoToolbox.
- On the phone it's the same UI; on iOS, **Add to Home Screen** makes it a standalone app with no address bar.

### Family & permissions

- Everyone gets their own account, with per-person switches for which libraries they see, whether they can subscribe, and whether they can download directly. Watch progress and favorites are kept separate.

### Maintenance

- Updates and rollbacks are buttons in the web UI. Routine upgrades download only a few megabytes, a broken update rolls itself back, and your data is never touched.
- Errors are written for humans — for the person who deploys things but doesn't write code. (Log and UI messages are currently in Chinese.)

### AI assistant

The assistant needs an LLM connected under **Settings → AI Models** (any OpenAI-compatible
endpoint). Skip it and everything else still works.

- Talk to your library from WeChat — "subscribe to Three-Body season 2 as soon as it's out" — by text or voice. Telegram and Discord work too.
- Under the hood, the assistant drives MovieClaw's own CLI rather than guessing at APIs. Ship a new backend endpoint and the assistant gains that ability automatically; long conversations compact their own context. The same CLI is [yours to install](#control-it-from-anywhere) — on any machine, for any agent.
- Missing subtitles? It makes its own: when none exist in your target language, it finds subtitles in another language, translates them, and saves the result as an external SRT next to the video.

What the assistant is and isn't allowed to do is covered next.

## Boundaries

### What it won't do

- Downloading stays with qBittorrent or Transmission — MovieClaw doesn't replace your download client.
- Hardware transcoding either exists on your machine or it doesn't; MovieClaw can't invent it. Software transcoding eats CPU, and the UI says so plainly before you turn it on.
- Remote access is yours to arrange (Tailscale, WireGuard, a reverse proxy). MovieClaw never touches your traffic.
- It ships no media content, ever. Tracker accounts are your own; the bundled site configs just save you setup time.

### What stays yours

- Your file names and directory layout are never touched unless you explicitly turn on organizing — and you can hand the library back to Jellyfin or Emby at any time.
- Runtime state lives in a single `data/` directory. Back that up, delete the container: nothing is lost.
- No telemetry, no phoning home, no cloud account. Your watch history never leaves the machine.

### Guardrails on the AI assistant

A media library, a file organizer, and an assistant that can execute commands, all in one
product — the risk is real. The self-hosting community already has horror stories of
auto-organizers wiping libraries: "everything deleted, not one source file left." So the
constraints here are enforced in code, not politely requested in a prompt:

- Credentials never reach bash. The assistant operates the product only through dedicated tools; no token appears in the environment of any `bash` subprocess.
- Dangerous operations require explicit confirmation. Deleting media files goes further still: the assistant must first list the exact items with a read-only command, read them back to you, and get your explicit yes in that same exchange. A hand-wavy "clean things up" is not consent.
- Deletion means delayed recycling, not an instant `rm`. Copies that are still seeding are left alone.
- Every tool call is visible in the conversation and traceable after the fact — if something goes wrong, you can see exactly which step did it.

## Quick Start

One prerequisite: Docker (Synology's built-in Container Manager counts; other NAS brands
have their own Docker packages). The official image
[`movieclaw/movieclaw`](https://hub.docker.com/r/movieclaw/movieclaw) runs everything in
a single container — no separate database, no Redis, and a TMDB key is already baked in,
so there's nothing to apply for. One tag covers both x86_64 and ARM64.

**Step 1**: Create a new folder, create a `docker-compose.yml` inside it, and paste:

```yaml
services:
  movieclaw:
    image: movieclaw/movieclaw:latest
    container_name: movieclaw
    init: true
    ports:
      # Left side is the host port — if it's taken, change the left side
      # (e.g. "8096:3000") and keep the right side at 3000
      - "3000:3000"
    volumes:
      - ./data:/app/data              # Runtime data — backing up this folder is all you need
                                      # (includes the hidden file .secret_key; make sure your
                                      # backup tool doesn't skip dotfiles)
      - /volume1/media:/media         # ← change to your media directory
      - /volume1/downloads:/downloads # ← change to your download client's save directory
      # Multiple media disks / download directories? Add one line each — no limit:
      # - /volume2/movies:/movies
    environment:
      - TZ=Asia/Shanghai              # ← change to your timezone
    # Want hardware transcoding with an iGPU / dGPU? First run `ls /dev/dri` on the host
    # to confirm it exists (ARM boxes and CPU-only hosts usually don't have it). Enable
    # the two lines below without it, and the container gets recreated and then fails to
    # start, leaving nothing but "no such file or directory". When in doubt, leave them
    # alone: the first-start log will tell you outright whether hardware decoding is
    # available and what's missing.
    # devices:
    #   - /dev/dri:/dev/dri
    restart: unless-stopped
```

**Step 2**: Point the `volumes` entries at real paths on your machine. There's one rule
to remember: **left of the colon** is a directory on your machine; **right of the colon**
is what MovieClaw sees inside the container — and it's the right-hand path you'll type
into the web UI later. Do mount your download client's save directory, or MovieClaw can't
see finished downloads and can't file them into your library.

> **The left-hand directory must already exist on the machine.** Docker doesn't error on
> a bad path — it quietly creates an empty folder for you, the container starts fine, the
> logs look clean, and your library sits empty. Synology users, watch the volume number
> and the capitalization (`/volume1` vs. `/volume2`, `media` vs. `Media`). `ls` the
> directory before you paste, or double-check it in your file manager.

**Step 3**: From that folder, bring it up (NAS GUI users: Container Manager →
**Project → Create**, pointed at the folder):

```bash
docker compose up -d
```

First start takes about ten seconds on a fast machine, a minute or two on a slower NAS.
Meanwhile the page just shows "connecting to the service…" — that's normal. You'll know
it's actually up when `docker logs movieclaw` prints `前端反代 已就绪` ("frontend proxy
ready"). If the
command instead fails with
`failed to bind host port 0.0.0.0:3000/tcp: address already in use`, port 3000 is
taken — see the [FAQ](#faq).

**Step 4**: Open `http://<host-ip>:3000`, follow the wizard to create the admin account,
then:

1. **Add a library**: **Library → Add Library**. The root path is the **in-container**
   path — the right-hand side of the colon from Step 2: `/media` in the example above,
   **not** `/volume1/media`. Scanning starts immediately; existing files get identified
   and scraped, and anything ambiguous waits in *pending identification* for your
   confirmation.
   Enter a wrong path and the page still says "scanning" — but it finishes with 0 files
   and the results read "root path does not exist, skipped". That line means the path is
   wrong.
2. **Connect your download client**: **Settings → Download Clients**, for qBittorrent /
   Transmission. If the client and MovieClaw see different paths, set up the path mapping
   here.
3. **Connect your sites**: **Settings → Resource Sites** — paste cookies / API keys, or
   let the browser extension sync them automatically.
4. Optional: **Settings → AI Models** to hook up an LLM and unlock the assistant;
   **Settings → Watched Import** to add "source directory → target library" rules, so
   downloads from any source flow into the library too.

> Just want one command to try it out?
> `docker run -d --name movieclaw --init -p 3000:3000 --restart unless-stopped -e TZ=Asia/Shanghai -v "$(pwd)/data:/app/data" -v /volume1/media:/media -v /volume1/downloads:/downloads movieclaw/movieclaw:latest`
> The mount rules are exactly the same as above.

### Everyday upgrades skip the image pull

From then on, day-to-day upgrades happen in **Settings → App → Version & Updates**:
MovieClaw pulls a few-megabyte artifact package from GitHub Releases (mirrors
configurable), applies it on the `data` volume, and the result survives container
recreation. If an update misbehaves, roll back on the same page — and a genuinely broken
one rolls itself back automatically.

`docker compose pull && docker compose up -d` is only needed when the release notes
explicitly say an update carries dependency changes and requires a new image. That's rare.
(How it works: [in-app-update.md](docs/design/in-app-update.md).)

## Bring Your Own Player

MovieClaw speaks Jellyfin's playback API, so third-party players connect to it **as if it
were a Jellyfin server** — just add the address. Support by client below; "verified on
device" means someone actually connected and played on real hardware:

| Client | Status | Notes |
| --- | --- | --- |
| Web player | Built in | Direct play first; transcodes only what the browser can't handle, and tells you the cost before it starts |
| Infuse / VidHub | **Verified on device** | Connects as a Jellyfin server: browsing, direct play, progress sync — zero changes on the player side |
| Fileball / SenPlayer | Same API | Rides the same Jellyfin-compatible path, but hasn't been individually verified on device |
| Emby / Jellyfin official apps | Not applicable | They connect to their own servers; MovieClaw can notify an Emby/Jellyfin instance to refresh after imports |
| LAN auto-discovery | Partial | Broadcasts can't reach the container on a bridged network; needs host networking or a manually entered address |
| Remote hardware transcoding | macOS Apple Silicon | Menu-bar app encoding through VideoToolbox (an always-on Mac mini is plenty). The protocol is open — other platforms can implement it |

Details in [jellyfin-compat.md](docs/design/jellyfin-compat.md),
[web-player.md](docs/design/web-player.md), and
[remote-transcode.md](docs/design/remote-transcode.md).

## Control It from Anywhere

`mclaw`, MovieClaw's command-line client, is a single static binary — no Python, no Node,
no package manager. Install it on any machine, and that machine can drive your library:
search titles, create subscriptions, monitor jobs, organize files.

**On the server itself there's nothing to install** — the image already ships it:

```sh
docker exec -it movieclaw mclaw status   # container name as in your compose file
```

**Everywhere else, it's one command.** The installer detects OS and architecture on its
own (x86 and ARM, on both Linux and macOS), verifies checksums, and installs into the
default PATH — so cron, systemd, and Dock-launched apps can find it too:

```sh
curl -fsSL https://raw.githubusercontent.com/movieclaw/movieclaw/main/scripts/install-cli.sh | sh
```

<details>
<summary><b>On Windows, use this instead</b></summary>

PowerShell won't pipe into `sh`, so Windows gets its own one-liner — it installs the same
thing (amd64 and ARM64 both covered):

```powershell
irm https://raw.githubusercontent.com/movieclaw/movieclaw/main/scripts/install-cli.ps1 | iex
```

</details>

Then run `mclaw login` to pair. With no arguments it scans the local network first;
across subnets or over a VPN, pass the address yourself
(`mclaw login --server http://192.168.1.10:3000`). The command displays a pairing code —
verify and approve it in the web UI under **Settings → Members & Devices → Devices**. The
token is handed straight back to the process, never printed, so it never ends up in your
clipboard or shell history.

Where nobody can click "approve" in a browser (NAS cron jobs, CI, headless containers),
use **Create token manually** on the same Devices page and inject the two lines it gives
you — `MOVIECLAW_SERVER` and `MOVIECLAW_TOKEN` — as environment variables. Credentials
never touch disk.

### Built for agents, too

The command tree is generated from the server's OpenAPI spec: add an endpoint to the
backend and the CLI picks up the matching command automatically — no guesswork. Outside a terminal
(pipes, agents), it emits JSON by default, so there are no human-formatted tables to
parse; destructive operations demand an explicit `--yes`. Any assistant that can run a
shell can drive MovieClaw with it — the built-in AI assistant goes through exactly this
path.

```sh
mclaw status                           # server and auth status
mclaw search titles "Three-Body"       # search titles
mclaw subscriptions list               # list subscriptions
mclaw jobs list                        # list background jobs
mclaw library organize-files 1 --yes   # organize existing file names in library 1 per the naming template
mclaw subscriptions --help             # every domain has --help, listing its commands and flags
```

Before handing a token to an external agent, read the
[assistant guardrails](#boundaries): a token carries the same privileges as the person
who approved it, and revoking one is a single click on the Devices page.

## FAQ

<details>
<summary><b>I forgot the admin password</b></summary>

Run one command on the machine hosting MovieClaw. **Nothing else is touched** — sites,
download clients, libraries, and subscriptions all stay put; only the password changes:

```bash
# Docker deployment (container name as in your compose file)
docker exec -it movieclaw python -m movieclaw_api.reset_password

# Source deployment: cd to the project root first (the parent of data/)
python -m movieclaw_api.reset_password
```

Type the new password twice at the prompt; no restart needed. Forgot the username as
well? Add `--show` to see it first. To force other signed-in devices out too, follow up
with `docker restart movieclaw`.

Why there's no "forgot password" link in the web UI: with self-hosting, there's no
trusted third party to vouch that you own the account, and real email recovery would
force every deployer to set up SMTP first. Instead, ownership is proven by something
stronger: **whoever can reach this machine's `data/` directory owns the server.**
Jellyfin, Vaultwarden, and Gitea make the same call.

Family **members** who forget their passwords don't need any of this — an admin resets
them with one click under **Settings → Members**.
</details>

<details>
<summary><b>Port 3000 is taken by another service</b></summary>

Change the **left** side of the `ports` colon — e.g. `"8096:3000"` — and browse to
`http://<host-ip>:8096`. The container-side port stays as it is.

Only under `--network host` does the container port become the host port, and only then
do you actually change the listening port: add `-e MOVIECLAW_WEB_PORT=8096`, or change it
after setup under **Settings → App → Network & Maintenance** (restarts itself on save).

**Host networking has a further trap**: the container's internal frontend (3001) and
backend (8000) also bind directly on the host. They're not currently configurable, and
`MOVIECLAW_WEB_PORT` doesn't reach them. If either port is taken, the container exits at
startup with `EADDRINUSE: address already in use` in `docker logs`. Host mode also
listens on UDP 7359 for Jellyfin LAN discovery, which collides with an existing Jellyfin
or Emby.

**Bottom line: unless you specifically need LAN auto-discovery, change the left side of
the colon and stay off host networking.**
</details>

<details>
<summary><b>Scraping keeps failing; the logs say TMDB is unreachable</b></summary>

`无法连通 TMDB` ("cannot reach TMDB"), `ConnectTimeout`, `CircuitOpenError`, or
`CERTIFICATE_VERIFY_FAILED` — whether in the logs or in the connectivity test under
**Settings → Network & Proxy** — all point to the same problem.

If your network can't reach `api.themoviedb.org` directly, set a proxy or mirror address
under **Settings → Network & Proxy** and confirm with the built-in connectivity test. By
default the proxy covers TMDB, artwork fetches, and GitHub updates, while tracker sites
stay on direct connections (usually faster that way).
</details>

<details>
<summary><b>What user does the container run as? Is PUID / PGID supported?</b></summary>

**It runs as root; PUID / PGID aren't supported yet.** The database and key files under
`data/` are owned by `root:root`.

Mounted media directories **must be writable** — filing imports and recycling replaced
versions both move files, and a read-only mount breaks those features. If the directories
belong to another user on your NAS, root can generally still write there; the friction
runs the other way — your own account may lack permissions on directories MovieClaw
creates. If that matters to you, loosen the directory permissions on the host before
mounting.
</details>

<details>
<summary><b>My library lives on an SMB / NFS network mount</b></summary>

Turn off *real-time file watching* when you create the library. File events over network
mounts are unreliable; periodic reconciliation plus a manual scan is far more
dependable.
</details>

## Building from Source

You'll need a free TMDB API key from
[themoviedb.org](https://www.themoviedb.org/settings/api) — the official image has one
baked in; only self-builds need their own.

```bash
git clone https://github.com/movieclaw/movieclaw.git
cd movieclaw
TMDB_API_KEY=your_key ./scripts/build-image.sh
#   Mirror acceleration for mainland China:  CN_MIRROR=1 TMDB_API_KEY=... ./scripts/build-image.sh
#   Cross-building for a NAS:                PLATFORM=linux/amd64 TMDB_API_KEY=... ./scripts/build-image.sh
```

The key can also live in a `.env` at the repo root. Note that `.env.example` ships the
line **commented out** (`# TMDB_API_KEY=`) — drop the `#` or the script won't see it.
With no key at all, the script fails fast instead of wasting your time on a build.

The build reaches out to `deb.debian.org`, `repo.jellyfin.org`, `pypi.org`,
`registry.npmjs.org`, and GitHub. On mainland-China networks, add `CN_MIRROR=1`. Behind
a corporate proxy that intercepts and re-signs TLS, the build dies at
`curl ... exit code 60` or pip's `CERTIFICATE_VERIFY_FAILED` — that's a certificate-trust
problem, not a script bug.

Then point the `image:` line in `docker-compose.yml` at `movieclaw:latest` and start.
As a self-check, the build generates PGS test samples and OCRs them back to SRT, failing
hard on any mismatch. The subtitle runtime and its release gates are described in
[docker-subtitle-runtime.md](docs/design/docker-subtitle-runtime.md).

## Local Development

**First, free up ports 3000 and 8000.** If this machine also runs the MovieClaw Docker
container, `docker compose down` it first — "used the image, now wants to hack on the
code" is the single most common contributor story, and the collision is guaranteed. When
a port is taken, the dev script fails immediately and tells you how to find the culprit
(requires `lsof`).

```bash
./scripts/dev.sh          # start backend and frontend together
./scripts/dev.sh api      # backend only
./scripts/dev.sh web      # frontend only
```

The script handles first-run setup on its own (virtualenv, dependencies, `.env`,
`pnpm install`). Logs carry colored `[api]` / `[web]` prefixes, and `Ctrl-C` tears
everything down cleanly — child processes included, ports freed. Note that it picks the
newest Python it can find for the virtualenv (3.14 → 3.11), which may not be the one
your `python3` points at.

Going manual takes **two terminals** — backend and frontend are both foreground
processes:

```bash
# Terminal 1: backend (Python 3.11+)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn movieclaw_api.main:app --factory --reload --reload-dir src
```

> Keep `--reload-dir src`. By default uvicorn watches the whole working directory, and
> runtime logs land in `data/logs/` — so every log write triggers reload detection, the
> detection writes another log line, and the loop floods the log until it's unreadable.
> `./scripts/dev.sh` pins this for you.

```bash
# Terminal 2: frontend (Node.js 20+)
pnpm install && pnpm web:dev
```

Web console at `http://127.0.0.1:3000`, API docs at `http://127.0.0.1:8000/docs`. A
blank `.env` boots fine — you just lose the TMDB-dependent features.

> Changing the backend port means changing it in two places. `APP_PORT` in `.env` moves
> the backend, but the frontend's proxy target is hardcoded to default to
> `http://127.0.0.1:8000` — you **must also** set `MOVIECLAW_API_PROXY_TARGET` in
> `apps/web/.env.local` to the new port. Change only one, and the page loads while every
> API call comes back empty. The frontend's own 3000 is hardcoded and not configurable.
> And on the manual path, a uvicorn port collision says only
> `[Errno 98] Address already in use`, without `dev.sh`'s friendlier hint.

When running from source, **the NER model behind torrent-name parsing needs placing by
hand** (the Docker image includes it): download `model.int8.onnx`, `tokenizer.json`, and
`labels.json` from [Releases](https://github.com/movieclaw/movieclaw/releases), drop them
into `data/models/torrent-ner/` (path configurable via `MOVIECLAW_NER_DIR`), and restart.
Without the model, the app runs fine — that one feature just stays off. The first time
extraction is actually triggered, the log notes that the model is missing and that
title/year/season/episode fields will stay empty. But that warning is emitted **lazily**:
it never appears at startup, so a clean boot log is not proof the model is in place.

## Docs & Support

The major design decisions behind every module — and the trade-offs that shaped them —
live in [`docs/design/`](docs/design/), one file per topic: library, metadata, subscriptions,
quality upgrades, Jellyfin compatibility, in-app updates, the CLI… browse by filename.
The reasoning behind this README's own structure is in
[readme-rewrite.md](docs/design/readme-rewrite.md).

Questions, ideas, bug reports — please
[open an issue](https://github.com/movieclaw/movieclaw/issues).

## License

[MIT](LICENSE)
