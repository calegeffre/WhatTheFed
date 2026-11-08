(() => {
  const container = document.getElementById("data-treemap");
  if (!container) return;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function pressureScore(bias) {
    return finite(bias) ? Math.round((3 + 2 * clamp(bias, -1, 1)) * 10) / 10 : null;
  }

  function pressureLabel(score) {
    if (score === null) return "Data unavailable";
    if (score <= 1.5) return "Strong lower-rate pressure";
    if (score < 2.5) return "Lower-rate pressure";
    if (score < 3) return "Slight lower-rate pressure";
    if (score === 3) return "Balanced pressure";
    if (score < 3.5) return "Slight higher-rate pressure";
    if (score < 4.5) return "Higher-rate pressure";
    return "Strong higher-rate pressure";
  }

  function pressureTone(score) {
    if (score === null) return "missing";
    if (score <= 1.5) return "cold";
    if (score < 3) return "cool";
    if (score === 3) return "balanced";
    if (score < 4.5) return "warm";
    return "hot";
  }

  function reading(label, value, unit = "", multiplier = 1) {
    return [label, finite(value)
      ? `${(value * multiplier).toLocaleString("en-US", { maximumFractionDigits: 2 })}${unit}`
      : "Unavailable"];
  }

  function buildGroups() {
    const cpi = window.__CPI_DASHBOARD_DATA__;
    const ppi = window.__PPI_DASHBOARD_DATA__;
    const labor = window.__LABOR_DASHBOARD_DATA__;
    const breakeven = window.__BREAKEVEN_DASHBOARD_DATA__;
    const treasury = window.__TREASURY_DASHBOARD_DATA__;
    const funding = window.__POLICY_RATE_DASHBOARD_DATA__;
    const fiscal = window.__FISCAL_DASHBOARD_DATA__;
    const gdp = window.__GDP_DASHBOARD_DATA__;
    const fomc = window.__FOMC_DASHBOARD_DATA__;
    const market = window.__MARKET_DASHBOARD_DATA__;
    const card = (id, label, payload, bias, readings) => ({
      id, label, score: pressureScore(bias), readings,
      asOf: payload?.metric_date || payload?.latest_snapshot_at,
      sourceUrl: payload?.source_url,
    });
    const m = (payload) => payload?.metrics || {};
    const yieldAt = (maturity) => Array.isArray(treasury?.points)
      ? treasury.points.find((point) => point?.maturity === maturity)?.yield_pct : undefined;
    const y2 = yieldAt("2Y");
    const y10 = yieldAt("10Y");
    const curveBias = finite(y2) && finite(y10) ? (y10 - y2) / 1.5 : null;
    const marketCards = ["kalshi", "polymarket"].map((provider) => {
      const payload = market?.providers?.[provider];
      const raw = payload?.probabilities;
      const valid = ["raise", "hold", "cut"].every((key) => finite(raw?.[key]) && raw[key] >= 0);
      const total = valid ? raw.raise + raw.hold + raw.cut : 0;
      const probabilities = finite(total) && total > 0
        ? Object.fromEntries(Object.entries(raw).filter(([key]) => ["raise", "hold", "cut"].includes(key))
          .map(([key, value]) => [key, value / total]))
        : null;
      const outcome = probabilities && Object.entries(probabilities).sort((a, b) => b[1] - a[1])[0];
      return {
        id: provider, label: provider === "kalshi" ? "Kalshi" : "Polymarket",
        score: pressureScore(probabilities ? probabilities.raise - probabilities.cut : null),
        marketOutcome: outcome ? `${outcome[0].toUpperCase()} ${Math.round(outcome[1] * 100)}%` : null,
        asOf: payload?.latest_published_at,
        sourceUrl: provider === "kalshi" ? "https://kalshi.com" : "https://polymarket.com",
        sourceLabel: "Provider website",
        readings: [
          ["Target meeting", market?.target_meeting || "Unavailable"],
          ...["raise", "hold", "cut"].map((key) => reading(
            `${key.charAt(0).toUpperCase() + key.slice(1)} probability`, probabilities?.[key], "%", 100,
          )),
        ],
      };
    });
    const policyCards = (Array.isArray(fomc?.signals) ? fomc.signals : []).map((signal, index) => ({
      id: `policy-${index}`, label: signal?.label === "Policy Bias" || signal?.label === "Previous Meeting Bias"
        ? "Previous decision" : String(signal?.label || "Statement signal"),
      score: finite(signal?.score) && signal.score >= 1 && signal.score <= 5 ? signal.score : null,
      asOf: fomc.meeting_date, sourceUrl: fomc.source_url,
      readings: [
        ["Context", "Last FOMC statement, not a current data reading"],
        ["Recorded decision", String(fomc.decision || "Unavailable").toUpperCase()],
        ["Statement mentions", finite(signal?.mentions) ? String(signal.mentions) : "Unavailable"],
      ],
    }));
    if (!policyCards.length) {
      policyCards.push({ id: "policy-missing", label: "FOMC statement", score: null, readings: [] });
    }
    return [
      { label: "Market Data", weight: 20, cards: marketCards },
      { label: "Inflation", weight: 26, cards: [
        card("cpi", "Consumer prices", cpi, m(cpi).cpi_bias, [
          reading("Core CPI, year over year", m(cpi).core_yoy, "%", 100),
          reading("Headline CPI, year over year", m(cpi).headline_yoy, "%", 100),
          reading("Core CPI, 3-month annualized", m(cpi).core_3m_annualized, "%", 100),
        ]),
        card("ppi", "Producer prices", ppi, m(ppi).ppi_bias, [
          reading("Core PPI, year over year", m(ppi).core_yoy, "%"),
          reading("Headline PPI, year over year", m(ppi).headline_yoy, "%"),
        ]),
        card("breakeven", "Inflation expectations", breakeven, m(breakeven).breakeven_bias, [
          reading("10-year breakeven", m(breakeven).breakeven_10y, "%"),
          reading("5-year breakeven", m(breakeven).breakeven_5y, "%"),
        ]),
      ] },
      { label: "Labor", weight: 18, cards: [
        card("labor", "Employment", labor, m(labor).labor_bias, [
          reading("Average monthly job growth, 3 months", m(labor).payroll_3m_avg, "k"),
          reading("Unemployment rate", m(labor).unemployment_rate, "%"),
          reading("Wage growth, year over year", m(labor).wages_yoy, "%", 100),
          reading("Openings per unemployed person", m(labor).openings_unemployed_ratio),
          reading("Labor-force participation", m(labor).participation_rate, "%"),
        ]),
      ] },
      { label: "Growth & Fiscal", weight: 12, cards: [
        card("gdp", "Economic growth", gdp, m(gdp).gdp_bias, [
          reading("Real GDP growth, annualized", m(gdp).real_gdp_growth_pct, "%"),
        ]),
        card("fiscal", "Fiscal impulse", fiscal, m(fiscal).fiscal_bias, [
          reading("Deficit (USD billions)", m(fiscal).deficit_billions),
          reading("Annual deficit change (USD billions)", m(fiscal).deficit_yoy_change_billions),
        ]),
      ] },
      { label: "Rates & Funding", weight: 12, cards: [
        card("treasury", "Treasury curve", treasury, curveBias, [
          reading("2-year Treasury yield", y2, "%"), reading("10-year Treasury yield", y10, "%"),
        ]),
        card("funding", "Funding conditions", funding, m(funding).policy_rate_bias, [
          reading("Effective federal funds rate", m(funding).effr, "%"),
          reading("SOFR", m(funding).sofr, "%"),
        ]),
      ] },
      { label: "Policy Context", weight: 12, cards: policyCards },
    ];
  }

  function layoutRectangles(items, width, height) {
    if (!finite(width) || !finite(height) || width <= 0 || height <= 0
        || items.some((item) => !finite(item.weight) || item.weight <= 0)) {
      throw new RangeError("Treemap dimensions and weights must be finite and positive.");
    }
    const rectangles = [];
    function split(part, x, y, w, h) {
      if (!part.length) return;
      if (part.length === 1) {
        rectangles.push({ item: part[0], x, y, width: w, height: h });
        return;
      }
      const total = part.reduce((sum, item) => sum + item.weight, 0);
      let index = 1;
      let firstWeight = part[0].weight;
      while (index < part.length - 1
          && Math.abs(firstWeight + part[index].weight - total / 2) < Math.abs(firstWeight - total / 2)) {
        firstWeight += part[index].weight;
        index += 1;
      }
      const fraction = firstWeight / total;
      if (w >= h) {
        split(part.slice(0, index), x, y, w * fraction, h);
        split(part.slice(index), x + w * fraction, y, w * (1 - fraction), h);
      } else {
        split(part.slice(0, index), x, y, w, h * fraction);
        split(part.slice(index), x, y + h * fraction, w, h * (1 - fraction));
      }
    }
    split(items, 0, 0, width, height);
    return rectangles;
  }

  const element = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const dialog = element("dialog", undefined, "treemap-dialog");
  dialog.setAttribute("aria-labelledby", "treemap-detail-title");
  document.body.appendChild(dialog);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  function showDetails(card) {
    dialog.replaceChildren();
    const header = element("div", undefined, "treemap-dialog-header");
    const title = element("h3", card.label);
    title.id = "treemap-detail-title";
    const close = element("button", "Close", "source-pill");
    close.type = "button";
    close.addEventListener("click", () => dialog.close());
    header.append(title, close);
    dialog.appendChild(header);
    dialog.appendChild(element("p", card.score === null ? "Data unavailable."
      : `${card.score.toFixed(1)}/5 - ${pressureLabel(card.score)}.`, "note"));
    dialog.appendChild(element("p",
      "1 = lower-rate pressure; 3 = balanced; 5 = higher-rate pressure. This score is not a probability.",
      "note"));
    dialog.appendChild(element("p", `As of: ${card.asOf || "Unavailable"}`, "note"));
    if (card.score === null) {
      dialog.appendChild(element("p",
        "The source did not provide a valid policy-pressure input. No neutral value has been substituted.",
        "note"));
    }
    const list = element("dl");
    card.readings.forEach(([label, value]) => {
      list.append(element("dt", label), element("dd", value));
    });
    dialog.appendChild(list);
    if (/^https:\/\//i.test(card.sourceUrl || "")) {
      const source = element("a", card.sourceLabel || "Official source", "source-pill");
      source.href = card.sourceUrl;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      dialog.appendChild(source);
    }
    dialog.showModal();
  }

  const groups = buildGroups().map((group) => {
    const section = element("section", undefined, "treemap-group");
    const heading = element("h3", group.label);
    heading.id = `treemap-group-${group.label.toLowerCase().replace(/[^a-z]+/g, "-")}`;
    section.setAttribute("aria-labelledby", heading.id);
    const tiles = element("div", undefined, "treemap-tiles");
    section.append(heading, tiles);
    container.appendChild(section);
    const cards = group.cards.map((card) => {
      const tile = element("button", undefined, `treemap-tile heat-${pressureTone(card.score)}`);
      tile.type = "button";
      tile.dataset.cardId = card.id;
      tile.setAttribute("aria-haspopup", "dialog");
      tile.append(
        element("span", card.label, "treemap-tile-label"),
        element("span", card.score === null ? "?" : `${card.score.toFixed(1)}/5`, "treemap-score"),
        element("span", pressureLabel(card.score), "treemap-direction"),
      );
      if (card.marketOutcome) {
        tile.appendChild(element("span", card.marketOutcome, "treemap-market-probability"));
      } else if (card.readings.length) {
        tile.appendChild(element("span", `${card.readings[0][0]}: ${card.readings[0][1]}`, "treemap-reading"));
      }
      const asOf = typeof card.asOf === "string" ? card.asOf.split("T")[0] : "unavailable";
      tile.appendChild(element("span", `As of ${asOf}`, "treemap-asof"));
      tile.addEventListener("click", () => showDetails(card));
      tiles.appendChild(tile);
      return { weight: 1, tile };
    });
    return { ...group, section, tiles, cards };
  });

  function position(node, rect, gap = 6) {
    Object.assign(node.style, {
      left: `${rect.x + gap / 2}px`, top: `${rect.y + gap / 2}px`,
      width: `${Math.max(0, rect.width - gap)}px`, height: `${Math.max(0, rect.height - gap)}px`,
    });
  }

  function renderLayout() {
    const width = container.clientWidth;
    if (!width) return;
    const stacked = width < 700;
    container.classList.toggle("treemap-stacked", stacked);
    const height = Math.max(800, width * 0.8);
    container.style.height = stacked ? "auto" : `${height}px`;
    const rectangles = stacked
      ? groups.map((item) => ({
        item, x: 0, y: 0, width, height: Math.max(230, Math.ceil(item.cards.length / 2) * 180 + 46),
      }))
      : layoutRectangles(groups, width, height);
    rectangles.forEach((rect) => {
      const group = rect.item;
      position(group.section, rect, stacked ? 0 : 8);
      const tiles = layoutRectangles(group.cards, group.tiles.clientWidth, group.tiles.clientHeight);
      tiles.forEach((tileRect) => {
        position(tileRect.item.tile, tileRect);
        tileRect.item.tile.classList.toggle("treemap-tile-compact", tileRect.height < 190 || tileRect.width < 170);
      });
    });
  }
  let previousWidth = 0;
  new ResizeObserver(() => {
    if (container.clientWidth !== previousWidth) {
      previousWidth = container.clientWidth;
      renderLayout();
    }
  }).observe(container);
  renderLayout();
})();
