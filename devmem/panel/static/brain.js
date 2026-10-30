// devmem brain wall: SSE client, panels and the canvas "brain" network.
(function () {
  "use strict";

  var U = window.devmem;
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var AGENT_COLORS = { claude: "#d6c1a4", "claude-subagent": "#998a75", codex: "#6db0f4" };
  var KIND_COLORS = { note: "#88a8c9", decision: "#aa9dc5", preference: "#c7959c", snippet: "#90b697", todo: "#ccb484" };
  var LINK_COLORS = { blocks: "#f47c70", depends_on: "#ccb484", relates_to: "#9c9890", overlaps: "#66635d", fixes: "#90b697", supersedes: "#4a463f" };
  var KIND_ORDER = ["decision", "preference", "note", "snippet", "todo"];
  var STALE_S = 6 * 3600;
  var $ = function (id) { return document.getElementById(id); };

  function agentColor(agent) {
    if (AGENT_COLORS[agent]) return AGENT_COLORS[agent];
    agent = String(agent || "");
    if (agent.indexOf("codex") >= 0) return AGENT_COLORS.codex;
    if (agent.indexOf("claude") >= 0) return AGENT_COLORS.claude;
    return "#9c9890";
  }

  function agentShort(agent) {
    return agent === "claude-subagent" ? "subagent" : String(agent || "agent");
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ------------------------------------------------------------------ wall mode + clock

  // The body state class is "wall-mode": plain "wall" collided with the .wall grid container.
  var hintTimer = null;

  function showWallHint() {
    var hint = $("wall-hint");
    hint.hidden = false;
    hint.classList.add("show");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { hint.classList.remove("show"); }, 3000);
  }

  function isWall() { return document.body.classList.contains("wall-mode"); }

  function setWall(on) {
    document.body.classList.toggle("wall-mode", on);
    try { localStorage.setItem("devmem.wall", on ? "1" : "0"); } catch (e) { /* storage unavailable */ }
    if (on) showWallHint();
    setTimeout(function () { if (window.devmemBrain) window.devmemBrain.resize(); }, 50);
  }
  (function initWall() {
    var on = false;
    try { on = localStorage.getItem("devmem.wall") === "1"; } catch (e) { /* ignore */ }
    if (on) { document.body.classList.add("wall-mode"); showWallHint(); }
    $("wall-toggle").addEventListener("click", function () { setWall(true); });
    $("wall-exit").addEventListener("click", function () { setWall(false); });
    document.addEventListener("keydown", function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey || /input|textarea|select/i.test(e.target.tagName)) return;
      if (e.key === "w" || e.key === "W") setWall(!isWall());
      else if (e.key === "Escape" && isWall()) setWall(false);
    });
  })();

  // ------------------------------------------------------------------ gauges

  var EXPECTED_LIMITS = [
    { agent: "claude", label: "five_hour", window_minutes: 300 },
    { agent: "claude", label: "seven_day", window_minutes: 10080 },
    { agent: "codex", label: "primary", window_minutes: 300 },
    { agent: "codex", label: "secondary", window_minutes: 10080 },
  ];
  var gaugeEls = {};
  var ARC_R = 80, ARC_C = 2 * Math.PI * ARC_R, ARC_LEN = ARC_C * 0.75;

  function windowName(l) {
    var named = { five_hour: "5h", seven_day: "weekly" };
    if (named[l.label]) return named[l.label];
    if (/^seven_day_/.test(l.label)) return "weekly " + l.label.replace(/^seven_day_/, "").replace(/_/g, " ");
    var m = l.window_minutes;
    var base = m === 300 ? "5h" : m === 10080 ? "weekly" : m ? (m % 1440 === 0 ? m / 1440 + "d" : Math.round(m / 60) + "h") : l.label;
    var prefix = l.label.indexOf(":") > 0 ? l.label.split(":")[0] + " " : "";
    return prefix + base;
  }

  function gaugeColor(left) {
    if (left === null || left === undefined) return "#66635d";
    if (left <= 10) return "#f47c70";
    if (left <= 25) return "#f97c3d";
    if (left <= 50) return "#ebb25f";
    return "#cecac3";
  }

  function makeGauge(key) {
    var el = document.createElement("div");
    el.className = "gauge";
    el.innerHTML =
      '<svg viewBox="0 0 200 178" role="img">' +
      '<circle class="track" cx="100" cy="100" r="' + ARC_R + '" stroke-dasharray="' + ARC_LEN + " " + ARC_C + '" transform="rotate(135 100 100)"/>' +
      '<circle class="val" cx="100" cy="100" r="' + ARC_R + '" stroke-dasharray="0 ' + ARC_C + '" transform="rotate(135 100 100)"/>' +
      '<text class="pct" x="100" y="108" text-anchor="middle"><tspan class="n">—</tspan><tspan class="pct-unit" dx="2">%</tspan></text>' +
      '<text class="sub" x="100" y="134" text-anchor="middle">left</text>' +
      "</svg>" +
      '<div class="g-label"></div><div class="g-reset"></div><div class="g-asof"></div>';
    $("gauges").appendChild(el);
    gaugeEls[key] = el;
    return el;
  }

  var limitsData = [];

  function renderLimits(limits) {
    var byKey = {};
    limits.forEach(function (l) { byKey[l.agent + ":" + l.label] = l; });
    var list = EXPECTED_LIMITS.map(function (e) { return byKey[e.agent + ":" + e.label] || Object.assign({ missing: true }, e); });
    limits.forEach(function (l) {
      if (!EXPECTED_LIMITS.some(function (e) { return e.agent === l.agent && e.label === l.label; })) list.push(l);
    });
    limitsData = list;
    list.forEach(function (l) {
      var key = l.agent + ":" + l.label;
      var el = gaugeEls[key] || makeGauge(key);
      var left = l.missing ? null : l.left_percent;
      var stale = !l.missing && (l.stale || (l.age_s !== null && l.age_s > STALE_S));
      el.classList.toggle("nodata", !!l.missing);
      el.classList.toggle("stale", !!stale);
      var color = l.missing || stale ? "#66635d" : gaugeColor(left);
      el.style.setProperty("--g", color);
      el.querySelector(".val").setAttribute("stroke-dasharray", (left === null ? 0 : ARC_LEN * Math.max(0, Math.min(100, left)) / 100) + " " + ARC_C);
      el.querySelector(".n").textContent = left === null ? "—" : Math.round(left);
      el.querySelector(".sub").textContent = l.missing ? "no data" : stale ? "stale" : "left";
      el.querySelector(".g-label").textContent = (l.agent === "codex" ? "Codex" : l.agent === "claude" ? "Claude" : l.agent) + " · " + windowName(l);
      el.dataset.resets = l.resets_at || "";
      el.dataset.asof = l.as_of || "";
      el.dataset.missing = l.missing ? "1" : "";
    });
    tickGauges();
  }

  function tickGauges() {
    Object.keys(gaugeEls).forEach(function (key) {
      var el = gaugeEls[key];
      var reset = el.querySelector(".g-reset"), asof = el.querySelector(".g-asof");
      if (el.dataset.missing) { reset.textContent = "no data yet"; asof.textContent = ""; return; }
      var r = U.parseTs(el.dataset.resets);
      if (r) {
        var s = (r.getTime() - Date.now()) / 1000;
        reset.innerHTML = s > 0 ? "resets in " + U.fmtDuration(s) : '<span class="flag">window reset · awaiting update</span>';
      } else reset.textContent = "reset time unknown";
      asof.textContent = el.dataset.asof ? "as of " + U.relTime(el.dataset.asof) : "";
    });
  }

  // ------------------------------------------------------------------ memory kinds

  function renderCounts(counts) {
    var by = counts.memories_by_kind || {};
    $("mem-total").textContent = counts.memories_total || 0;
    var max = Math.max(1, Math.max.apply(null, KIND_ORDER.map(function (k) { return by[k] || 0; })));
    $("kinds").innerHTML = KIND_ORDER.map(function (k) {
      return '<div class="kind-row" style="--c:' + KIND_COLORS[k] + '"><span class="k">' + k + '</span>' +
        '<span class="bar"><i style="width:' + ((by[k] || 0) / max * 100).toFixed(1) + '%"></i></span><b>' + (by[k] || 0) + "</b></div>";
    }).join("");
  }

  // ------------------------------------------------------------------ Azure DevOps card (the server only returns cached data here)

  function renderAdoCard(snapshot) {
    var card = $("ado-card");
    if (!snapshot || !snapshot.configured) { card.hidden = true; return; }
    card.hidden = false;
    var heading = $("ado-heading");
    if (snapshot.stale) {
      heading.innerHTML = '<a href="/ado">ADO</a> <span class="badge ado-card-stale">stale</span>';
      card.title = (snapshot.error || "ADO data is stale") + "; last refresh: " + (snapshot.refreshed_at || "unknown");
    } else {
      heading.innerHTML = '<a href="/ado">ADO</a>';
      card.title = "";
    }
    var count = function (name) { return (snapshot[name] || []).length; };
    $("ado-counts").innerHTML =
      '<span><b>' + count("to_review") + '</b> to review</span>' +
      '<span><b>' + count("my_approved_not_completed") + '</b> approved</span>' +
      '<span><b>' + count("my_changes_requested") + '</b> changes</span>' +
      '<span><b>' + count("my_waiting_on_others") + '</b> waiting</span>' +
      '<span><b>' + count("open_tasks") + '</b> tasks</span>';
  }

  function refreshAdoCard() {
    fetch("/api/ado").then(function (response) {
      return response.ok ? response.json() : null;
    }).then(renderAdoCard).catch(function (e) { console.warn("devmem: /api/ado failed", e); });
  }

  // ------------------------------------------------------------------ agents

  function subagentRows(list) {
    return '<ul class="subagents">' + list.map(function (x) {
      var last = x.last_tool ? '<span class="tool">' + esc(x.last_tool) + "</span> " + esc(x.last_arg || "") : esc(x.description || "");
      return '<li class="sub ' + esc(x.status) + '" title="' + esc(x.status) + '"><span class="dot"></span>' +
        '<a class="s-type" href="/messages/agent/' + encodeURIComponent(x.agent_id) + '">' + esc(x.agent_type || "subagent") + "</a>" +
        '<span class="s-last">' + last + "</span></li>";
    }).join("") + "</ul>";
  }

  function renderAgents(a) {
    var sessions = a.sessions || [], clients = a.connected || [], subs = a.subagents || {}, shownSubs = {};
    var rows = sessions.slice(0, 8).map(function (s) {
      // claude and claude-subagent rows share a session id: its subagents are listed once, under the first row.
      var children = s.agent.indexOf("claude") === 0 && subs[s.session_id] && !shownSubs[s.session_id] ? subs[s.session_id] : null;
      if (children) shownSubs[s.session_id] = 1;
      var connected = clients.some(function (c) { return !c.remote && c.family === (s.agent.indexOf("codex") >= 0 ? "codex" : "claude") && c.project === s.project; });
      var last = s.last_tool ? '<span class="tool">' + esc(s.last_tool) + "</span> " + esc(s.last_summary || "") :
        (s.tokens_10m ? U.fmtTokens(s.tokens_10m) + " tokens in 10 min" : "");
      return '<li class="agent' + (s.age_s !== null && s.age_s < 120 ? " active" : "") + '" style="--c:' + agentColor(s.agent) + '">' +
        '<span class="pip"></span><span class="a-main"><b>' + esc(agentShort(s.agent)) + "</b> · " + esc(s.project_name) +
        (connected ? ' <span class="hint">· mcp</span>' : "") + '</span><span class="a-age" data-ts="' + esc(s.last_ts) + '">' + U.relTime(s.last_ts) + "</span>" +
        '<span class="a-last">' + last + "</span>" + (children ? subagentRows(children) : "") + "</li>";
    });
    clients.forEach(function (c) {
      var shown = sessions.some(function (s) { return (s.agent.indexOf("codex") >= 0 ? "codex" : "claude") === c.family && s.project === c.project; });
      if (shown && !c.remote) return;
      rows.push('<li class="agent" style="--c:' + agentColor(c.family) + '"><span class="pip"></span>' +
        '<span class="a-main"><b>' + esc(c.client_name || c.family) + "</b> · " + esc(c.project_name) + '</span><span class="a-age">connected</span>' +
        '<span class="a-last">' + (c.last_tool ? 'memory: <span class="tool">' + esc(c.last_tool) + "</span>" : "idle") + "</span></li>");
    });
    $("agents").innerHTML = rows.length ? rows.join("") : '<li class="empty">No agent activity in the last 10 minutes.</li>';
    $("agents-hint").textContent = clients.length + " connected · " + sessions.length + " active";
  }

  // ------------------------------------------------------------------ feed

  var feedSeen = null;

  var feedKey = null;

  function renderFeed(items) {
    var key = items.map(function (e) { return e.id; }).join(",");
    if (key === feedKey) return;
    feedKey = key;
    var first = feedSeen === null;
    var seen = feedSeen || {};
    var next = {};
    var ul = $("feed");
    ul.innerHTML = items.map(function (e) {
      next[e.id] = 1;
      var fresh = !first && !seen[e.id] && !REDUCED;
      return '<li class="' + esc(e.kind) + (fresh ? " enter" : "") + '" style="--c:' + agentColor(e.agent) + '">' +
        '<span class="f-badge">' + esc(agentShort(e.agent)) + "</span>" +
        '<span class="f-main"><span class="f-proj">' + esc(e.project_name) + '</span><span class="f-tool">' + esc(e.name) + "</span></span>" +
        '<span class="f-time" data-ts="' + esc(e.ts) + '">' + U.relTime(e.ts) + "</span>" +
        (e.summary ? '<span class="f-sum">' + esc(e.summary) + "</span>" : "") + "</li>";
    }).join("") || '<li class="empty">No tool activity yet. It appears here as agents work.</li>';
    feedSeen = next;
  }

  // ------------------------------------------------------------------ messages

  var msgSeen = null;

  function renderMessages(m) {
    var items = m.items || [], first = msgSeen === null, seen = msgSeen || {}, next = {};
    // Compact chat bubbles, newest first; each opens its chat on /messages.
    $("msgs").innerHTML = items.map(function (x) {
      next[x.id] = 1;
      var fresh = !first && !seen[x.id] && !REDUCED;
      var color = x.mine ? "var(--color-user)" : x.who === "Claude" ? "var(--color-claude)" : "oklch(78% 0.07 " + (x.hue || 0) + ")";
      var href = x.chat ? "/messages?chat=" + encodeURIComponent(x.chat) : "/messages/thread/" + x.thread_id;
      return '<li class="mb' + (x.mine ? " mine" : "") + (x.unread ? " unread" : "") + (fresh ? " enter" : "") + '" style="--c:' + color + '">' +
        '<a href="' + href + '"><span class="m-line">' + esc((x.icon || "✉") + " " + (x.headline || x.text || x.line)) + "</span>" +
        '<span class="m-time" data-ts="' + esc(x.created_at) + '">' + U.relTime(x.created_at) + "</span></a></li>";
    }).join("") || '<li class="empty">No messages yet. Agents post with message_post; subagent tasks and reports show up here.</li>';
    msgSeen = next;
    $("msg-unread").textContent = m.unread ? m.unread + " unread" : "";
    var badge = $("nav-msg-badge");
    if (badge) { badge.textContent = m.badge || 0; badge.hidden = !m.badge; }
  }

  function tickRelTimes() {
    document.querySelectorAll("[data-ts]").forEach(function (el) { el.textContent = U.relTime(el.dataset.ts); });
  }

  // ------------------------------------------------------------------ brain renderer

  var Brain2D = (function () {
    var canvas = $("brain"), wrap = $("brain-wrap"), tip = $("brain-tip");
    var ctx = canvas.getContext("2d");
    var W = 0, H = 0, DPR = 1, G = null;        // size and geometry
    var outline = null;                        // pre-rendered silhouette
    var nodes = [], nodeById = {}, memLinks = [], agentLinks = [], projectLinks = [];
    var sim = null, hasD3 = !!(window.d3 && window.d3.forceSimulation);
    var impulses = [], flashes = {}, pendingBloom = {};
    var sprites = {};
    var running = false, lastFrame = 0, firstGraph = true, dirty = true, hover = null, active = true;
    var start = performance.now();
    var FONT = getComputedStyle(document.body).fontFamily;

    function sprite(color) {
      if (sprites[color]) return sprites[color];
      var s = document.createElement("canvas"), n = 64;
      s.width = s.height = n;
      var c = s.getContext("2d"), g = c.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
      g.addColorStop(0, color);
      g.addColorStop(0.25, color + "aa");
      g.addColorStop(1, color + "00");
      c.fillStyle = g;
      c.fillRect(0, 0, n, n);
      sprites[color] = s;
      return s;
    }

    function geometry() {
      var ry = Math.min(H * 0.37, W / 4.4), rx = ry * 0.93;
      var cx = W / 2, cy = H * 0.5;
      return { cx: cx, cy: cy, rx: rx, ry: ry, s: ry / 320,
        hemis: [{ x: cx - rx * 0.95, y: cy }, { x: cx + rx * 0.95, y: cy }] };
    }

    function wobble(theta, seed) {
      return 1 + 0.022 * Math.sin(7 * theta + seed) + 0.014 * Math.sin(13 * theta + seed * 2.3) + 0.008 * Math.sin(23 * theta + seed);
    }

    function renderOutline() {
      outline = document.createElement("canvas");
      outline.width = Math.max(1, Math.round(W * DPR));
      outline.height = Math.max(1, Math.round(H * DPR));
      var c = outline.getContext("2d");
      c.scale(DPR, DPR);
      var paths = G.hemis.map(function (h, i) {
        var p = new Path2D();
        for (var k = 0; k <= 160; k++) {
          var t = (k / 160) * Math.PI * 2, w = wobble(t, i * 1.7 + 0.4);
          // flatten the inner side so the two hemispheres meet along a fissure
          var inner = (i === 0 ? Math.cos(t) > 0 : Math.cos(t) < 0) ? 0.97 : 1;
          var x = h.x + Math.cos(t) * G.rx * w * inner, y = h.y + Math.sin(t) * G.ry * w;
          if (k === 0) p.moveTo(x, y); else p.lineTo(x, y);
        }
        p.closePath();
        return p;
      });
      // inner fill
      paths.forEach(function (p, i) {
        var h = G.hemis[i];
        var g = c.createRadialGradient(h.x, h.y - G.ry * 0.2, G.ry * 0.1, h.x, h.y, G.ry * 1.1);
        g.addColorStop(0, "rgba(239,235,228,0.025)");
        g.addColorStop(1, "rgba(239,235,228,0.005)");
        c.fillStyle = g;
        c.fill(p);
      });
      // soft glow outline (shadowBlur only here, rendered once per resize)
      c.lineWidth = 1.6;
      c.strokeStyle = "rgba(239,235,228,0.22)";
      c.shadowColor = "rgba(0,0,0,0)";
      c.shadowBlur = 22 * G.s + 6;
      paths.forEach(function (p) { c.stroke(p); });
      c.shadowBlur = 0;
      c.strokeStyle = "rgba(239,235,228,0.10)";
      c.lineWidth = 1;
      paths.forEach(function (p) { c.stroke(p); });
      // gyri: faint curved strokes inside each hemisphere
      var rnd = mulberry(7);
      c.strokeStyle = "rgba(239,235,228,0.04)";
      c.lineWidth = 1.2;
      G.hemis.forEach(function (h) {
        for (var k = 0; k < 16; k++) {
          var a = rnd() * Math.PI * 2, d = 0.25 + rnd() * 0.6;
          var x = h.x + Math.cos(a) * G.rx * d, y = h.y + Math.sin(a) * G.ry * d;
          var len = (0.18 + rnd() * 0.25) * G.rx, dir = a + Math.PI / 2 + (rnd() - 0.5);
          c.beginPath();
          c.moveTo(x - Math.cos(dir) * len / 2, y - Math.sin(dir) * len / 2);
          c.quadraticCurveTo(x + (rnd() - 0.5) * len * 0.6, y + (rnd() - 0.5) * len * 0.6, x + Math.cos(dir) * len / 2, y + Math.sin(dir) * len / 2);
          c.stroke();
        }
      });
      // longitudinal fissure
      c.strokeStyle = "rgba(239,235,228,0.07)";
      c.beginPath();
      c.moveTo(G.cx, G.cy - G.ry * 0.92);
      c.bezierCurveTo(G.cx - 6, G.cy - G.ry * 0.3, G.cx + 6, G.cy + G.ry * 0.3, G.cx, G.cy + G.ry * 0.92);
      c.stroke();
    }

    function mulberry(a) {
      return function () {
        a |= 0; a = (a + 0x6d2b79f5) | 0;
        var t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }

    function hash(str) {
      var h = 2166136261;
      for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
      return (h >>> 0) / 4294967296;
    }

    // ---- anchors: where each cluster lives inside the silhouette
    function assignAnchors() {
      var projects = nodes.filter(function (n) { return n.type === "project"; });
      var real = projects.filter(function (p) { return p.id !== "other"; });
      var halves = [[], []];
      real.forEach(function (p, i) { halves[i % 2].push(p); });
      halves.forEach(function (list, side) {
        var h = G.hemis[side], n = list.length;
        list.forEach(function (p, i) {
          // spread over the outer side of each hemisphere; the inner (fissure) side holds global memories
          var frac = n === 1 ? 0.5 : i / (n - 1);
          var a = side === 0 ? Math.PI * (0.42 + 1.16 * frac) : Math.PI * (-0.58 + 1.16 * frac);
          var d = n === 1 ? 0.3 : n === 2 ? 0.45 : (i % 2 ? 0.34 : 0.62);
          p.ax = h.x + Math.cos(a) * G.rx * d;
          p.ay = h.y + Math.sin(a) * G.ry * d;
        });
      });
      projects.forEach(function (p) {
        if (p.id === "other") { p.ax = G.cx; p.ay = G.cy + G.ry * 0.62; }
      });
      var hub = nodeById.__brain__;
      if (hub) { hub.ax = G.cx; hub.ay = G.cy; hub.fx = G.cx; hub.fy = G.cy; }
      nodes.forEach(function (n) {
        if (n.type === "memory") {
          var parent = nodeById[n.cluster === "global" ? "__brain__" : "p:" + n.cluster];
          n.ax = parent ? parent.ax : G.cx;
          n.ay = parent ? parent.ay : G.cy;
        }
      });
      // agents on the rim: spread along an ellipse just outside the brain
      var agents = nodes.filter(function (n) { return n.type === "agent"; });
      var ex = G.rx * 1.95 + 30 * G.s, ey = G.ry * 1.17;
      var top = agents.filter(function (a, i) { return i % 2 === 0; }), bottom = agents.filter(function (a, i) { return i % 2 === 1; });
      [[top, -Math.PI / 2], [bottom, Math.PI / 2]].forEach(function (group) {
        var list = group[0], centre = group[1], step = Math.min(0.55, 2.2 / Math.max(list.length, 1));
        list.forEach(function (a, k) {
          var ang = centre + (k - (list.length - 1) / 2) * step;
          var x = G.cx + Math.cos(ang) * ex, y = G.cy + Math.sin(ang) * ey;
          var p = a.project && nodeById["p:" + a.project];
          if (p && list.length === 1) x += (p.ax - G.cx) * 0.35; // lean toward its lobe
          a.fx = a.x = Math.max(40, Math.min(W - 40, x));
          a.fy = a.y = Math.max(28, Math.min(H - 28, y));
        });
      });
    }

    function contain(alpha) {
      var k = 0.9;
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.type === "agent" || n.fx !== undefined && n.fx !== null) continue;
        var best = 1e9, hb = null;
        for (var j = 0; j < 2; j++) {
          var h = G.hemis[j], dx = (n.x - h.x) / G.rx, dy = (n.y - h.y) / G.ry, d = Math.sqrt(dx * dx + dy * dy);
          if (d < best) { best = d; hb = h; }
        }
        if (best > k) {
          n.x = hb.x + (n.x - hb.x) * k / best;
          n.y = hb.y + (n.y - hb.y) * k / best;
          n.vx *= 0.5; n.vy *= 0.5;
        }
      }
    }

    function phyllotaxis(n, i, cx, cy, spacing) {
      var r = spacing * Math.sqrt(i + 0.5), a = i * 2.39996;
      return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    }

    function initialPositions() {
      var counters = {};
      nodes.forEach(function (n) {
        if (n.type === "memory" && n.x === undefined) {
          var c = counters[n.cluster] = (counters[n.cluster] || 0) + 1;
          var p = phyllotaxis(n, c, n.ax, n.ay, 7 * G.s + 2);
          n.x = p[0]; n.y = p[1];
        } else if (n.type === "project" && n.x === undefined) {
          n.x = n.ax; n.y = n.ay;
        }
      });
    }

    function buildSim(alpha) {
      initialPositions(); // before sim.nodes(): d3 would otherwise seed new nodes near (0, 0)
      if (!hasD3) { contain(); dirty = true; return; }
      var d3 = window.d3;
      if (!sim) {
        sim = d3.forceSimulation().stop().alphaDecay(0.035).velocityDecay(0.38)
          .force("charge", d3.forceManyBody().strength(function (n) { return n.type === "project" ? -70 * G.s : -5 * G.s; }).distanceMax(90))
          .force("collide", d3.forceCollide().radius(function (n) { return n.r + 1.6; }).iterations(1))
          .force("x", d3.forceX(function (n) { return n.ax; }).strength(function (n) { return n.type === "project" ? 0.35 : 0.06; }))
          .force("y", d3.forceY(function (n) { return n.ay; }).strength(function (n) { return n.type === "project" ? 0.35 : 0.06; }))
          .force("contain", contain);
      }
      sim.nodes(nodes.filter(function (n) { return n.type !== "agent"; }));
      sim.force("link", d3.forceLink(memLinks).id(function (n) { return n.id; })
        .distance(function (l) { return l.kind === "ml" ? 30 * G.s + 8 : (l.target.cluster === "global" ? 14 : 18) * G.s + 6; })
        .strength(function (l) { return l.kind === "ml" ? 0.025 : 0.12; }));
      sim.force("charge").strength(function (n) { return n.type === "project" ? -70 * G.s : -5 * G.s; });
      sim.force("x").x(function (n) { return n.ax; });
      sim.force("y").y(function (n) { return n.ay; });
      sim.alpha(Math.max(sim.alpha(), alpha));
      dirty = true;
    }

    var pendingGraph = null;

    function setGraph(g) {
      if (!G) { pendingGraph = g; return; }
      var old = nodeById;
      var next = [], byId = {};
      var now = performance.now();
      function keep(id, props) {
        var n = old[id] || { id: id, phase: hash(id) * Math.PI * 2, born: firstGraph ? now + Math.random() * 1400 : now };
        Object.assign(n, props);
        next.push(n); byId[id] = n;
        return n;
      }
      keep("__brain__", { type: "hub", r: 16 * G.s + 5, name: "Brain", color: "#efebe4" });
      (g.projects || []).forEach(function (p) {
        keep("p:" + p.id, { type: "project", project: p.id, name: p.name, path: p.path, memories: p.memories, activity: p.activity,
          r: Math.min(16, 6 + Math.sqrt(p.memories || 0) * 1.3) * G.s + 3 });
      });
      (g.memories || []).forEach(function (m) {
        var isNew = !old["m:" + m.id] && !firstGraph;
        var n = keep("m:" + m.id, { type: "memory", mid: m.id, title: m.title, kind: m.kind, cluster: m.cluster, project: m.project,
          r: 2.6 * G.s + 1, color: KIND_COLORS[m.kind] || "#9c9890" });
        if (isNew || pendingBloom[m.id]) { n.bloom = now; delete pendingBloom[m.id]; }
      });
      (g.agents || []).forEach(function (a) {
        keep("a:" + a.id, { type: "agent", agent: a.agent, session: a.session_id, project: a.project, name: a.project_name,
          active: a.active, connected: a.connected, r: 6 * G.s + 2.5, color: agentColor(a.agent) });
      });
      nodes = next; nodeById = byId;
      memLinks = []; agentLinks = []; projectLinks = [];
      nodes.forEach(function (n) {
        if (n.type === "memory") {
          var parent = n.cluster === "global" ? "__brain__" : "p:" + n.cluster;
          if (byId[parent]) memLinks.push({ source: parent, target: n.id, kind: "pm" });
        } else if (n.type === "agent") {
          agentLinks.push({ source: n, target: byId.__brain__ });
        } else if (n.type === "project") {
          projectLinks.push({ source: byId.__brain__, target: n });
        }
      });
      (g.links || []).forEach(function (l) {
        if (byId["m:" + l.from_id] && byId["m:" + l.to_id]) memLinks.push({ source: "m:" + l.from_id, target: "m:" + l.to_id, kind: "ml", type: l.type });
      });
      assignAnchors();
      buildSim(firstGraph ? 1 : 0.25);
      firstGraph = false;
      kick();
    }

    // ---- impulses
    function curvePoints(a, b) {
      var mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      var cx = mx + (G.cx - mx) * 0.25, cy = my + (G.cy - my) * 0.25;
      var pts = [];
      for (var i = 0; i <= 12; i++) {
        var t = i / 12, u = 1 - t;
        pts.push([u * u * a.x + 2 * u * t * cx + t * t * b.x, u * u * a.y + 2 * u * t * cy + t * t * b.y]);
      }
      return pts;
    }

    var lastImpulse = {};

    function findAgent(p) {
      var exact = nodeById["a:" + p.agent + ":" + p.session_id];
      if (exact) return exact;
      var fam = String(p.agent || "").indexOf("codex") >= 0 ? "codex" : "claude";
      var best = null;
      nodes.forEach(function (n) {
        if (n.type !== "agent") return;
        var nf = n.agent.indexOf("codex") >= 0 ? "codex" : "claude";
        if (nf === fam && (!best || n.project === p.project)) best = n;
      });
      return best;
    }

    function pulse(p) {
      if (!G) return;
      var hub = nodeById.__brain__, project = p.project ? nodeById["p:" + p.project] : null;
      var agent = findAgent(p), now = performance.now();
      if (p.type === "ado") {
        if (hub) flashes[hub.id] = now;
        dirty = true;
        kick();
        return;
      }
      if (p.type === "memory") {
        if (agent) agent.lit = now;
        if (!REDUCED && agent && hub) impulses.push({ path: curvePoints(agent, hub), start: now, dur: 900, color: agent.color, small: false, project: null, targets: [] });
        setTimeout(function () {
          if (hub) flashes[hub.id] = performance.now();
          pendingBloom[p.id] = 1;
          var existing = nodeById["m:" + p.id];
          if (existing) existing.bloom = performance.now();
          if (project) flashes[project.id] = performance.now();
          kick();
        }, agent && hub && !REDUCED ? 900 : 0);
        kick();
        return;
      }
      var key = (agent ? agent.id : "x") + p.type;
      if (lastImpulse[key] && now - lastImpulse[key] < 350) return;
      lastImpulse[key] = now;
      if (agent) agent.lit = now;
      if (!project) { kick(); return; }
      if (REDUCED) { if (hub) flashes[hub.id] = now; flashes[project.id] = now; dirty = true; return; }
      var color = agent ? agent.color : agentColor(p.agent);
      // continue into one or two neurons of the lobe
      var mems = nodes.filter(function (n) { return n.type === "memory" && n.cluster === p.project; });
      var targets = [];
      for (var i = 0; i < Math.min(2, mems.length); i++) targets.push(mems[Math.floor(Math.random() * mems.length)]);
      var dur = p.type === "usage" ? 900 : 1100, firstEnd = now;
      if (agent && hub) {
        impulses.push({ path: curvePoints(agent, hub), start: now, dur: dur, color: color, small: p.type === "usage", project: hub, targets: [] });
        firstEnd += dur;
      }
      if (hub) {
        impulses.push({ path: curvePoints(hub, project), start: firstEnd, dur: dur, color: color, small: p.type === "usage", project: project, targets: [] });
        firstEnd += dur;
      }
      targets.forEach(function (target) {
        impulses.push({ path: curvePoints(project, target), start: firstEnd, dur: dur, color: color, small: p.type === "usage", project: project, targets: [target] });
      });
      if (impulses.length > 60) impulses.splice(0, impulses.length - 60);
      kick();
    }

    function pointAlong(path, t) {
      var f = t * (path.length - 1), i = Math.min(path.length - 2, Math.floor(f)), r = f - i;
      return [path[i][0] + (path[i + 1][0] - path[i][0]) * r, path[i][1] + (path[i + 1][1] - path[i][1]) * r];
    }

    // ---- frame
    function draw(now) {
      var t = (now - start) / 1000;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (outline) ctx.drawImage(outline, 0, 0, W, H);

      // synapses: project -> memories
      ctx.lineWidth = 0.7;
      ctx.strokeStyle = "rgba(239,235,228,0.06)";
      ctx.beginPath();
      for (var i = 0; i < memLinks.length; i++) {
        var l = memLinks[i], s = typeof l.source === "object" ? l.source : nodeById[l.source], d = typeof l.target === "object" ? l.target : nodeById[l.target];
        if (!s || !d || s.x === undefined) continue;
        ctx.moveTo(s.x, s.y); ctx.lineTo(d.x, d.y);
      }
      ctx.stroke();

      memLinks.forEach(function (l) {
        if (l.kind !== "ml") return;
        var s = typeof l.source === "object" ? l.source : nodeById[l.source], d = typeof l.target === "object" ? l.target : nodeById[l.target];
        if (!s || !d || s.x === undefined || d.x === undefined) return;
        var color = LINK_COLORS[l.type] || "#9c9890";
        ctx.strokeStyle = color + "66"; ctx.lineWidth = 0.65;
        ctx.beginPath(); ctx.moveTo(s.x, s.y); ctx.lineTo(d.x, d.y); ctx.stroke();
        if (!REDUCED && /^(blocks|depends_on|supersedes)$/.test(l.type)) {
          var q = (t * 0.18 + hash(l.source + ">" + l.target + l.type)) % 1;
          ctx.fillStyle = color; ctx.beginPath(); ctx.arc(s.x + (d.x - s.x) * q, s.y + (d.y - s.y) * q, 1.2, 0, Math.PI * 2); ctx.fill();
        }
      });

      // synapses: agent -> brain, then brain -> project
      agentLinks.forEach(function (l) {
        var pts = curvePoints(l.source, l.target);
        ctx.strokeStyle = l.source.color + (l.source.active ? "55" : "26");
        ctx.lineWidth = l.source.active ? 1.3 : 0.9;
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (var k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
        ctx.stroke();
      });
      projectLinks.forEach(function (l) {
        var pts = curvePoints(l.source, l.target);
        ctx.strokeStyle = "rgba(239,235,228,0.18)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (var k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
        ctx.stroke();
      });

      // memories (neurons)
      var anyAnim = false;
      for (var m = 0; m < nodes.length; m++) {
        var n = nodes[m];
        if (n.type !== "memory" || n.x === undefined) continue;
        var age = now - n.born;
        if (age < 0) { anyAnim = true; continue; }
        var appear = Math.min(1, age / 600);
        if (appear < 1) anyAnim = true;
        var tw = REDUCED ? 0.8 : 0.62 + 0.3 * Math.sin(t * (0.6 + (n.phase % 1) * 0.9) + n.phase);
        var lit = n.lit && now - n.lit < 900 ? 1 - (now - n.lit) / 900 : 0;
        if (lit) anyAnim = true;
        var r = n.r * appear * (1 + lit * 0.8);
        ctx.globalAlpha = Math.min(1, (tw + lit) * appear);
        var sz = r * 5;
        ctx.drawImage(sprite(n.color), n.x - sz / 2, n.y - sz / 2, sz, sz);
        ctx.fillStyle = n.color;
        ctx.beginPath(); ctx.arc(n.x, n.y, Math.max(0.6, r * 0.55), 0, Math.PI * 2); ctx.fill();
        if (n.bloom) {
          var b = (now - n.bloom) / 1800;
          if (b < 1) {
            anyAnim = true;
            ctx.globalAlpha = (1 - b) * 0.9;
            ctx.strokeStyle = n.color;
            ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(n.x, n.y, n.r + b * 34 * G.s + 4, 0, Math.PI * 2); ctx.stroke();
            var bs = (1 - b) * 40 * G.s + 8;
            ctx.drawImage(sprite(n.color), n.x - bs / 2, n.y - bs / 2, bs, bs);
          } else n.bloom = 0;
        }
      }
      ctx.globalAlpha = 1;

      // Brain hub
      var hub = nodeById.__brain__;
      if (hub && hub.x !== undefined) {
        var hf = flashes[hub.id] ? (now - flashes[hub.id]) / 1400 : 1;
        if (hf < 1) anyAnim = true; else delete flashes[hub.id];
        var hl = hf < 1 ? 1 - hf : 0;
        ctx.globalAlpha = 0.9 + hl * 0.1;
        var hs = hub.r * (6.8 + hl * 4);
        ctx.drawImage(sprite("#3a3631"), hub.x - hs / 2, hub.y - hs / 2, hs, hs);
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#efebe4";
        ctx.beginPath(); ctx.arc(hub.x, hub.y, hub.r * 0.48, 0, Math.PI * 2); ctx.fill();
        ctx.font = "500 " + Math.max(11, fs - 1) + "px " + FONT;
        ctx.textAlign = "center";
        var hubLabel = "Brain", hubW = ctx.measureText(hubLabel).width;
        ctx.fillStyle = "rgba(3,2,1,0.78)";
        ctx.fillRect(hub.x - hubW / 2 - 5, hub.y + hub.r + 5, hubW + 10, fs + 4);
        ctx.fillStyle = "#efebe4";
        ctx.fillText(hubLabel, hub.x, hub.y + hub.r + 7);
      }

      // impulses
      for (var q = impulses.length - 1; q >= 0; q--) {
        var im = impulses[q], p = (now - im.start) / im.dur;
        if (p < 0) { anyAnim = true; continue; }
        if (p >= 1) {
          if (im.project) flashes[im.project.id] = now;
          im.targets.forEach(function (tn) { tn.lit = now; });
          impulses.splice(q, 1);
          continue;
        }
        anyAnim = true;
        var e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
        var sp = sprite(im.color), size = (im.small ? 16 : 26) * G.s + 8;
        for (var tr = 0; tr < 5; tr++) {
          var pp = pointAlong(im.path, Math.max(0, e - tr * 0.035));
          ctx.globalAlpha = (1 - tr / 5) * 0.9;
          var ts = size * (1 - tr * 0.13);
          ctx.drawImage(sp, pp[0] - ts / 2, pp[1] - ts / 2, ts, ts);
        }
      }
      ctx.globalAlpha = 1;

      // projects (lobes)
      var fs = Math.max(11, Math.min(17, 15 * G.s + 4));
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.font = "500 " + fs + "px " + FONT;
      nodes.forEach(function (n) {
        if (n.type !== "project" || n.x === undefined) return;
        var f = flashes[n.id] ? (now - flashes[n.id]) / 1400 : 1;
        if (f < 1) anyAnim = true; else delete flashes[n.id];
        var fl = f < 1 ? 1 - f : 0;
        var breathe = REDUCED ? 0 : 0.08 * Math.sin(t * 0.8 + n.phase);
        var gs = n.r * (4.2 + breathe + fl * 4);
        ctx.globalAlpha = 0.55 + fl * 0.45;
        ctx.drawImage(sprite(n.id === "p:other" ? "#24211c" : "#3a3631"), n.x - gs / 2, n.y - gs / 2, gs, gs);
        if (fl) {
          ctx.globalAlpha = fl;
          var fs2 = gs * 1.3;
          ctx.drawImage(sprite("#5c3a22"), n.x - fs2 / 2, n.y - fs2 / 2, fs2, fs2);
          ctx.strokeStyle = "rgba(249,124,61," + fl + ")";
          ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + (1 - fl) * 40 * G.s, 0, Math.PI * 2); ctx.stroke();
        }
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#cecac3";
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r * 0.5, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "rgba(3,2,1,0.78)";
        var label = n.name.length > 22 ? n.name.slice(0, 21) + "…" : n.name;
        var tw2 = ctx.measureText(label).width;
        ctx.fillRect(n.x - tw2 / 2 - 5, n.y + n.r + 3, tw2 + 10, fs + 4);
        ctx.fillStyle = fl ? "#f97c3d" : "rgba(206,202,195,0.92)";
        ctx.fillText(label, n.x, n.y + n.r + 5);
      });

      // agents (rim)
      ctx.font = "500 " + Math.max(10, fs - 2) + "px " + FONT;
      nodes.forEach(function (n) {
        if (n.type !== "agent") return;
        var lit = n.lit && now - n.lit < 1200 ? 1 - (now - n.lit) / 1200 : 0;
        if (lit) anyAnim = true;
        var pul = n.active && !REDUCED ? 0.5 + 0.5 * Math.sin(t * 3 + n.phase) : 0.3;
        var as = n.r * (5 + pul * 1.5 + lit * 4);
        ctx.globalAlpha = n.active || n.connected ? 0.9 : 0.45;
        ctx.drawImage(sprite(n.color), n.x - as / 2, n.y - as / 2, as, as);
        ctx.globalAlpha = 1;
        ctx.fillStyle = n.active || lit ? "#efebe4" : n.color;
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r * 0.6, 0, Math.PI * 2); ctx.fill();
        if (n.connected) {
          ctx.strokeStyle = n.color + "99"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 3, 0, Math.PI * 2); ctx.stroke();
        }
        var above = n.y < G.cy;
        ctx.textBaseline = above ? "bottom" : "top";
        ctx.fillStyle = n.color;
        ctx.fillText(agentShort(n.agent) + " · " + (n.name || ""), n.x, above ? n.y - n.r - 6 : n.y + n.r + 6);
      });
      ctx.textBaseline = "top";

      if (hover) drawHover(hover);
      return anyAnim;
    }

    function drawHover(n) {
      ctx.strokeStyle = "rgba(239,235,228,0.8)";
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 4, 0, Math.PI * 2); ctx.stroke();
    }

    function loop(now) {
      if (!running || !active) { running = false; return; }
      requestAnimationFrame(loop);
      var simActive = sim && sim.alpha() > sim.alphaMin();
      var busy = simActive || impulses.length > 0;
      var minGap = busy ? 33 : 50; // ~30 fps while animating, ~20 fps idle twinkle
      if (now - lastFrame < minGap) return;
      lastFrame = now;
      if (simActive) sim.tick();
      var anim = draw(now);
      if (REDUCED && !simActive && !anim && !impulses.length) { running = false; } // static until something changes
    }

    function kick() {
      dirty = true;
      if (document.hidden || !active) return;
      if (!running) { running = true; requestAnimationFrame(loop); }
    }

    function resize() {
      var r = wrap.getBoundingClientRect();
      if (r.width < 10 || r.height < 10) return;
      W = r.width; H = r.height;
      DPR = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
      var oldG = G;
      G = geometry();
      renderOutline();
      if (oldG && nodes.length) {
        var sx = G.s / oldG.s;
        nodes.forEach(function (n) {
          if (n.x !== undefined) { n.x = G.cx + (n.x - oldG.cx) * sx; n.y = G.cy + (n.y - oldG.cy) * sx; }
          if (n.type === "memory") n.r = 2.6 * G.s + 1;
          if (n.type === "agent") n.r = 6 * G.s + 2.5;
        });
        assignAnchors();
        buildSim(0.15);
      }
      if (pendingGraph) { var pg = pendingGraph; pendingGraph = null; setGraph(pg); }
      kick();
    }

    // hover + click
    canvas.addEventListener("mousemove", function (e) {
      var r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      var best = null, bd = 14 * 14;
      nodes.forEach(function (n) {
        if (n.x === undefined || n.type === "hub") return;
        var d = (n.x - x) * (n.x - x) + (n.y - y) * (n.y - y);
        if (d < Math.max(bd, n.r * n.r)) { bd = d; best = n; }
      });
      hover = best;
      if (!best) { tip.hidden = true; canvas.style.cursor = ""; kick(); return; }
      canvas.style.cursor = best.type === "project" && best.path ? "pointer" : "";
      var html = best.type === "memory" ? '<b style="color:' + best.color + '">' + esc(best.kind) + "</b> " + esc(best.title) +
          '<div class="muted">' + esc(best.project || "global") + "</div>"
        : best.type === "project" ? "<b>" + esc(best.name) + '</b><div class="muted">' + (best.memories || 0) + " memories · " + (best.activity || 0) + " events 7d</div>"
        : '<b style="color:' + best.color + '">' + esc(best.agent) + "</b> · " + esc(best.name || "") +
          '<div class="muted">' + (best.active ? "active" : "idle") + (best.connected ? " · mcp connected" : "") + "</div>";
      tip.innerHTML = html;
      tip.hidden = false;
      tip.style.left = Math.min(W - 330, x + 14) + "px";
      tip.style.top = Math.min(H - 60, y + 12) + "px";
      kick();
    });
    canvas.addEventListener("mouseleave", function () { hover = null; tip.hidden = true; kick(); });
    canvas.addEventListener("click", function () {
      if (hover && hover.type === "project" && hover.path) location.href = "/projects/detail?path=" + encodeURIComponent(hover.path);
      if (hover && hover.type === "memory") location.href = "/memory?q=" + encodeURIComponent(hover.title || "");
    });

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) running = false; else kick();
    });
    if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(wrap);
    else window.addEventListener("resize", resize);

    function setActive(on) {
      active = on;
      canvas.hidden = !on;
      if (!on) { tip.hidden = true; running = false; } else { resize(); kick(); }
    }

    resize();
    return { setGraph: setGraph, pulse: pulse, resize: resize, setActive: setActive };
  })();

  // ------------------------------------------------------------------ 2D / 3D switch

  // Glow is owned by brain3d.js; show the saved state even before (or without) the 3D view starting.
  (function () {
    var glow = $("glow-toggle"), on = false;
    if (!glow) return;
    try { on = localStorage.getItem("devmem.bloom") === "1"; } catch (e) { /* default off */ }
    glow.classList.toggle("on", on);
    glow.setAttribute("aria-pressed", on ? "true" : "false");
    glow.textContent = on ? "● Glow on" : "○ Glow off";
  })();

  var Brain = (function () {
    var mode = "3d", impl3d = null, lastGraph = null, reason = "";
    var btn = $("dim-toggle"), box3d = $("brain3d");
    try { mode = localStorage.getItem("devmem.brainMode") === "2d" ? "2d" : "3d"; } catch (e) { /* default 3D */ }

    function ensure3d() {
      if (impl3d) return true;
      if (!window.DevmemBrain3D || !window.ForceGraph3D) { reason = "3D library did not load"; return false; }
      if (!window.DevmemBrain3D.available()) { reason = "WebGL unavailable"; return false; }
      try {
        box3d.hidden = false;
        impl3d = window.DevmemBrain3D.create(box3d, { agentColor: agentColor, agentShort: agentShort, esc: esc, reduced: REDUCED });
        if (lastGraph) impl3d.setGraph(lastGraph);
        return true;
      } catch (err) {
        console.error("devmem 3D failed, using 2D", err);
        reason = "3D failed to start";
        box3d.hidden = true;
        impl3d = null;
        return false;
      }
    }

    function apply(wanted, remember) {
      var use3d = wanted === "3d" && ensure3d();
      mode = use3d ? "3d" : "2d";
      box3d.hidden = !use3d;
      $("brain-wrap").classList.toggle("is3d", use3d);
      if (impl3d) impl3d.setActive(use3d);
      if (use3d && lastGraph) impl3d.setGraph(lastGraph); // merge by id: positions are kept
      Brain2D.setActive(!use3d);
      if (!use3d && lastGraph) Brain2D.setGraph(lastGraph);
      btn.innerHTML = '<span class="seg' + (use3d ? "" : " active") + '">2D</span><span class="seg' + (use3d ? " active" : "") + '">3D</span>';
      document.body.classList.toggle("view-2d", !use3d);
      btn.title = use3d ? "Showing the 3D web; click for the flat 2D brain" :
        (wanted === "3d" && reason ? "3D unavailable (" + reason + "); showing 2D" : "Showing the 2D brain; click for the 3D web");
      btn.classList.toggle("unavailable", wanted === "3d" && !use3d);
      if (remember) { try { localStorage.setItem("devmem.brainMode", wanted); } catch (e) { /* ignore */ } }
    }

    btn.addEventListener("click", function () { apply(mode === "3d" ? "2d" : "3d", true); });
    apply(mode, false);

    return {
      setGraph: function (g) {
        lastGraph = g;
        if (mode === "3d" && impl3d) impl3d.setGraph(g); else Brain2D.setGraph(g);
      },
      pulse: function (p) { if (mode === "3d" && impl3d) impl3d.pulse(p); else Brain2D.pulse(p); },
      resize: function () { if (mode === "3d" && impl3d) impl3d.resize(); else Brain2D.resize(); },
      mode: function () { return mode; },
    };
  })();
  window.devmemBrain = Brain;

  // ------------------------------------------------------------------ state + stream

  var lastState = null;

  function applyState(s) {
    lastState = Object.assign({}, lastState || {}, s);
    if (s.graph) Brain.setGraph(s.graph);
    renderLimits(s.limits || []);
    renderAgents(s.agents || {});
    renderFeed(s.activity || []);
    renderCounts(s.counts || {});
  }

  var es = null, lastMessage = 0, backoff = 1000;

  function setStatus(state, text) {
    var el = $("status");
    el.dataset.state = state;
    el.querySelector(".txt").textContent = text;
  }

  function disconnect() { if (es) { es.onerror = null; es.close(); es = null; } }
  function connect() {
    disconnect();
    if (document.hidden) return;
    setStatus("connecting", "connecting");
    es = new EventSource("/api/stream");
    es.addEventListener("state", function (e) {
      lastMessage = Date.now(); backoff = 1000;
      setStatus("live", "live");
      try { applyState(JSON.parse(e.data)); } catch (err) { console.error(err); }
    });
    es.addEventListener("messages", function (e) {
      lastMessage = Date.now();
      try { renderMessages(JSON.parse(e.data)); } catch (err) { console.error(err); }
    });
    es.addEventListener("pulse", function (e) {
      lastMessage = Date.now();
      try {
        var pulses = JSON.parse(e.data);
        pulses.forEach(Brain.pulse);
        if (pulses.some(function (pulse) { return pulse.type === "ado"; })) refreshAdoCard();
      } catch (err) { console.error(err); }
    });
    es.onerror = function () {
      setStatus("offline", "offline · reconnecting");
      if (es.readyState === EventSource.CLOSED) {
        setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 15000);
      }
    };
  }

  refreshAdoCard();
  setInterval(refreshAdoCard, 30000);

  // Watchdog: a half-open connection (laptop sleep, server restart) sends nothing; reconnect it.
  setInterval(function () {
    if (!es) return;  // hidden: no stream on purpose
    if (lastMessage && Date.now() - lastMessage > 12000) {
      setStatus("offline", "offline · reconnecting");
      lastMessage = Date.now();
      connect();
    }
  }, 3000);

  // fix(2026-10-28): browsers allow 6 connections per site. A stream left open by a background tab or by a page kept for Back used
  // one up each, until new pages hung loading forever. Only the visible page keeps its stream.
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", function (e) { if (e.persisted) connect(); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) disconnect(); else if (!es) { lastMessage = Date.now(); connect(); } });

  setInterval(function () {
    var d = new Date();
    $("clock").textContent = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + "  " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    tickGauges();
    tickRelTimes();
  }, 1000);

  connect();
})();
