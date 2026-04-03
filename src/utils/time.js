function parseDuration(input) {
  if (!input || typeof input !== "string") {
    return null;
  }

  const normalized = input.trim().toLowerCase();
  const pattern = /(\d+)\s*(w|d|h|m|s)/gi;
  if (!normalized) {
    return null;
  }

  let total = 0;
  let consumed = 0;
  let match;

  while ((match = pattern.exec(normalized)) !== null) {
    consumed += match[0].length;
    const value = Number(match[1]);
    const unit = match[2];

    if (unit === "w") total += value * 7 * 24 * 60 * 60 * 1000;
    if (unit === "d") total += value * 24 * 60 * 60 * 1000;
    if (unit === "h") total += value * 60 * 60 * 1000;
    if (unit === "m") total += value * 60 * 1000;
    if (unit === "s") total += value * 1000;
  }

  const compact = normalized.replace(/\s+/g, "");
  const compactConsumed = compact.match(/(\d+)\s*(w|d|h|m|s)/gi)?.join("") || "";

  if (total === 0 && /^\d+$/.test(normalized)) {
    return Number(normalized) * 60 * 1000;
  }

  if (total === 0 || compactConsumed.length !== compact.length) {
    return null;
  }

  return total;
}

function formatDuration(ms) {
  if (!ms || ms < 1000) {
    return "under a second";
  }

  const units = [
    ["w", 7 * 24 * 60 * 60 * 1000],
    ["d", 24 * 60 * 60 * 1000],
    ["h", 60 * 60 * 1000],
    ["m", 60 * 1000],
    ["s", 1000]
  ];

  let remaining = ms;
  const parts = [];

  for (const [label, value] of units) {
    if (remaining >= value) {
      const amount = Math.floor(remaining / value);
      remaining -= amount * value;
      parts.push(`${amount}${label}`);
    }

    if (parts.length === 2) {
      break;
    }
  }

  return parts.join(" ");
}

function renderDiscordTimestamp(timestamp, style = "f") {
  return `<t:${Math.floor(timestamp / 1000)}:${style}>`;
}

module.exports = {
  parseDuration,
  formatDuration,
  renderDiscordTimestamp
};
