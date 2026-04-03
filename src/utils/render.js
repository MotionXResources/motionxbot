function parseAssignments(input) {
  if (!input) {
    return {};
  }

  return input
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .reduce((accumulator, pair) => {
      const separatorIndex = pair.indexOf("=");
      if (separatorIndex === -1) {
        return accumulator;
      }

      const key = pair.slice(0, separatorIndex).trim().toLowerCase();
      const value = pair.slice(separatorIndex + 1).trim();

      if (key) {
        accumulator[key] = value;
      }

      return accumulator;
    }, {});
}

function buildBasePlaceholders({ guild, channel, user }) {
  const now = new Date();

  return {
    server: guild?.name || "server",
    server_id: guild?.id || "",
    channel: channel?.toString?.() || "#channel",
    channel_id: channel?.id || "",
    user: user?.toString?.() || "user",
    user_id: user?.id || "",
    date: now.toLocaleDateString(),
    time: now.toLocaleTimeString(),
    iso_date: now.toISOString()
  };
}

function renderTemplateText(template, placeholders = {}) {
  return String(template || "").replace(/\{([a-z0-9_]+)\}/gi, (_, rawKey) => {
    const key = rawKey.toLowerCase();
    return placeholders[key] ?? `{${rawKey}}`;
  });
}

module.exports = {
  parseAssignments,
  buildBasePlaceholders,
  renderTemplateText
};
