const $ = (id) => document.getElementById(id);

const state = {
  topology: null,
  topologyName: "",
  topologySummary: null,
  devices: [],
  logs: [],
  requests: new Map(),
  selectedDevice: null,
  selectedRequest: null,
  deviceSearch: "",
  autoFollow: true,
  stream: null,
  reconnectTimer: null,
  toastTimer: null,
  theme: "light",
  fontSize: 12,
  logsHeight: 176,
  resizeSession: null,
};

const THEME_STORAGE_KEY = "ensp-ops-theme";
const FONT_SIZE_STORAGE_KEY = "ensp-ops-terminal-font-size";
const LOGS_HEIGHT_STORAGE_KEY = "ensp-ops-logs-height";
const DEFAULT_FONT_SIZE = 12;
const MIN_FONT_SIZE = 11;
const MAX_FONT_SIZE = 18;
const DEFAULT_LOGS_HEIGHT = 176;
const MIN_LOGS_HEIGHT = 136;
const MIN_TERMINAL_HEIGHT = 260;

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  if (state.toastTimer) clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("show"), 2400);
}

function sanitizeStreamText(value) {
  return String(value ?? "")
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "")
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\u001b[@-_]/g, "")
    .replace(/----\s*More\s*----/gi, "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "");
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  document.body.dataset.theme = state.theme;
  $("btnThemeToggle").setAttribute("aria-pressed", state.theme === "dark" ? "true" : "false");
  $("btnThemeToggle").setAttribute("data-tip", state.theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
  $("btnThemeToggle").setAttribute("aria-label", state.theme === "dark" ? "切换到浅色主题" : "切换到深色主题");
}

function initTheme() {
  let initialTheme = "light";
  try {
    const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (storedTheme === "light" || storedTheme === "dark") {
      initialTheme = storedTheme;
    } else if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
      initialTheme = "dark";
    }
  } catch {
    initialTheme = "light";
  }
  applyTheme(initialTheme);
}

function applyFontSize(size) {
  const next = clamp(Math.round(Number(size) || DEFAULT_FONT_SIZE), MIN_FONT_SIZE, MAX_FONT_SIZE);
  const logSize = clamp(next - 2, 10, 16);

  state.fontSize = next;
  document.documentElement.style.setProperty("--terminal-font-size", `${next}px`);
  document.documentElement.style.setProperty("--terminal-line-height", next >= 16 ? "1.56" : "1.46");
  document.documentElement.style.setProperty("--log-font-size", `${logSize}px`);
  document.documentElement.style.setProperty("--log-time-font-size", `${Math.max(8, logSize - 2)}px`);
  document.documentElement.style.setProperty("--log-badge-font-size", `${Math.max(7, logSize - 3)}px`);

  $("fontSizeControl").value = String(next);
  $("fontSizeValue").textContent = `${next}px`;
}

function initFontSize() {
  let stored = DEFAULT_FONT_SIZE;
  try {
    stored = Number(window.localStorage.getItem(FONT_SIZE_STORAGE_KEY)) || DEFAULT_FONT_SIZE;
  } catch {
    stored = DEFAULT_FONT_SIZE;
  }
  applyFontSize(stored);
}

function persistFontSize() {
  try {
    window.localStorage.setItem(FONT_SIZE_STORAGE_KEY, String(state.fontSize));
  } catch {
    // Ignore storage failures in embedded webviews.
  }
}

function maxLogsHeight() {
  const frame = $("contentFrame");
  if (!frame) return 360;
  return Math.max(MIN_LOGS_HEIGHT, Math.round(frame.clientHeight - MIN_TERMINAL_HEIGHT - 18));
}

function applyLogsHeight(height) {
  const next = clamp(Math.round(Number(height) || DEFAULT_LOGS_HEIGHT), MIN_LOGS_HEIGHT, maxLogsHeight());
  state.logsHeight = next;
  document.documentElement.style.setProperty("--logs-height", `${next}px`);
}

function initLogsHeight() {
  let stored = DEFAULT_LOGS_HEIGHT;
  try {
    stored = Number(window.localStorage.getItem(LOGS_HEIGHT_STORAGE_KEY)) || DEFAULT_LOGS_HEIGHT;
  } catch {
    stored = DEFAULT_LOGS_HEIGHT;
  }
  applyLogsHeight(stored);
}

function persistLogsHeight() {
  try {
    window.localStorage.setItem(LOGS_HEIGHT_STORAGE_KEY, String(state.logsHeight));
  } catch {
    // Ignore storage failures in embedded webviews.
  }
}

function eventTypeClass(type) {
  return `type-${String(type || "runtime").replace(/[^a-z0-9_-]/gi, "-")}`;
}

function formatClock(ts) {
  if (!ts) return "--:--:--";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function lineClassForType(type) {
  if (type === "command_send") return "command";
  if (type === "tool_error") return "error";
  if (["tool_start", "tool_end", "device_connect", "topology_synced"].includes(type)) return "meta";
  return "output";
}

function isTerminalEvent(type) {
  return ["device_connect", "command_send", "output", "tool_error"].includes(type);
}

function badgeLabel(type) {
  switch (type) {
    case "tool_start":
      return "工具开始";
    case "tool_end":
      return "工具完成";
    case "tool_error":
      return "工具失败";
    case "device_connect":
      return "设备连接";
    case "command_send":
      return "命令发送";
    case "output":
      return "终端输出";
    case "topology_synced":
      return "拓扑同步";
    case "topology_cleared":
      return "拓扑已清空";
    default:
      return "运行事件";
  }
}

function requestStatus(deviceName) {
  let status = "idle";
  for (const request of state.requests.values()) {
    const targets = request.targets || [];
    const matched = !deviceName || targets.includes(deviceName) || request.device === deviceName;
    if (!matched) continue;
    if (request.status === "running") return "running";
    if (request.status === "error") status = "error";
    if (request.status === "done" && status === "idle") status = "success";
  }
  return status;
}

function buildDeviceInventory() {
  const deviceMap = new Map();
  const topologyDevices = Array.isArray(state.topology?.devices) ? state.topology.devices : [];
  const hasCurrentTopology = topologyDevices.length > 0;
  const activeDeviceNames = new Set();

  for (const request of state.requests.values()) {
    for (const target of request.targets || []) {
      if (target) activeDeviceNames.add(target);
    }
    if (request.device) activeDeviceNames.add(request.device);
  }

  topologyDevices.forEach((device) => {
    const name = String(device.name || "").trim();
    if (!name) return;
    deviceMap.set(name, {
      name,
      port: device.comPort || device.port || "",
      model: device.model || "未知型号",
      source: "topology",
    });
  });

  state.devices.forEach((device) => {
    const name = String(device.name || "").trim();
    if (!name) return;
    const existing = deviceMap.get(name) || {
      name,
      port: "",
      model: "已注册设备",
      source: "registry",
      registrySource: String(device.registrySource || "legacy"),
    };
    existing.port = existing.port || device.port || "";
    existing.username = device.username || existing.username || "";
    existing.host = device.host || existing.host || "127.0.0.1";
    existing.registrySource = String(device.registrySource || existing.registrySource || "legacy");
    existing.source = existing.source === "topology" ? "topology+registry" : "registry";
    deviceMap.set(name, existing);
  });

  return [...deviceMap.values()]
    .filter((device) => {
      if (hasCurrentTopology) return true;
      if (device.source !== "registry") return true;
      if (activeDeviceNames.has(device.name)) return true;
      return !["legacy", "topology_sync", "auto_discover"].includes(String(device.registrySource || ""));
    })
    .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
}

function renderDevices() {
  const inventory = buildDeviceInventory();
  const inventoryNames = new Set(inventory.map((device) => device.name));
  if (state.selectedDevice && !inventoryNames.has(state.selectedDevice)) {
    state.selectedDevice = null;
  }
  const keyword = state.deviceSearch.trim().toLowerCase();
  const visibleDevices = inventory.filter((device) => !keyword || device.name.toLowerCase().includes(keyword));
  const deviceList = $("deviceList");

  if (!visibleDevices.length) {
    deviceList.innerHTML = `<div class="device-empty">${
      inventory.length
        ? "没有匹配到设备，请调整筛选关键字。"
        : "还没有设备。先上传 TOPO，页面会自动把拓扑中的设备同步进 MCP 注册表。"
    }</div>`;
    return;
  }

  deviceList.innerHTML = visibleDevices
    .map((device) => {
      const status = requestStatus(device.name);
      const active = state.selectedDevice === device.name ? "active" : "";
      const sourceText =
        device.source === "topology+registry"
          ? "拓扑已同步"
          : device.source === "topology"
            ? "仅拓扑"
            : "仅注册表";
      const meta = `${device.model} · ${sourceText}`;
      const port = device.port ? `TCP ${device.port}` : "端口未知";

      return `
        <button class="device-card ${active}" type="button" data-device="${escapeHtml(device.name)}">
          <span class="device-indicator ${status}"></span>
          <span>
            <div class="device-name">${escapeHtml(device.name)}</div>
            <div class="device-meta">${escapeHtml(meta)}</div>
          </span>
          <span class="device-port">${escapeHtml(port)}</span>
        </button>
      `;
    })
    .join("");

  deviceList.querySelectorAll("[data-device]").forEach((node) => {
    node.addEventListener("click", () => {
      const deviceName = node.getAttribute("data-device");
      state.selectedDevice = state.selectedDevice === deviceName ? null : deviceName;
      state.selectedRequest = null;
      renderAll();
    });
  });
}

function eventLinesForTerminal(events) {
  const lines = [];

  for (const event of events) {
    const type = event.type || "output";
    const prefix = event.device && type !== "output" ? `[${event.device}] ` : "";
    let text = sanitizeStreamText(event.text || "");

    if (type === "tool_start") text = `[系统] 收到 MCP 工具调用：${event.tool || "unknown"}`;
    if (type === "tool_end") text = `[系统] 工具执行完成：${event.tool || "unknown"}`;
    if (type === "tool_error") text = `[错误] ${text}`;
    if (type === "device_connect") text = `[系统] 已连接设备：${event.device || ""}`;
    if (type === "command_send") text = `> ${text}`;
    if (type === "topology_synced") text = `[系统] ${text}`;
    if (type === "topology_cleared") text = `[系统] ${text}`;

    for (const chunk of String(text).split("\n")) {
      const lineText = sanitizeStreamText(chunk).trimEnd();
      if (!lineText.trim()) continue;
      lines.push({
        className: lineClassForType(type),
        text: `${prefix}${lineText}`,
      });
    }
  }

  return lines;
}

function filteredTerminalEvents() {
  let events = [...state.logs]
    .sort((a, b) => (a.seq || 0) - (b.seq || 0))
    .filter((event) => isTerminalEvent(event.type));

  if (state.selectedDevice) {
    events = events.filter((event) => event.device === state.selectedDevice);
  }

  return events;
}

function filteredLogEvents() {
  let events = [...state.logs].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  events = events.filter((event) => ["tool_start", "tool_end", "tool_error", "topology_synced", "topology_cleared"].includes(event.type));

  if (state.selectedDevice) {
    events = events.filter((event) => event.device === state.selectedDevice || !event.device);
  }

  return events;
}

function isAbnormalLogText(text) {
  return /未找到|失败|异常|error|not found|unable|invalid|不存在|无法/i.test(text);
}

function summarizeLogEvent(event) {
  const text = sanitizeStreamText(event.text || "").trim();

  switch (event.type) {
    case "tool_start":
      return "开始";
    case "tool_end":
      return isAbnormalLogText(text) ? text.split("\n")[0] : "已完成";
    case "tool_error":
      return text || "工具执行失败";
    case "topology_synced":
    case "topology_cleared":
      return text;
    default:
      return text;
  }
}

function renderTerminal() {
  const terminalEmpty = $("terminalEmpty");
  const terminalLines = $("terminalLines");
  const events = filteredTerminalEvents();
  const lines = eventLinesForTerminal(events);

  $("selectedDeviceLabel").textContent = state.selectedDevice || "全部";

  if (!events.length) {
    terminalEmpty.hidden = false;
    terminalLines.hidden = true;
    terminalLines.innerHTML = "";
    $("terminalSubtitle").textContent = state.selectedDevice
      ? `当前只显示 ${state.selectedDevice} 的运行输出`
      : "等待 MCP 命令或上传 TOPO";
    return;
  }

  terminalEmpty.hidden = true;
  terminalLines.hidden = false;
  $("terminalSubtitle").textContent = "";

  terminalLines.innerHTML = lines
    .map((line) => `<div class="terminal-line ${line.className}">${escapeHtml(line.text)}</div>`)
    .join("");

  if (state.autoFollow) {
    $("terminalScreen").scrollTop = $("terminalScreen").scrollHeight;
  }
}

function renderLogs() {
  const logsList = $("logsList");
  const events = filteredLogEvents();
  state.selectedRequest = null;

  if (!events.length) {
    logsList.innerHTML = `<div class="log-empty">还没有收到 MCP 事务日志。连接 MCP 后执行一次命令，日志会自动出现。</div>`;
    return;
  }

  logsList.innerHTML = events
    .map((event) => {
      const tool = event.tool ? `<span class="log-tool">${escapeHtml(event.tool)}</span>` : "";
      const device = event.device ? `<span class="log-device">${escapeHtml(event.device)}</span>` : "";
      const text = summarizeLogEvent(event);
      return `
        <div class="log-entry ${eventTypeClass(event.type)}">
          <span class="log-time">${escapeHtml(formatClock(event.ts))}</span>
          <span class="log-badge ${escapeHtml(event.type || "")}">${escapeHtml(badgeLabel(event.type))}</span>
          <span class="log-message">${device}${tool}<span>${escapeHtml(text)}</span></span>
        </div>
      `;
    })
    .join("");

  if (state.autoFollow) {
    logsList.scrollTop = logsList.scrollHeight;
  }
}

function updateStatuses() {
  const online =
    state.stream?.readyState === EventSource.OPEN ||
    state.stream?.readyState === EventSource.CONNECTING ||
    state.stream === null;

  $("footerOnlineDot").className = online ? "online-dot live" : "online-dot";
  $("footerOnlineText").textContent = online ? "MCP 在线" : "MCP 断开";

  if (state.topologyName) {
    const summary = state.topologySummary;
    $("topologyMeta").textContent = summary
      ? `${state.topologyName} · 新增 ${summary.created.length} 台，更新 ${summary.updated.length} 台`
      : `${state.topologyName} · 等待同步`;
  } else {
    $("topologyMeta").textContent = "等待上传 TOPO";
  }

  $("syncSummary").textContent = state.topologySummary
    ? `新增 ${state.topologySummary.created.length} / 更新 ${state.topologySummary.updated.length}`
    : "未同步";
}

function renderAll() {
  updateStatuses();
  renderDevices();
  renderTerminal();
  renderLogs();
}

function mergeRequest(event) {
  if (!event.request_id) return;

  const existing = state.requests.get(event.request_id) || {
    request_id: event.request_id,
    tool: event.tool || "",
    targets: [],
    status: "running",
    started_at: event.ts,
  };

  existing.tool = event.tool || existing.tool;
  if (event.device) {
    existing.device = event.device;
    if (!existing.targets.includes(event.device)) existing.targets.push(event.device);
  }

  if (event.type === "tool_error") existing.status = "error";
  if (event.type === "tool_end") existing.status = "done";
  if (event.type === "tool_start") existing.status = "running";
  if (event.ts) existing.started_at = existing.started_at || event.ts;

  state.requests.set(event.request_id, existing);
}

function ingestSnapshot(snapshot) {
  state.logs = Array.isArray(snapshot.recent_events) ? snapshot.recent_events.slice(-500) : [];
  state.requests.clear();

  const recentRequests = Array.isArray(snapshot.recent_requests) ? snapshot.recent_requests : [];
  recentRequests.forEach((request) => {
    state.requests.set(request.request_id, {
      request_id: request.request_id,
      tool: request.tool,
      targets: request.targets || [],
      status: request.status || "done",
      started_at: request.started_at,
      device: (request.targets || [])[0] || "",
    });
  });

  const activeRequests = Array.isArray(snapshot.active_requests) ? snapshot.active_requests : [];
  activeRequests.forEach((request) => {
    state.requests.set(request.request_id, {
      request_id: request.request_id,
      tool: request.tool,
      targets: request.targets || [],
      status: request.status || "running",
      started_at: request.started_at,
      device: (request.targets || [])[0] || "",
    });
  });

  state.logs.forEach(mergeRequest);
}

function pushRuntimeEvent(event) {
  state.logs.push(event);
  if (state.logs.length > 500) state.logs = state.logs.slice(-500);
  mergeRequest(event);
  renderAll();
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url} 返回 ${response.status}`);
  return response.json();
}

async function refreshSnapshot() {
  const [runtime, devices] = await Promise.all([
    fetchJson("/api/runtime"),
    fetchJson("/api/devices"),
  ]);

  state.devices = Array.isArray(devices.devices) ? devices.devices : [];
  ingestSnapshot(runtime);

  try {
    state.topology = await fetchJson("/api/topology");
    if (state.topology && !state.topologyName) state.topologyName = "topology.json";
  } catch {
    state.topology = null;
    state.topologyName = "";
    state.topologySummary = null;
  }

  renderAll();
}

async function persistTopology(payload) {
  const response = await fetch("/api/topology", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const json = await response.json();
  if (!response.ok || !json.ok) throw new Error(json.error || "拓扑保存失败");
  return json;
}

function decodeTopoBytes(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return new TextDecoder("utf-16le").decode(bytes);
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return new TextDecoder("utf-16be").decode(bytes);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    try {
      return new TextDecoder("gbk").decode(bytes);
    } catch (fallbackError) {
      return new TextDecoder("utf-8").decode(bytes);
    }
  }
}

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const result = reader.result;
        resolve(typeof result === "string" ? result : decodeTopoBytes(result));
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => reject(reader.error || new Error("文件读取失败"));
    reader.readAsArrayBuffer(file);
  });
}

function parseTopo(xmlText) {
  if (xmlText.trim().startsWith("{")) {
    const json = JSON.parse(xmlText);
    return {
      devices: Array.isArray(json.devices) ? json.devices : [],
      links: Array.isArray(json.links) ? json.links : [],
    };
  }

  const normalized = xmlText.replace(
    /^<\?xml[^?]*\?>/,
    '<?xml version="1.0" encoding="utf-8" ?>'
  );
  const doc = new DOMParser().parseFromString(normalized, "text/xml");
  const parserError = doc.querySelector("parsererror");
  if (parserError) throw new Error("TOPO 解析失败，请确认文件格式是否正确");

  const devices = [];
  const links = [];

  doc.querySelectorAll("devices > dev").forEach((node) => {
    devices.push({
      id: node.getAttribute("id") || "",
      name: node.getAttribute("name") || "",
      model: node.getAttribute("model") || "",
      comPort: node.getAttribute("com_port") || "",
      x: Number.parseFloat(node.getAttribute("cx") || "0") || 0,
      y: Number.parseFloat(node.getAttribute("cy") || "0") || 0,
    });
  });

  doc.querySelectorAll("lines > line").forEach((node) => {
    const srcId = node.getAttribute("srcDeviceID") || "";
    const dstId = node.getAttribute("destDeviceID") || "";
    const pairs = node.querySelectorAll("interfacePair");
    if (pairs.length === 0) {
      links.push({ srcId, dstId });
      return;
    }
    pairs.forEach((pair) => {
      links.push({
        srcId,
        dstId,
        lineName: pair.getAttribute("lineName") || "",
        srcIndex: pair.getAttribute("srcIndex") || "",
        tarIndex: pair.getAttribute("tarIndex") || "",
      });
    });
  });

  return { devices, links };
}

async function handleTopoFile(file) {
  const text = await readFileAsText(file);
  const topology = parseTopo(text);
  const response = await persistTopology(topology);

  state.topology = topology;
  state.topologyName = file.name;
  state.topologySummary = response.registry || null;
  state.selectedDevice = null;
  state.selectedRequest = null;

  await refreshSnapshot();
  showToast(`拓扑已同步，共 ${topology.devices.length} 台设备可用于 MCP`);
}

function connectStream() {
  if (state.stream) state.stream.close();

  const stream = new EventSource("/api/runtime/stream");
  state.stream = stream;
  renderAll();

  stream.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      pushRuntimeEvent(event);
    } catch (error) {
      console.error("Failed to parse runtime event:", error);
    }
  };

  stream.onerror = () => {
    renderAll();
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connectStream, 1500);
  };
}

async function resyncTopology() {
  if (!state.topology?.devices?.length) {
    showToast("当前没有可同步的拓扑，请先上传 TOPO");
    return;
  }

  const response = await persistTopology(state.topology);
  state.topologySummary = response.registry || null;
  await refreshSnapshot();
  showToast("拓扑设备已重新同步到 MCP");
}

async function clearTopology() {
  const confirmed = window.confirm("确定要清空当前拓扑吗？这会删除已保存的 topology.json，并移除本次拓扑同步到注册表的设备。");
  if (!confirmed) return;

  const response = await fetch("/api/topology/clear", { method: "POST" });
  const json = await response.json();
  if (!response.ok || !json.ok) {
    throw new Error(json.error || "清空拓扑失败");
  }

  if (!json.registry?.count && !json.registry?.restored_count && !state.topology && !state.topologyName) {
    showToast("当前没有可清空的拓扑或同步设备");
    return;
  }

  state.topology = null;
  state.topologyName = "";
  state.topologySummary = null;
  state.selectedDevice = null;
  state.selectedRequest = null;
  state.deviceSearch = "";
  $("deviceSearch").value = "";

  await refreshSnapshot();
  showToast("当前拓扑已清空");
}

function bindEvents() {
  $("fileInput").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    try {
      await handleTopoFile(file);
    } catch (error) {
      showToast(error.message || "上传失败");
    } finally {
      event.target.value = "";
    }
  });

  $("btnUploadSide").addEventListener("click", () => $("fileInput").click());
  $("btnResyncSide").addEventListener("click", () => resyncTopology().catch((error) => showToast(error.message)));
  $("btnClearTopology").addEventListener("click", () => clearTopology().catch((error) => showToast(error.message)));

  $("deviceSearch").addEventListener("input", (event) => {
    state.deviceSearch = event.target.value || "";
    renderDevices();
  });

  $("fontSizeControl").addEventListener("input", (event) => {
    applyFontSize(event.target.value);
    persistFontSize();
  });

  $("fontSizeReset").addEventListener("click", () => {
    applyFontSize(DEFAULT_FONT_SIZE);
    persistFontSize();
  });

  $("btnThemeToggle").addEventListener("click", () => {
    const nextTheme = state.theme === "dark" ? "light" : "dark";
    applyTheme(nextTheme);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch {
      // Ignore storage failures in embedded webviews.
    }
  });

  $("autoFollow").addEventListener("change", (event) => {
    state.autoFollow = Boolean(event.target.checked);
  });

  const divider = $("panelDivider");
  const finishResize = () => {
    if (!state.resizeSession) return;
    state.resizeSession = null;
    document.body.classList.remove("is-resizing");
    persistLogsHeight();
  };

  divider.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    state.resizeSession = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: state.logsHeight || DEFAULT_LOGS_HEIGHT,
    };
    divider.setPointerCapture(event.pointerId);
    document.body.classList.add("is-resizing");
  });

  divider.addEventListener("pointermove", (event) => {
    if (!state.resizeSession || event.pointerId !== state.resizeSession.pointerId) return;
    const delta = state.resizeSession.startY - event.clientY;
    applyLogsHeight(state.resizeSession.startHeight + delta);
  });

  divider.addEventListener("pointerup", (event) => {
    if (state.resizeSession && event.pointerId === state.resizeSession.pointerId) {
      if (divider.hasPointerCapture(event.pointerId)) divider.releasePointerCapture(event.pointerId);
      finishResize();
    }
  });

  divider.addEventListener("pointercancel", finishResize);

  divider.addEventListener("dblclick", () => {
    applyLogsHeight(DEFAULT_LOGS_HEIGHT);
    persistLogsHeight();
  });

  divider.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    applyLogsHeight((state.logsHeight || DEFAULT_LOGS_HEIGHT) + (event.key === "ArrowUp" ? 24 : -24));
    persistLogsHeight();
  });

  window.addEventListener("resize", () => applyLogsHeight(state.logsHeight || DEFAULT_LOGS_HEIGHT));
}

async function init() {
  initTheme();
  initFontSize();
  initLogsHeight();
  bindEvents();
  try {
    await refreshSnapshot();
  } catch (error) {
    showToast(error.message || "初始化失败");
  }
  connectStream();
  renderAll();
}

init();
