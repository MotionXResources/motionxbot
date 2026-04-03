const path = require("node:path");
const dotenv = require("dotenv");

dotenv.config();

module.exports = {
  token: process.env.DISCORD_TOKEN || "",
  clientId: process.env.DISCORD_CLIENT_ID || "",
  guildId: process.env.DISCORD_GUILD_ID || "",
  botStatus: process.env.BOT_STATUS || "Watching server workflows",
  port: Number(process.env.PORT || 3000),
  storePath: path.join(__dirname, "..", "data", "store.json")
};
