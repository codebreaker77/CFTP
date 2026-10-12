"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  connected: false,
  host: "127.0.0.1",
  port: 9000,
  files: [],
  transfers: [],
  busy: false,
  view: "files",
  sort: 1,
  selected: null,
  maxSize: 100 * 1024 * 1024,
  broadcasts: { clients: [], jobs: [], inbox: [] },
  broadcastsInitialized: false,
};
let notificationTimer;
let transferTimer;
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );

function bytes(size) {
  if (size === 0) return "0 B";
  const unit = Math.min(3, Math.floor(Math.log(size) / Math.log(1024)));
  return `${(size / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: unit ? 1 : 0 })} ${["B", "KB", "MB", "GB"][unit]}`;
}
function date(value, full = false) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "—";
  return parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(full ? { year: "numeric" } : {}),
  });
}
function timeAgo(value) {
  const seconds = Math.max(
    0,
    Math.round((Date.now() - new Date(value).valueOf()) / 1000),
  );
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return date(value);
}
function notify(message, error = false) {
  clearTimeout(notificationTimer);
  $("notification").textContent = message;
  $("notification").classList.toggle("error", error);
  $("notification").hidden = false;
  notificationTimer = setTimeout(() => {
    $("notification").hidden = true;
  }, 4500);
}
function pageError(message = "") {
  $("page-error").textContent = message;
  $("page-error").hidden = !message;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-CFTP-Request": "1", ...options.headers },
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      data.error ||
        data.error_message ||
        "The operation could not be completed.",
    );
  return data;
}
function updateConnection(data) {
  Object.assign(state, {
    connected: data.connected,
    host: data.host,
    port: data.port,
    maxSize: data.max_file_size || state.maxSize,
  });
  $("header-dot").classList.toggle("online", state.connected);
  $("sidebar-dot").classList.toggle("online", state.connected);
  $("header-status").textContent = state.connected
    ? `Connected · ${state.host}:${state.port}`
    : "Connect a server";
  $("sidebar-status").textContent = state.connected
    ? "Server connected"
    : "Not connected";
  $("sidebar-address").textContent = `${state.host}:${state.port}`;
  $("disconnect-button").hidden = !state.connected;
  if (!state.connected) state.files = [];
  renderFiles();
}
function setBusy(busy) {
  state.busy = busy;
  for (const id of [
    "upload-button",
    "drop-zone",
    "refresh-button",
    "empty-button",
    "connection-button",
    "server-settings",
    "disconnect-button",
    "detail-download",
    "broadcast-button",
    "new-broadcast-button",
  ])
    $(id).disabled = busy;
  document.querySelectorAll(".download-action").forEach((button) => {
    button.disabled = busy;
  });
}
function fileType(name) {
  const extension = name.includes(".")
    ? name.split(".").pop().toLowerCase()
    : "";
  const type = ["png", "jpg", "jpeg", "svg", "webp", "gif"].includes(extension)
    ? "image"
    : ["zip", "gz", "tar", "7z"].includes(extension)
      ? "archive"
      : ["txt", "md", "csv", "json", "py", "js", "html"].includes(extension)
        ? "text"
        : "";
  return {
    extension: extension ? `${extension.toUpperCase()} file` : "File",
    type,
  };
}
function renderFiles() {
  const search = $("search").value.trim().toLowerCase();
  const files = state.files
    .filter((file) => file.name.toLowerCase().includes(search))
    .sort((a, b) => state.sort * a.name.localeCompare(b.name));
  $("file-count").textContent =
    `${state.files.length} ${state.files.length === 1 ? "file" : "files"}`;
  $("nav-count").textContent = state.files.length;
  $("sort-indicator").textContent = state.sort === 1 ? "↓" : "↑";
  $("sort-name")
    .closest("th")
    .setAttribute("aria-sort", state.sort === 1 ? "ascending" : "descending");
  $("file-rows").innerHTML = files
    .map((file) => {
      const type = fileType(file.name);
      return `<tr><td><button class="file-name-button" data-details="${escapeHTML(file.name)}" aria-label="Details for ${escapeHTML(file.name)}"><span class="file-icon ${type.type}">${icon("file")}</span><span class="file-name-text">${escapeHTML(file.name)}<span class="file-extension">${type.extension}</span></span></button></td><td>${bytes(file.size)}</td><td>${date(file.modified)}</td><td class="row-actions"><button class="icon-button download-action" data-broadcast="${escapeHTML(file.name)}" aria-label="Broadcast ${escapeHTML(file.name)}" title="Broadcast to all clients" ${state.busy ? "disabled" : ""}>${icon("broadcast")}</button><button class="icon-button download-action" data-download="${escapeHTML(file.name)}" aria-label="Download ${escapeHTML(file.name)}" title="Download file" ${state.busy ? "disabled" : ""}>${icon("download")}</button></td></tr>`;
    })
    .join("");
  $("empty-state").hidden = files.length > 0;
  if (search && state.files.length) {
    $("empty-title").textContent = "Nothing by that name.";
    $("empty-description").textContent =
      "Try another search or clear the filter to see all your files.";
    $("empty-button").innerHTML = "Clear search";
  } else if (state.connected) {
    $("empty-title").textContent = "No files yet";
    $("empty-description").textContent =
      "Upload a file to your connected server to get started.";
    $("empty-button").innerHTML = `${icon("upload")}Upload your first file`;
  } else {
    $("empty-title").textContent = "Connect to view files";
    $("empty-description").textContent =
      "Choose a CFTP server to browse, upload, and share files.";
    $("empty-button").innerHTML = `${icon("link")}Connect a server`;
  }
  $("storage-summary").textContent = state.connected
    ? `${state.files.length} ${state.files.length === 1 ? "file" : "files"} · ${bytes(state.files.reduce((total, file) => total + file.size, 0))} in your workspace`
    : "Your workspace is ready when you are.";
}
function transferRow(transfer) {
  const success = transfer.success && transfer.integrity_matched;
  const label =
    transfer.direction === "upload"
      ? "Uploaded"
      : transfer.direction === "broadcast"
        ? "Broadcast received"
        : "Downloaded";
  const speed =
    transfer.throughput_mb_s > 0
      ? ` · ${transfer.throughput_mb_s.toFixed(1)} MB/s`
      : "";
  return `<div class="transfer-row"><span class="transfer-direction">${icon(transfer.direction)}</span><div class="transfer-row-name"><strong>${escapeHTML(transfer.filename)}</strong><span>${label} · ${bytes(transfer.bytes_transferred)}${speed}</span></div><span class="transfer-status ${success ? "" : "failed"}" title="${escapeHTML(transfer.error_message || "SHA-256 checksum matched")}">${icon(success ? "check" : "x")}${success ? "Verified" : "Failed"}</span><span class="transfer-time">${timeAgo(transfer.completed_at)}</span></div>`;
}
function renderActivity() {
  const empty = `<div class="activity-empty"><span class="quiet-icon">${icon("arrow")}</span>Your next transfer starts the story.</div>`;
  $("recent-transfers").innerHTML = state.transfers.length
    ? state.transfers.slice(0, 3).map(transferRow).join("")
    : empty;
  $("activity-list").innerHTML = state.transfers.length
    ? state.transfers.map(transferRow).join("")
    : empty;
  $("activity-count").textContent =
    `${state.transfers.length} ${state.transfers.length === 1 ? "transfer" : "transfers"}`;
  $("activity-dot").hidden = !state.transfers.length;
}
async function refreshActivity() {
  const data = await api("/api/activity");
  state.transfers = data.transfers;
  renderActivity();
}
async function refreshFiles({ announce = false } = {}) {
  if (!state.connected) {
    renderFiles();
    return;
  }
  $("refresh-button").disabled = true;
  try {
    const data = await api("/api/files");
    state.files = data.files;
    renderFiles();
    pageError();
    if (announce) notify("Your files are up to date.");
  } catch (error) {
    pageError(error.message);
    updateConnection(await api("/api/status"));
  } finally {
    $("refresh-button").disabled = state.busy;
  }
}
function changeView(view) {
  state.view = view;
  $("files-view").hidden = view !== "files";
  $("activity-view").hidden = view !== "activity";
  $("broadcasts-view").hidden = view !== "broadcasts";
  $("broadcast-button").hidden = view === "broadcasts";
  $("upload-button").hidden = view !== "files";
  $("breadcrumb-current").textContent =
    view === "files"
      ? "Files"
      : view === "activity"
        ? "Transfer activity"
        : "Broadcasts";
  $("page-eyebrow").textContent =
    view === "files"
      ? "A CLEAR SPACE FOR YOUR FILES"
      : "EVERY FILE HAS A JOURNEY";
  $("page-title").textContent =
    view === "files"
      ? "Files"
      : view === "activity"
        ? "Transfer activity"
        : "Broadcasts";
  $("page-description").textContent =
    view === "files"
      ? "Manage files on your connected server."
      : view === "activity"
        ? "Review completed transfers and checksum verification."
        : "Share a server file with every connected client.";
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view)
      button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (view === "activity")
    refreshActivity().catch((error) => notify(error.message, true));
  if (view === "broadcasts")
    refreshBroadcasts().catch((error) => notify(error.message, true));
}
function openConnection() {
  $("host-input").value = state.host;
  $("port-input").value = state.port;
  $("connection-error").hidden = true;
  $("connection-dialog").showModal();
}
function pickFiles() {
  if (state.busy) return;
  if (!state.connected) {
    openConnection();
    return;
  }
  $("file-input").click();
}
function progress(title, filename, value, stage) {
  clearTimeout(transferTimer);
  $("transfer-toast").hidden = false;
  $("transfer-title").textContent = title;
  $("transfer-filename").textContent = filename;
  $("transfer-percent").textContent =
    value === null ? "…" : `${Math.round(value)}%`;
  if (value === null) $("transfer-progress").removeAttribute("value");
  else $("transfer-progress").value = value;
  $("transfer-stage").textContent = stage;
}
function finishProgress() {
  transferTimer = setTimeout(() => {
    $("transfer-toast").hidden = true;
  }, 1800);
}
function sendFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-CFTP-Request", "1");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.timeout = 180000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        progress(
          "Sending your file",
          file.name,
          Math.min(99, (event.loaded / Math.max(event.total, 1)) * 100),
          `${bytes(event.loaded)} of ${bytes(event.total)} sent to the workspace`,
        );
    };
    xhr.upload.onload = () =>
      progress(
        "Verifying your transfer",
        file.name,
        null,
        "Moving to the CFTP server and checking the checksum…",
      );
    xhr.onload = () => {
      let data;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(new Error("The server returned an unexpected response."));
        return;
      }
      if (
        xhr.status >= 200 &&
        xhr.status < 300 &&
        data.success &&
        data.integrity_matched
      )
        resolve(data);
      else
        reject(new Error(data.error || data.error_message || "Upload failed."));
    };
    xhr.onerror = () =>
      reject(
        new Error(
          "Could not reach the workspace. Check that the web server is running.",
        ),
      );
    xhr.ontimeout = () =>
      reject(new Error("The upload timed out. Reconnect and try again."));
    xhr.send(file);
  });
}
async function uploadFiles(fileList) {
  if (state.busy) return;
  if (!state.connected) {
    openConnection();
    return;
  }
  const files = Array.from(fileList);
  if (!files.length) return;
  if (files.some((file) => file.size > state.maxSize)) {
    notify("Each file must be 100 MB or smaller.", true);
    return;
  }
  setBusy(true);
  pageError();
  let sent = 0;
  try {
    for (const file of files) {
      if (
        state.files.some((existing) => existing.name === file.name) &&
        !window.confirm(`Replace “${file.name}” on the server?`)
      )
        continue;
      progress("Sending your file", file.name, 0, "Preparing your transfer…");
      await sendFile(file);
      sent += 1;
      progress(
        "Safely delivered",
        file.name,
        100,
        "SHA-256 checksum matched. Your file is ready.",
      );
      await refreshFiles();
    }
    if (sent)
      notify(`${sent === 1 ? "File" : `${sent} files`} uploaded and verified.`);
  } catch (error) {
    pageError(error.message);
    notify(error.message, true);
    $("transfer-toast").hidden = true;
    try {
      updateConnection(await api("/api/status"));
    } catch {
      /* Original error remains visible. */
    }
  } finally {
    setBusy(false);
    $("file-input").value = "";
    finishProgress();
    refreshActivity().catch(() => {});
  }
}
async function downloadFile(
  filename,
  downloadUrl = `/api/download?filename=${encodeURIComponent(filename)}`,
) {
  if (state.busy) return;
  setBusy(true);
  pageError();
  progress(
    "Bringing your file back",
    filename,
    null,
    "Retrieving from CFTP and verifying its checksum…",
  );
  try {
    const response = await fetch(downloadUrl);
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "Download failed.");
    }
    const size = Number(response.headers.get("Content-Length"));
    const checksum = response.headers.get("X-CFTP-Checksum");
    if (!checksum || !/^[a-f0-9]{64}$/i.test(checksum))
      throw new Error("The download could not be verified.");
    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      progress(
        "Bringing your file back",
        filename,
        size ? (received / size) * 100 : 100,
        `${bytes(received)} of ${bytes(size)} received`,
      );
    }
    const url = URL.createObjectURL(
      new Blob(chunks, { type: "application/octet-stream" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    progress(
      "Ready to keep",
      filename,
      100,
      "Download complete. SHA-256 checksum matched.",
    );
    notify("File downloaded and verified.");
  } catch (error) {
    pageError(error.message);
    notify(error.message, true);
    $("transfer-toast").hidden = true;
    try {
      updateConnection(await api("/api/status"));
    } catch {
      /* Original error remains visible. */
    }
  } finally {
    setBusy(false);
    finishProgress();
    refreshActivity().catch(() => {});
  }
}
function showDetails(filename) {
  const file = state.files.find((item) => item.name === filename);
  if (!file) return;
  state.selected = file;
  $("detail-name").textContent = file.name;
  $("detail-size").textContent =
    `${bytes(file.size)} (${file.size.toLocaleString()} bytes)`;
  $("detail-modified").textContent =
    `${date(file.modified, true)} · ${new Date(file.modified).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
  $("detail-checksum").textContent = file.sha256;
  $("copy-checksum").innerHTML = `${icon("copy")}Copy`;
  $("detail-dialog").showModal();
}

document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => changeView(button.dataset.view)),
  );
document
  .querySelectorAll("[data-close]")
  .forEach((button) =>
    button.addEventListener("click", () => $(button.dataset.close).close()),
  );
document.querySelectorAll("dialog").forEach((dialog) =>
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }
  }),
);
$("connection-button").addEventListener("click", openConnection);
$("server-settings").addEventListener("click", openConnection);
$("help-button").addEventListener("click", () => $("help-dialog").showModal());
$("upload-button").addEventListener("click", pickFiles);
$("drop-zone").addEventListener("click", pickFiles);
$("file-input").addEventListener("change", (event) =>
  uploadFiles(event.target.files),
);
$("search").addEventListener("input", renderFiles);
$("sort-name").addEventListener("click", () => {
  state.sort *= -1;
  renderFiles();
});
$("refresh-button").addEventListener("click", () =>
  refreshFiles({ announce: true }),
);
$("view-activity").addEventListener("click", () => changeView("activity"));
$("empty-button").addEventListener("click", () => {
  if ($("search").value && state.files.length) {
    $("search").value = "";
    renderFiles();
  } else pickFiles();
});
$("file-rows").addEventListener("click", (event) => {
  const details = event.target.closest("[data-details]");
  const download = event.target.closest("[data-download]");
  const broadcast = event.target.closest("[data-broadcast]");
  if (details) showDetails(details.dataset.details);
  if (download) downloadFile(download.dataset.download);
  if (broadcast) openBroadcast(broadcast.dataset.broadcast);
});
$("detail-download").addEventListener("click", () => {
  $("detail-dialog").close();
  downloadFile(state.selected.name);
});
$("copy-checksum").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(state.selected.sha256);
    $("copy-checksum").innerHTML = `${icon("check")}Copied`;
  } catch {
    notify("Select the checksum text to copy it.", true);
  }
});
$("connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("connect-submit");
  button.disabled = true;
  button.textContent = "Connecting…";
  $("connection-error").hidden = true;
  try {
    const data = await api("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: $("host-input").value.trim(),
        port: Number($("port-input").value),
      }),
    });
    state.files = data.files;
    updateConnection(data);
    $("connection-dialog").close();
    pageError();
    notify("Connected. Your workspace is ready.");
  } catch (error) {
    $("connection-error").textContent = error.message;
    $("connection-error").hidden = false;
  } finally {
    button.disabled = false;
    button.innerHTML = `Connect to server${icon("chevron")}`;
  }
});
$("disconnect-button").addEventListener("click", async () => {
  try {
    updateConnection(await api("/api/disconnect", { method: "POST" }));
    $("connection-dialog").close();
    notify("Disconnected from the server.");
  } catch (error) {
    $("connection-error").textContent = error.message;
    $("connection-error").hidden = false;
  }
});
let dragDepth = 0;
const dropZone = $("drop-zone");
for (const eventName of ["dragenter", "dragover", "dragleave", "drop"])
  document.addEventListener(eventName, (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    if (eventName === "dragenter") {
      dragDepth += 1;
      if (!state.busy) dropZone.classList.add("dragging");
    }
    if (eventName === "dragleave") {
      dragDepth -= 1;
      if (dragDepth <= 0) {
        dragDepth = 0;
        dropZone.classList.remove("dragging");
      }
    }
    if (eventName === "drop") {
      dragDepth = 0;
      dropZone.classList.remove("dragging");
      uploadFiles(event.dataTransfer.files);
    }
  });
async function initialize() {
  try {
    updateConnection(await api("/api/status"));
    await Promise.all([refreshFiles(), refreshActivity(), refreshBroadcasts()]);
  } catch {
    pageError(
      "Could not reach the workspace. Start the web server and refresh this page.",
    );
  }
}
initialize();

function renderBroadcasts() {
  const openJobs = new Set(
    Array.from($("broadcast-jobs").querySelectorAll("details[open]")).map(
      (details) => details.dataset.id,
    ),
  );
  const { clients, jobs, inbox } = state.broadcasts;
  const eligible = clients.filter((client) => client.receives_broadcasts);
  $("peer-count").textContent =
    `${clients.length} ${clients.length === 1 ? "client" : "clients"}`;
  $("inbox-count").textContent =
    `${inbox.length} ${inbox.length === 1 ? "file" : "files"}`;
  $("broadcast-nav-count").textContent = inbox.length;
  $("broadcast-target-count").textContent =
    `All ${eligible.length} connected ${eligible.length === 1 ? "client" : "clients"} will receive this file`;
  $("peer-list").innerHTML = clients.length
    ? clients
        .map(
          (client) =>
            `<div class="peer-item"><span class="server-dot ${client.receives_broadcasts ? "online" : ""}"></span><div><strong>${escapeHTML(client.name)}</strong><span class="peer-address">${escapeHTML(client.address)}</span></div><span class="peer-status ${client.receives_broadcasts ? "" : "off"}">${client.receives_broadcasts ? "Receiving" : "Legacy / disabled"}</span></div>`,
        )
        .join("")
    : '<div class="activity-empty">Connect a client to receive broadcasts.</div>';
  $("inbox-list").innerHTML = inbox.length
    ? inbox
        .map(
          (entry) =>
            `<div class="peer-item"><span class="file-icon">${icon("file")}</span><div><strong>${escapeHTML(entry.filename)}</strong><span class="peer-address">${bytes(entry.bytes_transferred)} · ${timeAgo(entry.completed_at)}</span></div><span class="peer-status ${entry.success ? "" : "off"}">${entry.success ? "Verified" : "Failed"}</span>${entry.success ? `<button class="icon-button download-action" data-inbox="${escapeHTML(entry.id)}" aria-label="Save received ${escapeHTML(entry.filename)}" title="Save received file">${icon("download")}</button>` : ""}</div>`,
        )
        .join("")
    : '<div class="activity-empty">Received broadcasts will appear here.</div>';
  $("broadcast-jobs").innerHTML = jobs.length
    ? jobs
        .map(
          (job) =>
            `<div class="broadcast-job"><span class="transfer-direction">${icon("broadcast")}</span><div class="broadcast-job-main"><strong>${escapeHTML(job.filename)}</strong><span>${bytes(job.size)} · ${date(job.created_at)} · ${job.total} ${job.total === 1 ? "recipient" : "recipients"}</span><details class="recipient-details" data-id="${escapeHTML(job.id)}"><summary>Delivery details</summary>${job.recipients.map((recipient) => `<div><span>${escapeHTML(recipient.name)}</span><span title="${escapeHTML(recipient.error || "")}">${escapeHTML(recipient.status)}</span></div>`).join("")}</details></div><span class="broadcast-result ${job.status === "sending" && job.verified < job.total ? "pending" : job.failed ? "failed" : ""}">${job.status === "sending" && job.verified < job.total ? "Sending · " : ""}${job.verified}/${job.total} verified</span></div>`,
        )
        .join("")
    : '<div class="activity-empty">No broadcasts sent in this session.</div>';
  $("broadcast-jobs")
    .querySelectorAll("details")
    .forEach((details) => {
      details.open = openJobs.has(details.dataset.id);
    });
}

async function refreshBroadcasts() {
  const previous = state.broadcasts.inbox[0]?.id;
  state.broadcasts = await api("/api/broadcasts");
  if (
    state.broadcasts.connection &&
    state.broadcasts.connection.connected !== state.connected
  )
    updateConnection(state.broadcasts.connection);
  renderBroadcasts();
  if (
    state.broadcastsInitialized &&
    state.broadcasts.inbox[0]?.id &&
    state.broadcasts.inbox[0]?.id !== previous
  ) {
    notify(`Received broadcast: ${state.broadcasts.inbox[0].filename}`);
    refreshActivity().catch(() => {});
  }
  state.broadcastsInitialized = true;
}

async function openBroadcast(filename = "") {
  if (state.busy) return;
  if (!state.connected) {
    openConnection();
    return;
  }
  if (!state.files.length) {
    notify("Upload a file before broadcasting it.");
    return;
  }
  $("broadcast-file-select").innerHTML =
    '<option value="">Select a file</option>' +
    state.files
      .map(
        (file) =>
          `<option value="${escapeHTML(file.name)}">${escapeHTML(file.name)} (${bytes(file.size)})</option>`,
      )
      .join("");
  $("broadcast-file-select").value = filename;
  $("broadcast-error").hidden = true;
  $("broadcast-dialog").showModal();
  $("broadcast-submit").disabled = true;
  try {
    await refreshBroadcasts();
    $("broadcast-submit").disabled = false;
  } catch (error) {
    $("broadcast-error").textContent = error.message;
    $("broadcast-error").hidden = false;
  }
}

$("broadcast-button").addEventListener("click", () => openBroadcast());
$("new-broadcast-button").addEventListener("click", () => openBroadcast());
$("broadcast-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("broadcast-submit");
  button.disabled = true;
  button.textContent = "Starting broadcast…";
  $("broadcast-error").hidden = true;
  try {
    const job = await api("/api/broadcast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: $("broadcast-file-select").value }),
    });
    $("broadcast-dialog").close();
    notify(
      `Broadcast started for ${job.total} ${job.total === 1 ? "client" : "clients"}.`,
    );
    changeView("broadcasts");
    await refreshBroadcasts();
  } catch (error) {
    $("broadcast-error").textContent = error.message;
    $("broadcast-error").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Broadcast file";
  }
});
$("inbox-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-inbox]");
  if (!button) return;
  const entry = state.broadcasts.inbox.find(
    (item) => item.id === button.dataset.inbox,
  );
  if (entry)
    downloadFile(
      entry.filename,
      `/api/inbox-download?id=${encodeURIComponent(entry.id)}`,
    );
});
async function pollBroadcasts() {
  if (!document.hidden) {
    try {
      await refreshBroadcasts();
    } catch {
      /* Connection failures remain actionable through the main controls. */
    }
  }
  setTimeout(
    pollBroadcasts,
    state.broadcasts.jobs.some((job) => job.status === "sending") ? 600 : 2500,
  );
}
setTimeout(pollBroadcasts, 2500);
