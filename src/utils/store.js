const fs = require("node:fs");
const path = require("node:path");
const { storePath } = require("../config");

let store = null;

function createGuildDefaults() {
  return {
    logChannelId: null,
    autoRoles: [],
    reminders: [],
    jobs: [],
    tags: {},
    templates: {},
    checklists: {},
    todos: [],
    approvals: {},
    heartbeat: null
  };
}

function normalizeGuildData(data = {}) {
  return {
    ...createGuildDefaults(),
    ...data,
    autoRoles: Array.isArray(data.autoRoles) ? data.autoRoles : [],
    reminders: Array.isArray(data.reminders) ? data.reminders : [],
    jobs: Array.isArray(data.jobs) ? data.jobs : [],
    tags: data.tags && typeof data.tags === "object" ? data.tags : {},
    templates: data.templates && typeof data.templates === "object" ? data.templates : {},
    checklists: data.checklists && typeof data.checklists === "object" ? data.checklists : {},
    todos: Array.isArray(data.todos) ? data.todos : [],
    approvals: data.approvals && typeof data.approvals === "object" ? data.approvals : {},
    heartbeat: data.heartbeat && typeof data.heartbeat === "object" ? data.heartbeat : null
  };
}

function loadStore() {
  if (store) {
    return store;
  }

  fs.mkdirSync(path.dirname(storePath), { recursive: true });

  if (!fs.existsSync(storePath)) {
    store = { guilds: {} };
    fs.writeFileSync(storePath, JSON.stringify(store, null, 2));
    return store;
  }

  const raw = fs.readFileSync(storePath, "utf8");
  const parsed = raw ? JSON.parse(raw) : {};
  store = {
    guilds: parsed.guilds && typeof parsed.guilds === "object" ? parsed.guilds : {}
  };

  for (const [guildId, guildData] of Object.entries(store.guilds)) {
    store.guilds[guildId] = normalizeGuildData(guildData);
  }

  saveStore();
  return store;
}

function saveStore() {
  if (!store) {
    loadStore();
  }

  fs.writeFileSync(storePath, JSON.stringify(store, null, 2));
}

function getGuildData(guildId) {
  const currentStore = loadStore();
  if (!currentStore.guilds[guildId]) {
    currentStore.guilds[guildId] = createGuildDefaults();
    saveStore();
  }

  return currentStore.guilds[guildId];
}

function updateGuildData(guildId, updater) {
  const guildData = getGuildData(guildId);
  updater(guildData);
  saveStore();
  return guildData;
}

module.exports = {
  loadStore,
  saveStore,
  getGuildData,
  updateGuildData
};
