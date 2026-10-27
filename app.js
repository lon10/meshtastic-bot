const ui = {
  active: {kind: "longfast", peer: null},
  messages: new Map(),
  hasMore: false,
  hasNewer: false,
  loadingNewer: false,
  unreadIds: new Set(),
  firstUnreadId: null,
  longfastUnread: 0,
  channelConfig: null,
  version: 0,
  state: null,
  chats: [],
  channels: [],
  chatSection: "channels",
  usbDevices: [],
  editingTemplate: null,
  transportInitialized: false,
  replyTo: null,
  tab: "dashboard",
  selectedNodeId: null,
  radioSettingsRevision: null,
  traces: [],
  nodeTraces: [],
  nodeTracesTarget: null,
  creatingChannel: false,
};
const reactionChoices = ["👍", "❤️", "😂", "😮", "😢", "👎", "🔥", "🎉"];
const emojiGroups = [
  {id: "recent", icon: "🕘", label: "Недавние"},
  {id: "Smileys & Emotion", icon: "😀", label: "Смайлы"},
  {id: "People & Body", icon: "👋", label: "Люди"},
  {id: "Animals & Nature", icon: "🐻", label: "Природа"},
  {id: "Food & Drink", icon: "🍕", label: "Еда"},
  {id: "Travel & Places", icon: "🚗", label: "Места"},
  {id: "Activities", icon: "⚽", label: "Занятия"},
  {id: "Objects", icon: "💡", label: "Предметы"},
  {id: "Symbols", icon: "❤️", label: "Символы"},
  {id: "Flags", icon: "🏳️", label: "Флаги"},
  {id: "Component", icon: "🏻", label: "Тон кожи"},
];
let reactionMenu = null;
let longPressTimer = null;
let emojiCatalogPromise = null;
let readTimer = null;
let readInFlight = false;

const tabNames = ["dashboard", "chats", "settings"];
let mapCenter = null;
let mapLocation = "выбранный город";
let nodeMap = null;
let nodeLayer = null;
let nodeMapSignature = null;
let nodeMapBounds = null;

function ensureNodeMap() {
  if (nodeMap || !mapCenter || typeof L === "undefined" || ui.tab !== "dashboard") return;
  nodeMap = L.map("node-map", {scrollWheelZoom: false}).setView(mapCenter, 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(nodeMap);
  nodeLayer = L.layerGroup().addTo(nodeMap);
}

function renderNodeMap() {
  const nodes = ui.state?.known_nodes || [];
  const positioned = nodes.filter(node => Number.isFinite(node.position?.lat) &&
    Number.isFinite(node.position?.lon));
  document.getElementById("map-summary").textContent = positioned.length
    ? `${positioned.length} из ${nodes.length} нод передали координаты. Начальный вид — ${mapLocation}; нажмите «Все ноды», чтобы увидеть остальные.`
    : "Координаты нод пока неизвестны. Ноды без GPS остаются в списке на вкладке «Чаты».";
  document.getElementById("map-fit").disabled = !positioned.length;
  if (typeof L === "undefined") {
    document.getElementById("map-summary").textContent = "Карта временно недоступна: библиотека не загрузилась.";
    return;
  }
  ensureNodeMap();
  if (!nodeMap) return;
  const signature = JSON.stringify(positioned.map(node =>
    [node.id, node.name, node.is_self, node.position.lat, node.position.lon]));
  if (signature === nodeMapSignature) return;
  nodeMapSignature = signature;
  nodeLayer.clearLayers();
  nodeMapBounds = positioned.length ? L.latLngBounds([]) : null;
  for (const node of positioned) {
    const point = [node.position.lat, node.position.lon];
    nodeMapBounds.extend(point);
    const marker = L.circleMarker(point, {
      radius: node.is_self ? 10 : 7,
      color: node.is_self ? "#ffffff" : "#0b3d3a",
      weight: 2,
      fillColor: node.is_self ? "#f5ce72" : "#55d5bd",
      fillOpacity: .95,
    }).addTo(nodeLayer);
    const title = document.createElement("span");
    title.textContent = node.name;
    marker.bindTooltip(title);
    const popup = document.createElement("div");
    popup.className = "map-popup";
    const name = document.createElement("strong");
    name.textContent = node.is_self ? `${node.name} · моя нода` : node.name;
    const details = document.createElement("span");
    details.textContent = `${node.id} · ${node.position.lat.toFixed(5)}, ${node.position.lon.toFixed(5)}`;
    popup.append(name, details);
    const showDetails = document.createElement("button");
    showDetails.type = "button";
    showDetails.textContent = "Информация о ноде";
    showDetails.addEventListener("click", () => showNodeDetails(node.id));
    popup.append(showDetails);
    if (!node.is_self) {
      const openChat = document.createElement("button");
      openChat.type = "button";
      openChat.textContent = "Открыть личку";
      openChat.addEventListener("click", () => selectChat("dm", node.id));
      popup.append(openChat);
    }
    marker.bindPopup(popup);
  }
}

function setTab(name, updateHash = true) {
  const tab = tabNames.includes(name) ? name : "dashboard";
  const previousTab = ui.tab;
  ui.tab = tab;
  closeReactionMenu();
  for (const entry of tabNames) {
    const selected = entry === tab;
    document.getElementById(`${entry}-view`).hidden = !selected;
    const button = document.getElementById(`tab-${entry}`);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  if (updateHash && location.hash !== `#${tab}`) location.hash = tab;
  if (tab === "dashboard") {
    renderNodeMap();
    if (nodeMap) requestAnimationFrame(() => nodeMap.invalidateSize());
  }
  if (tab === "chats") {
    renderMessages(previousTab !== "chats" && ui.unreadIds.size ? "initial" : "keep");
    queueVisibleRead();
  }
  if (tab === "settings") loadTemplates().catch(error => feedback(error.message, true));
}

function renderDashboard() {
  const state = ui.state;
  if (!state) return;
  document.getElementById("dashboard-bot-status").textContent = state.bot_enabled === false
    ? "Бот выключен — включите его в настройках." : "Бот включён";
  document.getElementById("dashboard-node-count").textContent = String(state.known_nodes?.length || 0);
  document.getElementById("dashboard-chat-count").textContent = String(ui.chats.length);
  document.getElementById("dashboard-connection").textContent = state.connected
    ? (state.selected_transport === "usb" ? "USB" : "Bluetooth")
    : state.connecting ? "Подключение…" : "Отключено";
  document.getElementById("dashboard-device").textContent = state.connected
    ? `${state.node_id || "Нода"} · ${state.selected_device || "устройство"}`
    : "Откройте настройки для подключения";
}

function recentEmojis() {
  try {
    const value = JSON.parse(localStorage.getItem("meshtastic-recent-emojis") || "[]");
    return Array.isArray(value) ? value.filter(item => typeof item === "string").slice(0, 24) : [];
  } catch { return []; }
}

function rememberEmoji(emoji) {
  try {
    localStorage.setItem("meshtastic-recent-emojis",
      JSON.stringify([emoji, ...recentEmojis().filter(item => item !== emoji)].slice(0, 24)));
  } catch { /* Private browsing may disable local storage. */ }
}

function loadEmojiCatalog() {
  if (!emojiCatalogPromise) {
    emojiCatalogPromise = getJson("/emoji-data.json").then(data => data.items.map(
      ([emoji, group, ruName, ruKeywords, enName, enKeywords]) => ({
        emoji, group, ruName, enName,
        search: `${emoji} ${ruName} ${ruKeywords} ${enName} ${enKeywords}`.toLocaleLowerCase("ru"),
      })
    )).catch(error => {
      emojiCatalogPromise = null;
      throw error;
    });
  }
  return emojiCatalogPromise;
}

function closeReactionMenu() {
  reactionMenu?.remove();
  reactionMenu = null;
}

function openReactionMenu(message, x, y, focusSearch = false) {
  closeReactionMenu();
  const messageId = message.id;
  const menu = document.createElement("div");
  menu.className = "reaction-menu";
  menu.setAttribute("role", "group");
  menu.setAttribute("aria-label", "Действия с сообщением");
  const replyAction = document.createElement("button");
  replyAction.type = "button";
  replyAction.className = "reply-action";
  replyAction.textContent = "↩ Ответить";
  replyAction.addEventListener("click", () => selectReply(message));
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Поиск эмодзи: сердце, кот, smile…";
  search.setAttribute("aria-label", "Поиск эмодзи");
  const tabs = document.createElement("div");
  tabs.className = "emoji-tabs";
  const grid = document.createElement("div");
  grid.className = "emoji-grid";
  const footer = document.createElement("div");
  footer.className = "emoji-footer";
  const manual = document.createElement("div");
  manual.className = "emoji-manual";
  const manualInput = document.createElement("input");
  manualInput.type = "text";
  manualInput.placeholder = "Вставить свой эмодзи";
  manualInput.setAttribute("aria-label", "Вставить свой эмодзи");
  manualInput.maxLength = 64;
  const manualButton = document.createElement("button");
  manualButton.type = "button";
  manualButton.textContent = "ОК";
  manualButton.addEventListener("click", () => sendReaction(messageId, manualInput.value.trim()));
  manualInput.addEventListener("keydown", event => {
    if (event.key === "Enter") sendReaction(messageId, manualInput.value.trim());
  });
  manual.append(manualInput, manualButton);
  menu.append(replyAction, search, tabs, grid, footer, manual);
  let catalog = null;
  let category = "recent";
  let visibleLimit = 160;

  function render() {
    tabs.replaceChildren();
    for (const group of emojiGroups) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "emoji-tab";
      tab.textContent = group.icon;
      tab.title = group.label;
      tab.setAttribute("aria-label", group.label);
      tab.setAttribute("aria-pressed", String(group.id === category));
      tab.addEventListener("click", () => {
        category = group.id;
        search.value = "";
        visibleLimit = 160;
        render();
      });
      tabs.append(tab);
    }

    const query = search.value.trim().toLocaleLowerCase("ru");
    const words = query.split(/\s+/).filter(Boolean);
    let matches;
    if (!catalog) {
      matches = query ? [] : reactionChoices.map(emoji => ({emoji, ruName: emoji}));
    } else if (words.length) {
      matches = catalog.filter(item => words.every(word => item.search.includes(word)));
      matches.sort((a, b) => Number(b.ruName.toLocaleLowerCase("ru").startsWith(query)) -
                              Number(a.ruName.toLocaleLowerCase("ru").startsWith(query)));
    } else if (category === "recent") {
      const byEmoji = new Map(catalog.map(item => [item.emoji, item]));
      matches = [...new Set([...recentEmojis(), ...reactionChoices])]
        .map(emoji => byEmoji.get(emoji) || {emoji, ruName: emoji});
    } else {
      matches = catalog.filter(item => item.group === category);
    }

    grid.replaceChildren();
    for (const item of matches.slice(0, visibleLimit)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "emoji-option";
      button.textContent = item.emoji;
      button.title = item.ruName;
      button.setAttribute("aria-label", item.ruName);
      button.addEventListener("click", () => sendReaction(messageId, item.emoji));
      grid.append(button);
    }
    if (!matches.length) {
      const empty = document.createElement("p");
      empty.className = "emoji-empty";
      empty.textContent = catalog ? "Эмодзи не найдены" : "Загрузка списка эмодзи…";
      grid.append(empty);
    }
    footer.replaceChildren();
    const info = document.createElement("span");
    info.textContent = words.length ? `Найдено: ${matches.length}` :
      (emojiGroups.find(group => group.id === category)?.label || "Эмодзи");
    footer.append(info);
    if (matches.length > visibleLimit) {
      const more = document.createElement("button");
      more.type = "button";
      more.textContent = "Показать ещё";
      more.addEventListener("click", () => { visibleLimit += 160; render(); });
      footer.append(more);
    }
  }

  search.addEventListener("input", () => { visibleLimit = 160; render(); });
  render();
  document.body.append(menu);
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - bounds.height - 8))}px`;
  reactionMenu = menu;
  if (focusSearch) search.focus({preventScroll: true});
  loadEmojiCatalog().then(items => {
    if (reactionMenu === menu) {
      catalog = items;
      render();
    }
  }).catch(error => {
    if (reactionMenu === menu) footer.textContent = `Не удалось загрузить эмодзи: ${error.message}`;
  });
}

document.addEventListener("pointerdown", event => {
  if (reactionMenu && !reactionMenu.contains(event.target)) closeReactionMenu();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeReactionMenu();
});
document.getElementById("messages").addEventListener("scroll", closeReactionMenu);

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

async function postJson(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload), cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function feedback(message, error = false) {
  const element = document.getElementById("feedback");
  element.textContent = message;
  element.classList.toggle("error", error);
}

function selectedTransport() {
  return document.getElementById("transport").value;
}

function renderDeviceOptions() {
  const transport = selectedTransport();
  const devices = transport === "ble" ? (ui.state?.devices || []) : ui.usbDevices;
  const select = document.getElementById("devices");
  const previous = select.value;
  select.replaceChildren(new Option(devices.length ? "Выберите устройство" : "Устройств нет", ""));
  for (const device of devices) {
    select.add(new Option(`${device.name} · ${device.address}`, device.address));
  }
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  document.getElementById("manual-wrap").hidden = transport !== "ble";
}

function renderState(state) {
  ui.state = state;
  if (Array.isArray(state.map_center) && state.map_center.length === 2) {
    mapCenter = state.map_center;
    mapLocation = state.weather_location || "выбранный город";
    document.getElementById("map-location").textContent = `Ноды на карте · ${mapLocation}`;
    document.getElementById("map-reset").textContent = mapLocation;
    document.getElementById("weather-location").textContent = `Погода · ${mapLocation}`;
    document.getElementById("node-map").setAttribute("aria-label", `Карта района ${mapLocation} с нодами, передавшими координаты`);
  }
  if (!ui.transportInitialized) {
    document.getElementById("transport").value = state.selected_transport || "ble";
    ui.transportInitialized = true;
  }
  const status = document.getElementById("status");
  document.getElementById("connect-form").hidden = state.connected;
  document.getElementById("connected-panel").hidden = !state.connected;
  status.classList.toggle("online", state.connected);
  status.textContent = state.connected
    ? `${state.selected_transport === "usb" ? "USB" : "Bluetooth"} подключён · ${state.node_id}`
    : state.connecting ? "Подключение к ноде…" : "Нода отключена";
  document.getElementById("connected-node").textContent = state.node_id || "Нода";
  document.getElementById("selected-device").textContent = state.selected_device
    ? `${state.selected_transport === "usb" ? "USB" : "Bluetooth"} · ${state.selected_device}`
    : "";
  document.getElementById("scan").disabled = state.scanning;
  document.getElementById("scan").textContent = state.scanning ? "Поиск…" : "Найти";
  document.getElementById("disconnect").disabled = !state.connected;
  document.getElementById("send-button").disabled = !state.connected ||
    (ui.active.kind === "channel" && !(state.node_settings?.channels || []).some(
      channel => `#${channel.index}` === ui.active.peer && channel.role !== 0));
  document.getElementById("bot-enabled").checked = state.bot_enabled !== false;
  document.getElementById("bot-mode-status").textContent = state.bot_enabled === false
    ? "Бот выключен: новые личные сообщения сохраняются без автоответа."
    : "Бот включён: шаблоны и команды отвечают в личке.";
  if (state.error) feedback(state.error, true);
  else if (state.notice) feedback(state.notice);
  renderDeviceOptions();

  const datalist = document.getElementById("known-nodes");
  datalist.replaceChildren();
  for (const node of state.known_nodes || []) {
    const option = document.createElement("option");
    option.value = node.id;
    option.label = node.name;
    datalist.append(option);
  }
  renderNodes();
  renderNodeDetails();
  renderNodeMap();
  renderDashboard();
  renderRadioSettings(state);
}

function renderRadioSettings(state) {
  const key = state.connected ? `${state.node_id}:${state.node_settings_revision}` : "disconnected";
  if (ui.radioSettingsRevision === key) return;
  ui.radioSettingsRevision = key;
  const root = document.getElementById("radio-settings-root");
  root.replaceChildren();
  const settings = state.node_settings;
  if (!state.connected || !settings) {
    const note = document.createElement("p");
    note.className = "settings-note";
    note.textContent = state.connected ? "Чтение настроек ноды…" : "Подключите ноду, чтобы прочитать настройки.";
    root.append(note);
    return;
  }
  const grid = document.createElement("div");
  grid.className = "radio-settings-grid";
  const makeDetails = title => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = title;
    details.append(summary);
    grid.append(details);
    return details;
  };
  const makeField = (labelText, type, value, options = null, secret = false) => {
    const label = document.createElement("label");
    label.textContent = labelText;
    let input;
    if (type === "enum") {
      input = document.createElement("select");
      for (const option of options || []) {
        const choice = document.createElement("option");
        choice.value = String(option.value);
        choice.textContent = option.label.replaceAll("_", " ");
        input.append(choice);
      }
      input.value = String(value);
    } else {
      input = document.createElement("input");
      if (type === "bool") {
        input.type = "checkbox";
        input.checked = Boolean(value);
        label.classList.add("radio-check");
      } else if (type === "integer" || type === "float") {
        input.type = "number";
        input.step = type === "float" ? "any" : "1";
        input.value = String(value);
        input.required = true;
      } else {
        input.type = secret ? "password" : "text";
        input.value = secret ? "" : String(value ?? "");
        input.maxLength = 120;
        if (secret) {
          input.placeholder = "Оставьте пустым, чтобы не менять";
          input.autocomplete = "new-password";
        }
      }
    }
    label.append(input);
    return {label, input};
  };
  const readField = (field, input) => field.type === "bool" ? input.checked
    : field.type === "enum" || field.type === "integer" ? Number(input.value)
      : field.type === "float" ? Number(input.value) : input.value;
  const owner = makeDetails("Имя ноды");
  owner.open = true;
  const ownerForm = document.createElement("form");
  const ownerFields = document.createElement("div");
  ownerFields.className = "radio-fields";
  const longName = makeField("Полное имя", "string", settings.owner.long_name);
  const shortName = makeField("Короткое имя (до 4 байт)", "string", settings.owner.short_name);
  longName.input.required = true;
  shortName.input.required = true;
  ownerFields.append(longName.label, shortName.label);
  const ownerSave = document.createElement("button");
  ownerSave.type = "submit";
  ownerSave.textContent = "Сохранить имя";
  ownerForm.append(ownerFields, ownerSave);
  ownerForm.addEventListener("submit", async event => {
    event.preventDefault();
    ownerSave.disabled = true;
    try {
      await postJson("/api/node-owner", {long_name: longName.input.value.trim(),
                                         short_name: shortName.input.value.trim()});
      feedback("Имя отправляется в ноду");
    } catch (error) { feedback(error.message, true); }
    finally { ownerSave.disabled = false; }
  });
  owner.append(ownerForm);
  const positionDetails = makeDetails("Фиксированная позиция");
  const positionForm = document.createElement("form");
  const positionFields = document.createElement("div");
  positionFields.className = "radio-fields";
  const latitude = makeField("Широта", "float", settings.position?.latitude ?? "");
  const longitude = makeField("Долгота", "float", settings.position?.longitude ?? "");
  const altitude = makeField("Высота, м", "integer", settings.position?.altitude ?? 0);
  latitude.input.placeholder = "Например, 55.75";
  longitude.input.placeholder = "Например, 37.62";
  positionFields.append(latitude.label, longitude.label, altitude.label);
  const positionNote = document.createElement("p");
  positionNote.className = "settings-note";
  positionNote.textContent = "Команда включает фиксированную позицию. Чтобы вернуться к GPS, выключите «Фиксированная позиция» в группе «Геопозиция».";
  const positionSave = document.createElement("button");
  positionSave.type = "submit";
  positionSave.textContent = "Установить координаты";
  positionForm.append(positionFields, positionNote, positionSave);
  positionForm.addEventListener("submit", async event => {
    event.preventDefault();
    positionSave.disabled = true;
    try {
      await postJson("/api/node-fixed-position", {
        latitude: Number(latitude.input.value), longitude: Number(longitude.input.value),
        altitude: Number(altitude.input.value),
      });
      feedback("Фиксированные координаты отправляются в ноду");
    } catch (error) { feedback(error.message, true); }
    finally { positionSave.disabled = false; }
  });
  positionDetails.append(positionForm);
  for (const group of settings.groups) {
    const details = makeDetails(group.title);
    const form = document.createElement("form");
    const fields = document.createElement("div");
    fields.className = "radio-fields";
    const controls = new Map();
    for (const field of group.fields) {
      const control = makeField(field.label, field.type, field.value, field.options, field.secret);
      controls.set(field.name, control.input);
      fields.append(control.label);
    }
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Сохранить";
    form.append(fields, save);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const changes = {};
      for (const field of group.fields) {
        const value = readField(field, controls.get(field.name));
        if (field.secret ? value !== "" : value !== field.value) changes[field.name] = value;
      }
      if (!Object.keys(changes).length) return feedback("Изменений нет");
      save.disabled = true;
      try {
        await postJson("/api/node-settings", {section: group.section, changes});
        feedback(`${group.title}: изменения отправляются в ноду`);
      } catch (error) { feedback(error.message, true); }
      finally { save.disabled = false; }
    });
    details.append(form);
  }
  for (const channel of settings.channels) {
    const details = makeDetails(`Канал ${channel.index}: ${channel.name || "без имени"}`);
    const form = document.createElement("form");
    const fields = document.createElement("div");
    fields.className = "radio-fields";
    const role = makeField("Роль", "enum", channel.role, settings.channel_roles);
    const name = makeField("Имя (до 11 байт)", "string", channel.name);
    const uplink = makeField("Uplink в MQTT", "bool", channel.uplink_enabled);
    const downlink = makeField("Downlink из MQTT", "bool", channel.downlink_enabled);
    const psk = makeField("Новый ключ HEX (2, 32 или 64 символа)", "string", "", null, true);
    psk.input.placeholder = channel.has_psk ? "Ключ задан; оставьте пустым, чтобы сохранить" : "Ключ не задан";
    fields.append(role.label, name.label, uplink.label, downlink.label, psk.label);
    const note = document.createElement("p");
    note.className = "settings-note";
    note.textContent = "Ключ никогда не возвращается в браузер. Смена канала или ключа меняет доступ к сети.";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Сохранить канал";
    form.append(fields, note, save);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const changes = {};
      if (Number(role.input.value) !== channel.role) changes.role = Number(role.input.value);
      if (name.input.value !== channel.name) changes.name = name.input.value;
      if (uplink.input.checked !== channel.uplink_enabled) changes.uplink_enabled = uplink.input.checked;
      if (downlink.input.checked !== channel.downlink_enabled) changes.downlink_enabled = downlink.input.checked;
      if (psk.input.value.trim()) changes.psk_hex = psk.input.value.trim();
      if (!Object.keys(changes).length) return feedback("Изменений нет");
      save.disabled = true;
      try {
        await postJson("/api/node-channel", {index: channel.index, changes});
        psk.input.value = "";
        feedback(`Канал ${channel.index}: изменения отправляются в ноду`);
      } catch (error) { feedback(error.message, true); }
      finally { save.disabled = false; }
    });
    details.append(form);
  }
  root.append(grid);
}

function peerName(peer) {
  return (ui.state?.known_nodes || []).find(node => node.id === peer)?.name || peer;
}

function formatNodeTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString("ru-RU");
}

function showNodeDetails(nodeId) {
  ui.selectedNodeId = nodeId;
  ui.nodeTraces = [];
  ui.nodeTracesTarget = nodeId;
  renderNodeDetails();
  const dialog = document.getElementById("node-details");
  if (!dialog.open) dialog.showModal();
  loadNodeTraces(nodeId).catch(error => feedback(error.message, true));
}

function renderNodeDetails() {
  if (!ui.selectedNodeId) return;
  const node = (ui.state?.known_nodes || []).find(item => item.id === ui.selectedNodeId);
  const dialog = document.getElementById("node-details");
  if (!node) {
    if (dialog.open) dialog.close();
    ui.selectedNodeId = null;
    return;
  }
  document.getElementById("node-details-title").textContent = node.name;
  document.getElementById("node-details-subtitle").textContent = node.is_self
    ? `${node.id} · моя нода` : node.id;
  document.getElementById("node-open-chat").hidden = node.is_self;
  document.getElementById("node-trace-start").hidden = node.is_self;
  document.getElementById("node-trace-start").disabled = !ui.state?.connected ||
    ui.nodeTraces.some(trace => ["queued", "running"].includes(trace.status));
  const content = document.getElementById("node-details-content");
  content.replaceChildren();
  const section = (title, rows) => {
    const group = document.createElement("section");
    group.className = "node-detail-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("dl");
    for (const [label, value] of rows) {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value === null || value === undefined || value === ""
        ? "Нет данных" : String(value);
      list.append(term, description);
    }
    group.append(heading, list);
    content.append(group);
  };
  const fmt = (value, suffix, digits = 1) => Number.isFinite(value)
    ? `${Number(value.toFixed(digits))}${suffix}` : null;
  const metrics = node.device_metrics || {};
  const battery = Number.isFinite(metrics.battery_level)
    ? metrics.battery_level > 100 ? "Внешнее питание" : `${metrics.battery_level}%` : null;
  const uptime = Number.isFinite(metrics.uptime_seconds) && metrics.uptime_seconds >= 0
    ? `${Math.floor(metrics.uptime_seconds / 86400)} д ${Math.floor(metrics.uptime_seconds % 86400 / 3600)} ч ${Math.floor(metrics.uptime_seconds % 3600 / 60)} мин`
    : null;
  section("Идентификация", [
    ["ID", node.id], ["Короткое имя", node.short_name],
    ["Модель", node.hardware_model], ["Роль", node.role],
  ]);
  section("Связь", [
    ["Последний сигнал", formatNodeTime(node.last_heard)],
    ["Хопы", node.hops_away], ["SNR", fmt(node.snr, " дБ")],
    ["RSSI", fmt(node.rssi, " dBm", 0)], ["Канал", node.channel],
  ]);
  section("Питание и телеметрия", [
    ["Батарея", battery], ["Напряжение", fmt(metrics.voltage, " В", 2)],
    ["Загрузка канала", fmt(metrics.channel_utilization, "%")],
    ["Передача", fmt(metrics.air_util_tx, "%")], ["Время работы", uptime],
  ]);
  section("Геопозиция", [
    ["Координаты", node.position ? `${node.position.lat.toFixed(5)}, ${node.position.lon.toFixed(5)}` : null],
    ["Высота", fmt(node.position?.altitude, " м", 0)],
    ["Обновлено", formatNodeTime(node.position?.time)],
  ]);
  renderTraceList(document.getElementById("node-trace-list"), ui.nodeTraces, false);
}

function renderTraceList(container, traces, showTarget) {
  container.replaceChildren();
  if (!traces.length) {
    const empty = document.createElement("p");
    empty.className = "node-empty";
    empty.textContent = "Трассировок пока нет";
    container.append(empty);
    return;
  }
  const statusNames = {queued: "В очереди", running: "Выполняется", success: "Готово",
    failed: "Ошибка", timeout: "Нет ответа", interrupted: "Прервано"};
  for (const trace of traces) {
    const entry = document.createElement("article");
    entry.className = "trace-entry";
    const title = document.createElement("strong");
    title.textContent = showTarget ? `${peerName(trace.target)} · ${trace.target}` :
      `Трассировка #${trace.id}`;
    const meta = document.createElement("small");
    const time = new Date(trace.requested_at);
    meta.textContent = `${statusNames[trace.status] || trace.status} · ${
      Number.isNaN(time.getTime()) ? trace.requested_at : time.toLocaleString("ru-RU")
    } · ${trace.source === "bot" ? "команда в личке" : "веб-интерфейс"}`;
    entry.append(title, document.createElement("br"), meta);
    if (trace.forward_route?.length) {
      const forward = document.createElement("p");
      forward.className = "trace-route";
      forward.textContent = `Туда: ${trace.forward_route.join(" → ")}`;
      entry.append(forward);
    }
    if (trace.reverse_route?.length) {
      const backward = document.createElement("p");
      backward.className = "trace-route";
      backward.textContent = `Обратно: ${trace.reverse_route.join(" → ")}`;
      entry.append(backward);
    } else if (trace.status === "success") {
      const absent = document.createElement("p");
      absent.className = "node-empty";
      absent.textContent = "Обратный маршрут нода не сообщила";
      entry.append(absent);
    }
    if (trace.error) {
      const error = document.createElement("p");
      error.className = "trace-error";
      error.textContent = trace.error;
      entry.append(error);
    }
    container.append(entry);
  }
}

async function loadTraceHistory() {
  const result = await getJson("/api/traces");
  ui.traces = result.traces;
  renderTraceList(document.getElementById("trace-history-list"), ui.traces, true);
}

async function loadNodeTraces(nodeId) {
  const result = await getJson(`/api/traces?target=${encodeURIComponent(nodeId)}`);
  if (ui.selectedNodeId !== nodeId) return;
  ui.nodeTracesTarget = nodeId;
  ui.nodeTraces = result.traces;
  renderTraceList(document.getElementById("node-trace-list"), ui.nodeTraces, false);
  document.getElementById("node-trace-start").disabled = !ui.state?.connected ||
    ui.nodeTraces.some(trace => ["queued", "running"].includes(trace.status));
}

function renderNodes() {
  const nodes = ui.state?.known_nodes || [];
  const query = document.getElementById("node-search").value.trim().toLocaleLowerCase("ru-RU");
  const visible = nodes.filter(node =>
    `${node.name} ${node.short_name} ${node.id}`.toLocaleLowerCase("ru-RU").includes(query));
  document.getElementById("node-count").textContent = query
    ? `${visible.length} из ${nodes.length}` : String(nodes.length);
  const list = document.getElementById("node-list");
  list.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "node-empty";
    empty.textContent = nodes.length ? "Совпадений нет" :
      "Список пуст. Подключите ноду и дождитесь данных от сети.";
    list.append(empty);
    return;
  }
  for (const node of visible) {
    const item = document.createElement("button");
    item.className = "node-item";
    item.type = "button";
    item.title = `Информация о ноде ${node.name}`;
    item.addEventListener("click", () => showNodeDetails(node.id));
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = node.is_self ? `${node.name} · моя нода` : node.name;
    const detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent = [node.id, node.short_name || null,
      node.hops_away == null ? null : `${node.hops_away} хоп(ов)`,
      node.last_heard ? new Date(node.last_heard * 1000).toLocaleString("ru-RU") : null]
      .filter(Boolean).join(" · ");
    item.append(name, detail);
    list.append(item);
  }
}

function renderChatList() {
  const channelList = document.getElementById("chat-list");
  const dmList = document.getElementById("dm-list");
  channelList.replaceChildren();
  dmList.replaceChildren();
  const makeButton = (list, label, preview, kind, peer, unread = 0) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-item";
    button.classList.toggle("active", ui.active.kind === kind && ui.active.peer === peer);
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = label;
    const excerpt = document.createElement("span");
    excerpt.className = "preview";
    excerpt.textContent = preview;
    const nameRow = document.createElement("span");
    nameRow.className = "name-row";
    nameRow.append(name);
    if (unread) {
      const badge = document.createElement("span");
      badge.className = "unread-badge";
      badge.textContent = unread > 99 ? "99+" : String(unread);
      badge.setAttribute("aria-label", `${unread} непрочитанных`);
      nameRow.append(badge);
    }
    button.classList.toggle("has-unread", unread > 0);
    button.append(nameRow, excerpt);
    button.addEventListener("click", () => selectChat(kind, peer));
    list.append(button);
  };
  const channelItems = [{label: "LongFast", preview: "Основной канал", kind: "longfast",
    peer: null, unread: ui.longfastUnread},
    ...ui.channels.map(channel => ({label: channel.name || `Канал ${channel.index}`,
      preview: channel.active ? channel.preview : "История · канал неактивен",
      kind: "channel", peer: `#${channel.index}`, unread: channel.unread || 0}))];
  const dmItems = ui.chats.map(chat => ({label: peerName(chat.peer), preview: chat.text,
    kind: "dm", peer: chat.peer, unread: chat.unread || 0}));
  if (ui.active.kind === "dm" && !ui.chats.some(chat => chat.peer === ui.active.peer)) {
    dmItems.push({label: peerName(ui.active.peer), preview: "Новый диалог", kind: "dm",
      peer: ui.active.peer, unread: 0});
  }
  const renderGroups = (list, items) => {
    const unread = items.filter(item => item.unread);
    const read = items.filter(item => !item.unread);
    for (const [label, groupItems] of [["Непрочитанные", unread],
                                       [unread.length ? "Прочитанные" : "Все чаты", read]]) {
      if (!groupItems.length) continue;
      const group = document.createElement("section");
      group.className = "chat-group";
      const heading = document.createElement("div");
      heading.className = "chat-group-label";
      heading.textContent = label;
      const row = document.createElement("div");
      row.className = "chat-group-items";
      for (const item of groupItems) {
        makeButton(row, item.label, item.preview, item.kind, item.peer, item.unread);
      }
      group.append(heading, row);
      list.append(group);
    }
  };
  renderGroups(channelList, channelItems);
  renderGroups(dmList, dmItems);
  document.getElementById("channel-panel").hidden = ui.chatSection !== "channels";
  document.getElementById("dm-panel").hidden = ui.chatSection !== "dms";
  document.getElementById("show-channels").setAttribute("aria-selected", ui.chatSection === "channels");
  document.getElementById("show-dms").setAttribute("aria-selected", ui.chatSection === "dms");
  const free = (ui.state?.node_settings?.channels || []).some(channel => channel.index > 0 && channel.role === 0);
  document.getElementById("open-create-channel").disabled = !ui.state?.connected || !free || ui.creatingChannel;
  document.getElementById("create-channel-button").disabled = !ui.state?.connected || !free || ui.creatingChannel;
  const total = ui.longfastUnread + ui.channels.reduce((sum, channel) => sum + (channel.unread || 0), 0) +
    ui.chats.reduce((sum, chat) => sum + (chat.unread || 0), 0);
  const tabBadge = document.getElementById("chats-unread");
  tabBadge.hidden = total === 0;
  tabBadge.textContent = total > 99 ? "99+" : String(total);
}

function renderChatTitle() {
  const isDm = ui.active.kind === "dm";
  const index = ui.active.kind === "channel" ? Number(ui.active.peer?.slice(1)) : null;
  const channel = ui.channels.find(item => item.index === index);
  document.getElementById("chat-title").textContent = isDm ? peerName(ui.active.peer)
    : ui.active.kind === "channel" ? (channel?.name || `Канал ${index}`) : "LongFast";
  document.getElementById("chat-subtitle").textContent = isDm
    ? `Личный диалог · ${ui.active.peer} · ПКМ/удержание: ответить или реакция`
    : `Общий канал${index == null ? "" : ` · слот ${index}`}${channel && !channel.active ? " · неактивен" : ""} · ПКМ/удержание: ответить или реакция`;
  const configButton = document.getElementById("open-channel-config");
  configButton.hidden = isDm;
  configButton.disabled = !ui.state?.connected;
}

function renderReplyComposer() {
  const preview = document.getElementById("reply-preview");
  preview.hidden = !ui.replyTo;
  if (!ui.replyTo) return;
  const ownId = ui.state?.node_id || ui.state?.last_node_id;
  document.getElementById("reply-author").textContent =
    ui.replyTo.sender === ownId ? "Вы" : peerName(ui.replyTo.sender);
  document.getElementById("reply-text").textContent = ui.replyTo.text;
}

function selectReply(message) {
  ui.replyTo = {id: message.id, sender: message.sender, text: message.text};
  closeReactionMenu();
  renderReplyComposer();
  document.getElementById("message-text").focus();
}

function deliveryLabel(message) {
  const channel = ui.active.kind !== "dm";
  const labels = channel
    ? {pending: "Ожидание ретрансляции", relayed: "Ретрансляция подтверждена",
       failed: "Передача не удалась", unconfirmed: "Ретрансляция не подтверждена"}
    : {pending: "Ожидание доставки", relayed: "В сети · адресат не подтвердил",
       delivered: "Доставлено", failed: "Не доставлено",
       unconfirmed: "Доставка не подтверждена"};
  return labels[message.delivery_status] || "Статус неизвестен";
}

function renderMessages(scrollMode = "keep") {
  const list = document.getElementById("messages");
  const oldHeight = list.scrollHeight;
  const oldTop = list.scrollTop;
  const atBottom = oldHeight - oldTop - list.clientHeight < 90;
  list.replaceChildren();
  const messages = [...ui.messages.values()].sort((a, b) =>
    a.received_at.localeCompare(b.received_at) || a.id - b.id);
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Сообщений пока нет";
    list.append(empty);
  }
  for (const message of messages) {
    if (message.id === ui.firstUnreadId) {
      const divider = document.createElement("div");
      divider.id = "unread-divider";
      divider.className = "unread-divider";
      divider.textContent = "Непрочитанные сообщения";
      list.append(divider);
    }
    const article = document.createElement("article");
    article.className = "bubble";
    article.dataset.messageId = String(message.id);
    article.classList.toggle("unread", ui.unreadIds.has(message.id));
    article.classList.toggle("mine", message.sender === (ui.state?.node_id || "") ||
      message.sender === (ui.state?.last_node_id || ""));
    const meta = document.createElement("div");
    meta.className = "meta";
    const sender = document.createElement("strong");
    sender.textContent = article.classList.contains("mine") ? "Вы" : peerName(message.sender);
    const time = document.createElement("time");
    time.dateTime = message.received_at;
    time.textContent = new Date(message.received_at).toLocaleString("ru-RU");
    meta.append(sender, time);
    if (message.hops !== null) {
      const hops = document.createElement("span");
      hops.textContent = `${message.hops} хоп(ов)`;
      meta.append(hops);
    }
    const body = document.createElement("p");
    body.className = "text";
    body.textContent = message.text;
    article.append(meta);
    if (message.reply_to) {
      const quote = document.createElement("div");
      quote.className = "reply-quote";
      const author = document.createElement("strong");
      const ownId = ui.state?.node_id || ui.state?.last_node_id;
      author.textContent = message.reply_to.sender
        ? (message.reply_to.sender === ownId ? "Вы" : peerName(message.reply_to.sender))
        : "Ответ на сообщение";
      const excerpt = document.createElement("span");
      excerpt.textContent = message.reply_to.text
        ? message.reply_to.text.slice(0, 140)
        : "Исходное сообщение недоступно";
      quote.append(author, excerpt);
      article.append(quote);
    }
    article.append(body);
    if (message.can_react && ui.state?.connected &&
        (ui.active.kind !== "channel" || (ui.state.node_settings?.channels || []).some(
          channel => `#${channel.index}` === ui.active.peer && channel.role !== 0))) {
      let touchStart = null;
      article.title = "Правая кнопка мыши или долгое нажатие — ответить или реакция";
      article.addEventListener("contextmenu", event => {
        event.preventDefault();
        openReactionMenu(message, event.clientX, event.clientY, event.pointerType === "mouse");
      });
      article.addEventListener("pointerdown", event => {
        if (event.pointerType !== "touch") return;
        const startX = event.clientX;
        const startY = event.clientY;
        touchStart = {x: startX, y: startY};
        clearTimeout(longPressTimer);
        longPressTimer = setTimeout(() => {
          openReactionMenu(message, startX, startY);
          longPressTimer = null;
        }, 550);
      });
      article.addEventListener("pointermove", event => {
        if (event.pointerType === "touch" && longPressTimer && touchStart &&
            (Math.abs(event.clientX - touchStart.x) > 8 ||
             Math.abs(event.clientY - touchStart.y) > 8)) {
          clearTimeout(longPressTimer);
          longPressTimer = null;
          touchStart = null;
        }
      });
      for (const eventName of ["pointerup", "pointercancel", "pointerleave"]) {
        article.addEventListener(eventName, () => {
          clearTimeout(longPressTimer);
          longPressTimer = null;
          touchStart = null;
        });
      }
    }
    const reactionBar = document.createElement("div");
    reactionBar.className = "reaction-bar";
    const counts = new Map();
    for (const reaction of message.reactions || []) {
      const item = counts.get(reaction.emoji) || {count: 0, mine: false};
      item.count += 1;
      item.mine ||= reaction.sender === (ui.state?.node_id || ui.state?.last_node_id);
      counts.set(reaction.emoji, item);
    }
    for (const [emoji, item] of counts) {
      const chip = document.createElement("span");
      chip.className = `reaction-chip${item.mine ? " mine" : ""}`;
      chip.textContent = `${emoji} ${item.count}`;
      chip.title = `${item.count} реакций`;
      reactionBar.append(chip);
    }
    if (reactionBar.childNodes.length) article.append(reactionBar);
    if (article.classList.contains("mine")) {
      const delivery = document.createElement("span");
      delivery.className = `delivery ${message.delivery_status || "unknown"}`;
      delivery.textContent = deliveryLabel(message);
      if (message.delivery_error) delivery.title = `Meshtastic: ${message.delivery_error}`;
      article.append(delivery);
    }
    list.append(article);
  }
  document.getElementById("history").hidden = !ui.hasMore;
  document.getElementById("newer").hidden = !ui.hasNewer;
  if (scrollMode === "older") list.scrollTop = oldTop + list.scrollHeight - oldHeight;
  else if (scrollMode === "next") list.scrollTop = Math.max(0, oldHeight - list.clientHeight + 30);
  else if (scrollMode === "initial" && ui.firstUnreadId) {
    const divider = document.getElementById("unread-divider");
    if (divider) list.scrollTop += divider.getBoundingClientRect().top - list.getBoundingClientRect().top - 8;
  } else if (scrollMode === "bottom" || (scrollMode === "initial" && !ui.firstUnreadId) ||
             (scrollMode !== "initial" && atBottom && !ui.hasNewer)) {
    list.scrollTop = list.scrollHeight;
  }
  else list.scrollTop = oldTop;
  queueVisibleRead();
}

function queueVisibleRead() {
  clearTimeout(readTimer);
  readTimer = setTimeout(markVisibleRead, 180);
}

async function refreshChatCounts() {
  const [chats, channels] = await Promise.all([getJson("/api/chats"), getJson("/api/channels")]);
  ui.chats = chats.chats;
  ui.channels = channels.channels;
  ui.longfastUnread = channels.longfast_unread || 0;
  renderChatList();
}

async function markVisibleRead() {
  if (ui.tab !== "chats" || document.hidden || readInFlight) return;
  const list = document.getElementById("messages");
  const viewport = list.getBoundingClientRect();
  const unread = ui.active.kind === "dm"
    ? ui.chats.find(chat => chat.peer === ui.active.peer)?.unread || 0
    : ui.active.kind === "longfast" ? ui.longfastUnread
    : ui.channels.find(channel => `#${channel.index}` === ui.active.peer)?.unread || 0;
  const atLatestBottom = !ui.hasNewer && !ui.loadingNewer &&
    list.scrollHeight - list.scrollTop - list.clientHeight <= 24 &&
    viewport.bottom > 0 && viewport.bottom <= window.innerHeight + 12;
  if (atLatestBottom && (unread || ui.unreadIds.size)) {
    const version = ui.version;
    const {kind, peer} = ui.active;
    readInFlight = true;
    try {
      await postJson("/api/read-all", {kind, peer});
      if (version === ui.version) {
        ui.unreadIds.clear();
        ui.firstUnreadId = null;
        list.querySelectorAll(".bubble.unread").forEach(article => article.classList.remove("unread"));
        document.getElementById("unread-divider")?.remove();
      }
      await refreshChatCounts();
    } catch (error) {
      feedback(`Не удалось отметить прочитанное: ${error.message}`, true);
    } finally {
      readInFlight = false;
      if (version === ui.version && ui.unreadIds.size) queueVisibleRead();
    }
    return;
  }
  if (!ui.unreadIds.size) return;
  const ids = [];
  for (const article of list.querySelectorAll(".bubble.unread")) {
    const rect = article.getBoundingClientRect();
    const overlap = Math.min(rect.bottom, viewport.bottom, window.innerHeight) -
      Math.max(rect.top, viewport.top, 0);
    if (overlap >= Math.min(rect.height / 2, 30)) ids.push(Number(article.dataset.messageId));
  }
  if (!ids.length) return;
  const version = ui.version;
  const {kind, peer} = ui.active;
  readInFlight = true;
  for (const id of ids) ui.unreadIds.delete(id);
  for (const article of list.querySelectorAll(".bubble.unread")) {
    if (ids.includes(Number(article.dataset.messageId))) article.classList.remove("unread");
  }
  try {
    await postJson("/api/read", {kind, peer, ids});
    await refreshChatCounts();
  } catch (error) {
    if (version === ui.version) for (const id of ids) ui.unreadIds.add(id);
    feedback(`Не удалось отметить прочитанное: ${error.message}`, true);
  } finally {
    readInFlight = false;
    if (ui.unreadIds.size || (unread && atLatestBottom)) queueVisibleRead();
  }
}

async function sendReaction(messageId, emoji) {
  try {
    await postJson("/api/react", {kind: ui.active.kind, peer: ui.active.peer,
                                  message_id: messageId, emoji});
    rememberEmoji(emoji);
    closeReactionMenu();
    feedback("Реакция передаётся в радио");
    setTimeout(() => loadMessages("poll").catch(error => feedback(error.message, true)), 1000);
  } catch (error) { feedback(error.message, true); }
}

async function loadMessages(mode = "poll") {
  const version = ui.version;
  const {kind, peer} = ui.active;
  let path = `/api/messages?kind=${kind}`;
  if (peer) path += `&peer=${encodeURIComponent(peer)}`;
  if (mode === "older") {
    const oldest = [...ui.messages.values()].sort((a, b) =>
      a.received_at.localeCompare(b.received_at) || a.id - b.id)[0];
    if (!oldest) return;
    path += `&before=${oldest.id}`;
  } else if (mode === "newer") {
    const newest = [...ui.messages.values()].sort((a, b) =>
      a.received_at.localeCompare(b.received_at) || a.id - b.id).at(-1);
    if (newest) path += `&after=${newest.id}`;
  } else if (mode === "initial") {
    path += "&unread=1";
  }
  const result = await getJson(path);
  if (version !== ui.version) return;
  if (mode === "initial") {
    ui.messages.clear();
    ui.unreadIds.clear();
    ui.firstUnreadId = result.first_unread_id;
  } else if (!ui.firstUnreadId && result.first_unread_id) {
    ui.firstUnreadId = result.first_unread_id;
  }
  for (const message of result.messages) {
    ui.messages.set(message.id, message);
    if (message.unread) ui.unreadIds.add(message.id);
    else ui.unreadIds.delete(message.id);
  }
  if (mode === "initial" || mode === "older") ui.hasMore = result.has_more;
  if (mode === "initial" || mode === "newer" || ui.hasNewer) ui.hasNewer = result.has_newer;
  renderMessages(mode === "older" ? "older" : mode === "initial" ? "initial" :
    mode === "newer" ? "next" : "keep");
}

function selectChat(kind, peer) {
  setTab("chats");
  ui.chatSection = kind === "dm" ? "dms" : "channels";
  if (ui.active.kind === kind && ui.active.peer === peer) return;
  closeReactionMenu();
  ui.replyTo = null;
  renderReplyComposer();
  ui.active = {kind, peer};
  document.getElementById("send-button").disabled = !ui.state?.connected ||
    (kind === "channel" && !(ui.state?.node_settings?.channels || []).some(
      channel => `#${channel.index}` === peer && channel.role !== 0));
  ui.version += 1;
  ui.messages.clear();
  ui.hasMore = false;
  ui.hasNewer = false;
  ui.unreadIds.clear();
  ui.firstUnreadId = null;
  renderChatTitle();
  renderChatList();
  renderMessages("bottom");
  loadMessages("initial").catch(error => feedback(error.message, true));
  if (matchMedia("(max-width: 800px)").matches) {
    document.querySelector(".chat").scrollIntoView({behavior: "smooth", block: "start"});
  }
}

async function refresh() {
  try {
    const [state, chats, channels] = await Promise.all([
      getJson("/api/state"), getJson("/api/chats"), getJson("/api/channels")]);
    renderState(state);
    ui.chats = chats.chats;
    ui.channels = channels.channels;
    ui.longfastUnread = channels.longfast_unread || 0;
    renderDashboard();
    renderChatTitle();
    renderChatList();
    if (!ui.hasNewer || !ui.messages.size) {
      await loadMessages(ui.messages.size ? "poll" : "initial");
    }
    await loadTraceHistory();
    if (ui.selectedNodeId) await loadNodeTraces(ui.selectedNodeId);
  } catch (error) { feedback(`Ошибка обновления: ${error.message}`, true); }
}

async function refreshUsb() {
  const result = await getJson("/api/ports");
  ui.usbDevices = result.devices;
  renderDeviceOptions();
}

async function loadWeather(force = false) {
  const button = document.getElementById("weather-refresh");
  button.disabled = true;
  try {
    const {weather} = await getJson(`/api/weather${force ? "?refresh=1" : ""}`);
    document.getElementById("weather-temp").textContent = `${Math.round(weather.temperature)}°`;
    document.getElementById("weather-condition").textContent = weather.condition;
    const details = [];
    if (typeof weather.feels_like === "number") details.push(`ощущается как ${Math.round(weather.feels_like)}°`);
    if (typeof weather.wind_speed === "number") details.push(`ветер ${Math.round(weather.wind_speed)} км/ч`);
    if (typeof weather.humidity === "number") details.push(`влажность ${Math.round(weather.humidity)}%`);
    document.getElementById("weather-details").textContent = details.join(" · ");
    document.getElementById("weather-time").textContent =
      `${weather.observed_at.replace("T", " ")} ${weather.time_label || ""}${weather.stale ? " · кешированные данные" : ""}`;
  } catch (error) {
    document.getElementById("weather-temp").textContent = "—";
    document.getElementById("weather-condition").textContent = "Погода временно недоступна";
    document.getElementById("weather-details").textContent = "Попробуйте обновить позже";
    document.getElementById("weather-time").textContent = "Нет данных";
  } finally {
    button.disabled = false;
  }
}

async function loadTemplates() {
  const result = await getJson("/api/templates");
  const list = document.getElementById("template-list");
  list.replaceChildren();
  for (const template of result.templates) {
    const row = document.createElement("div");
    row.className = "template-row";
    const key = document.createElement("strong");
    key.textContent = template.key;
    const value = document.createElement("span");
    value.textContent = template.value;
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "Изменить";
    edit.addEventListener("click", () => {
      ui.editingTemplate = template.id;
      document.getElementById("template-key").value = template.key;
      document.getElementById("template-value").value = template.value;
      document.getElementById("template-cancel").hidden = false;
      document.getElementById("template-key").focus();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Удалить";
    remove.addEventListener("click", async () => {
      try { await postJson("/api/templates/delete", {id: template.id}); await loadTemplates(); }
      catch (error) { feedback(error.message, true); }
    });
    row.append(key, value, edit, remove);
    list.append(row);
  }
  if (!result.templates.length) list.textContent = "Шаблонов пока нет";
}

document.getElementById("transport").addEventListener("change", () => {
  renderDeviceOptions();
  if (selectedTransport() === "usb") refreshUsb().catch(error => feedback(error.message, true));
});

document.getElementById("node-search").addEventListener("input", renderNodes);
document.getElementById("node-details-close").addEventListener("click", () =>
  document.getElementById("node-details").close());
document.getElementById("node-details").addEventListener("close", () => { ui.selectedNodeId = null; });
document.getElementById("node-open-chat").addEventListener("click", () => {
  const peer = ui.selectedNodeId;
  document.getElementById("node-details").close();
  if (peer) selectChat("dm", peer);
});
document.getElementById("node-trace-start").addEventListener("click", async () => {
  const target = ui.selectedNodeId;
  if (!target) return;
  const button = document.getElementById("node-trace-start");
  button.disabled = true;
  try {
    await postJson("/api/traces", {target});
    feedback(`Трассировка до ${target} поставлена в очередь`);
    await Promise.all([loadTraceHistory(), loadNodeTraces(target)]);
  } catch (error) { feedback(error.message, true); }
  finally {
    button.disabled = !ui.state?.connected ||
      ui.nodeTraces.some(trace => ["queued", "running"].includes(trace.status));
  }
});
document.getElementById("map-reset").addEventListener("click", () => {
  if (nodeMap && mapCenter) nodeMap.setView(mapCenter, 10);
});
document.getElementById("map-fit").addEventListener("click", () => {
  if (nodeMap && nodeMapBounds?.isValid()) nodeMap.fitBounds(nodeMapBounds, {padding: [30, 30], maxZoom: 14});
});
document.getElementById("weather-refresh").addEventListener("click", () => loadWeather(true));
document.getElementById("refresh-nodes").addEventListener("click", async () => {
  try {
    const state = await getJson("/api/state");
    renderState(state);
    renderChatList();
    renderChatTitle();
    feedback(`Список нод обновлён: ${state.known_nodes.length}`);
  } catch (error) { feedback(error.message, true); }
});

document.getElementById("scan").addEventListener("click", async () => {
  try {
    if (selectedTransport() === "usb") {
      await refreshUsb();
      feedback(ui.usbDevices.length ? "USB-порты обновлены" : "USB-порты не найдены");
    } else {
      await postJson("/api/scan");
      feedback("Поиск Bluetooth запущен");
    }
  } catch (error) { feedback(error.message, true); }
});

document.getElementById("connect-form").addEventListener("submit", async event => {
  event.preventDefault();
  const transport = selectedTransport();
  const device = document.getElementById("devices").value ||
    (transport === "ble" ? document.getElementById("device-manual").value.trim() : "");
  try { await postJson("/api/connect", {transport, device}); feedback("Подключение запущено"); await refresh(); }
  catch (error) { feedback(error.message, true); }
});

document.getElementById("disconnect").addEventListener("click", async () => {
  try { await postJson("/api/disconnect"); feedback("Отключение запущено"); await refresh(); }
  catch (error) { feedback(error.message, true); }
});

document.getElementById("new-chat-form").addEventListener("submit", event => {
  event.preventDefault();
  const peer = document.getElementById("new-peer").value.trim();
  if (!/^![0-9a-fA-F]{8}$/.test(peer)) return feedback("ID должен иметь вид !1234abcd", true);
  selectChat("dm", peer.toLowerCase());
  document.getElementById("new-peer").value = "";
});

function randomChannelKey() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function hexToBase64(hex) {
  return btoa((hex.match(/../g) || []).map(byte => String.fromCharCode(parseInt(byte, 16))).join(""));
}

function generateChannelKey() {
  document.getElementById("channel-key").value = hexToBase64(randomChannelKey());
}

document.getElementById("show-channels").addEventListener("click", () => {
  ui.chatSection = "channels";
  renderChatList();
});
document.getElementById("show-dms").addEventListener("click", () => {
  ui.chatSection = "dms";
  renderChatList();
});
const createChannelDialog = document.getElementById("create-channel-dialog");
function closeCreateChannelDialog() {
  if (!ui.creatingChannel) createChannelDialog.close();
}
document.getElementById("open-create-channel").addEventListener("click", () => {
  document.getElementById("create-channel-form").reset();
  document.getElementById("create-channel-error").hidden = true;
  generateChannelKey();
  createChannelDialog.showModal();
  document.getElementById("channel-name").focus();
});
document.getElementById("close-create-channel").addEventListener("click", closeCreateChannelDialog);
document.getElementById("cancel-create-channel").addEventListener("click", closeCreateChannelDialog);
createChannelDialog.addEventListener("cancel", event => {
  if (ui.creatingChannel) event.preventDefault();
});
createChannelDialog.addEventListener("close", () => {
  document.getElementById("channel-key").value = "";
});
document.getElementById("generate-channel-key").addEventListener("click", generateChannelKey);
document.getElementById("create-channel-form").addEventListener("submit", async event => {
  event.preventDefault();
  const errorMessage = document.getElementById("create-channel-error");
  const showError = message => {
    errorMessage.textContent = message;
    errorMessage.hidden = false;
  };
  errorMessage.hidden = true;
  const name = document.getElementById("channel-name").value.trim();
  const psk_base64 = document.getElementById("channel-key").value.trim();
  if (new TextEncoder().encode(name).length >= 12 || !name) {
    return showError("Имя канала: от 1 до 11 байт UTF-8");
  }
  try {
    const raw = atob(psk_base64);
    if (btoa(raw) !== psk_base64 ||
        !([16, 32].includes(raw.length) || (raw.length === 1 && raw.charCodeAt(0) <= 10))) {
      throw new Error("Неверная длина ключа");
    }
  } catch {
    return showError("Ключ должен быть Base64 для 1, 16 или 32 байт (например, AQ==)");
  }
  ui.creatingChannel = true;
  renderChatList();
  try {
    const result = await postJson("/api/channels/create", {name, psk_base64});
    createChannelDialog.close();
    feedback(`Канал ${name} создаётся в слоте ${result.index}. Сохраните ключ для участников.`);
    for (let attempt = 0; attempt < 8; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1200));
      await refresh();
      if (ui.channels.some(channel => channel.index === result.index && channel.active)) {
        selectChat("channel", `#${result.index}`);
        document.getElementById("channel-name").value = "";
        return;
      }
    }
    feedback("Команда отправлена в ноду. Проверьте её статус и обновите страницу.");
  } catch (error) { showError(error.message); }
  finally { ui.creatingChannel = false; renderChatList(); }
});

const channelConfigDialog = document.getElementById("channel-config");
document.getElementById("open-channel-config").addEventListener("click", async () => {
  const index = ui.active.kind === "channel" ? Number(ui.active.peer.slice(1))
    : ui.state?.longfast_channel_index ?? 0;
  const button = document.getElementById("open-channel-config");
  button.disabled = true;
  try {
    const config = await getJson(`/api/channel-config?index=${index}`);
    ui.channelConfig = {index, name: config.name, role: config.role,
      uplink_enabled: config.uplink_enabled, downlink_enabled: config.downlink_enabled};
    document.getElementById("channel-config-title").textContent =
      `Конфигурация: ${config.name || (index === 0 ? "LongFast" : `Канал ${index}`)}`;
    document.getElementById("channel-config-subtitle").textContent = `Слот ${index} · подключённая Heltec`;
    document.getElementById("channel-config-name").value = config.name;
    const role = document.getElementById("channel-config-role");
    role.replaceChildren();
    for (const [value, label] of (index === 0
      ? [[1, "Основной"]] : [[2, "Дополнительный"], [0, "Отключён"]])) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = label;
      role.append(option);
    }
    role.value = String(config.role);
    document.getElementById("channel-config-uplink").checked = config.uplink_enabled;
    document.getElementById("channel-config-downlink").checked = config.downlink_enabled;
    const currentKey = document.getElementById("channel-config-current-key");
    const currentKeyBase64 = document.getElementById("channel-config-current-key-base64");
    currentKey.type = "password";
    currentKey.value = config.psk_hex;
    currentKey.placeholder = config.psk_hex ? "" : "Ключ не задан";
    currentKeyBase64.type = "password";
    currentKeyBase64.value = hexToBase64(config.psk_hex);
    currentKeyBase64.placeholder = config.psk_hex ? "" : "Ключ не задан";
    document.getElementById("channel-config-reveal").textContent = "Показать ключ";
    document.getElementById("channel-config-new-key").value = "";
    channelConfigDialog.showModal();
  } catch (error) { feedback(error.message, true); }
  finally { button.disabled = !ui.state?.connected; }
});
document.getElementById("close-channel-config").addEventListener("click", () => channelConfigDialog.close());
document.getElementById("channel-config-cancel").addEventListener("click", () => channelConfigDialog.close());
channelConfigDialog.addEventListener("close", () => {
  ui.channelConfig = null;
  document.getElementById("channel-config-current-key").value = "";
  document.getElementById("channel-config-current-key-base64").value = "";
  document.getElementById("channel-config-new-key").value = "";
});
document.getElementById("channel-config-reveal").addEventListener("click", () => {
  const field = document.getElementById("channel-config-current-key");
  field.type = field.type === "password" ? "text" : "password";
  document.getElementById("channel-config-current-key-base64").type = field.type;
  document.getElementById("channel-config-reveal").textContent =
    field.type === "password" ? "Показать ключ" : "Скрыть ключ";
});
document.getElementById("channel-config-generate").addEventListener("click", () => {
  document.getElementById("channel-config-new-key").value = randomChannelKey();
});
document.getElementById("channel-config-form").addEventListener("submit", async event => {
  event.preventDefault();
  const original = ui.channelConfig;
  if (!original) return;
  const name = document.getElementById("channel-config-name").value.trim();
  const role = Number(document.getElementById("channel-config-role").value);
  const uplink = document.getElementById("channel-config-uplink").checked;
  const downlink = document.getElementById("channel-config-downlink").checked;
  const psk_hex = document.getElementById("channel-config-new-key").value.trim();
  if (new TextEncoder().encode(name).length >= 12) return feedback("Имя: менее 12 байт UTF-8", true);
  if (psk_hex && !/^(?:[0-9a-fA-F]{2}|[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$/.test(psk_hex)) {
    return feedback("Ключ: 2, 32 или 64 символа HEX", true);
  }
  const changes = {};
  if (name !== original.name) changes.name = name;
  if (role !== original.role) changes.role = role;
  if (uplink !== original.uplink_enabled) changes.uplink_enabled = uplink;
  if (downlink !== original.downlink_enabled) changes.downlink_enabled = downlink;
  if (psk_hex) changes.psk_hex = psk_hex;
  if (!Object.keys(changes).length) return feedback("Изменений нет");
  const save = document.getElementById("channel-config-save");
  save.disabled = true;
  try {
    await postJson("/api/node-channel", {index: original.index, changes});
    channelConfigDialog.close();
    feedback(`Канал ${original.index}: изменения отправлены в ноду`);
    setTimeout(() => refresh().catch(error => feedback(error.message, true)), 1200);
  } catch (error) { feedback(error.message, true); }
  finally { save.disabled = false; }
});

document.getElementById("send-form").addEventListener("submit", async event => {
  event.preventDefault();
  const textArea = document.getElementById("message-text");
  const text = textArea.value.trim();
  try {
    await postJson("/api/send", {kind: ui.active.kind, destination: ui.active.peer, text,
                                 reply_to_id: ui.replyTo?.id || null});
    textArea.value = "";
    ui.replyTo = null;
    renderReplyComposer();
    feedback("Сообщение передаётся в радио");
    setTimeout(() => refresh().catch(error => feedback(error.message, true)), 1200);
  } catch (error) { feedback(error.message, true); }
});

document.getElementById("cancel-reply").addEventListener("click", () => {
  ui.replyTo = null;
  renderReplyComposer();
});

document.getElementById("history").addEventListener("click", () => {
  loadMessages("older").catch(error => feedback(error.message, true));
});
document.getElementById("newer").addEventListener("click", () => {
  loadMessages("newer").catch(error => feedback(error.message, true));
});
document.getElementById("messages").addEventListener("scroll", () => {
  queueVisibleRead();
  const list = document.getElementById("messages");
  if (ui.tab === "chats" && ui.hasNewer && !ui.loadingNewer &&
      list.scrollHeight - list.scrollTop - list.clientHeight < 100) {
    ui.loadingNewer = true;
    loadMessages("newer").catch(error => feedback(error.message, true))
      .finally(() => { ui.loadingNewer = false; });
  }
}, {passive: true});
window.addEventListener("scroll", queueVisibleRead, {passive: true});
window.addEventListener("resize", queueVisibleRead);
document.addEventListener("visibilitychange", () => { if (!document.hidden) queueVisibleRead(); });

document.getElementById("show-templates").addEventListener("click", async () => {
  setTab("settings");
  document.getElementById("templates-panel").scrollIntoView({behavior: "smooth"});
});

document.getElementById("bot-enabled").addEventListener("change", async event => {
  const toggle = event.currentTarget;
  const enabled = toggle.checked;
  toggle.disabled = true;
  try {
    await postJson("/api/bot", {enabled});
    if (ui.state) ui.state.bot_enabled = enabled;
    renderDashboard();
    document.getElementById("bot-mode-status").textContent = enabled
      ? "Бот включён: шаблоны и команды отвечают в личке."
      : "Бот выключен: новые личные сообщения сохраняются без автоответа.";
    feedback(enabled ? "Ответы бота включены" : "Ответы бота выключены");
  } catch (error) {
    toggle.checked = !enabled;
    feedback(error.message, true);
  } finally { toggle.disabled = false; }
});

function clearTemplateForm() {
  ui.editingTemplate = null;
  document.getElementById("template-form").reset();
  document.getElementById("template-cancel").hidden = true;
}

document.getElementById("template-cancel").addEventListener("click", clearTemplateForm);
document.getElementById("template-form").addEventListener("submit", async event => {
  event.preventDefault();
  const key = document.getElementById("template-key").value.trim();
  const value = document.getElementById("template-value").value.trim();
  try {
    await postJson("/api/templates", {id: ui.editingTemplate, key, value});
    clearTemplateForm();
    await loadTemplates();
    feedback("Шаблон сохранён");
  } catch (error) { feedback(error.message, true); }
});

for (const name of tabNames) {
  const button = document.getElementById(`tab-${name}`);
  button.addEventListener("click", () => setTab(name));
  button.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const step = event.key === "ArrowRight" ? 1 : -1;
    const next = tabNames[(tabNames.indexOf(name) + step + tabNames.length) % tabNames.length];
    setTab(next);
    document.getElementById(`tab-${next}`).focus();
  });
}
window.addEventListener("hashchange", () => setTab(location.hash.slice(1), false));
setTab(location.hash.slice(1), false);
refresh();
refreshUsb().catch(error => feedback(error.message, true));
loadWeather();
setInterval(refresh, 5000);
setInterval(loadWeather, 600000);
