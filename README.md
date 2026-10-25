# Local English studio

A local app for drafting practical English lessons for Korean adults. It keeps drafts, a weekly calendar, a manual publishing queue, community notes, a small lead list, and backups in SQLite on this computer.

It listens on **127.0.0.1 only**. It does not post, comment, message, or contact anyone by itself. Scheduled jobs are off until you turn them on in Settings, and the calendar job only creates drafts.

## What the tests verified

These behaviors were exercised with pytest against a local fake Ollama server and fake HTTP transports. They do not require a model download or live platform credentials.

- Draft generation sends `POST /api/chat` with `"format": "json"`, checks the JSON with Pydantic, retries invalid output, and refuses a repeated recent topic.
- If that server is down, or returns an Ollama error, the app says no post was generated and does not invent a draft.
- The weekly calendar is stored in SQLite. With the job flag off, the scheduler does not call the network. With the flag on, Monday creates the Monday draft only.
- Naver Blog has no official write API. The queue exports text and refuses to mark the item published.
- Facebook, Instagram, and Naver Cafe adapters mark published only after a confirmed success body. A timeout blocks retry until you acknowledge the post is not on the platform. Tokens are not shown on the settings page or in stored attempt text.
- Instagram text-only posts are not sent. Cafe fields are MS949 percent-encoded.
- RSS import, manual URL import, and the approval gate work. There is no comment or message submit route.
- A follow-up date is not stored unless consent is recorded.
- Analytics counts come from SQLite. Engagement says `not available` unless an official payload included the metric.
- SQLite backup and restore round-trip through the backup API, including the restore confirmation checkbox.

## What still needs your machine

- Real lesson text needs Ollama on your PC and a model you pull yourself. The suggested model for 16 GB of RAM is `qwen2.5:7b`.
- Official publishing needs credentials you create in a Meta or Naver developer app and put in `.env`. This repository's tests never call those live APIs.
- Instagram also needs a public `https` image URL. This app does not host one.

## Setup on Linux Mint

From the project folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

If `python3 -m venv` says ensurepip is unavailable, install the distro package yourself and try again:

```bash
sudo apt install python3-venv
python3 -m venv .venv
```

The cloud machine used to write this app did not have ensurepip, so the venv there was created with the `virtualenv` tool. On Mint, `python3 -m venv` is the path to use.

### Ollama, run by you

Do not skip the model pull if you want real drafts. These commands are for your PC:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
```

Leave Ollama running. The default host is `http://127.0.0.1:11434`.

## Start

Foreground, in this folder:

```bash
make run
```

That is `uvicorn` on `127.0.0.1` and port `8000` (or `APP_PORT` from the environment). Open `http://127.0.0.1:8000/`.

Background, still only on loopback, and still running after you close the terminal:

```bash
chmod +x scripts/*.sh
./scripts/run-background.sh
```

Stop it:

```bash
./scripts/stop.sh
```

The pid is `data/app.pid`. The server log is `data/logs/server.log`. Application events are also in `data/logs/app.log` and on the Logs page.

## Backup and restore

Stop the app first so nothing is writing the database.

```bash
./scripts/backup.sh
./scripts/restore.sh data/backups/studio-YYYYMMDDTHHMMSSZ.db
```

Or from the dashboard: Logs and backups. Restore does nothing until you tick the confirmation box. Backup files stay under `data/backups/` and are not committed.

## Jobs

Settings has two switches. Both start off.

- Weekly calendar, 08:00 Asia/Seoul, creates that weekday's drafts only.
- Public RSS import, 09:00 Asia/Seoul, reads the two allowlisted lesson feeds only.

There is no publish job.

## Troubleshooting

- Pages load but Generate says no post was generated: Ollama is not running, the model name does not match `ollama list`, or the host in Settings is not `127.0.0.1` or `localhost`.
- Port already in use: change `APP_PORT` in `.env` and start again. Check `data/app.pid` before starting a second copy.
- A publishing button is missing: that platform is manual-only, credentials are empty, Instagram has no public https image, or a previous attempt is waiting for you to confirm it did not land.
- Nothing was restored: the confirmation box was off, or the file name is not a `.db` inside `data/backups/`.
- Tests: `.venv/bin/python -m pytest` from this folder.

## Layout

`app/main.py` builds the FastAPI app. Content, publishing, research, leads, analytics, backup, and the scheduler live in their own packages. Templates are Jinja2 and `app/static/style.css` has no external assets. Official publishing notes are in `docs/publishing.md`.
