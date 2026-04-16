# MotionXBot

Automation-first Discord bot rewritten in Python with `discord.py`.

This build is now focused on the features you explicitly asked for: audio transfer/search, audio submission review, timed channel deletion, DM moderation tools, and message-count checks.

## Features

- `/audio setup|settings|submit|status|approve|deny|search`
- `mtxaudios <query>`
- `/transfer messages|all|forum|thread`
- `/channel delete-in|delete-status|delete-cancel`
- `/check messages`
- `/whisper send`
- `/warn send`
- `/dmlog user|recent`

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy the env template:

```bash
copy .env.example .env
```

3. Fill in:

- `DISCORD_TOKEN`
- `DISCORD_GUILD_ID`
- `BOT_STATUS`
- `PORT`

`DISCORD_CLIENT_ID` is optional now and kept only for compatibility with older setups.

4. Start the bot:

```bash
python -m motionxbot
```

The bot now syncs slash commands on startup. If `DISCORD_GUILD_ID` is set, it syncs to that guild for faster updates. Otherwise it syncs globally.

## Notes

- Time fields accept both compact and natural durations like `15m`, `2h`, `1d`, `24 hrs`, or `10 minutes`.
- Data is stored in `data/store.json`.
- `/transfer` reposts messages and files with short attribution. It does not impersonate the original authors.
- `/audio search` searches the current channel or thread by default, or a source you specify, and returns clean embed cards with download/open buttons.
- `mtxaudios <query>` runs the same audio search against the channel or thread where you type it.
- `/whisper send` can post through the bot into a text channel, thread, forum, category broadcast, or DM a user.
- `/warn send` DMs a custom warning and saves it to the warning log.
- `/dmlog` shows inbound and outbound DM history handled by the bot.
- `/channel delete-in` schedules deletion for the current channel/thread/forum, can post countdown warnings, and can optionally only delete if it stayed idle.
- `/check messages` counts a member's messages either server-wide or inside one channel/thread/forum, with an optional duration filter.
- `/transfer forum` recreates forum posts in the target forum and then copies the thread history into them.
- `/transfer thread` now accepts either a forum or a thread as the source, and either a forum or a thread as the target.
- If Discord's thread picker does not show a forum post you need, `/transfer thread` also accepts a raw thread ID or a copied Discord thread link for both source and target.
- Transfer commands support `mp3_only` for just MP3s and `audio_only` for MP3/WAV/other audio attachments, both with file-only reposting plus a creator line.
- Thread/file attribution is kept short, for example `voice-note.mp3 by @original_creator`.
- Forum tags only carry across automatically when the target forum already has matching tag names.
- Audio submission review flow:
  - Admin runs `/audio setup`
  - Creator runs `/audio submit`
  - Bot creates a private temporary review channel
  - Creator uploads the audio there
  - Reviewer uses `/audio approve` or `/audio deny`
  - Approved audio is posted into the configured destination and the temp review channel is cleaned up automatically
- This bot requires the `Message Content` and `Server Members` privileged intents in the Discord Developer Portal.

## Validation

Run:

```bash
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

Linux:

```bash
bash scripts/check.sh
```

## Hostinger VPS

This repo is now set up for a plain Hostinger Linux VPS with `systemd`.

Recommended target:

- Hostinger Linux VPS
- Ubuntu 24.04
- Python `3.11+`

Quick install flow after you clone the repo to `/opt/motionxbot`:

```bash
cd /opt/motionxbot
cp .env.example .env
nano .env
bash scripts/hostinger/install.sh
```

That script:

- creates `.venv`
- installs Python dependencies
- writes `/etc/systemd/system/motionxbot.service`
- enables and starts the bot service

Useful Hostinger VPS commands after install:

```bash
systemctl status motionxbot
journalctl -u motionxbot -f
bash scripts/hostinger/update.sh
```

If you want to install the service manually instead, a template unit file is included at:

```bash
deploy/hostinger/motionxbot.service
```

Hostinger VPS notes:

- keep your repo in a stable path like `/opt/motionxbot`
- leave `.env` in the repo root so the service can read it
- Discord bots do not need a public web domain just to stay online
- if you rotate your token, update `.env` and run `systemctl restart motionxbot`

## Docker

```bash
docker build -t motionxbot .
docker run --env-file .env -p 3000:3000 motionxbot
```
