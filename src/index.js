const {
  ActivityType,
  Client,
  Collection,
  Events,
  GatewayIntentBits
} = require("discord.js");
const { botStatus, token } = require("./config");
const { loadCommands } = require("./runtime/load-commands");
const { startHealthServer } = require("./runtime/health-server");
const { startScheduler } = require("./runtime/scheduler");
const { loadStore, saveStore, getGuildData } = require("./utils/store");
const { logToGuild } = require("./utils/discord");

if (!token) {
  console.error("Missing DISCORD_TOKEN in your environment.");
  process.exit(1);
}

loadStore();

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMembers,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages
  ]
});

client.commands = new Collection(loadCommands());
startHealthServer(client);

client.once(Events.ClientReady, async (readyClient) => {
  console.log(`Logged in as ${readyClient.user.tag}`);
  readyClient.user.setActivity(botStatus, { type: ActivityType.Watching });
  startScheduler(readyClient);
});

client.on(Events.GuildMemberAdd, async (member) => {
  const guildData = getGuildData(member.guild.id);
  if (!guildData.autoRoles.length) {
    return;
  }

  const appliedRoles = [];

  for (const roleId of guildData.autoRoles) {
    const role = member.guild.roles.cache.get(roleId);
    if (!role) {
      continue;
    }

    await member.roles.add(role).catch(() => null);
    appliedRoles.push(role.name);
  }

  if (appliedRoles.length) {
    await logToGuild(
      client,
      member.guild.id,
      `Auto-role applied to ${member.user.tag}: ${appliedRoles.join(", ")}`
    );
    saveStore();
  }
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (!interaction.isChatInputCommand()) {
    return;
  }

  const command = client.commands.get(interaction.commandName);
  if (!command) {
    return;
  }

  try {
    await command.execute(interaction, client);
  } catch (error) {
    console.error(`Command ${interaction.commandName} failed:`, error);
    const payload = {
      content: "That command failed. Check the console for the stack trace.",
      ephemeral: true
    };

    if (interaction.replied || interaction.deferred) {
      await interaction.followUp(payload).catch(() => null);
    } else {
      await interaction.reply(payload).catch(() => null);
    }
  }
});

client.login(token);
