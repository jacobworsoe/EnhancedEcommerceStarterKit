const accountList = document.getElementById("account-list");
const scanForm = document.getElementById("scan-form");
const startButton = document.getElementById("start-button");
const logEl = document.getElementById("log");
const resultsSection = document.getElementById("results-section");

let pollTimer = null;

async function loadAccounts() {
  const res = await fetch("/api/accounts");
  const data = await res.json();
  accountList.innerHTML = "";
  if (data.accounts.length === 0) {
    accountList.innerHTML = '<li class="muted">No accounts connected yet.</li>';
    return;
  }
  for (const acc of data.accounts) {
    const li = document.createElement("li");
    const statusClass = acc.status === "connected" ? "status-ok" : "status-warn";
    li.innerHTML = `
      <span>${acc.email}</span>
      <span class="${statusClass}">${acc.status}</span>
      <button data-email="${acc.email}" class="disconnect">Disconnect</button>
    `;
    accountList.appendChild(li);
  }
  document.querySelectorAll(".disconnect").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`/api/accounts/${encodeURIComponent(btn.dataset.email)}/disconnect`, { method: "POST" });
      loadAccounts();
    });
  });
}

function renderTable(tableId, rows, columns) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = columns.map((col) => `<td>${col(row)}</td>`).join("");
    tbody.appendChild(tr);
  }
}

function eur(value) {
  return `€${Number(value ?? 0).toFixed(2)}`;
}

function renderResults(result) {
  renderTable("ga4-table", result.ga4, [
    (r) => r.project_id,
    (r) => r.dataset_id,
    (r) => r.location,
    (r) => r.table_count,
    (r) => (r.active_bytes / 1024 ** 3).toFixed(2),
    (r) => (r.longterm_bytes / 1024 ** 3).toFixed(2),
    (r) => eur(r.monthly_cost_eur),
    (r) => r.cost_source,
  ]);
  renderTable("gtm-table", result.gtm, [
    (r) => r.project_id,
    (r) => r.service_type,
    (r) => r.service_name,
    (r) => r.region,
    (r) => (r.is_likely_gtm_ss ? "yes" : "no"),
    (r) => (r.avg_instances_30d != null ? r.avg_instances_30d.toFixed(2) : "n/a"),
    (r) => eur(r.monthly_cost_eur),
    (r) => r.cost_confidence,
    (r) => r.hostname ?? "unknown",
  ]);
  resultsSection.classList.remove("hidden");
}

async function pollStatus() {
  const res = await fetch("/api/scan/status");
  const data = await res.json();
  logEl.classList.remove("hidden");
  logEl.textContent = data.log.join("\n");
  logEl.scrollTop = logEl.scrollHeight;

  if (data.status === "running") {
    pollTimer = setTimeout(pollStatus, 1200);
    return;
  }

  startButton.disabled = false;
  startButton.textContent = "Start scan";

  if (data.status === "error") {
    logEl.textContent += `\n\nERROR: ${data.error}`;
    return;
  }

  if (data.status === "done") {
    const resultRes = await fetch("/api/scan/result");
    const result = await resultRes.json();
    renderResults(result);
  }
}

scanForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  startButton.disabled = true;
  startButton.textContent = "Scanning...";
  logEl.classList.remove("hidden");
  logEl.textContent = "";

  const body = {
    fx_rate: parseFloat(document.getElementById("fx-rate").value),
    billing_export_days: parseInt(document.getElementById("billing-export-days").value, 10),
  };
  const res = await fetch("/api/scan/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 409) {
    const data = await res.json();
    logEl.textContent = data.error;
    startButton.disabled = false;
    startButton.textContent = "Start scan";
    return;
  }
  if (pollTimer) clearTimeout(pollTimer);
  pollStatus();
});

loadAccounts();

const params = new URLSearchParams(window.location.search);
if (params.get("connected") || params.get("oauth_error")) {
  window.history.replaceState({}, "", window.location.pathname);
}
