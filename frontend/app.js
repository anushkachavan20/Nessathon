function resolveApiBase() {
  const { protocol, hostname, port } = window.location;

  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    if (port === '3000') {
      return `${protocol}//${hostname}:8000`;
    }
    if (port === '8080') {
      return `${protocol}//${hostname}:8081`;
    }
    return `${protocol}//${hostname}:8000`;
  }

  const remotePortHost = hostname.match(/^(\d+)-(.*)$/);
  if (remotePortHost) {
    return `${protocol}//8081-${remotePortHost[2]}`;
  }

  return `${protocol}//${hostname}:8081`;
}

const API = resolveApiBase();
let selectedIncidentId = null;
let incidentCache = [];
let nextScanAllowedAt = 0;

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });

  const raw = await res.text();
  let data = {};
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (_e) {
      data = { raw };
    }
  }

  if (!res.ok) {
    throw new Error(JSON.stringify(data));
  }
  return data;
}

function setStatus(text) {
  document.getElementById('status').textContent = text;
}

async function inject(scenario, enabled) {
  try {
    const result = await api('/inject-failure', {
      method: 'POST',
      body: JSON.stringify({ scenario, enabled })
    });
    const observedStatus = result.forwarded_to_observed ? 'observed service synced' : 'observed service unavailable';
    const disabledOthers = Array.isArray(result.disabled_others) && result.disabled_others.length
      ? `; auto-disabled: ${result.disabled_others.join(', ')}`
      : '';
    setStatus(`Failure "${scenario}" set to ${enabled} (${observedStatus}). Generating traffic for scan data...`);
    // Auto-generate 20 orders so monitor scan has telemetry to analyze
    if (enabled) {
      for (let i = 0; i < 20; i++) {
        try {
          await api('/orders', {
            method: 'POST',
            body: JSON.stringify({
              customer_id: `c-${Math.floor(Math.random() * 9999)}`,
              amount: Number((Math.random() * 200 + 20).toFixed(2))
            })
          });
        } catch (_) { /* errors expected during failure injection */ }
      }
      // Auto-scan right after traffic generation so incidents are created immediately.
      try {
        const scanResult = await api('/monitor/scan', { method: 'POST' });
        if (scanResult.message) {
          setStatus(`Failure "${scenario}" ON — 20 orders generated, but scan says: ${scanResult.message}${disabledOthers}`);
        } else {
          setStatus(`Failure "${scenario}" ON — incident ${scanResult.incident?.id || 'updated'} ${scanResult.deduplicated ? 'deduplicated' : 'created'} automatically${disabledOthers}.`);
        }
      } catch (scanErr) {
        setStatus(`Failure "${scenario}" ON — traffic generated; auto-scan failed: ${scanErr.message}${disabledOthers}`);
      }
    } else {
      setStatus(`Failure "${scenario}" OFF.${disabledOthers}`);
    }
    await refreshAll();
    return result;
  } catch (err) {
    setStatus(`Inject failed: ${err.message}`);
    // Still try to refresh even if inject had an error
    try { await refreshAll(); } catch(e) { }
  }
}

async function createSampleOrder() {
  try {
    const payload = {
      customer_id: `c-${Math.floor(Math.random() * 1000)}`,
      amount: Number((Math.random() * 200 + 20).toFixed(2))
    };
    await api('/orders', { method: 'POST', body: JSON.stringify(payload) });
    setStatus('Order created.');
  } catch (err) {
    setStatus(`Order error (expected during failures): ${err.message}`);
  }
  await refreshAll();
}

async function bulkOrders(count) {
  for (let i = 0; i < count; i += 1) {
    await createSampleOrder();
  }
}

async function runScan() {
  const now = Date.now();
  if (now < nextScanAllowedAt) {
    const waitSeconds = Math.ceil((nextScanAllowedAt - now) / 1000);
    setStatus(`Scan cooldown active. Wait ${waitSeconds}s to avoid LLM rate limits.`);
    return;
  }

  nextScanAllowedAt = Date.now() + 30000;
  try {
    const result = await api('/monitor/scan', { method: 'POST' });
    if (result.message) {
      setStatus(`Scan: ${result.message} — inject failure and create orders first, or click "Inject CPU+Memory+OOM Signals".`);
    } else {
      setStatus(`Scan complete. ${result.deduplicated ? 'Deduplicated existing incident.' : 'New incident created — review in the panel.'}`);
    }
  } catch (err) {
    if ((err.message || '').includes('429')) {
      nextScanAllowedAt = Date.now() + 60000;
    }
    setStatus(`Scan failed: ${err.message}`);
  }
  await refreshAll();
}

async function injectMemoryLeakSignals() {
  try {
    await api('/monitor/ingest', {
      method: 'POST',
      body: JSON.stringify({
        service: 'order-api',
        cpu_pct: 95,
        memory_mb: 980,
        logs: [
          'OutOfMemoryError: Java heap space',
          'GC overhead limit exceeded',
          'Possible memory leak in request worker'
        ],
        alerts: [
          'critical: cpu_spike_memory_pressure',
          'critical: sustained_memory_growth'
        ]
      })
    });
    setStatus('Injected CPU/memory/log signals for memory leak scenario.');
  } catch (err) {
    setStatus(`Signal ingest failed: ${err.message}`);
  }
  await refreshAll();
}

function incidentHtml(incident) {
  const activeClass = selectedIncidentId === incident.id ? 'active' : '';
  return `
    <div class="list-item selectable ${activeClass}" onclick="selectIncident('${incident.id}')">
      <div><b>${incident.severity}</b> | <b>${incident.scenario}</b> | confidence: <b>${incident.confidence}</b></div>
      <div>${incident.summary}</div>
      <div class="small">root_cause: ${incident.root_cause || 'n/a'}</div>
      <div class="small">error_rate: ${incident.metrics.error_rate}, p95: ${incident.metrics.p95_latency}ms, cpu_peak: ${incident.metrics.cpu_peak ?? '-'}%, mem_peak: ${incident.metrics.memory_peak ?? '-'}MB, samples: ${incident.metrics.sample_size}</div>
      <div><b>Proposed fix:</b> ${incident.proposed_fix.action}</div>
      <div class="small">AI engine: ${incident.ai_output?.engine || 'n/a'} | AI confidence: ${incident.ai_output?.confidence ?? incident.confidence}</div>
      <div class="small">Risk: ${incident.proposed_fix.risk} | Click to open assistant panel</div>
      <div class="small">Status: ${incident.status} | Created: ${incident.created_at}</div>
    </div>
  `;
}

function renderSidePanel() {
  const root = document.getElementById('incident-panel');
  if (!selectedIncidentId) {
    root.innerHTML = 'Select an incident from the list to review and take action.';
    return;
  }

  const incident = incidentCache.find((x) => x.id === selectedIncidentId);
  if (!incident) {
    selectedIncidentId = null;
    root.innerHTML = 'Selected incident is no longer available. Pick another incident.';
    return;
  }

  const canApprove = incident.status === 'open' || incident.status === 'approved';
  const canDeny = incident.status === 'open' || incident.status === 'approved';
  const canExecute = incident.status === 'approved';
  const suggestedFix = (incident.proposed_fix?.action || 'n/a')
    .split(' && ')
    .map((cmd) => cmd.trim())
    .filter(Boolean)
    .join('<br/>');

  root.innerHTML = `
    <div><b>Incident:</b> ${incident.id}</div>
    <div><b>Service:</b> ${incident.service}</div>
    <div><b>Scenario:</b> ${incident.scenario}</div>
    <div><b>Severity:</b> ${incident.severity}</div>
    <div><b>Confidence:</b> ${incident.confidence}</div>
    <div><b>Status:</b> ${incident.status}</div>
    <hr />
    <div><b>Summary</b></div>
    <div>${incident.summary}</div>
    <div class="small"><b>Root Cause:</b> ${incident.root_cause || 'n/a'}</div>
    <div class="small"><b>AI Engine:</b> ${incident.ai_output?.engine || 'n/a'} | <b>AI Confidence:</b> ${incident.ai_output?.confidence ?? incident.confidence}</div>
    <hr />
    <div><b>Suggested Fix</b></div>
    <div><code>${suggestedFix}</code></div>
    <div class="small"><b>Verify:</b> ${incident.proposed_fix.verify}</div>
    <div class="small"><b>Risk:</b> ${incident.proposed_fix.risk}</div>
    <div class="panel-actions">
      ${canApprove ? `<button onclick="approveIncident('${incident.id}')">Approve</button>` : ''}
      ${canDeny ? `<button class="danger" onclick="denyIncident('${incident.id}')">Deny</button>` : ''}
      ${canExecute ? `<button onclick="executeIncident('${incident.id}')">Execute</button>` : ''}
    </div>
  `;
}

function selectIncident(id) {
  selectedIncidentId = id;
  renderSidePanel();
  refreshIncidents();
}

async function approveIncident(id) {
  try {
    await api(`/incidents/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ approved_by: 'hackathon-operator' })
    });
    setStatus(`Incident ${id} approved.`);
  } catch (err) {
    setStatus(`Approve failed: ${err.message}`);
  }
  await refreshAll();
}

async function denyIncident(id) {
  try {
    await api(`/incidents/${id}/deny`, {
      method: 'POST',
      body: JSON.stringify({ denied_by: 'hackathon-operator', reason: 'Needs manual check' })
    });
    setStatus(`Incident ${id} denied.`);
  } catch (err) {
    setStatus(`Deny failed: ${err.message}`);
  }
  await refreshAll();
}

async function executeIncident(id) {
  try {
    await api(`/incidents/${id}/execute`, { method: 'POST' });
    setStatus(`Incident ${id} executed and resolved.`);
  } catch (err) {
    setStatus(`Execute failed: ${err.message}`);
  }
  await refreshAll();
}

async function refreshIncidents() {
  const items = await api('/incidents');
  incidentCache = items;
  if (!selectedIncidentId && items.length) {
    selectedIncidentId = items[0].id;
  }
  const root = document.getElementById('incidents');
  root.innerHTML = items.length ? items.map(incidentHtml).join('') : '<div class="small">No incidents yet.</div>';
  renderSidePanel();
}

async function refreshTelemetry() {
  const items = await api('/telemetry?limit=20');
  const root = document.getElementById('telemetry');
  root.innerHTML = items
    .map((x) => `<div class="list-item small">${x.time} | ${x.type} | ${x.status || x.message || ''} | latency=${x.latency_ms || '-'} | queue=${x.queue_lag ?? '-'}</div>`)
    .join('');
}

async function refreshAudit() {
  const items = await api('/audit?limit=20');
  const root = document.getElementById('audit');
  root.innerHTML = items
    .slice()
    .reverse()
    .map((x) => `<div class="list-item small">${x.time} | ${x.action} | ${JSON.stringify(x.details)}</div>`)
    .join('');
}

async function refreshPersistedAudit() {
  const items = await api('/audit/persisted?limit=20');
  const root = document.getElementById('audit-persisted');
  root.innerHTML = items
    .slice()
    .reverse()
    .map((x) => `<div class="list-item small">${x.time} | ${x.action} | ${JSON.stringify(x.details)}</div>`)
    .join('');
}

async function refreshHealth() {
  try {
    const h = await api('/health');
    const c = h.components || {};
    const color = h.status === 'ok' ? '#22c55e' : h.status === 'degraded' ? '#f59e0b' : '#ef4444';
    const badge = (s) => {
      const bc = s === 'ok' ? '#22c55e' : s === 'warning' ? '#f59e0b' : '#ef4444';
      return `<span style="background:${bc};color:#fff;border-radius:4px;padding:1px 7px;font-size:11px;">${s.toUpperCase()}</span>`;
    };
    const rows = Object.entries(c).map(([name, v]) =>
      `<tr><td style="padding:2px 8px;">${name}</td><td>${badge(v.status)}</td><td style="color:#888;font-size:11px;">${v.error || v.note || (v.memory_mb ? v.memory_mb + ' MB' : '')}</td></tr>`
    ).join('');
    document.getElementById('health-status').innerHTML =
      `<div style="margin-bottom:4px;">Overall: <b style="color:${color};">${h.status.toUpperCase()}</b></div>
       <table style="width:100%;border-collapse:collapse;">${rows}</table>`;
  } catch(e) {
    document.getElementById('health-status').textContent = 'Backend unreachable';
  }
}

async function refreshAll() {
  try { await refreshHealth(); } catch(e) { console.error('Health refresh failed:', e); }
  try { await refreshIncidents(); } catch(e) { console.error('Incidents refresh failed:', e); }
  try { await refreshTelemetry(); } catch(e) { console.error('Telemetry refresh failed:', e); }
  try { await refreshAudit(); } catch(e) { console.error('Audit refresh failed:', e); }
  try { await refreshPersistedAudit(); } catch(e) { console.error('Persisted audit refresh failed:', e); }
}

refreshAll();
