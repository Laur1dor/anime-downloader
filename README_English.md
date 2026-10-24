# AnimeDownloader

[Русский](README.md) · English

A Telegram bot for downloading supported anime and TV sources, with optional delivery to the owner's Jellyfin library. Choose a voice track, episodes and quality; provider selection is automatic. Source families include AnimeGO, YummyAnime and a dedicated Sopranos source. Individual players, regional availability and metadata coverage vary.

## Docker setup

Use Linux, Docker Engine and Compose v2. Copy `.env.example` to `.env`, then supply `BOT_TOKEN`, `ADMIN_ID`, `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. Start privately with `ACCESS_MODE=admin` and one concurrent job:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs --tail=100 bot
```

Bot API has no host port published. The optional restart feature mounts Docker socket, which grants extensive host access; remove that mount and clear `RELOAD_CONTAINERS` if unnecessary.

## Jellyfin, NAS and Proxmox

Mount storage on the host first. Set `LIBRARY_HOST_DIR=/srv/media/Anime`, `MEDIA_HOST_ROOT=/srv/media`, `MEDIA_ROOT=/app/media`, and your actual `JELLYFIN_URL` and API key in `.env`. Jellyfin needs read access, the bot needs write access to the same files.

```bash
docker compose -f compose.yml -f compose.library.yml config --quiet
docker compose -f compose.yml -f compose.library.yml up -d --build
```

Use TV libraries for Anime/Series and movie libraries for AnimeMovies/Movies; verify directory names in `animedl/models.py`. Only the owner can use the owner's storage. Telegram may receive several MP4 parts while the library retains the complete file.

For Proxmox LXC, mount storage on the host and pass it through an unused mount point. Check UID/GID mappings for unprivileged containers and Docker nesting requirements for your host version. Back up bind-mounted media and Jellyfin configuration separately from rootfs. Check mounts before starting: an absent NAS must not become a local folder on the system disk. Do not format or recreate pools to migrate. Hardware transcoding is optional for compatible direct-play media.

## Networking and recovery

Check the catalog, player/CDN, metadata and Telegram separately from the server. Some sources need a Russian IP; others may be inaccessible from Russia. A global proxy can break a working provider. Configure and verify your own routing as needed. Some extractors deliberately connect directly. Free public proxies are not a reliable default.

- HTTP 403 before download concerns source access or sessions, not Telegram size.
- Missing quality can mean a failed probe; inspect the warning and retry.
- Large videos split without re-encoding. Reserve space for original and parts. Very large keyframe gaps may prevent splitting below the limit.
- The bounded disk retry cache defaults to 4 GB and 24 hours; it is not permanent storage.
- After an ambiguous upload timeout, inspect the chat before retrying: Telegram may have accepted the file.
- Retry failed episodes instead of submitting the entire season again.
- Jellyfin resume tracking is provided by Jellyfin. Intro skipping needs a separate compatible plugin and client configuration.

Keep `.env`, keys, cookies and user databases private. Report errors with timestamp, source, selected episodes and redacted logs. Download and store only content you are entitled to use.

## Events and errors

Owner-only `/stats` and `/errors` show the retained event history. Events include actions and download/delivery outcomes, sources, quality and sizes. Message contents, complete URLs, keys and exception text are excluded. User identifiers remain personal data: restrict database access and tell users about collection.

The local SQLite journal defaults to 50,000 events and 30 days (`AUDIT_MAX_EVENTS`, `AUDIT_RETENTION_DAYS`). Statistics cover the retained window. Repeated identical error alerts go only to the owner, at most once per 30 minutes; `AUDIT_ALERTS=0` disables alerts.

For optional PostgreSQL, set a long random alphanumeric `AUDIT_POSTGRES_PASSWORD` in `.env`, then run:

```bash
docker compose -f compose.yml -f compose.observability.yml up -d --build
```

You may also include `-f compose.library.yml`. PostgreSQL 17 Alpine uses a separate volume and internal network without publishing a host port, so another host service on 5432 does not conflict. Local events replicate asynchronously with stable identifiers and retries. PostgreSQL retention is time-based, without the local row-count cap. Back it up separately. Alpine is an image variant, not a user-count limit; capacity depends on activity and resources. Error monitoring does not automatically change production code.

## Development and CI

Python 3.13, `ffmpeg`/`ffprobe` and Chromium are used:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium --only-shell
python -m unittest discover -s tests -v
docker build -t anime-downloader-check .
```

GitHub Actions runs tests, including actual `ffmpeg`, and builds both images without secrets. It does not deploy to your server. Tests and samples do not replace a complete real-world upload of a file larger than 2 GB. See the Russian README for the troubleshooting table.
