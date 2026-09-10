const jobsEl = document.getElementById("jobs");
const emptyEl = document.getElementById("empty");
const countsEl = document.getElementById("counts");
const detailEl = document.getElementById("detail");
const itemsEl = document.getElementById("items");
const dialog = document.getElementById("add-dialog");
const form = document.getElementById("add-form");
const addError = document.getElementById("add-error");
const selectAll = document.getElementById("select-all");
const removeBtn = document.getElementById("remove-btn");
const rerunBtn = document.getElementById("rerun-btn");
const retryBtn = document.getElementById("retry-btn");
const retryDetailBtn = document.getElementById("retry-detail-btn");
const removeDialog = document.getElementById("remove-dialog");
const removeForm = document.getElementById("remove-form");
const removeCopy = document.getElementById("remove-copy");
const removeFiles = document.getElementById("remove-files");
const removeError = document.getElementById("remove-error");

let filter = "";
let selectedId = null;
const checkedIds = new Set();
let pendingRemoveIds = [];

function authToken() {
  return localStorage.getItem("page-scraper-token") || localStorage.getItem("pagecapture-token") || "";
}

function headers() {
  const token = authToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...headers(), ...(options.headers || {}) },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch (_) {}
    throw new Error(detail);
  }
  if (resp.status === 204) return {};
  return resp.json();
}

function fmtEta(seconds) {
  if (seconds == null) return "…";
  if (seconds <= 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function unsavedCount(job) {
  if (job.unsaved != null) return job.unsaved;
  return Math.max((job.failed || 0) - (job.partial || 0), 0);
}

function updateBulkButtons() {
  const n = checkedIds.size;
  removeBtn.disabled = n === 0;
  rerunBtn.disabled = n === 0;
  const canRetry = [...jobsEl.querySelectorAll("tr[data-id].checked")].some(
    (tr) => Number(tr.dataset.unsaved || 0) > 0
  );
  retryBtn.disabled = !canRetry;
  const rows = jobsEl.querySelectorAll("tr[data-id]");
  const checkedRows = jobsEl.querySelectorAll("tr[data-id].checked");
  selectAll.checked = rows.length > 0 && checkedRows.length === rows.length;
  selectAll.indeterminate = checkedRows.length > 0 && checkedRows.length < rows.length;
}

function renderJobs(data) {
  const jobs = data.jobs || [];
  const counts = data.counts || {};
  countsEl.textContent = `queued ${counts.queued || 0} · running ${counts.running || 0} · done ${counts.completed || 0}`;
  jobsEl.innerHTML = "";
  emptyEl.classList.toggle("hidden", jobs.length > 0);
  for (const job of jobs) {
    const done = job.done || 0;
    const total = job.total || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const tr = document.createElement("tr");
    tr.dataset.id = job.id;
    tr.dataset.unsaved = String(unsavedCount(job));
    const isChecked = checkedIds.has(job.id);
    if (isChecked) tr.classList.add("checked");
    tr.innerHTML = `
      <td class="check"><input type="checkbox"${isChecked ? " checked" : ""}></td>
      <td>${escapeHtml(job.name || job.id.slice(0, 8))}</td>
      <td><span class="pill ${job.status}">${job.status}</span></td>
      <td>
        <div class="bar" title="${done}/${total}"><span style="width:${pct}%"></span></div>
        ${done}/${total}
      </td>
      <td>${job.status === "running" || job.status === "queued" ? fmtEta(job.eta_seconds) : "—"}</td>
      <td>${job.failed || 0}${job.partial ? ` (${job.partial} saved)` : ""}</td>
      <td class="path">${escapeHtml(job.output_dir || "")}</td>`;
    const checkbox = tr.querySelector("input[type=checkbox]");
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) checkedIds.add(job.id);
      else checkedIds.delete(job.id);
      tr.classList.toggle("checked", checkbox.checked);
      updateBulkButtons();
    });
    tr.addEventListener("click", () => showDetail(job.id));
    jobsEl.appendChild(tr);
  }
  updateBulkButtons();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refresh() {
  const query = filter ? `?status=${encodeURIComponent(filter)}` : "";
  try {
    renderJobs(await api(`/api/jobs${query}`));
    if (selectedId) await showDetail(selectedId, true);
  } catch (err) {
    countsEl.textContent = err.message;
  }
}

async function showDetail(id, quiet = false) {
  selectedId = id;
  let job;
  try {
    job = await api(`/api/jobs/${id}`);
  } catch (err) {
    selectedId = null;
    detailEl.classList.add("hidden");
    return;
  }
  detailEl.classList.remove("hidden");
  document.getElementById("detail-title").textContent = job.name || job.id;
  document.getElementById("detail-meta").textContent =
    `${job.status} · ${job.done}/${job.total} · ETA ${fmtEta(job.eta_seconds)} · ${job.output_dir}`;
  retryDetailBtn.disabled = unsavedCount(job) <= 0;
  itemsEl.innerHTML = "";
  for (const item of job.items || []) {
    const tr = document.createElement("tr");
    const files = [
      item.screenshot_url ? `<a class="file" href="${escapeHtml(item.screenshot_url)}" target="_blank" rel="noreferrer">png</a>` : "",
      item.dom_url ? `<a class="file" href="${escapeHtml(item.dom_url)}" target="_blank" rel="noreferrer">html</a>` : "",
    ].filter(Boolean);
      const note = [item.reason, item.error].filter(Boolean).join(" — ");
      const issue = item.status === "failed" || item.error || (item.missing && item.missing.length);
      tr.innerHTML = `
      <td><span class="pill ${item.status}">${item.status}</span></td>
      <td>${files.join(" · ") || "no"}</td>
      <td>${item.video_url ? `<a class="file" href="${escapeHtml(item.video_url)}" target="_blank" rel="noreferrer">mp4</a>` : "no"}</td>
      <td>${escapeHtml(item.preset)}</td>
      <td>${escapeHtml(item.url)}</td>
      <td class="${issue ? "issue" : ""}">${escapeHtml(note)}</td>`;
    itemsEl.appendChild(tr);
  }
  if (!quiet) detailEl.scrollIntoView({ behavior: "smooth" });
}

function openRemoveDialog(ids) {
  pendingRemoveIds = ids;
  removeError.textContent = "";
  removeFiles.checked = false;
  const n = ids.length;
  removeCopy.textContent =
    n === 1
      ? "Are you sure you want to remove this job?"
      : `Are you sure you want to remove ${n} jobs?`;
  removeDialog.showModal();
}

selectAll.addEventListener("change", () => {
  const on = selectAll.checked;
  for (const tr of jobsEl.querySelectorAll("tr[data-id]")) {
    const id = tr.dataset.id;
    const checkbox = tr.querySelector("input[type=checkbox]");
    checkbox.checked = on;
    tr.classList.toggle("checked", on);
    if (on) checkedIds.add(id);
    else checkedIds.delete(id);
  }
  updateBulkButtons();
});

document.getElementById("filters").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  filter = button.dataset.filter;
  for (const child of event.currentTarget.querySelectorAll("button")) {
    child.classList.toggle("active", child === button);
  }
  refresh();
});

document.getElementById("add-btn").addEventListener("click", () => {
  addError.textContent = "";
  dialog.showModal();
});
document.getElementById("cancel-add").addEventListener("click", () => dialog.close());
document.getElementById("close-detail").addEventListener("click", () => {
  selectedId = null;
  detailEl.classList.add("hidden");
});
document.getElementById("cancel-btn").addEventListener("click", async () => {
  if (!selectedId) return;
  await api(`/api/jobs/${selectedId}/cancel`, { method: "POST", body: "{}" });
  refresh();
});
removeBtn.addEventListener("click", () => {
  if (checkedIds.size === 0) return;
  openRemoveDialog([...checkedIds]);
});
document.getElementById("remove-detail-btn").addEventListener("click", () => {
  if (!selectedId) return;
  openRemoveDialog([selectedId]);
});
document.getElementById("rerun-detail-btn").addEventListener("click", () => {
  if (!selectedId) return;
  rerunJobs([selectedId]);
});
retryDetailBtn.addEventListener("click", () => {
  if (!selectedId) return;
  retryJobs([selectedId]);
});
retryBtn.addEventListener("click", () => {
  if (checkedIds.size === 0) return;
  retryJobs([...checkedIds]);
});
document.getElementById("remove-cancel").addEventListener("click", () => removeDialog.close());
removeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  removeError.textContent = "";
  const ids = pendingRemoveIds;
  if (!ids.length) {
    removeDialog.close();
    return;
  }
  try {
    await api("/api/jobs/delete", {
      method: "POST",
      body: JSON.stringify({ ids, delete_files: removeFiles.checked }),
    });
    for (const id of ids) {
      checkedIds.delete(id);
      if (selectedId === id) {
        selectedId = null;
        detailEl.classList.add("hidden");
      }
    }
    removeDialog.close();
    refresh();
  } catch (err) {
    removeError.textContent = err.message;
  }
});
rerunBtn.addEventListener("click", () => {
  if (checkedIds.size === 0) return;
  rerunJobs([...checkedIds]);
});

async function retryJobs(ids) {
  try {
    const result = await api("/api/jobs/retry", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (result.skipped && result.skipped.length && !(result.retried || []).length) {
      countsEl.textContent = result.skipped.map((row) => row.reason).join("; ");
    }
    refresh();
  } catch (err) {
    countsEl.textContent = err.message;
  }
}

async function rerunJobs(ids) {
  try {
    const result = await api("/api/jobs/rerun", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    const created = result.created || [];
    checkedIds.clear();
    for (const id of created) checkedIds.add(id);
    if (created.length) selectedId = created[0];
    refresh();
  } catch (err) {
    countsEl.textContent = err.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  addError.textContent = "";
  const data = new FormData(form);
  const presets = [...form.querySelectorAll("input[name=preset]:checked")].map((el) => el.value);
  const cookies = String(data.get("cookies") || "")
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [name, ...rest] = part.split("=");
      return { name: name.trim(), value: rest.join("=").trim() };
    });
  const payload = {
    name: data.get("name") || null,
    urls: String(data.get("urls") || "").split(/\s+/).filter(Boolean),
    presets,
    output_dir: data.get("output_dir") || null,
    user_agent: data.get("user_agent") || null,
    cookies,
  };
  for (const key of ["viewport_width", "viewport_height", "stable_ms", "timeout_ms", "video_seconds"]) {
    const value = data.get(key);
    if (value) payload[key] = Number(value);
  }
  try {
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    form.querySelector("input[value=desktop]").checked = true;
    dialog.close();
    refresh();
  } catch (err) {
    addError.textContent = err.message;
  }
});

refresh();
setInterval(refresh, 1000);

document.addEventListener("click", async (event) => {
  const link = event.target.closest("a.file");
  if (!link || !authToken()) return;
  event.preventDefault();
  try {
    const resp = await fetch(link.getAttribute("href"), { headers: headers() });
    if (!resp.ok) throw new Error(resp.statusText);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener");
  } catch (err) {
    countsEl.textContent = err.message;
  }
});
