/* Public Neo4j query results only: no database connection or credentials in the browser. */
(() => {
  const timeline = document.getElementById("meeting-timeline");
  if (!timeline) return;
  const payload = window.__MEETING_EVIDENCE_DATA__;
  const available = payload?.available && Array.isArray(payload.meetings)
    && Array.isArray(payload.nodes) && Array.isArray(payload.edges)
    && Array.isArray(payload.limitations);
  const meetings = new Map((available ? payload.meetings : [])
    .map((meeting) => [meeting.meeting_date, meeting]));
  const nodes = new Map((available ? payload.nodes : []).map((node) => [node.id, node]));
  const outgoing = new Map();
  (available ? payload.edges : []).forEach((edge) => {
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
    outgoing.get(edge.source).push(edge);
  });

  function element(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  }

  function related(id, relation) {
    return (outgoing.get(id) || [])
      .filter((edge) => edge.relation === relation)
      .map((edge) => nodes.get(edge.target))
      .filter(Boolean);
  }

  function renderValues(observation) {
    const values = element("details", undefined, "evidence-values");
    values.appendChild(element("summary", "View values"));
    const list = element("dl");
    Object.entries(JSON.parse(observation.details)).forEach(([key, value]) => {
      list.appendChild(element("dt", key.replace(/_/g, " ")));
      list.appendChild(element("dd", typeof value === "object" ? JSON.stringify(value) : String(value)));
    });
    values.appendChild(list);
    return values;
  }

  function renderEvidence(content, meetingDate) {
    content.replaceChildren();
    if (!available) {
      content.appendChild(element("p",
        payload?.reason || "Meeting evidence unavailable: no verified Neo4j export was loaded.", "note"));
      return;
    }
    const meeting = meetings.get(meetingDate);
    if (!meeting) {
      content.appendChild(element("p",
        "No evidence is available for this meeting. The current export only covers completed meetings included in the historical backtest.",
        "note"));
      return;
    }
    const forecast = related(meeting.id, "HAS_RECONSTRUCTION")[0];
    if (!forecast) throw new Error("Meeting evidence is missing its model reconstruction.");
    const signals = related(forecast.id, "USES_SIGNAL")
      .sort((a, b) => a.label.localeCompare(b.label));
    if (!signals.length) throw new Error("Meeting evidence has no economic inputs.");

    content.appendChild(element("p",
      `Recorded decision: ${meeting.actual.toUpperCase()}. Retrospective model: ${meeting.predicted.toUpperCase()}.`,
      "note"));
    content.appendChild(element("p",
      "These are reconstructed model inputs, not the Fed's stated reasoning. Bias runs from -1 (cut pressure) to +1 (raise pressure).",
      "note"));
    const wrap = element("div", undefined, "evidence-table-wrap");
    const table = element("table", undefined, "evidence-table");
    table.appendChild(element("caption", `Economic inputs for ${meetingDate}; availability dates are estimates.`));
    const head = element("thead");
    const header = element("tr");
    ["Source", "Signal", "Observation period", "Estimated available", "Model bias", "Data"].forEach((label) => {
      const cell = element("th", label);
      cell.scope = "col";
      header.appendChild(cell);
    });
    head.appendChild(header);
    table.appendChild(head);
    const body = element("tbody");
    signals.forEach((signal) => {
      const observation = related(signal.id, "DERIVED_FROM")[0];
      const source = observation && related(observation.id, "SOURCED_FROM")[0];
      if (!observation || !source || !Number.isFinite(signal.bias)) {
        throw new Error(`Incomplete evidence for ${signal.label}.`);
      }
      const row = element("tr");
      const sourceCell = element("td");
      if (/^https:\/\//i.test(source.source_url || "")) {
        const link = element("a", "Official source", "source-pill");
        link.href = source.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        sourceCell.appendChild(link);
      } else {
        sourceCell.textContent = "Unavailable";
      }
      row.appendChild(sourceCell);
      row.appendChild(element("td", signal.label));
      row.appendChild(element("td", observation.observation_period));
      row.appendChild(element("td", observation.estimated_available_on));
      row.appendChild(element("td", `${signal.bias > 0 ? "+" : ""}${signal.bias.toFixed(2)}`));
      const dataCell = element("td");
      dataCell.appendChild(renderValues(observation));
      row.appendChild(dataCell);
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    content.appendChild(wrap);
    content.appendChild(element("p", payload.limitations.join(" "), "note"));
  }

  timeline.addEventListener("toggle", (event) => {
    const card = event.target;
    if (!card.matches(".meeting-evidence-card")) return;
    card.querySelector(".evidence-toggle").textContent = card.open ? "Hide data" : "View data";
    if (!card.open || card.dataset.loaded === "true") return;
    const content = card.querySelector(".evidence-content");
    try {
      renderEvidence(content, card.dataset.meetingDate);
      card.dataset.loaded = "true";
    } catch (error) {
      console.error("Unable to display meeting evidence:", error);
      content.replaceChildren(element("p",
        "Meeting evidence could not be displayed because its exported data is incomplete or invalid.", "note"));
    }
  }, true);
})();
