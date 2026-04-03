# MotionXBot

Automation-first Discord bot built with `discord.js`.

This project is aimed at boring, useful server work instead of fun commands. It ships with 50+ slash subcommands across scheduling, reusable messaging, approvals, checklists, todos, role automation, channel ops, and cleanup workflows.

## What it does

- Schedules one-time reminders with `/reminder create`
- Runs recurring channel jobs with `/job create`
- Sends recurring heartbeat messages with `/heartbeat set`
- Stores reusable snippets with `/tag`
- Stores placeholder-driven templates with `/template`
- Tracks shared operational checklists with `/checklist`
- Tracks team todos with `/todo`
- Applies roles automatically to new members with `/autorole`
- Bulk adds or removes roles with `/bulkrole`
- Locks, unlocks, archives, or slowmodes channels with `/channel`
- Deletes recent bot or user messages with `/cleanup`
- Tracks approval requests with `/approval`
- Sends audit events to a log channel with `/logchannel`
- Shows a quick automation summary with `/botstatus`

## Setup

1. Install dependencies:

```bash
npm install
```

2. Copy the example env file and fill in your Discord app values:

```bash
copy .env.example .env
```

3. Add these values in `.env`:

- `DISCORD_TOKEN`
- `DISCORD_CLIENT_ID`
- `DISCORD_GUILD_ID` for fast guild-scoped command registration during development
- `BOT_STATUS` optional custom status text

4. Register slash commands:

```bash
npm run deploy
```

5. Start the bot:

```bash
npm start
```

## Command groups

- `/automation-help`
- `/reminder create|list|cancel|snooze`
- `/job create|list|pause|resume|remove|run-now`
- `/tag create|update|post|list|delete`
- `/template create|update|send|list|delete`
- `/checklist create|add-item|done|reset|show|list|delete`
- `/todo add|list|done|remove`
- `/autorole add|remove|list`
- `/bulkrole add|remove`
- `/channel lock|unlock|slowmode|archive`
- `/cleanup bot|user`
- `/approval create|list|approve|reject`
- `/logchannel set|clear|show`
- `/heartbeat set|status|clear`
- `/botstatus`

## Notes

- Time inputs accept values like `15m`, `2h`, `1d`, or `1h30m`.
- Templates and scheduled messages support built-ins like `{server}`, `{channel}`, `{user}`, `{date}`, and `{time}`.
- Data is stored locally in `data/store.json`, so this is easy to run without a database.
- For auto-role and bulk-role commands, the bot role needs to sit above the roles it manages.

## Validation

Run the syntax checker with:

```bash
npm run check
```

## Free hosting

The best fit for a free always-on Discord bot is currently Northflank, not Vercel. This bot now exposes:

- `/healthz` for platform health checks
- port `3000`
- a production `Dockerfile`

### Recommended host: Northflank

1. Push this repo to GitHub.
2. In Northflank, create a new service from the repo.
3. Let it build from the included `Dockerfile`.
4. Add environment variables:
   - `DISCORD_TOKEN`
   - `DISCORD_CLIENT_ID`
   - `DISCORD_GUILD_ID`
   - `BOT_STATUS`
   - `PORT=3000`
5. Set the service port to `3000`.
6. Use `/healthz` as the health check path.

### Important storage note

This project currently stores automation data in `data/store.json`. On most free cloud hosts, local disk is ephemeral, so reminders, todos, templates, and other saved bot data can be lost after redeploys or restarts.

If you want durable cloud hosting, the next upgrade should be moving storage to a database.
