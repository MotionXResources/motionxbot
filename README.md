# MotionXBot

Automation-first Discord bot rewritten in Python with `discord.py`.

This build keeps the same operations-first command surface as the earlier JavaScript version: reminders, recurring jobs, templates, tags, approvals, todos, checklists, autoroles, bulk role changes, channel moderation, cleanup, heartbeat jobs, and message/forum transfer tooling.

## Features

- `/automation-help`
- `/reminder create|list|cancel|snooze`
- `/job create|list|pause|resume|remove|run-now`
- `/tag create|update|post|list|delete`
- `/template create|update|send|list|delete`
- `/checklist create|add-item|done|reset|show|list|delete`
- `/todo add|list|done|remove`
- `/approval create|list|approve|reject`
- `/whisper send|history`
- `/warn send|list|clear`
- `/dmlog user|recent`
- `/note add|list|remove`
- `/timeout set|clear`
- `/autoresponse add|list|remove`
- `/autorole add|remove|list`
- `/bulkrole add|remove`
- `/channel lock|unlock|slowmode|archive`
- `/cleanup bot|user`
- `/logchannel set|clear|show`
- `/heartbeat set|status|clear`
- `/audio search`
- `mtxaudios <query>`
- `/transfer messages|all|forum|thread`
- `/botstatus`

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

- Time fields accept compact durations like `15m`, `2h`, `1d`, or `1h30m`.
- Data is stored in `data/store.json`.
- `/transfer` reposts messages and files with short attribution. It does not impersonate the original authors.
- `/audio search` searches the current channel or thread by default, or a source you specify, and returns clean embed cards with download/open buttons.
- `mtxaudios <query>` runs the same audio search against the channel or thread where you type it.
- `/whisper send` can post through the bot into a text channel, thread, forum, category broadcast, or DM a user.
- `/warn send` DMs a custom warning and saves it to the warning log.
- `/dmlog` shows inbound and outbound DM history handled by the bot.
- `/autoresponse` adds simple trigger-based automation replies.
- `/timeout` and `/note` add quick moderation controls on top of the existing cleanup/role/channel tools.
- `/transfer forum` recreates forum posts in the target forum and then copies the thread history into them.
- `/transfer thread` now accepts either a forum or a thread as the source, and either a forum or a thread as the target.
- If Discord's thread picker does not show a forum post you need, `/transfer thread` also accepts a raw thread ID or a copied Discord thread link for both source and target.
- Transfer commands support `mp3_only` for just MP3s and `audio_only` for MP3/WAV/other audio attachments, both with file-only reposting plus a creator line.
- Thread/file attribution is kept short, for example `voice-note.mp3 by @original_creator`.
- Forum tags only carry across automatically when the target forum already has matching tag names.
- This bot requires the `Message Content` and `Server Members` privileged intents in the Discord Developer Portal.

## Validation

Run:

```bash
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

## PebbleHost

For PebbleHost Python hosting:

- startup command: `python -m motionxbot`
- Python version: `3.11+`
- install command: `pip install -r requirements.txt`

## Docker

```bash
docker build -t motionxbot .
docker run --env-file .env -p 3000:3000 motionxbot
```
