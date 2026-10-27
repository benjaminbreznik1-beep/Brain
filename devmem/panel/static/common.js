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

  window.devmem = { fmtTokens: fmtTokens, parseTs: parseTs, fmtDuration: fmtDuration, relTime: relTime, barChart: barChart,
    agentColors: { claude: "#fbbf24", "claude-subagent": "#fde68a", codex: "#22d3ee" } };

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
