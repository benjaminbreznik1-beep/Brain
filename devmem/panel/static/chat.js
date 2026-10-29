// devmem chats: WhatsApp-style list and conversation, live over the shared SSE stream (messages only).
(function () {
  "use strict";

  var U = window.devmem;
  var $ = function (id) { return document.getElementById(id); };
  var app = $("chat-app"), listEl = $("chat-list"), dataEl = $("chat-data");
  var scroller = $("chat-scroll"), itemsEl = $("chat-items"), pill = $("new-pill"), form = $("chat-form"), input = $("chat-body");
  var chat = dataEl ? JSON.parse(dataEl.textContent) : null;
  var filters = app ? app.getAttribute("data-filters") || "" : "";
  var LONG_CHARS = 700, LONG_LINES = 12, TOOL_PREVIEW = 3;
  var rendered = {}, firstPaint = true, lastSig = null, lastFeed = null;
  var headlineMode = true;
  try { headlineMode = localStorage.getItem("devmem-chat-mode") !== "full"; } catch (_) {}

  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function color(member) {
    if (!member) return "var(--muted)";
    if (member.kind === "user") return "var(--violet)";
    if (member.kind === "claude") return "var(--claude)";
    return "hsl(" + (member.hue || 0) + " 70% 72%)";
  }

  function member(key) {
    return (chat.members || {})[key] || { name: String(key || "").replace(/^x:/, ""), kind: "other", hue: 0 };
  }

  function short(text, size) {
    text = String(text || "").replace(/\s+/g, " ").trim();
    return text.length > size ? text.slice(0, size - 1) + "…" : text;
  }

  function clock(d) { return d ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : ""; }

  function dayKey(d) { return d ? d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate() : "?"; }

  function dayLabel(d) {
    if (!d) return "Unknown date";
    var today = new Date(), yesterday = new Date(Date.now() - 864e5);
    if (dayKey(d) === dayKey(today)) return "Today";
    if (dayKey(d) === dayKey(yesterday)) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
  }

  function whoHtml(m) {
    var name = esc(m.name);
    return m.agent_id ? '<a href="/messages/agent/' + encodeURIComponent(m.agent_id) + '" title="' + esc(m.type || "") + (m.description ? " · " + esc(m.description) : "") + '">' + name + "</a>" : name;
  }

  function bubbleHtml(item) {
    var m = member(item.who), d = U.parseTs(item.at), text = item.text || "";
    var long = text.length > LONG_CHARS || text.split("\n").length > LONG_LINES;
    var report = item.label === "report" || item.label === "stopped";
    var head = item.mine ? "" : '<div class="b-who">' + whoHtml(m) + (m.declared ? ' <span class="b-declared" title="self-declared sender">*</span>' : "") +
      (item.to ? ' <span class="b-to">' + esc(item.to.charAt(0) === "@" || item.to.charAt(0) === "#" ? item.to : "→ " + item.to) + "</span>" : "") + "</div>";
    var label = item.label ? '<span class="b-label ' + esc(item.label) + '">' + esc(item.label) + (item.status ? " · " + esc(item.status) : "") + "</span>" : "";
    return '<div class="bubble ' + (item.mine ? "mine" : "theirs") + (item.label ? " l-" + esc(item.label) : "") + (long ? " long" : "") + '" style="--c:' + color(m) + '">' +
      head + (item.subject ? '<div class="b-subject">' + esc(item.subject) + "</div>" : "") +
      (report ? '<pre class="b-text">' + esc(text) + "</pre>" : '<div class="b-text">' + esc(text) + "</div>") +
      (long ? '<button type="button" class="b-more">Show more</button>' : "") +
      '<div class="b-foot">' + label + '<time title="' + esc(item.at || "") + '">' + clock(d) + "</time></div></div>";
  }

  function toolsHtml(item) {
    var m = member(item.who), tools = item.tools || [];
    var preview = tools.slice(0, TOOL_PREVIEW).map(function (t) { return t.name + (t.arg ? " " + short(t.arg.split("/").pop() || t.arg, 32) : ""); }).join(" · ");
    return '<details class="sysline" style="--c:' + color(m) + '"><summary>⚙ <b>' + esc(m.name) + "</b>: " + esc(preview) + (tools.length > TOOL_PREVIEW ? " …" : "") +
      ' <span class="count">(' + tools.length + ")</span> <time>" + clock(U.parseTs(item.at)) + "</time></summary><ol>" +
      tools.map(function (t) { return '<li><span class="tool">' + esc(t.name) + "</span> " + esc(t.arg || "") + "</li>"; }).join("") + "</ol></details>";
  }

  function headlineHtml(item) {
    var detail = item.text ? '<pre class="headline-detail">' + esc(item.text) + "</pre>" : "";
    var tone = { "▶": "live", "✓": "done", "⚠": "warn", "?": "question", "■": "stopped", "✉": "message" }[item.icon] || "message";
    return '<details class="headline-item"><summary><span class="headline-icon st-' + tone + '">' + esc(item.icon || "✉") + '</span> ' + esc(item.headline || item.text || "") +
      '<time title="' + esc(item.at || "") + '">' + clock(U.parseTs(item.at)) + "</time></summary>" + detail + "</details>";
  }

  function nodeFrom(html) {
    var box = document.createElement("div");
    box.innerHTML = html;
    return box.firstChild;
  }

  function atBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; }

  function renderChat() {
    if (!chat || !itemsEl) return;
    var stick = firstPaint || atBottom(), next = {}, nodes = [], fresh = 0, day = null;
    (headlineMode ? (chat.headlines || []) : (chat.items || [])).forEach(function (item) {
      var d = U.parseTs(item.at), dk = dayKey(d);
      if (dk !== day) {
        day = dk;
        var dkKey = "d:" + dk, label = dayLabel(d);
        next[dkKey] = rendered[dkKey] && rendered[dkKey].sig === label ? rendered[dkKey] : { el: nodeFrom('<div class="day-sep"><span>' + esc(label) + "</span></div>"), sig: label };
        nodes.push(next[dkKey].el);
      }
      var sig = JSON.stringify(item), old = rendered[item.key];
      if (old && old.sig === sig) { next[item.key] = old; nodes.push(old.el); return; }
      var el = nodeFrom(headlineMode ? headlineHtml(item) : (item.type === "tools" ? toolsHtml(item) : bubbleHtml(item)));
      if (old && old.el.open) el.open = true;  // keep an expanded tool list open while it grows
      if (old && old.el.classList.contains("open")) el.classList.add("open");
      if (!old && !firstPaint) { fresh += 1; el.classList.add("enter"); }
      next[item.key] = { el: el, sig: sig };
      nodes.push(el);
    });
    if (!nodes.length) nodes.push(nodeFrom('<div class="chat-none muted">No bubbles yet.</div>'));
    itemsEl.replaceChildren.apply(itemsEl, nodes);
    rendered = next;
    if (stick) { scroller.scrollTop = scroller.scrollHeight; pill.hidden = true; }
    else if (fresh) pill.hidden = false;
    firstPaint = false;
    renderHead();
  }

  function renderHead() {
    var box = $("chat-members");
    if (!box) return;
    var keys = Object.keys(chat.members || {});
    box.innerHTML = keys.map(function (key) {
      var m = chat.members[key];
      return '<span class="chip' + (m.status ? " st-" + esc(m.status) : "") + '" style="--c:' + color(m) + '" title="' + esc(m.status || m.kind || "") + '">' +
        (m.status ? '<span class="dot"></span>' : "") + whoHtml(m) + "</span>";
    }).join("");
    if (input) {
      var t = chat.target;
      input.placeholder = t ? "Message " + t.name + (t.why === "open question" ? " (answers their question)" : "") : "Message";
    }
  }

  function rowHtml(c) {
    var on = chat && chat.key === c.key;
    return '<li class="chat-row' + (on ? " on" : "") + '"><a href="/messages?chat=' + encodeURIComponent(c.key) + filters + '">' +
      '<span class="avatar k-' + esc(c.kind) + '" style="--h:' + c.hue + '">' + esc(c.kind === "c" ? "#" : (c.title || "?").charAt(0).toUpperCase()) + "</span>" +
      '<span class="cr-main"><span class="cr-top"><b class="cr-title">' + esc(c.title) + '</b><time data-rel datetime="' + esc(c.last_at) + '">' + U.relTime(c.last_at) + "</time></span>" +
      '<span class="cr-bottom"><span class="cr-preview">' + (c.running ? '<span class="cr-live" title="agents still working">●</span> ' : "") + esc(c.state || "") + "</span>" +
      (c.unread && !on ? '<span class="msg-badge" title="unread">' + c.unread + "</span>" : "") + "</span><span class=\"cr-preview\">" + esc(c.headline || c.preview) + "</span></span></a></li>";
  }

  function refreshList() {
    if (!listEl) return;
    fetch("/api/chats" + (filters ? "?" + filters.slice(1) : "")).then(function (r) { return r.ok ? r.json() : null; }).then(function (rows) {
      if (!rows) return;
      listEl.innerHTML = rows.map(rowHtml).join("") || '<li class="chat-none muted">No chats yet.</li>';
    }).catch(function (e) { console.warn("devmem: chat list refresh failed", e); });
  }

  var loading = null;
  function refreshChat() {
    if (!chat) return;
    if (loading) { loading.again = true; return; }
    loading = { again: false };
    fetch("/api/chat?read=1&key=" + encodeURIComponent(chat.key)).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (data) { chat = data; renderChat(); }
    }).catch(function (e) { console.warn("devmem: chat refresh failed", e); }).then(function () {
      var again = loading.again; loading = null;
      if (again) refreshChat();
    });
  }

  function onFeed(feed) {
    var sig = JSON.stringify(feed.sig || null);
    var badge = $("nav-msg-badge");
    if (badge) { badge.textContent = feed.badge || 0; badge.hidden = !feed.badge; }
    if (lastSig !== null && sig !== lastSig) refreshChat();
    if (lastFeed !== null && JSON.stringify(feed) !== lastFeed) refreshList();
    lastSig = sig; lastFeed = JSON.stringify(feed);
  }

  var es = null, backoff = 1000;
  function disconnect() { if (es) { es.onerror = null; es.close(); es = null; } }
  function connect() {
    disconnect();
    if (document.hidden) return;
    es = new EventSource("/api/stream?only=messages");
    es.addEventListener("messages", function (e) {
      backoff = 1000;
      try { onFeed(JSON.parse(e.data)); } catch (err) { console.error(err); }
    });
    es.onerror = function () {
      if (es.readyState === EventSource.CLOSED) { setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000); }
    };
  }

  // fix(2026-10-28): browsers allow 6 connections per site. A stream left open by a background tab or by a page kept for Back used
  // one up each, until new pages hung loading forever. Only the visible page keeps its stream.
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", function (e) { if (e.persisted) connect(); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) disconnect(); else if (!es) connect(); });

  if (itemsEl) {
    itemsEl.addEventListener("click", function (e) {
      var more = e.target.closest(".b-more");
      if (!more) return;
      var bubble = more.closest(".bubble");
      bubble.classList.toggle("open");
      more.textContent = bubble.classList.contains("open") ? "Show less" : "Show more";
    });
    scroller.addEventListener("scroll", function () { if (atBottom()) pill.hidden = true; });
    pill.addEventListener("click", function () { scroller.scrollTop = scroller.scrollHeight; pill.hidden = true; });
  }

  var modeButton = $("headline-toggle");
  function setModeLabel() { if (modeButton) modeButton.textContent = headlineMode ? "Headlines" : "Full"; }
  if (modeButton) modeButton.addEventListener("click", function () {
    headlineMode = !headlineMode; rendered = {}; firstPaint = true;
    try { localStorage.setItem("devmem-chat-mode", headlineMode ? "headlines" : "full"); } catch (_) {}
    setModeLabel(); renderChat();
  });

  if (form) {
    var error = $("chat-error");
    var grow = function () { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; };
    input.addEventListener("input", grow);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.submit(); }
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (!input.value.trim()) return;
      var button = form.querySelector("button");
      button.disabled = true; error.hidden = true;
      fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json" } }).then(function (r) {
        if (r.ok) { input.value = ""; grow(); firstPaint = true; refreshChat(); refreshList(); return; }
        return r.json().catch(function () { return {}; }).then(function (body) { throw new Error(body.detail || ("send failed (" + r.status + ")")); });
      }).catch(function (err) { error.textContent = err.message; error.hidden = false; }).then(function () { button.disabled = false; input.focus(); });
    });
  }

  setModeLabel(); renderChat();
  connect();
  setInterval(refreshChat, 30000);  // statuses also change with time (running → stale → stopped)
  setInterval(function () {
    document.querySelectorAll("time[data-rel]").forEach(function (el) { el.textContent = U.relTime(el.getAttribute("datetime")); });
  }, 30000);
})();
