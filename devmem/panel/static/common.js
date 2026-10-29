// Shared helpers for devmem pages (no dependencies).
(function () {
  "use strict";

  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
    return String(Math.round(n));
  }

  function parseTs(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value))) return new Date(Number(value) * 1000);
    var s = String(value).replace(" ", "T");
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += "Z";
    var d = new Date(s);
    return isNaN(d) ? null : d;
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    var d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600),
        m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (d) return d + "d " + h + "h";
    if (h) return h + "h " + String(m).padStart(2, "0") + "m";
    if (m) return m + "m " + String(s).padStart(2, "0") + "s";
    return s + "s";
  }

  function relTime(value) {
    var d = value instanceof Date ? value : parseTs(value);
    if (!d) return "—";
    var s = (Date.now() - d.getTime()) / 1000;
    if (s < 5) return "now";
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  // Simple stacked bar chart into an <svg>; rows: [{x, series: {name: value}}]
  function barChart(svg, rows, colors) {
    var w = svg.clientWidth || 800, h = svg.clientHeight || 220, pad = 28;
    var max = 1;
    rows.forEach(function (r) {
      var t = 0; for (var k in r.series) t += r.series[k]; max = Math.max(max, t);
    });
    var bw = (w - pad) / Math.max(rows.length, 1);
    var parts = ['<line x1="' + pad + '" y1="' + (h - 18) + '" x2="' + w + '" y2="' + (h - 18) + '" stroke="rgba(120,150,255,.25)"/>',
      '<text x="0" y="12" fill="#8a96bd" font-size="11">' + fmtTokens(max) + "</text>"];
    rows.forEach(function (r, i) {
      var y = h - 18;
      Object.keys(r.series).forEach(function (k) {
        var bh = (r.series[k] / max) * (h - 36);
        y -= bh;
        parts.push('<rect x="' + (pad + i * bw + 1) + '" y="' + y + '" width="' + Math.max(1, bw - 3) + '" height="' + bh +
          '" rx="2" fill="' + (colors[k] || "#a78bfa") + '"><title>' + r.x + " " + k + ": " + fmtTokens(r.series[k]) + "</title></rect>");
      });
      if (rows.length < 16 || i % Math.ceil(rows.length / 10) === 0)
        parts.push('<text x="' + (pad + i * bw) + '" y="' + (h - 4) + '" fill="#8a96bd" font-size="10">' + String(r.x).slice(5) + "</text>");
    });
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    svg.innerHTML = parts.join("");
  }

  var AGENT_COLORS = { claude: "#fbbf24", "claude-subagent": "#fde68a", codex: "#22d3ee" };
  var byId = function (id) { return document.getElementById(id); };

  function agentColor(agent) {
    if (AGENT_COLORS[agent]) return AGENT_COLORS[agent];
    agent = String(agent || "");
    if (agent.indexOf("codex") >= 0) return AGENT_COLORS.codex;
    if (agent.indexOf("claude") >= 0) return AGENT_COLORS.claude;
    return "#c4b5fd";
  }

  function agentShort(agent) {
    return agent === "claude-subagent" ? "subagent" : String(agent || "agent");
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Token block, shown on the Overview page (it moved there from the Brain wall).

  function splitHtml(map) {
    var agents = Object.keys(map || {}).sort();
    if (!agents.length) return '<div class="empty">no usage</div>';
    return agents.map(function (a) {
      return '<div style="--c:' + agentColor(a) + '"><span>' + esc(agentShort(a)) + "</span><b>" + fmtTokens(map[a].total) + "</b></div>";
    }).join("");
  }

  function renderSpark(hourly) {
    var svg = byId("spark");
    var w = svg.clientWidth || 300, h = svg.clientHeight || 80;
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);
    var series = hourly.series || {};
    var agents = Object.keys(series).sort();
    var max = 1;
    agents.forEach(function (a) { series[a].forEach(function (v) { max = Math.max(max, v); }); });
    var step = w / 23, out = [];
    out.push('<line x1="0" y1="' + (h - 1) + '" x2="' + w + '" y2="' + (h - 1) + '" stroke="rgba(120,150,255,.18)"/>');
    agents.forEach(function (a, idx) {
      var c = agentColor(a);
      var pts = series[a].map(function (v, i) { return [i * step, h - 3 - (v / max) * (h - 10)]; });
      var line = pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
      out.push('<defs><linearGradient id="sg' + idx + '" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="' + c + '" stop-opacity=".35"/>' +
        '<stop offset="1" stop-color="' + c + '" stop-opacity="0"/></linearGradient></defs>');
      out.push('<polygon points="0,' + h + " " + line + " " + w + "," + h + '" fill="url(#sg' + idx + ')"/>');
      out.push('<polyline points="' + line + '" fill="none" stroke="' + c + '" stroke-width="1.8" stroke-linejoin="round"/>');
    });
    out.push('<text x="2" y="11" fill="#56618a" font-size="10">' + fmtTokens(max) + "/h</text>");
    if (!agents.length) out.push('<text x="' + w / 2 + '" y="' + h / 2 + '" fill="#56618a" font-size="12" text-anchor="middle">no usage in 24 h</text>');
    svg.innerHTML = out.join("");
  }

  function renderTokens(t) {
    byId("tok-today").textContent = fmtTokens(t.today_total);
    byId("tok-week").textContent = fmtTokens(t.week_total);
    byId("tok-today-split").innerHTML = splitHtml(t.today);
    byId("tok-week-split").innerHTML = splitHtml(t.week);
    renderSpark(t.hourly || {});
    var models = (t.models_today || []).slice(0, 5);
    byId("models").innerHTML = models.length ? models.map(function (m) {
      var tot = Math.max(1, m.total);
      var seg = function (cls, v) { return '<i class="' + cls + '" style="width:' + (v / tot * 100).toFixed(2) + '%"></i>'; };
      return '<li style="--c:' + agentColor(m.agent) + '" title="in ' + m.input + " · out " + m.output + " · cache read " + m.cache_read + " · cache write " + m.cache_write + '">' +
        '<span class="m-name">' + esc(m.model) + '</span><span class="m-total">' + fmtTokens(m.total) + "</span>" +
        '<span class="seg">' + seg("s-in", m.input) + seg("s-out", m.output) + seg("s-cr", m.cache_read) + seg("s-cw", m.cache_write) + "</span></li>";
    }).join("") : '<li class="empty">no usage today</li>';
  }

  window.devmem = { fmtTokens: fmtTokens, parseTs: parseTs, fmtDuration: fmtDuration, relTime: relTime, barChart: barChart,
    renderTokens: renderTokens, renderSpark: renderSpark, agentColors: AGENT_COLORS };

  // Render any <time data-rel> as relative time.
  document.querySelectorAll("time[data-rel]").forEach(function (el) {
    el.title = el.getAttribute("datetime") || "";
    el.textContent = relTime(el.getAttribute("datetime"));
  });
  document.querySelectorAll("[data-tokens]").forEach(function (el) {
    el.title = el.getAttribute("data-tokens");
    el.textContent = fmtTokens(el.getAttribute("data-tokens"));
  });

  var adoLink = document.querySelector('a[href="/ado"]');
  var adoBadge = adoLink && adoLink.querySelector(".ado-change-badge");
  var adoChanges = document.getElementById("ado-changes");

  // Stored as "<journal_id>:<seq>"; a seq from an earlier panel run (other journal_id) counts as nothing seen.
  function seenAdoSeq(journalId) {
    try {
      var match = /^([0-9a-f]+):(\d+)$/.exec(localStorage.getItem("devmem.ado.seen") || "");
      return match && match[1] === journalId ? Number(match[2]) : 0;
    } catch (_) { return 0; }
  }

  function setSeenAdoSeq(journalId, seq) {
    try { localStorage.setItem("devmem.ado.seen", journalId + ":" + seq); } catch (_) {}
  }

  function showAdoChanges(changes) {
    if (!adoChanges) return;
    adoChanges.textContent = "";
    if (!changes.length) { adoChanges.hidden = true; return; }
    var heading = document.createElement("strong");
    heading.textContent = "Changed since you last looked";
    var list = document.createElement("ul");
    changes.forEach(function (change) {
      var item = document.createElement("li"), text = document.createElement("span"), at = document.createElement("time");
      text.textContent = change.text;
      at.setAttribute("datetime", change.at);
      at.setAttribute("data-rel", "");
      at.title = change.at;
      at.textContent = relTime(change.at);
      item.append(text, at); list.appendChild(item);
    });
    adoChanges.append(heading, list); adoChanges.hidden = false;
  }

  // On the ADO page the box keeps what was unseen when the page opened, plus anything newer.
  var adoPageSince = null;

  function refreshAdoChanges() {
    fetch("/api/ado/changes").then(function (response) {
      if (!response.ok) throw new Error("ADO changes failed");
      return response.json();
    }).then(function (data) {
      var seen = seenAdoSeq(data.journal_id), latest = data.latest_seq || 0;
      if (adoBadge) adoBadge.hidden = !(latest > seen);
      if (adoChanges) {
        if (adoPageSince === null) adoPageSince = seen;
        showAdoChanges((data.changes || []).filter(function (change) { return change.seq > adoPageSince; }));
        setSeenAdoSeq(data.journal_id, latest);
        if (adoBadge) adoBadge.hidden = true;
      }
    }).catch(function () {});
  }

  refreshAdoChanges();
  setInterval(refreshAdoChanges, 30000);
})();
