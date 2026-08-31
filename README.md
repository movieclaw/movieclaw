<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.png">
    <img alt="MovieClaw" src="docs/images/logo-light.png" width="360">
  </picture>
</p>

<h3 align="center">A new generation of intelligent media server — your best movie & TV agent</h3>

<p align="center">
  Point MovieClaw at the folders where you keep your media, and get a poster wall, series subscriptions, and multi-device playback in one seamless flow.<br>
  One container replaces the six-app stack of Jellyfin + Sonarr + Radarr + Prowlarr + Bazarr + Overseerr.<br>
  All your data stays on your own machine.
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="#get-running-in-5-minutes">Quick Start</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#what-it-can-do">Features</a> ·
  <a href="#boundaries">Boundaries</a> ·
  <a href="#control-it-from-another-machine">CLI</a> ·
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

There is really only one thing you want to do: see something you like, click "Subscribe",
and have it automatically downloaded, renamed, and placed on your poster wall the moment a
release appears — ready to play. At home, this pipeline usually takes six containers:
Jellyfin to serve the media, Sonarr and Radarr for subscriptions, Prowlarr for indexers,
Bazarr for subtitles, Overseerr for family requests — plus the ongoing work of keeping them
all in agreement about the same library.

MovieClaw folds that whole pipeline into a single container:

| What you want | The usual way | MovieClaw |
| --- | --- | --- |
| Playback, poster wall, progress sync | Jellyfin / Emby / Plex | Built in |
| Metadata scraping | Bundled with the above, plus tinyMediaManager for hard cases | Built in, dual-source TMDB + Douban |
| Series subscriptions, automatic downloads | Sonarr + Radarr | Built in |
| Site / indexer integration | Prowlarr / Jackett | Built in, with 23 private tracker site configs |
| Subtitles | Bazarr | Built in, including PGS bitmap subtitle → SRT conversion |
| Family requests and permissions | Overseerr / Jellyseerr | Built-in member management |
| Driving all of this with one chat message | Nothing off the shelf | Built-in AI assistant |
| Total | 6 containers / 6 configs | 1 container / 1 `data` directory |

An all-in-one design has trade-offs; the boundaries are spelled out in the
[next section](#boundaries).

## Screenshots

The four screenshots below follow the order of actual use: land on the home page, browse
the library, open a detail page, subscribe and follow a series. They are taken from a real
running instance, with metadata genuinely scraped from TMDB.

<table>
  <tr>
    <td width="50%"><img src="docs/images/library-movies.jpg" alt="Movie library"></td>
    <td width="50%"><img src="docs/images/series-detail.jpg" alt="Series detail"></td>
  </tr>
  <tr>
    <td><b>Library</b>: localized titles, posters, and years, with an alphabet index on the right for quick jumps</td>
    <td><b>Series detail</b>: episode stills, audio track and subtitle listings, with missing episodes greyed out</td>
  </tr>
  <tr>
    <td><img src="docs/images/discover.jpg" alt="Discover page"></td>
    <td><img src="docs/images/subscriptions.jpg" alt="Subscriptions"></td>
  </tr>
  <tr>
    <td><b>Discover</b>: switch between TMDB and Douban charts at any time — subscribe the moment you see something</td>
    <td><b>Subscriptions</b>: per-show delivery progress, plus what might land in your library over the next seven days</td>
  </tr>
</table>

The interface is liquid glass: the sidebar, input fields, and floating buttons refract the
background image you set, with a hint of chromatic aberration at the edges. Change the
background under "Settings → Appearance" and the refraction follows; the look stays
consistent across every device connected to the same instance.

<p align="center">
  <img src="docs/images/glass.jpg" width="900" alt="Workbench with liquid glass panels refracting the background image">
</p>

On the phone it is the same interface — not a separate stripped-down build. On iOS,
"Add to Home Screen" runs it as a standalone app: no browser address bar, and the notch
and home indicator are both accounted for.

<p align="center">
  <img src="docs/images/mobile.jpg" width="620" alt="Library and series detail on a phone">
</p>

## What It Can Do

### Subscriptions & Downloads

- Click "Subscribe" on a show you want, and the moment a release appears it is downloaded, renamed, and added to the library automatically — you do nothing.
- Quality upgrades: write your target quality as a rule and better releases replace old ones automatically, with the old files going into a delayed-deletion "recycle" queue; want to keep both 1080p REMUX and 4K? Turn on "keep old versions side by side".
- Torrent names are not brute-force guessed with regex — resolution, source, codec, subtitles, audio track, and release group each get their own field:

  ```text
  三体.Three-Body.2023.S01E05.2160p.WEB-DL.H265.AAC.国语中字-OurTV
  ↓
  Series · Season 1 Episode 5 · 2160p · H.265 · WEB-DL · AAC
  Subtitles: Chinese    Audio: Mandarin    Release group: OurTV
  ```

  Even Chinese-tracker-only shorthand like "国语中字" (Mandarin audio, Chinese subs) gets split into separate subtitle and audio fields — done by a small model bundled with the image, not a pile of regexes.

- Nurse a new tracker account first: turn on site protection and the subscription pipeline skips that site while manual search still works; combined with seeding tools, build up your share ratio before opening it up.

### Media Library

- Open the app to a poster wall — synopses, ratings, cast and crew, and episode stills are all stored locally, browsable even when offline.
- No file renaming is required: point it at your existing directories and it just works. Letting it organize your files is a separate switch, off by default — it doesn't touch your disks.
- When it can't identify something, it doesn't guess: unidentified items go into a "pending identification" queue with a clear explanation, e.g. "3 equally plausible candidates; the machine won't choose for you" — claim once and the whole group is resolved.
- Scraping taste is adjustable: language, posters, naming templates, whether to write NFO files and episode stills — all under "Settings → Scraping & Organizing". Don't like an auto-picked image? Swap it manually and lock it.

### Playback

- Enter one address in Infuse and you're connected — MovieClaw presents itself as a Jellyfin server, and your watch progress syncs back.
- Play directly in the browser with audio tracks, subtitles, and resume positions all in place; direct play whenever possible, transcoding only for codecs the browser can't handle — and it tells you the cost before transcoding.
- No hardware decoding? Hand transcoding to a Mac: the remote transcoding worker is a menu-bar app for Apple Silicon that uses VideoToolbox hardware encoding.
- The phone gets the same interface; on iOS, "Add to Home Screen" gives you a standalone app with no address bar.

### Family & Permissions

- One account per person, with per-item switches for which libraries they can see, whether they can subscribe, and whether they can download directly; progress and favorites are kept separately for each person.

### Maintenance

- Update and roll back with a click in the web UI. Routine upgrades download only a few MB of artifacts; a broken update falls back automatically, and your data is untouched.
- Errors speak plainly: logs and UI messages are written for people who deploy things but don't write code. (They are currently in Chinese.)

### AI Assistant

Requires connecting a large language model ("Settings → AI Models"; any OpenAI-compatible
endpoint works). Without one, everything else works normally.

- Just say it in WeChat — e.g. "subscribe to Three-Body season 2 as soon as it's out"; voice messages work too, and Telegram and Discord conversations are supported as well.
- The assistant drives MovieClaw's own command line rather than guessing at APIs; when a new endpoint lands in the backend, the assistant automatically gains that capability, and long conversations get their context compacted automatically. The same CLI is [yours to install too](#control-it-from-another-machine) — other machines and other agents can use it.
- Missing subtitles? It makes its own: when subtitles in your target language can't be found, it automatically finds a source, translates it, and writes an external SRT to disk.

Permission boundaries are covered in the next section.

<a id="boundaries"></a>

## What It Doesn't Do, and What It Never Locks In

### What it doesn't do

- Downloading is delegated to qBittorrent or Transmission — MovieClaw does not replace your download client.
- Hardware transcoding either exists on your machine or it doesn't; MovieClaw can't conjure it. Software transcoding is CPU-hungry, and the UI spells out the cost before you enable it.
- Remote access is your own setup (Tailscale, WireGuard, a reverse proxy) — MovieClaw never touches your traffic.
- It provides no media content whatsoever. Tracker accounts are your own; the bundled site configs merely make them convenient to use.

### Your data stays yours

- It never changes your file names or directory structure unless you explicitly ask it to organize — you can hand the library back to Jellyfin or Emby at any time.
- All runtime data lives in a single `data/` directory. Back it up, delete the container — your data is untouched.
- No telemetry, no phoning home, no cloud account required. Your watch history never leaves the machine.

### The AI assistant's permission boundaries

Putting a media library, file organizing, and a command-executing assistant into one
product carries real risk. People in the community have already had auto-organizing "wipe
everything — not a single source file left". So the constraints here live in code, not as
gentleman's agreements in a prompt:

- Credentials never enter bash. The assistant operates the product exclusively through dedicated tools; no token is visible in the environment variables of any `bash` subprocess.
- Dangerous operations require explicit confirmation. "Delete media files" additionally requires first listing the exact items to be deleted via a read-only command, reading them back to you, and getting your explicit consent in that same exchange. A vague "clean things up" does not count as consent.
- Deletion goes through delayed recycling, not an immediate `rm`. Copies that are still seeding are left alone.
- Every tool call is visible and traceable in the conversation — if something goes wrong, you can see exactly which step did it.

## Get Running in 5 Minutes

The only prerequisite: Docker installed on the machine (Synology users can use the
built-in Container Manager; other NAS brands have their own Docker packages).
The official image [`movieclaw/movieclaw`](https://hub.docker.com/r/movieclaw/movieclaw)
runs everything in a single container: no separate database, no Redis to configure — even
a TMDB key is built in (no need to apply for your own). The same tag supports both x86_64
and ARM64.

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
    # to confirm it exists (ARM boxes and CPU-only hosts usually don't have it). If it
    # doesn't exist and you enable the two lines below anyway, the container gets
    # recreated and then fails to start, leaving only "no such file or directory".
    # When in doubt, leave them alone: the first-start log will tell you outright
    # whether hardware decoding is available and what's missing.
    # devices:
    #   - /dev/dri:/dev/dri
    restart: unless-stopped
```

**Step 2**: Change the paths under `volumes` to real paths on your machine. There is only
one rule: the **left** side of the colon is a directory on your machine, the **right**
side is the path MovieClaw sees inside the container — and whenever you enter a path in
the web UI later, you enter the right-hand one. Be sure to mount your download client's
save directory, or MovieClaw won't be able to see finished downloads and can't organize
them into the library.

> **The left side of the colon must be a directory that already exists on the machine.**
> Docker won't error on a wrong path — it silently creates an empty folder for you, the
> container starts normally, the logs are spotless, and your library is blank.
> Synology users especially: watch the volume number and letter case (`/volume1` vs.
> `/volume2`, `media` vs. `Media`). `ls` the directory before pasting, or double-check it
> in your file manager.

**Step 3**: Start it from that folder (NAS GUI users: Container Manager → "Project → Create",
pointed at this folder):

```bash
docker compose up -d
```

First startup takes ten-odd seconds on a fast machine, and possibly a minute or two on a
slower NAS. During this time the page only shows "connecting to the service…" — that's
normal; once "前端反代 已就绪" (frontend proxy ready) appears in `docker logs movieclaw`,
it is truly up. If this step fails immediately with
`failed to bind host port 0.0.0.0:3000/tcp: address already in use`, port 3000 is taken —
see the second entry in the [FAQ](#faq) below.

**Step 4**: Open `http://<host-IP>:3000` in a browser, follow the wizard to create the
admin account, then:

1. **Create a library**: "Library → Add Library". For the root path, enter the
   **in-container path** — the right-hand side of the colon from Step 2: `/media` in the
   example above, **not** `/volume1/media`. Scanning starts as soon as the library is
   created; existing files are identified and scraped, and anything ambiguous lands in
   "pending identification" for you to confirm.
   If you enter a wrong path, the page still says "scanning", but it finishes with 0 files
   and the scan results say "root path does not exist, skipped". That message means the
   path is wrong.
2. **Connect a download client**: "Settings → Download Clients" for qBittorrent /
   Transmission; if the client and MovieClaw see different paths, configure the path
   mapping here.
3. **Connect sites**: "Settings → Resource Sites" — enter cookies / API keys, or install
   the browser extension for automatic sync.
4. Optional: "Settings → AI Models" to connect an LLM and unlock the AI assistant;
   "Settings → Watched Import" to add a "source directory → target library" rule so
   downloads from any source also flow into the library automatically.

> Just want a single command to try it?
> `docker run -d --name movieclaw --init -p 3000:3000 --restart unless-stopped -e TZ=Asia/Shanghai -v "$(pwd)/data:/app/data" -v /volume1/media:/media -v /volume1/downloads:/downloads movieclaw/movieclaw:latest`
> The mount path rules are exactly the same as above.

### Routine upgrades: no image re-pull needed

After installation, day-to-day upgrades happen directly in
"Settings → App → Version & Updates": what gets downloaded is a few-MB artifact package
from GitHub Releases (an accelerated mirror can be configured), and the update lands on
the `data` volume, surviving container recreation. If an update goes wrong you can roll
back on the same page, and a broken update is automatically rolled back to a working
version by the container.

Only when the release notes explicitly say "contains dependency changes, Docker image
upgrade required" do you need `docker compose pull && docker compose up -d`.
That is rare. (See [in-app-update.md](docs/design/in-app-update.md) for how it works.)

## Watch with Other Players

MovieClaw exposes a Jellyfin-compatible playback API — third-party players
**treat it as a Jellyfin server**: just enter the address. The table below lists the
support status per client; "verified on device" means it was actually connected and
played on real hardware:

| Client | Status | Notes |
| --- | --- | --- |
| Web player | Built in | Direct play first; transcodes only codecs the browser can't handle, and tells you the cost before transcoding |
| Infuse / VidHub | **Verified on device** | Connects as a Jellyfin server: browsing, direct play, progress sync — zero changes on the player side |
| Fileball / SenPlayer | Same API | Uses the same Jellyfin-compatible path, but not individually verified on device |
| Emby / Jellyfin official apps | Not applicable | They connect to their own servers; MovieClaw can notify Emby/Jellyfin to refresh after imports |
| LAN auto-discovery | Partial | Broadcasts can't reach the container on a bridged network; requires host networking or a manually entered address |
| Remote hardware transcoding | macOS Apple Silicon | Menu-bar app using VideoToolbox hardware encoding (an always-on Mac mini is plenty). The protocol is open; other platforms can implement it |

Details in [jellyfin-compat.md](docs/design/jellyfin-compat.md),
[web-player.md](docs/design/web-player.md), and
[remote-transcode.md](docs/design/remote-transcode.md).

## Control It from Another Machine

MovieClaw's command line, `mclaw`, is a static binary — installing it requires no Python,
Node, or package manager. Whichever machine you install it on can drive your library:
search titles, create subscriptions, watch jobs, organize files, all from commands.

**No install needed on the server itself** — the image already ships it:

```sh
docker exec -it movieclaw mclaw status   # adjust the container name to match your compose file
```

**One command installs it on any other machine.** The script detects OS and architecture
by itself (x86 and ARM covered on both Linux and macOS), verifies checksums, and installs
into the default PATH so cron, systemd, and Dock-launched apps can find it too:

```sh
curl -fsSL https://raw.githubusercontent.com/movieclaw/movieclaw/main/scripts/install-cli.sh | sh
```

<details>
<summary><b>On Windows, use this one</b></summary>

PowerShell can't pipe into `sh`, so Windows gets its own command — it installs the same
thing (both amd64 and ARM64 covered):

```powershell
irm https://raw.githubusercontent.com/movieclaw/movieclaw/main/scripts/install-cli.ps1 | iex
```

</details>

After installing, run `mclaw login` to pair: with no arguments it first scans the local
network; across subnets or over a VPN, give it the address yourself
(`mclaw login --server http://192.168.1.10:3000`). The command shows a pairing code —
verify and approve it on the web under "Settings → Members & Devices → Devices". The token
goes straight back to the process without ever being displayed, so it never touches your
clipboard or shell history.

For environments where nobody can click approve in a browser (NAS cron jobs, CI, headless
containers), use "Create token manually" on the same Devices page and inject the two lines
it gives you — `MOVIECLAW_SERVER` and `MOVIECLAW_TOKEN` — as environment variables.
The credentials never touch disk.

### Any agent can use it

The command tree is generated from the server's OpenAPI spec: add an endpoint to the
backend and the CLI automatically gains that capability — no guessing. In non-terminal
environments (pipes, agents) it outputs JSON by default, so there are no human-oriented
tables to parse; destructive operations require an explicit `--yes`. Any assistant that
can run a shell can therefore drive MovieClaw once the CLI is installed — the product's
built-in AI assistant goes through exactly the same path.

```sh
mclaw status                           # server and auth status
mclaw search titles "Three-Body"       # search titles
mclaw subscriptions list               # list subscriptions
mclaw jobs list                        # list background jobs
mclaw library organize-files 1 --yes   # organize existing file names in library 1 per the naming template
mclaw subscriptions --help             # every domain has --help, listing its commands and flags
```

Before handing it to an external agent, read
[the AI assistant's permission boundaries](#boundaries): such a token has the same
privileges as the person who approved it, and can be revoked at any time with one click on
the Devices page.

## FAQ

<details>
<summary><b>I forgot the admin password</b></summary>

Run one command on the machine running MovieClaw to reset it — **no config or data is
touched**: sites, download clients, libraries, and subscriptions are all preserved; only
the password changes:

```bash
# Docker deployment (adjust the container name to match your compose file)
docker exec -it movieclaw python -m movieclaw_api.reset_password

# Source deployment: cd to the project root first (the parent of data/)
python -m movieclaw_api.reset_password
```

Enter the new password twice at the prompt — no service restart needed. Forgot the
username too? Add `--show` to see it first. To also log out devices signed in elsewhere,
follow up with `docker restart movieclaw`.

Why there is no web-based "forgot password": in self-hosting there is no trusted third
party to prove "you own this account", and doing real email recovery would force every
deployer to configure SMTP first. Instead, identity is proven by something harder:
**whoever can access this machine's `data/` directory is the owner**. Jellyfin,
Vaultwarden, and Gitea work the same way.

A family **member** who forgot their password doesn't need this command: an admin resets
it with one click under "Settings → Members".
</details>

<details>
<summary><b>Port 3000 is taken by another service</b></summary>

Change the **left** side of the colon under `ports`, e.g. `"8096:3000"`, then access it
at `http://<host-IP>:8096`. The in-container port needs no change.

Only with `--network host` does the in-container port become the host port, and only then
do you need to actually change the listening port: add `-e MOVIECLAW_WEB_PORT=8096`, or
change it after installation under "Settings → App → Network & Maintenance" (auto-restarts
on save).

**But host networking has another pitfall**: the container's internal frontend (3001) and
backend (8000) also bind those host ports directly. They are currently not configurable —
`MOVIECLAW_WEB_PORT` doesn't cover them. If either is taken by another service, the
container fails to start and exits, with `EADDRINUSE: address already in use` in
`docker logs`. Host networking also listens on UDP 7359 (Jellyfin LAN auto-discovery),
which collides with an existing Jellyfin / Emby.

**So: unless you genuinely need LAN auto-discovery, change the left side of the colon as
above and stay off host networking.**
</details>

<details>
<summary><b>Scraping keeps failing; the logs say TMDB is unreachable</b></summary>

Messages like `无法连通 TMDB` (cannot reach TMDB), `ConnectTimeout`, `CircuitOpenError`,
or `CERTIFICATE_VERIFY_FAILED` in the logs or in the connectivity test under
"Settings → Network & Proxy" all fall into this category.

If your network can't reach `api.themoviedb.org` directly, configure a proxy or mirror
address under "Settings → Network & Proxy" and verify with the on-page connectivity test.
By default the proxy covers TMDB, image origin fetches, and GitHub updates; tracker sites
stay on direct connections (usually faster that way).
</details>

<details>
<summary><b>What user does the container run as? Is PUID / PGID supported</b></summary>

**It runs as root; PUID / PGID are not currently supported.** The database and key files
under `data/` are owned by `root:root`.

Mounted media directories **need to be writable** — organizing imports and quality-upgrade
recycling both move files; a read-only mount breaks those features. If those directories
belong to another user on your NAS, root can usually still read and write them, but going
the other way — accessing directories MovieClaw created with your own account — may hit
permission issues. If that bothers you, loosen the directory permissions on the host
before mounting.
</details>

<details>
<summary><b>My library lives on an SMB / NFS network mount</b></summary>

Turn off "real-time file watching" when creating the library. File events on network
mounts are unreliable; periodic reconciliation plus manual scans are more dependable.
</details>

## Building from Source

You first need a free TMDB API key from
[themoviedb.org](https://www.themoviedb.org/settings/api)
(the official image has one built in; only self-builds need this).

```bash
git clone https://github.com/movieclaw/movieclaw.git
cd movieclaw
TMDB_API_KEY=your_key ./scripts/build-image.sh
#   Mirror acceleration for mainland China:  CN_MIRROR=1 TMDB_API_KEY=... ./scripts/build-image.sh
#   Cross-building for a NAS:                PLATFORM=linux/amd64 TMDB_API_KEY=... ./scripts/build-image.sh
```

The key can also go into a `.env` at the repository root. Note that the
`# TMDB_API_KEY=` line in `.env.example` is **commented out** — remove the `#` before
filling it in, or the script won't see it. If no key is provided, the script errors out
immediately rather than wasting time starting the build.

The build needs outbound access to `deb.debian.org`, `repo.jellyfin.org`, `pypi.org`,
`registry.npmjs.org`, and GitHub. On mainland-China networks, add `CN_MIRROR=1`.
If you're behind a corporate proxy that intercepts and re-signs TLS, the build will stall
at `curl ... exit code 60` or pip's `CERTIFICATE_VERIFY_FAILED` — that's a certificate
trust problem, not a script problem.

Then change the `image` line in `docker-compose.yml` to `movieclaw:latest` and start.
The build automatically generates PGS test samples and OCRs them back to SRT, failing the
build on any mismatch. The subtitle runtime architecture and release gates are described
in [docker-subtitle-runtime.md](docs/design/docker-subtitle-runtime.md).

## Local Development

**First make sure ports 3000 and 8000 are free.** If this machine is running the
MovieClaw Docker container, stop it with `docker compose down` first. This is the most
typical contributor profile (used the image first, then wanted to hack on the code) and
it collides 100% of the time. When a port is taken the script errors out directly and
tells you how to find the occupying process (requires `lsof`).

```bash
./scripts/dev.sh          # start backend and frontend together
./scripts/dev.sh api      # backend only
./scripts/dev.sh web      # frontend only
```

The script handles first-time setup automatically (creates the virtualenv, installs
dependencies, generates `.env`, runs `pnpm install`). Logs get colored `[api]` / `[web]`
prefixes, and `Ctrl-C` stops everything at once (child processes are cleaned up together
and the ports are released cleanly). When creating the virtualenv it picks the newest
Python on the machine (3.14 → 3.11), which may not be the one your `python3` points to.

Manual installation needs **two terminals** — the backend and frontend are both foreground
processes:

```bash
# Terminal 1: backend (Python 3.11+)
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn movieclaw_api.main:app --factory --reload --reload-dir src
```

> Don't drop `--reload-dir src`: uvicorn watches the entire current directory by default,
> and runtime logs are written under `data/logs/` — writing a log triggers reload
> detection, the detection itself writes another log line, and the flood makes the logs
> unreadable. Starting via `./scripts/dev.sh` takes care of this; it's pinned in the
> entrypoint already.

```bash
# Terminal 2: frontend (Node.js 20+)
pnpm install && pnpm web:dev
```

Web console at `http://127.0.0.1:3000`, API docs at `http://127.0.0.1:8000/docs`.
An empty `.env` with nothing filled in still starts — only TMDB-related features are
unavailable.

> Changing ports means changing two places. The backend port is `APP_PORT` in `.env`,
> but the frontend's proxy target is hardcoded to default to `http://127.0.0.1:8000` —
> you **must also** set `MOVIECLAW_API_PROXY_TARGET` in `apps/web/.env.local` to the new
> port. Change only one and the page opens but every API call returns nothing.
> The frontend's 3000 is hardcoded and not configurable. On the manual path, a uvicorn
> port collision only throws `[Errno 98] Address already in use`, without the friendlier
> hint `dev.sh` provides.

When running from source, **the NER model used for structured torrent-name extraction must
be placed manually** (the Docker image has it built in): download `model.int8.onnx`,
`tokenizer.json`, and `labels.json` from
[Releases](https://github.com/movieclaw/movieclaw/releases) into
`data/models/torrent-ner/` (path configurable via `MOVIECLAW_NER_DIR`), then restart.
Without the model the service still starts normally — only this feature is unavailable:
the first time torrent-name extraction is actually triggered, the log prints a message
saying the model was not found and that title/year/season/episode fields will stay empty.
Note this message is **lazy**: it won't appear in the startup log, so don't take a clean
startup as proof the model is in place.

## Docs & Support

Every module's major design decisions and trade-offs are recorded in
[`docs/design/`](docs/design/), one file per topic: library, metadata, subscriptions,
quality upgrades, Jellyfin compatibility, in-app updates, CLI… browse by filename for
whatever interests you. The rationale behind this README's own structure is in
[readme-rewrite.md](docs/design/readme-rewrite.md).

Questions, suggestions, and bug reports are all welcome as
[Issues](https://github.com/movieclaw/movieclaw/issues).

## License

[MIT](LICENSE)
