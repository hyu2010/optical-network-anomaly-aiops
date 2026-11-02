/* Network Fault Simulator — frontend logic.
   Loads topology + faults catalog, lets the user trigger a fault, renders
   the resulting alarm cascade, keeps a incident-history sidebar, and
   highlights affected NEs/spans on the canvas. */

const $ = (sel) => document.querySelector(sel);

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

let topology = null;       // {nodes, edges, topology_id}
let visNetwork = null;     // vis-network instance
let visNodes = null;       // vis DataSet
let visEdges = null;       // vis DataSet
let faultMeta = null;      // {faults: [...], targets: {link, link_z, ne, board, port}}

// ----- topology rendering ------------------------------------------------

async function loadTopology() {
  topology = await fetch("/api/topology").then(r => r.json());
  $("#topology-id").textContent = `topology=${topology.topology_id}`;

  visNodes = new vis.DataSet(topology.nodes.map(n => ({
    id: n.id, label: n.label, x: n.x, y: n.y, fixed: false,
    shape: "box",
    color: { background: "#cee6ff", border: "#0969da" },
    font:  { color: "#1f2328", size: 14, face: "monospace" },
  })));
  visEdges = new vis.DataSet(topology.edges.map(e => ({
    id: e.id, from: e.from, to: e.to, label: e.label,
    arrows: e.arrows,
    color:  { color: "#999" },
    font:   { color: "#6a737d", size: 11, align: "top" },
    smooth: { enabled: true, type: "curvedCW", roundness: 0.18 },
    services: e.services,
  })));

  visNetwork = new vis.Network($("#topology-canvas"),
    { nodes: visNodes, edges: visEdges },
    {
      physics: { enabled: false },
      interaction: { hover: true, zoomView: true, dragView: true,
                     dragNodes: true },
      edges: { width: 2 },
    },
  );

  visNetwork.on("selectNode", (params) => {
    if (!params.nodes.length) return;
    const ne = params.nodes[0];
    autoFillTarget("ne", ne);
  });
  visNetwork.on("selectEdge", (params) => {
    if (!params.edges.length) return;
    const span = params.edges[0];
    autoFillTarget("link", span);
  });
}

// ----- fault catalog + form ----------------------------------------------

async function loadFaults() {
  faultMeta = await fetch("/api/faults").then(r => r.json());
  // Wire up both fault-picker blocks (R1 always visible; R2 hidden by default).
  populateFaultSelect("");      // primary block
  populateFaultSelect("-2");    // compound block
}

function populateFaultSelect(suffix) {
  const sel = $(`#fault-select${suffix}`);
  sel.innerHTML = "";
  faultMeta.faults.forEach(f => {
    const opt = document.createElement("option");
    opt.value = f.id;
    opt.textContent = f.label;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => onFaultChange(suffix));
  onFaultChange(suffix);
}

function onFaultChange(suffix) {
  const fault = currentFault(suffix);
  $(`#fault-help${suffix}`).textContent =
    `target_kind = ${fault.target_kind}` +
    (fault.keywords.length
      ? `; L2 keywords = [${fault.keywords.join(", ")}]`
      : "");
  populateTargetSelect(fault.target_kind, suffix);
}

function currentFault(suffix = "") {
  const id = $(`#fault-select${suffix}`).value;
  return faultMeta.faults.find(f => f.id === id);
}

function populateTargetSelect(kind, suffix) {
  const sel = $(`#target-select${suffix}`);
  sel.innerHTML = "";
  const boardBlock = $(`#target-board-block${suffix}`);
  boardBlock.hidden = (kind !== "board");
  const zBlock = $(`#target-z-block${suffix}`);
  if (zBlock) zBlock.hidden = (kind !== "link_z");

  if (kind === "board") {
    const nes = [...new Set(faultMeta.targets.board.map(([ne]) => ne))];
    nes.forEach(ne => {
      const opt = document.createElement("option");
      opt.value = ne; opt.textContent = ne;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", () => populateBoardSelect(suffix), { once: true });
    populateBoardSelect(suffix);
  } else if (kind === "link_z") {
    // connector_dirty — span dropdown plus z_km/magnitude inputs.  Each span
    // carries its length so we can default z to mid-span.
    const spans = faultMeta.targets.link_z || [];
    spans.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = `${s.id} (${s.length_km} km)`;
      opt.dataset.lengthKm = s.length_km;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", () => defaultZForSpan(suffix));
    defaultZForSpan(suffix);
  } else {
    const opts = faultMeta.targets[kind] || [];
    opts.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      sel.appendChild(opt);
    });
  }
}

function defaultZForSpan(suffix = "") {
  // Set z_km to mid-span for the selected connector_dirty span and bound the
  // input to the span length.
  const sel = $(`#target-select${suffix}`);
  const opt = sel.selectedOptions[0];
  const zInput = $(`#target-z-input${suffix}`);
  if (!opt || !zInput) return;
  const len = parseFloat(opt.dataset.lengthKm || "0");
  if (len > 0) {
    zInput.max = len;
    zInput.value = Math.round(len * 0.5 * 10) / 10;  // mid-span, 0.1 km
  }
}

function populateBoardSelect(suffix = "") {
  const ne = $(`#target-select${suffix}`).value;
  const sel = $(`#target-board-select${suffix}`);
  sel.innerHTML = "";
  const pairs = faultMeta.targets.board.filter(([n]) => n === ne);
  pairs.forEach(([_n, b]) => {
    const opt = document.createElement("option");
    opt.value = b; opt.textContent = b;
    sel.appendChild(opt);
  });
}

function readFaultSpec(suffix = "") {
  const f = currentFault(suffix);
  if (!f) return null;
  const target = $(`#target-select${suffix}`).value;
  const args = [target];
  if (f.target_kind === "board") args.push($(`#target-board-select${suffix}`).value);
  if (f.target_kind === "link_z") {
    args.push(parseFloat($(`#target-z-input${suffix}`).value));
    args.push(parseFloat($(`#target-mag-input${suffix}`).value));
  }
  return { fault_type: f.id, args, target_kind: f.target_kind };
}

function autoFillTarget(kind, value) {
  // If a fault matching this kind is already selected, just set the dropdown.
  const f = currentFault();
  if (f && f.target_kind === kind) {
    $("#target-select").value = value;
    return;
  }
  // Otherwise switch to the first fault that uses this kind.
  const candidate = faultMeta.faults.find(x => x.target_kind === kind);
  if (candidate) {
    $("#fault-select").value = candidate.id;
    onFaultChange();
    setTimeout(() => { $("#target-select").value = value; }, 0);
  }
}

// ----- triggering --------------------------------------------------------

async function trigger() {
  const compound = $("#compound-checkbox").checked;
  const f1 = readFaultSpec("");
  if (!f1) return;

  let url, body;
  if (compound) {
    const f2 = readFaultSpec("-2");
    if (!f2) return;
    const delay = parseInt($("#delay-ms-input").value || "0", 10) || 0;
    url = "/api/trigger_multi";
    body = { faults: [f1, f2], delay_ms: delay };
  } else {
    url = "/api/trigger";
    body = { fault_type: f1.fault_type, args: f1.args };
  }

  $("#trigger-btn").disabled = true;
  $("#trigger-btn").textContent = "...";
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (resp.error) {
      alert("Trigger failed: " + resp.error);
      return;
    }
    showCascade(resp);
    highlightOnCanvas(resp);
    const useSuppressed = $("#suppress-checkbox").checked;
    const sid = useSuppressed ? resp.incident_id_suppressed
                              : resp.incident_id_raw;
    showPpe(sid, resp.ppe_trace_count || 0);
    await refreshHistory();
  } finally {
    $("#trigger-btn").disabled = false;
    $("#trigger-btn").textContent = "Trigger";
  }
}

function showCascade(resp) {
  const useSuppressed = $("#suppress-checkbox").checked;
  const list = useSuppressed ? resp.suppressed : resp.raw;
  const sid  = useSuppressed ? resp.incident_id_suppressed : resp.incident_id_raw;

  // Render group legend, e.g. "R1=fiber_cut span_OMS3_BC | R2=ne_power_off E"
  const groups = resp.groups || [{ id: "R1",
                                   fault_type: resp.fault_type,
                                   fault_target: resp.fault_target }];
  const legend = groups.map(g =>
    `<span class="group-tag tag-${g.id}">${g.id}</span> ${g.fault_type} on ${g.fault_target}`
  ).join(" &nbsp;|&nbsp; ");

  $("#alarms-heading").innerHTML =
    `Alarms <span class="muted">incident #${sid} — ${legend} ` +
    `(${useSuppressed ? "suppressed view" : "raw cascade"} — ${list.length} alarms)</span>`;

  const tbody = $("#alarms-table tbody");
  tbody.innerHTML = "";
  list.forEach((a, i) => {
    const tr = document.createElement("tr");
    if (a.is_root) tr.classList.add("is-root");
    const gid = a.group_id || "R1";
    tr.classList.add(`row-${gid}`);
    const loc = (a.port || a.board || a.ne || "-");
    const desc = a.description || "";
    const tObs  = a.t_observed_ms ?? a.t_offset_ms;
    const tProp = a.t_offset_ms;
    const tTip = `propagation: +${tProp} ms\nobserved (poll-jittered): +${tObs} ms`;
    tr.innerHTML =
      `<td>${i + 1}</td>` +
      `<td title="${escapeHtml(desc)}">${a.name}</td>` +
      `<td class="col-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</td>` +
      `<td>${a.layer}</td>` +
      `<td>${loc}</td>` +
      `<td>${a.container_id || ""}</td>` +
      `<td class="severity-${a.severity}">${a.severity}</td>` +
      `<td title="${tTip}">${tObs}</td>` +
      `<td>${a.is_root ? "★" : ""}</td>` +
      `<td><span class="group-tag tag-${gid}">${gid}</span></td>`;
    tbody.appendChild(tr);
  });
}

function showPpe(incidentId, traceCount) {
  // The server renders PPE on-demand by incident id; cache-bust with a
  // timestamp so re-triggering the same fault doesn't show a stale image.
  const wrap = $("#ppe-image-wrap");
  const placeholder = $("#ppe-placeholder");
  const img = $("#ppe-image");
  const link = $("#ppe-image-link");
  const meta = $("#ppe-meta");
  if (!incidentId || !traceCount) {
    wrap.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent =
      incidentId ? "This incident has no PPE traces (PPE was not generated)."
                 : "Trigger a fault to see the longitudinal power profile.";
    meta.textContent = "";
    img.removeAttribute("src");
    return;
  }
  const url = `/api/incidents/${incidentId}/ppe.png?t=${Date.now()}`;
  img.src = url;
  link.href = url;
  meta.textContent = ` — incident #${incidentId}, ${traceCount} trace${traceCount === 1 ? "" : "s"}`;
  placeholder.hidden = true;
  wrap.hidden = false;
  showPpeAnomalies(incidentId);
}

// Render the points the receiver flagged abnormal — the localised root cause
// (which OMS span and how far into it), per dropping OCh.
async function showPpeAnomalies(incidentId) {
  const box = $("#ppe-anomalies");
  if (!box) return;
  box.innerHTML = "";
  let data;
  try {
    data = await fetch(`/api/incidents/${incidentId}/ppe.json`).then(r => r.json());
  } catch (e) { return; }
  const rows = [];
  for (const tr of (data.traces || [])) {
    if (tr.degraded) {
      rows.push(`<li><strong>${tr.och} → ${tr.drop_ne}</strong>: ` +
                `<span class="sup-flag">DEGRADED — receiver sees no light</span></li>`);
    }
    for (const a of (tr.anomalies || [])) {
      if (!a.is_root) continue;            // show ground-truth flags
      let where;
      if (a.span_id && a.offset_in_span_km != null) {
        where = `${a.span_id.replace("span_", "")} @ ${a.offset_in_span_km.toFixed(1)} km` +
                (a.z_km != null ? ` (path z=${a.z_km.toFixed(1)} km)` : "");
      } else {
        where = "receiver-side (no span localisation)";
      }
      rows.push(`<li><strong>${tr.och} → ${tr.drop_ne}</strong>: ` +
                `<span class="group-tag tag-R1">${a.kind}</span> ${where}` +
                (a.magnitude_db != null ? ` &nbsp;<span class="muted">${a.magnitude_db.toFixed(2)} dB</span>` : "") +
                `</li>`);
    }
  }
  if (rows.length) {
    box.innerHTML = `<div class="small" style="margin-top:6px">` +
      `<strong>Flagged by PPE (localised root cause):</strong>` +
      `<ul class="ppe-anom-list">${rows.join("")}</ul></div>`;
  }
}

function highlightOnCanvas(resp) {
  // reset all NE/edge styles
  topology.nodes.forEach(n => visNodes.update({
    id: n.id, color: { background: "#cee6ff", border: "#0969da" }
  }));
  topology.edges.forEach(e => visEdges.update({
    id: e.id, color: { color: "#999" }, dashes: false, width: 2
  }));

  const list = resp.raw;
  const affectedNEs = new Set(list.map(a => a.ne).filter(Boolean));
  const affectedSpans = new Set(
    list.map(a => a.container_id).filter(c => c && c.startsWith("span_"))
  );
  affectedNEs.forEach(ne => visNodes.update({
    id: ne, color: { background: "#ffd9d9", border: "#d1242f" },
  }));
  affectedSpans.forEach(span => visEdges.update({
    id: span, color: { color: "#d1242f" }, dashes: [4, 4], width: 3,
  }));
}

// ----- history -----------------------------------------------------------

async function refreshHistory() {
  const list = await fetch("/api/incidents?limit=20").then(r => r.json());
  const ul = $("#incident-history");
  ul.innerHTML = "";
  list.forEach(s => {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    li.innerHTML =
      `<strong>#${s.id}</strong> ${s.fault_type} ` +
      `<span class="muted">on ${s.fault_target}</span> ` +
      `<span class="muted">(${s.alarm_count} alarms)</span>` +
      `<span class="sup-flag">${s.suppressed ? "[suppressed]" : "[raw]"}</span>` +
      ` <a class="view-link" href="/api/incidents/${s.id}/dump.txt" ` +
      `title="View the Huawei NMS dump on this page">view .txt</a>` +
      ` <a class="dump-link" href="/api/incidents/${s.id}/dump.txt" ` +
      `download="incident_${s.id}_dump.txt" title="Download Huawei NMS dump">&darr;</a>`;
    li.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("dump-link")) return;  // let the download link work
      ev.preventDefault();            // the view-link must not navigate away
      loadIncident(s.id);
      showDump(s.id);
    });
    ul.appendChild(li);
  });
}

// Fetch an incident's Huawei NMS dump and render it inline (no download).
async function showDump(id) {
  const viewer = $("#dump-viewer");
  const codeEl = $("#dump-text");
  const titleEl = $("#dump-title");
  const dl = $("#dump-download");
  // Mark the active row in the history list.
  document.querySelectorAll("#incident-history li").forEach(li =>
    li.classList.toggle("active", parseInt(li.dataset.id, 10) === id));
  titleEl.textContent = `incident #${id} — Huawei NMS dump`;
  dl.href = `/api/incidents/${id}/dump.txt`;
  dl.setAttribute("download", `incident_${id}_dump.txt`);
  viewer.hidden = false;
  codeEl.textContent = "loading…";
  try {
    const resp = await fetch(`/api/incidents/${id}/dump.txt`);
    codeEl.textContent = await resp.text();
  } catch (e) {
    codeEl.textContent = `Failed to load dump for incident ${id}: ${e}`;
  }
}

async function loadIncident(id) {
  const s = await fetch(`/api/incidents/${id}`).then(r => r.json());
  // Look up the trace count for this incident (cheap dedicated query path).
  let traceCount = 0;
  try {
    const meta = await fetch(`/api/incidents/${id}/ppe.json`).then(r => r.json());
    traceCount = meta.n_traces || 0;
  } catch (e) { /* tolerated — older DBs without PPE tables */ }
  // Reconstruct the groups array from the alarms' group_id values; match the
  // /api/trigger_multi response shape so showCascade renders the legend.
  const seen = new Map();
  for (const a of s.alarms) {
    const g = a.group_id || "R1";
    if (!seen.has(g)) seen.set(g, { id: g, fault_type: "?", fault_target: "?" });
  }
  // Best effort: split a compound fault_target like "R1:span_OMS3_BC | R2:E"
  // back into per-group fields.
  const fts = (s.fault_type || "").split("+");
  const tgts = (s.fault_target || "").split(" | ");
  Array.from(seen.values()).forEach((g, i) => {
    g.fault_type   = fts[i]  || s.fault_type   || "?";
    g.fault_target = (tgts[i] || s.fault_target || "?").replace(/^R\d+:/, "");
  });
  const fakeResp = {
    incident_id_raw: id, incident_id_suppressed: id,
    fault_type: s.fault_type, fault_target: s.fault_target,
    groups: Array.from(seen.values()),
    raw: s.alarms, suppressed: s.alarms,
  };
  $("#suppress-checkbox").checked = s.suppressed;
  showCascade(fakeResp);
  highlightOnCanvas(fakeResp);
  showPpe(id, traceCount);
}

// ----- buttons -----------------------------------------------------------

async function clearAll() {
  if (!confirm("Wipe all incidents from incidents.db?")) return;
  await fetch("/api/clear", { method: "POST" });
  $("#alarms-table tbody").innerHTML = "";
  $("#alarms-heading").innerHTML = `Alarms <span class="muted">(empty)</span>`;
  $("#dump-viewer").hidden = true;   // nothing left to review
  await refreshHistory();
  // reset canvas highlights
  topology.nodes.forEach(n => visNodes.update({
    id: n.id, color: { background: "#cee6ff", border: "#0969da" }
  }));
  topology.edges.forEach(e => visEdges.update({
    id: e.id, color: { color: "#999" }, dashes: false, width: 2
  }));
  showPpe(null, 0);
}

// ----- YANG Files & Services tab ----------------------------------------

const _jsonLoaded = new Set();

function highlightJson(text) {
  // Minimal token colorizer.  Operates on already-escaped text.
  return text.replace(
    /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\b\d+\.?\d*\b/g,
    (m, str, colon, bool_) => {
      if (str) {
        return colon
          ? `<span class="json-key">${str}</span>${colon}`
          : `<span class="json-string">${str}</span>`;
      }
      if (bool_) return `<span class="json-${bool_ === "null" ? "null" : "bool"}">${m}</span>`;
      return `<span class="json-number">${m}</span>`;
    },
  );
}

async function loadYangFile(name) {
  if (_jsonLoaded.has(name)) return;
  const text = await fetch(`/api/yang/${name}`).then(r => r.text());
  let pretty;
  try {
    pretty = JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    pretty = text;   // fall back to raw if anything else surfaced
  }
  $(`#json-${name}`).innerHTML = highlightJson(escapeHtml(pretty));
  _jsonLoaded.add(name);
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab-content").forEach(c => {
    const isActive = c.dataset.pane === name;
    c.classList.toggle("active", isActive);
    c.hidden = !isActive;
  });
  if (name !== "services") loadYangFile(name);
}

async function loadServices() {
  const resp = await fetch("/api/services").then(r => r.json());
  $("#services-count").textContent = resp.n_services;
  $("#yang-meta").textContent =
    `${resp.n_services} services on topology ${resp.topology_id}`;
  const tbody = $("#services-table tbody");
  tbody.innerHTML = "";
  resp.services.forEach(s => {
    const tr = document.createElement("tr");
    const hasShared = s.spans.some(sp => sp.shared_with && sp.shared_with.length);
    if (hasShared) tr.classList.add("shared-span");
    const pathHtml = s.spans.map((sp, i) => {
      const shared = sp.shared_with && sp.shared_with.length;
      const cls = shared ? "span-chip shared" : "span-chip";
      const title = shared
        ? `shared with ${sp.shared_with.join(", ")} — R4 candidate`
        : "";
      const chip = `<span class="${cls}" title="${escapeHtml(title)}">` +
                   `${sp.id}<small style="opacity:.7"> ${sp.length_km} km</small></span>`;
      return i > 0 ? `<span class="arrow">&rarr;</span>${chip}` : chip;
    }).join("");
    const r4 = hasShared
      ? `<span class="badge-share" title="this service's path shares a span with another service — R4 demo">R4</span>`
      : "";
    tr.innerHTML =
      `<td><strong>${escapeHtml(s.service_name)}</strong>${r4}</td>` +
      `<td>${escapeHtml(s.source)}</td>` +
      `<td>${escapeHtml(s.destination)}</td>` +
      `<td>${escapeHtml(s.och_tunnel)}</td>` +
      `<td>${escapeHtml(s.odu_tunnel)}</td>` +
      `<td>${pathHtml}</td>` +
      `<td>${s.total_km}</td>` +
      `<td>${s.tx_power_dbm ?? "—"}</td>`;
    tbody.appendChild(tr);
  });
}

function setupTabs() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
}

// ----- bootstrap ---------------------------------------------------------

(async function init() {
  await loadTopology();
  await loadFaults();
  await refreshHistory();
  setupTabs();
  await loadServices();
  $("#trigger-btn").addEventListener("click", trigger);
  $("#suppress-checkbox").addEventListener("change", () => {
    // re-render the latest incident if there is one selected in history
    const first = $("#incident-history li");
    if (first) loadIncident(parseInt(first.dataset.id, 10));
  });
  $("#clear-btn").addEventListener("click", clearAll);
  $("#dump-close").addEventListener("click", () => { $("#dump-viewer").hidden = true; });
  $("#compound-checkbox").addEventListener("change", (ev) => {
    $("#fault-block-2").hidden = !ev.target.checked;
  });
})();
