<p align="center">
  <img src="static/grabia_icon.png" alt="Grabia icon" width="128">
</p>

<h1 align="center">Grab<span style="color:#6a9f4b">ia</span></h1>

<p align="center">
  A self-hosted web app for downloading stuff from the <b><a href="https://archive.org">Internet Archive</a></b>.
</p>

---

> **Heads up:** This is very much an early project, built mostly through vibe coding by someone who only kind-of knows what they're doing. It works for me, but expect rough edges, questionable decisions, and the occasional "why did I (or the stupid machine) do it that way?" moment. Bug reports and PRs are welcome, unless you're gonna be a dick.

## What is it?

Grab**ia** is a lightweight, browser-based download manager specifically for Internet Archive. You give it an archive URL, it fetches the file list, and then downloads everything, or just the files you pick, to a local directory. It runs as a small Flask server on your machine and you control it through a web UI.

## Features

- **Queue up multiple archives** and download them sequentially
- **Pick individual files** from an archive or grab the whole thing
- **Pause, resume, and stop** downloads whenever you want
- **Bandwidth limiting** with an optional schedule (e.g. go full speed at night, throttle during the day)
- **Retry logic** for failed downloads with configurable retries and delay
- **Drag-and-drop reordering** of archives and files to set download priority
- **Metadata refresh** to detect new, changed, or removed files on the archive
- **Post-download processing** — convert disc images to CHD (with auto CD/DVD detection), compress ISOs to CSO, or extract archives in place
- **Compression presets** for CHD conversion (default, lzma, zlib, flac, none) with transparent codec labels
- **Dark and light themes**
- **Password-protected** web UI (Is it secure? Probably not! Needs an audit, in the mean time don't open the port up to the internet)

## Getting started

### Docker (recommended)

```bash
docker compose up -d
```

Or pull the pre-built image directly:

```bash
docker pull ghcr.io/scheeseman486/grabia:latest
```

The Docker image includes all processing tools (chdman, maxcso, 7z, unrar) out of the box. See `docker-compose.yml` for volume and port configuration.

### Manual

You'll need Python 3.10+ installed.

```bash
git clone https://github.com/scheeseman486/grabia.git
cd grabia
./run.sh
```

The `run.sh` script creates a virtual environment, installs dependencies, and starts the server. On first launch you'll be asked to set up a username and password.

Then just open [http://localhost:5000](http://localhost:5000) in your browser.

If you'd rather do it yourself:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

For file processing to work outside Docker, you'll need to install the relevant tools yourself: `chdman` (from MAME), `maxcso`, `7z` (`p7zip-full`), and/or `unrar`. Grabia detects them on your PATH automatically — check Settings to see what's available.

### Internet Archive credentials

To download restricted/logged-in-only items you'll need to add your archive.org email and password in the Settings page. You can test your credentials from there too.

## Configuration

Most settings are in the web UI under the gear icon, including:

- Download directory
- Max retries and retry delay
- Bandwidth limits and scheduling
- Theme (dark/light)
- Files per page

The server port defaults to `5000` but can be changed with the `GRABIA_PORT` environment variable.

## Tech stack

Nothing fancy: Flask, SQLite, vanilla JS, and plain CSS.

## Future Plans

- **Torrent support?** Probably not going to integrate a torrent client, but might optionally pass off archive torrent files to another downloader so it can automatically seed downloads.
- **Sync/Uploads?** The interface could also be useful for managing archives on IA itself.

## License

GPL-2.0 — see [LICENSE](LICENSE) for details.
