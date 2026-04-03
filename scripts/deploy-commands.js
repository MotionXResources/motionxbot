const { REST, Routes } = require("discord.js");
const { clientId, guildId, token } = require("../src/config");
const { loadCommandData } = require("../src/runtime/load-commands");

if (!token || !clientId) {
  console.error("Missing DISCORD_TOKEN or DISCORD_CLIENT_ID.");
  process.exit(1);
}

const rest = new REST({ version: "10" }).setToken(token);
const body = loadCommandData();

async function deploy() {
  if (guildId) {
    await rest.put(Routes.applicationGuildCommands(clientId, guildId), { body });
    console.log(`Registered ${body.length} guild commands to ${guildId}.`);
    return;
  }

  await rest.put(Routes.applicationCommands(clientId), { body });
  console.log(`Registered ${body.length} global commands.`);
}

deploy().catch((error) => {
  console.error("Command deployment failed:", error);
  process.exit(1);
});
