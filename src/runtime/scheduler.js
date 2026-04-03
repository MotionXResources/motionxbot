const { buildBasePlaceholders, renderTemplateText } = require("../utils/render");
const { getGuildData, loadStore, saveStore } = require("../utils/store");
const { logToGuild, resolveGuildTextChannel } = require("../utils/discord");

let intervalHandle = null;
let busy = false;

async function processReminders(client, guild, guildData, now) {
  const dueReminders = guildData.reminders.filter((reminder) => reminder.dueAt <= now);
  if (dueReminders.length === 0) {
    return;
  }

  for (const reminder of dueReminders) {
    try {
      if (reminder.delivery === "dm") {
        const user = await client.users.fetch(reminder.userId);
        await user.send(`Reminder: ${reminder.message}`);
      } else {
        const channel = await resolveGuildTextChannel(guild, reminder.channelId);
        if (channel) {
          await channel.send({
            content: `<@${reminder.userId}> reminder: ${reminder.message}`
          });
        }
      }
    } catch (error) {
      reminder.dueAt = now + 15 * 60 * 1000;
      reminder.lastError = error.message;
      continue;
    }

    guildData.reminders = guildData.reminders.filter((item) => item.id !== reminder.id);
    await logToGuild(client, guild.id, `Reminder delivered for <@${reminder.userId}>: ${reminder.message}`);
  }
}

async function processJobs(client, guild, guildData, now) {
  for (const job of guildData.jobs) {
    if (!job.enabled || job.nextRunAt > now) {
      continue;
    }

    const channel = await resolveGuildTextChannel(guild, job.channelId);
    if (!channel) {
      job.nextRunAt = now + job.intervalMs;
      continue;
    }

    const placeholders = buildBasePlaceholders({
      guild,
      channel,
      user: client.user
    });

    await channel
      .send({
        content: renderTemplateText(job.message, placeholders)
      })
      .catch(() => null);

    job.lastRunAt = now;
    job.nextRunAt = now + job.intervalMs;
    await logToGuild(client, guild.id, `Scheduled job \`${job.name}\` ran in ${channel}.`);
  }
}

async function processHeartbeat(client, guild, guildData, now) {
  const heartbeat = guildData.heartbeat;
  if (!heartbeat || !heartbeat.enabled || heartbeat.nextRunAt > now) {
    return;
  }

  const channel = await resolveGuildTextChannel(guild, heartbeat.channelId);
  if (!channel) {
    heartbeat.nextRunAt = now + heartbeat.intervalMs;
    return;
  }

  const content = renderTemplateText(heartbeat.message, buildBasePlaceholders({
    guild,
    channel,
    user: client.user
  }));

  await channel.send({ content }).catch(() => null);
  heartbeat.lastRunAt = now;
  heartbeat.nextRunAt = now + heartbeat.intervalMs;
}

async function tick(client) {
  if (busy) {
    return;
  }

  busy = true;
  const now = Date.now();
  const store = loadStore();

  try {
    for (const guildId of Object.keys(store.guilds)) {
      const guild = client.guilds.cache.get(guildId) || (await client.guilds.fetch(guildId).catch(() => null));
      if (!guild) {
        continue;
      }

      const guildData = getGuildData(guildId);
      await processReminders(client, guild, guildData, now);
      await processJobs(client, guild, guildData, now);
      await processHeartbeat(client, guild, guildData, now);
    }

    saveStore();
  } finally {
    busy = false;
  }
}

function startScheduler(client) {
  if (intervalHandle) {
    clearInterval(intervalHandle);
  }

  intervalHandle = setInterval(() => {
    tick(client).catch((error) => {
      console.error("Scheduler tick failed:", error);
    });
  }, 15 * 1000);

  tick(client).catch((error) => {
    console.error("Initial scheduler tick failed:", error);
  });
}

module.exports = {
  startScheduler
};
