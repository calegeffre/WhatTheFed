/* Public Neo4j query results only: no database connection or credentials in the browser. */
(() => {
  const payload = window.__MEETING_EVIDENCE_DATA__;
  const summary = document.getElementById("evidence-summary");
  const picker = document.getElementById("evidence-meeting");
  const layout = document.getElementById("evidence-layout");
  const svg = document.getElementById("evidence-graph");
  const details = document.getElementById("evidence-details");
  const limitations = document.getElementById("evidence-limitations");
  if (!summary || !picker || !layout || !svg || !details || !limitations) return;

  if (!payload?.available || !Array.isArray(payload.meetings) || !payload.meetings.length
      || !Array.isArray(payload.nodes) || !Array.isArray(payload.edges)
      || !Array.isArray(payload.limitations)) {
    picker.disabled = true;
    layout.hidden = true;
    summary.textContent = payload?.reason || "Meeting evidence unavailable: no Neo4j export was loaded.";
    console.warn("Meeting Evidence Explorer unavailable:", summary.textContent);
    return;
  }

  const nodes = new Map(payload.nodes.map((node) => [node.id, node]));
  const outgoing = new Map();
  payload.edges.forEach((edge) => {
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
    outgoing.get(edge.source).push(edge);
  });
  const meetings = [...payload.meetings].sort((a, b) => b.meeting_date.localeCompare(a.meeting_date));
  meetings.forEach((meeting) => {
    const option = document.createElement("option");
    option.value = meeting.id;
    option.textContent = `${meeting.meeting_date} | ${meeting.actual.toUpperCase()}`;
    picker.appendChild(option);
  });
  limitations.textContent = payload.limitations.join(" ");
  layout.hidden = false;

  const ns = "http://www.w3.org/2000/svg";
  const element = (tag, attributes = {}, text) => {
    const result = document.createElementNS(ns, tag);
    Object.entries(attributes).forEach(([key, value]) => result.setAttribute(key, String(value)));
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const color = { meeting: "#8dc8ff", decision: "#f7b267", forecast: "#bca7ff", signal: "#79c0ff", observation: "#7bd389", source: "#c6cdd7" };
  const relationLabel = {
    HAS_DECISION: "outcome", HAS_RECONSTRUCTION: "reconstructed",
    USES_SIGNAL: "uses", DERIVED_FROM: "derived from", SOURCED_FROM: "source",
  };

  function selectNode(node) {
    svg.querySelectorAll(".evidence-node").forEach((item) => {
      item.setAttribute("aria-pressed", String(item.dataset.nodeId === node.id));
    });
    details.replaceChildren();
    const title = document.createElement("h3");
    title.textContent = node.label;
    details.appendChild(title);
    const type = document.createElement("p");
    type.className = "note";
    type.textContent = `${node.kind.toUpperCase()} | ${node.meeting_date || node.observation_period || node.domain || "official source"}`;
    details.appendChild(type);
    const description = document.createElement("pre");
    description.textContent = node.details;
    details.appendChild(description);
    const fields = [
      ["Bias", node.bias],
      ["Observation period", node.observation_period],
      ["Estimated availability (not a verified release date)", node.estimated_available_on],
      ["Model version", node.model_version],
      ["Model probabilities", node.probabilities],
      ["Confidence", node.confidence],
      ["Target lower (%)", node.target_lower],
      ["Target upper (%)", node.target_upper],
    ];
    fields.forEach(([label, value]) => {
      if (value === undefined || value === null) return;
      const field = document.createElement("p");
      field.className = "note";
      field.textContent = `${label}: ${value}`;
      details.appendChild(field);
    });
    if (node.source_url && /^https:\/\//i.test(node.source_url)) {
      const link = document.createElement("a");
      link.className = "source-pill";
      link.href = node.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open official source";
      details.appendChild(link);
    }
    const relations = outgoing.get(node.id) || [];
    relations.forEach((edge) => {
      const target = nodes.get(edge.target);
      if (!target) return;
      const button = document.createElement("button");
      button.className = "year-tab";
      button.textContent = `${relationLabel[edge.relation]}: ${target.label}`;
      button.addEventListener("click", () => selectNode(target));
      details.appendChild(button);
    });
  }

  function render() {
    const meeting = meetings.find((item) => item.id === picker.value);
    if (!meeting) return;
    const selectedNodes = new Map();
    const edges = [];
    const queue = [meeting.id];
    while (queue.length) {
      const id = queue.shift();
      if (selectedNodes.has(id)) continue;
      const node = nodes.get(id);
      if (!node) continue;
      selectedNodes.set(id, node);
      (outgoing.get(id) || []).forEach((edge) => {
        edges.push(edge);
        queue.push(edge.target);
      });
    }
    const positions = new Map();
    const byKind = (kind) => [...selectedNodes.values()]
      .filter((node) => node.kind === kind)
      .sort((a, b) => (a.domain || a.id).localeCompare(b.domain || b.id));
    positions.set(meeting.id, { x: 12, y: 330 });
    byKind("decision").forEach((node) => positions.set(node.id, { x: 12, y: 460 }));
    byKind("forecast").forEach((node) => positions.set(node.id, { x: 230, y: 330 }));
    ["signal", "observation", "source"].forEach((kind, column) => {
      byKind(kind).forEach((node, index) => {
        positions.set(node.id, { x: 445 + column * 185, y: 55 + index * 88 });
      });
    });
    svg.replaceChildren();
    const defs = element("defs");
    const marker = element("marker", {
      id: "evidence-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse",
    });
    marker.appendChild(element("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#8196ac" }));
    defs.appendChild(marker);
    svg.appendChild(defs);
    [["Meeting / outcome", 12], ["Reconstruction", 230], ["Signal", 445], ["Observation", 630], ["Official source", 815]]
      .forEach(([label, x]) => svg.appendChild(element("text", { x, y: 24, fill: "#b8cbe0", "font-size": 15 }, label)));
    edges.forEach((edge) => {
      const from = positions.get(edge.source);
      const to = positions.get(edge.target);
      if (!from || !to) return;
      const sameColumn = from.x === to.x;
      svg.appendChild(element("path", {
        d: sameColumn
          ? `M ${from.x + 80} ${from.y + 58} L ${to.x + 80} ${to.y}`
          : `M ${from.x + 160} ${from.y + 29} C ${from.x + 190} ${from.y + 29}, ${to.x - 25} ${to.y + 29}, ${to.x} ${to.y + 29}`,
        fill: "none", stroke: "#8196ac", "stroke-opacity": 0.55, "marker-end": "url(#evidence-arrow)",
      }));
    });
    selectedNodes.forEach((node) => {
      const position = positions.get(node.id);
      const group = element("g", {
        class: "evidence-node", transform: `translate(${position.x} ${position.y})`,
        tabindex: 0, role: "button", "aria-label": `${node.kind}: ${node.label}`, "aria-pressed": "false",
      });
      group.dataset.nodeId = node.id;
      group.appendChild(element("rect", { width: 160, height: 58, rx: 10, fill: "#14263b", stroke: color[node.kind] }));
      group.appendChild(element("text", { x: 9, y: 23, fill: color[node.kind], "font-size": 13 }, node.label));
      group.appendChild(element("text", { x: 9, y: 44, fill: "#9db2c9", "font-size": 11 },
        node.kind === "signal" ? `Bias ${Number(node.bias).toFixed(2)}` : node.kind));
      group.appendChild(element("title", {}, `${node.label}: ${node.details}`));
      group.addEventListener("click", () => selectNode(node));
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectNode(node);
        }
      });
      svg.appendChild(group);
    });
    summary.textContent = `${meeting.meeting_date}: actual ${meeting.actual.toUpperCase()}, reconstructed model ${meeting.predicted.toUpperCase()}. ${selectedNodes.size} nodes from a verified Neo4j snapshot.`;
    selectNode(nodes.get(meeting.id));
  }
  picker.addEventListener("change", render);
  render();
})();
