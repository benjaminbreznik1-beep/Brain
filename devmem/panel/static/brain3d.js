// devmem 3D brain: a glowing neural web on 3d-force-graph (WebGL / three.js).
//
// The pinned 3d-force-graph bundle does not expose THREE. Its second three.js copy must stay coupled to r162, so the few classes needed for
// canvas-texture sprites (labels, glow halos) are taken from objects the library itself creates:
// a probe node's default mesh and the post-processing composer's render target.  If that fails the web
// still renders with the library's default spheres and hover tooltips.
(function () {
  "use strict";

  var KIND_COLORS = { note: "#60a5fa", decision: "#a78bfa", preference: "#f472b6", snippet: "#34d399", todo: "#fb923c" };
  var LINK_COLORS = { blocks: "#fb923c", depends_on: "#fbbf24", relates_to: "#94a3b8", overlaps: "#64748b", fixes: "#34d399", supersedes: "#78716c" };
  var R = 120;                       // overall web radius (world units)
  var ADDITIVE = 2;                  // THREE.AdditiveBlending

  function webglAvailable() {
    try {
      var c = document.createElement("canvas");
      return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
    } catch (e) { return false; }
  }

  function create(container, opts) {
    var agentColor = opts.agentColor, agentShort = opts.agentShort, esc = opts.esc, REDUCED = !!opts.reduced;
    var C = null;                    // harvested three.js constructors
    var Graph = ForceGraph3D({ controlType: "orbit", rendererConfig: { antialias: true, alpha: false, powerPreference: "low-power" } })(container);
    var nodes = [], links = [], nodeById = {}, linkByKey = {};
    var pendingGraph = null, pendingBloom = {}, anims = [], lastImpulse = {};
    var active = true, ready = false, fitted = false, resumeTimer = null, loopOn = false, lastTwinkle = 0;
    var texCache = {}, geomCache = {};
    var bloomPass = null, bloomAdded = false, bloomEnabled = true, bloomError = "";

    try { bloomEnabled = localStorage.getItem("devmem.glow") !== "0"; } catch (e) { /* default on */ }

    function bloomActive() { return !!(bloomPass && bloomPass.enabled); }

    function haloBaseSize(n) {
      var size = n.radius * (n.type === "hub" ? 10 : n.type === "project" ? 7 : n.type === "agent" ? 9 : 6);
      return size * (bloomActive() ? 0.62 : 1);
    }

    function haloOpacity(n) { return n.baseGlow * (bloomActive() ? 0.42 : 1); }

    function syncHalos() {
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i], parts = n.parts;
        if (!parts || !parts.halo || isAnimating(n)) continue;
        parts.halo.material.opacity = haloOpacity(n);
        parts.halo.scale.setScalar(haloBaseSize(n));
      }
    }

    function resizeBloom(width, height) {
      if (!bloomPass || !bloomPass.setSize) return;
      // UnrealBloomPass halves internally; pass CSS canvas size times renderer pixel ratio for real half-resolution bloom.
      var ratio = Graph.renderer && Graph.renderer().getPixelRatio ? Graph.renderer().getPixelRatio() : window.devicePixelRatio || 1;
      bloomPass.setSize(Math.max(1, Math.ceil(width * ratio)), Math.max(1, Math.ceil(height * ratio)));
    }

    function ensureBloom() {
      if (bloomAdded || !window.__devmemBloom) return;
      try {
        var composer = Graph.postProcessingComposer && Graph.postProcessingComposer();
        if (!composer || !composer.addPass) { bloomError = "3D graph has no post-processing composer"; setGlow(bloomEnabled); return; }
        // A plain x/y object is sufficient; UnrealBloomPass copies it into its own Vector2.
        bloomPass = new window.__devmemBloom({ x: 1, y: 1 }, 0.75, 0.45, 0.25);
        bloomPass.enabled = bloomEnabled;
        composer.addPass(bloomPass);
        bloomAdded = true;
        var rect = container.getBoundingClientRect();
        resizeBloom(rect.width, rect.height);
        syncHalos();
      } catch (e) {
        // The graph and its halo fallback remain usable if the module/composer is incompatible.
        bloomPass = null;
        bloomError = String((e && e.message) || e);
        console.warn("devmem: bloom unavailable", e);
      }
      setGlow(bloomEnabled);
    }

    function bloomFailed() {
      bloomError = window.__devmemBloomError || "bloom module failed to load";
      console.warn("devmem: bloom unavailable:", bloomError);
      setGlow(bloomEnabled);
    }

    function setGlow(on) {
      bloomEnabled = on;
      try { localStorage.setItem("devmem.glow", on ? "1" : "0"); } catch (e) { /* storage unavailable */ }
      if (bloomPass) bloomPass.enabled = on;
      syncHalos();
      var button = document.getElementById("glow-toggle");
      if (button) {
        button.setAttribute("aria-pressed", on ? "true" : "false");
        button.classList.toggle("on", on);
        button.textContent = on ? "● Glow on" : "○ Glow off";
        button.title = on ? "Turn off 3D bloom glow (3D view only)" : "Turn on 3D bloom glow (3D view only)";
        // Make a missing bloom effect visible instead of a toggle that silently does nothing.
        button.classList.toggle("unavailable", !bloomPass);
        if (!bloomPass) button.title = "Glow effect not active: " + (bloomError || "bloom module still loading");
      }
    }

    Graph.renderer().setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    Graph.backgroundColor("#050814")
      .showNavInfo(false)
      .enableNodeDrag(false)
      .nodeId("id")
      .warmupTicks(40)
      .cooldownTime(12000)
      .d3AlphaDecay(0.025)
      .d3VelocityDecay(0.32)
      .linkOpacity(0.32)
      .linkWidth(0)
      .linkColor(function (l) { return l.color; })
      .linkDirectionalParticles(function (l) { return l.particles || 0; })
      .linkDirectionalParticleColor(function (l) { return l.pcolor || "#ffffff"; })
      .linkDirectionalParticleWidth(function (l) { return l.pwidth || 1.6; })
      .linkDirectionalParticleSpeed(function (l) { return l.kind === "ah" ? 0.016 : 0.035; })
      .linkDirectionalParticleResolution(6)
      .nodeLabel(tooltip)
      .onNodeClick(function (n) {
        if (n.type === "project" && n.path) location.href = "/projects/detail?path=" + encodeURIComponent(n.path);
        else if (n.type === "memory") location.href = "/memory?q=" + encodeURIComponent(n.title || "");
      })
      .onEngineStop(function () {
        if (!fitted && nodes.length > 2) { fitted = true; Graph.zoomToFit(900, 30); }
      });

    var glowButton = document.getElementById("glow-toggle");
    if (glowButton) glowButton.addEventListener("click", function () { setGlow(!bloomEnabled); });
    setGlow(bloomEnabled);
    window.addEventListener("devmem-bloom", ensureBloom);
    window.addEventListener("devmem-bloom-failed", bloomFailed);
    if (window.__devmemBloomError) bloomFailed();
    ensureBloom();

    // ---------------------------------------------------------------- forces: web, clusters, loose brain shape
    var shapeNodes = [];
    function shapeForce(alpha) {
      for (var i = 0; i < shapeNodes.length; i++) {
        var n = shapeNodes[i];
        if (n.fx !== undefined && n.fx !== null) continue;
        // ellipsoid: wider left-right, like a brain seen from the front
        var ex = n.x / (R * 1.35), ey = n.y / (R * 0.95), ez = n.z / (R * 1.05);
        var r = Math.sqrt(ex * ex + ey * ey + ez * ez) || 1e-6;
        if (n.type === "agent") {
          var shell = 1.45;          // agents float on the outside
          var k = (shell - r) * 0.08 * alpha;
          n.vx += n.x / r * k; n.vy += n.y / r * k; n.vz += n.z / r * k;
        } else if (r > 1) {
          var k2 = (r - 1) * 0.12 * alpha;
          n.vx -= n.x * k2; n.vy -= n.y * k2; n.vz -= n.z * k2;
        }
        if (n.type === "project") {  // two loose hemispheres
          var side = n.side || 1;
          n.vx += (side * R * 0.5 - n.x) * 0.02 * alpha;
        }
      }
    }
    shapeForce.initialize = function (ns) { shapeNodes = ns; };
    Graph.d3Force("shape", shapeForce);
    Graph.d3Force("charge").strength(function (n) {
      return n.type === "project" ? -140 : n.type === "agent" ? -60 : n.type === "hub" ? -30 : -9;
    });
    Graph.d3Force("link")
      .distance(function (l) { return l.kind === "ah" ? 85 : l.kind === "hp" ? 62 : l.kind === "hm" ? 26 : l.kind === "ml" ? 34 : 14 + (l.spread || 0); })
      .strength(function (l) { return l.kind === "ah" ? 0.04 : l.kind === "hp" ? 0.16 : l.kind === "ml" ? 0.07 : 0.55; });

    // ---------------------------------------------------------------- auto-rotation, paused while the user orbits/zooms
    var controls = Graph.controls();
    if (controls) {
      controls.autoRotate = !REDUCED;
      controls.autoRotateSpeed = 0.55;
      controls.enableDamping = true;
      controls.dampingFactor = 0.08;
      if (controls.addEventListener) {
        controls.addEventListener("start", function () {
          controls.autoRotate = false;
          clearTimeout(resumeTimer);
        });
        controls.addEventListener("end", function () {
          clearTimeout(resumeTimer);
          if (!REDUCED) resumeTimer = setTimeout(function () { controls.autoRotate = true; }, 6000);
        });
      }
    }

    // ---------------------------------------------------------------- harvest three.js classes from a probe node
    function harvest() {
      try {
        var probe = Graph.graphData().nodes[0];
        var mesh = probe && probe.__threeObj;
        var comp = Graph.postProcessingComposer();
        var texture = comp && (comp.renderTarget1 || comp.readBuffer) && (comp.renderTarget1 || comp.readBuffer).texture;
        if (!mesh || !mesh.geometry || !mesh.material || !texture) return false;
        var Geo = mesh.geometry.constructor;
        C = {
          Mesh: mesh.constructor,
          Object3D: Object.getPrototypeOf(mesh.constructor.prototype).constructor,
          Sphere: Geo,
          BufferGeometry: Object.getPrototypeOf(Geo.prototype).constructor,
          Attr: mesh.geometry.getAttribute("position").constructor,
          Material: mesh.material.constructor,
          Texture: texture.constructor,
        };
        // smoke-construct once so a mismatch falls back instead of failing later
        new C.Mesh(new C.Sphere(1, 6, 4), new C.Material({ color: 0xffffff }));
        return true;
      } catch (e) {
        C = null;
        return false;
      }
    }

    function bootstrap() {
      Graph.graphData({ nodes: [{ id: "__probe" }], links: [] });
      var tries = 0;
      (function poll() {
        tries++;
        var probe = Graph.graphData().nodes[0];
        if ((probe && probe.__threeObj) || tries > 90) {
          if (!harvest()) console.warn("devmem 3D: sprite classes unavailable; using default spheres");
          if (C) Graph.nodeThreeObject(makeNode);
          else Graph.nodeVal(function (n) { return n.val; }).nodeColor(function (n) { return n.color; }).nodeOpacity(0.9);
          ready = true;
          Graph.graphData({ nodes: [], links: [] });
          if (pendingGraph) { var g = pendingGraph; pendingGraph = null; setGraph(g); }
          return;
        }
        requestAnimationFrame(poll);
      })();
    }

    // ---------------------------------------------------------------- node objects
    function canvasTexture(canvas) {
      var t = new C.Texture(canvas);
      if ("colorSpace" in t) t.colorSpace = "srgb";
      t.needsUpdate = true;
      return t;
    }

    function haloTexture() {
      if (texCache.halo) return texCache.halo;
      var c = document.createElement("canvas"), s = 64;
      c.width = c.height = s;
      var x = c.getContext("2d"), g = x.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
      g.addColorStop(0, "rgba(255,255,255,1)");
      g.addColorStop(0.18, "rgba(255,255,255,0.55)");
      g.addColorStop(0.5, "rgba(255,255,255,0.12)");
      g.addColorStop(1, "rgba(255,255,255,0)");
      x.fillStyle = g;
      x.fillRect(0, 0, s, s);
      return (texCache.halo = canvasTexture(c));
    }

    function labelTexture(text, color) {
      var key = text + "|" + color;
      if (texCache[key]) return texCache[key];
      var c = document.createElement("canvas"), x = c.getContext("2d"), fs = 44;
      x.font = "600 " + fs + "px system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
      var w = Math.ceil(x.measureText(text).width) + 28;
      c.width = w; c.height = fs + 22;
      x.font = "600 " + fs + "px system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
      x.fillStyle = "rgba(5,8,20,0.62)";
      roundRect(x, 0, 0, w, c.height, 14);
      x.fill();
      x.fillStyle = color;
      x.textBaseline = "middle";
      x.fillText(text, 14, c.height / 2 + 2);
      var t = canvasTexture(c);
      t.aspect = w / c.height;
      return (texCache[key] = t);
    }

    function roundRect(x, l, t, w, h, r) {
      x.beginPath();
      x.moveTo(l + r, t); x.lineTo(l + w - r, t); x.quadraticCurveTo(l + w, t, l + w, t + r);
      x.lineTo(l + w, t + h - r); x.quadraticCurveTo(l + w, t + h, l + w - r, t + h);
      x.lineTo(l + r, t + h); x.quadraticCurveTo(l, t + h, l, t + h - r);
      x.lineTo(l, t + r); x.quadraticCurveTo(l, t, l + r, t);
      x.closePath();
    }

    function planeGeometry() {
      if (geomCache.plane) return geomCache.plane;
      var g = new C.BufferGeometry();
      g.setAttribute("position", new C.Attr(new Float32Array([-0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0]), 3));
      g.setAttribute("normal", new C.Attr(new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1]), 3));
      g.setAttribute("uv", new C.Attr(new Float32Array([0, 0, 1, 0, 1, 1, 0, 1]), 2));
      g.setIndex([0, 1, 2, 0, 2, 3]);
      return (geomCache.plane = g);
    }

    function sphereGeometry(r, detail) {
      var key = r + ":" + detail;
      return geomCache[key] || (geomCache[key] = new C.Sphere(r, detail, Math.max(4, detail - 4)));
    }

    function billboard(renderer, scene, camera) {
      this.quaternion.copy(camera.quaternion);
      this.updateMatrixWorld();
    }

    // A camera-facing quad.  Lambert with black diffuse + emissive map behaves like an unlit sprite.
    function quad(tex, color, size, additive, opacity) {
      var m = new C.Material({ color: 0x000000, emissive: color, emissiveMap: tex, map: tex, transparent: true,
        depthWrite: false, opacity: opacity });
      if (additive) m.blending = ADDITIVE;
      var mesh = new C.Mesh(planeGeometry(), m);
      mesh.scale.set(size * (tex.aspect || 1), size, 1);
      mesh.onBeforeRender = billboard;
      mesh.renderOrder = additive ? 1 : 2;
      return mesh;
    }

    function makeNode(n) {
      var g = new C.Object3D();
      var color = n.color;
      var core = new C.Mesh(sphereGeometry(n.radius, n.type === "memory" ? 8 : 16),
        new C.Material({ color: color, emissive: color, emissiveIntensity: n.type === "hub" ? 1.5 : 0.85, transparent: true,
          opacity: n.type === "hub" ? 0.98 : 0.95 }));
      g.add(core);
      var halo = quad(haloTexture(), color, haloBaseSize(n), true, haloOpacity(n));
      g.add(halo);
      var label = null;
      if (n.type === "project" || n.type === "agent" || n.type === "hub") {
        var text = n.type === "agent" ? agentShort(n.agent) + " · " + (n.name || "") : n.name;
        if (text.length > 26) text = text.slice(0, 25) + "…";
        label = quad(labelTexture(text, n.type === "agent" ? color : "#ede9fe"), 0xffffff, n.type === "agent" ? 5 : n.type === "hub" ? 7.5 : 6.5, false, 0.95);
        label.position.y = n.radius + (n.type === "agent" ? 6 : n.type === "hub" ? 10 : 8);
        g.add(label);
      }
      n.parts = { core: core, halo: halo, label: label, group: g };
      if (n.bloomAt) g.scale.setScalar(0.001);
      return g;
    }

    function tooltip(n) {
      if (n.type === "memory") return '<b style="color:' + n.color + '">' + esc(n.kind) + "</b> " + esc(n.title) + "<br><small>" + esc(n.project || "global") + "</small>";
      if (n.type === "project") return "<b>" + esc(n.name) + "</b><br><small>" + (n.memories || 0) + " memories · " + (n.activity || 0) + " events 7d</small>";
      if (n.type === "agent") return '<b style="color:' + n.color + '">' + esc(n.agent) + "</b> · " + esc(n.name || "") + "<br><small>" +
        (n.active ? "active" : "idle") + (n.connected ? " · mcp connected" : "") + "</small>";
      return "<b>Brain</b><br><small>where your knowledge meets the models</small>";
    }

    // ---------------------------------------------------------------- graph merge (keeps positions and three objects)
    function setGraph(g) {
      if (!ready) { pendingGraph = g; return; }
      var now = performance.now();
      var next = [], byId = {};
      function keep(id, props) {
        var n = nodeById[id] || { id: id, phase: Math.random() * Math.PI * 2 };
        Object.assign(n, props);
        next.push(n); byId[id] = n;
        return n;
      }
      keep("__brain__", { type: "hub", name: "Brain", radius: 12, color: "#e9d5ff", baseGlow: 1, val: 18, fx: 0, fy: 0, fz: 0 });
      var side = 1;
      (g.projects || []).forEach(function (p) {
        var n = keep("p:" + p.id, { type: "project", project: p.id, name: p.name, path: p.path, memories: p.memories, activity: p.activity,
          radius: Math.min(9, 4.2 + Math.sqrt(p.memories || 0) * 0.7), color: p.id === "other" ? "#64748b" : "#8b5cf6", baseGlow: 0.55, val: 10 });
        if (!n.side) { n.side = side; side = -side; }
        if (n.x === undefined) { n.x = n.side * R * 0.5 + (Math.random() - 0.5) * 40; n.y = (Math.random() - 0.5) * 60; n.z = (Math.random() - 0.5) * 60; }
      });
      (g.memories || []).forEach(function (m) {
        var fresh = !nodeById["m:" + m.id];
        var n = keep("m:" + m.id, { type: "memory", mid: m.id, title: m.title, kind: m.kind, cluster: m.cluster, project: m.project,
          radius: 1.5, color: KIND_COLORS[m.kind] || "#94a3b8", baseGlow: 0.42, val: 1 });
        if (fresh) {
          var parent = byId[m.cluster === "global" ? "__brain__" : "p:" + m.cluster];
          if (parent && parent.x !== undefined) {
            n.x = parent.x + (Math.random() - 0.5) * 8; n.y = parent.y + (Math.random() - 0.5) * 8; n.z = parent.z + (Math.random() - 0.5) * 8;
          }
          if (nodes.length) { // bloom in, except on the first load
            n.bloomAt = now; anims.push({ node: n, kind: "bloom", start: now, dur: 1100 }); }
          delete pendingBloom[m.id];
        }
      });
      (g.agents || []).forEach(function (a) {
        keep("a:" + a.id, { type: "agent", agent: a.agent, session: a.session_id, project: a.project, name: a.project_name,
          active: a.active, connected: a.connected, radius: 3.2, color: agentColor(a.agent), baseGlow: a.active ? 0.9 : 0.5, val: 4 });
      });
      var nextLinks = [], nextKeys = {};
      function link(src, dst, kind, color, spread, suffix) {
        var key = src + ">" + dst + (suffix || "");
        var l = linkByKey[key] || { source: src, target: dst };
        l.kind = kind; l.color = color; l.spread = spread || 0; l.particles = 0;
        nextLinks.push(l); nextKeys[key] = l;
      }
      var perCluster = {};
      next.forEach(function (n) {
        if (n.type === "memory") {
          var parent = n.cluster === "global" ? "__brain__" : "p:" + n.cluster;
          if (!byId[parent]) return;
          var k = perCluster[parent] = (perCluster[parent] || 0) + 1;
          link(parent, n.id, n.cluster === "global" ? "hm" : "pm", n.cluster === "global" ? "#6d5fd6" : "#5b5bd6", Math.sqrt(k) * 2.2);
        } else if (n.type === "agent") {
          link(n.id, "__brain__", "ah", n.color);
        } else if (n.type === "project") {
          link("__brain__", n.id, "hp", "#6d5fd6");
        }
      });
      (g.links || []).forEach(function (l) {
        if (!byId["m:" + l.from_id] || !byId["m:" + l.to_id]) return;
        link("m:" + l.from_id, "m:" + l.to_id, "ml", LINK_COLORS[l.type] || "#94a3b8", 0, ":" + l.type);
        var edge = nextLinks[nextLinks.length - 1];
        edge.particles = /^(blocks|depends_on|supersedes)$/.test(l.type) ? 1 : 0;
        edge.pwidth = 0.8; edge.pcolor = edge.color;
      });
      nodes = next; nodeById = byId; links = nextLinks; linkByKey = nextKeys;
      syncHalos();
      Graph.graphData({ nodes: nodes, links: links });
      startLoop();
    }

    // ---------------------------------------------------------------- pulses
    function findAgent(p) {
      var exact = nodeById["a:" + p.agent + ":" + p.session_id];
      if (exact) return exact;
      var fam = String(p.agent || "").indexOf("codex") >= 0 ? "codex" : "claude", best = null;
      nodes.forEach(function (n) {
        if (n.type !== "agent") return;
        var nf = n.agent.indexOf("codex") >= 0 ? "codex" : "claude";
        if (nf === fam && (!best || n.project === p.project)) best = n;
      });
      return best;
    }

    function flash(node, dur) {
      if (!node) return;
      anims.push({ node: node, kind: "flash", start: performance.now(), dur: dur || 1300 });
      startLoop();
    }

    function emit(l, color, width) {
      if (!l || REDUCED) return;
      l.pcolor = color; l.pwidth = width;
      try { Graph.emitParticle(l); } catch (e) { /* link not yet laid out */ }
    }

    function pulse(p) {
      if (!ready) return;
      var hub = nodeById.__brain__, project = p.project ? nodeById["p:" + p.project] : null;
      var agent = findAgent(p), now = performance.now();
      if (p.type === "ado") {
        flash(hub, 1600);
        return;
      }
      if (p.type === "memory") {
        if (agent) flash(agent, 900);
        var memoryLink = agent && hub && linkByKey[agent.id + ">" + hub.id];
        emit(memoryLink, agent ? agent.color : agentColor(p.agent), 2.6);
        setTimeout(function () {
          flash(hub, 1300);
          pendingBloom[p.id] = 1;
          var existing = nodeById["m:" + p.id];
          if (existing) {
            existing.bloomAt = performance.now();
            anims.push({ node: existing, kind: "bloom", start: existing.bloomAt, dur: 1100 });
          }
          flash(project, 1600);
        }, memoryLink && !REDUCED ? 950 : 0);
        return;
      }
      var key = (agent ? agent.id : "x") + p.type;
      if (lastImpulse[key] && now - lastImpulse[key] < 350) return;
      lastImpulse[key] = now;
      var color = agent ? agent.color : agentColor(p.agent);
      var small = p.type === "usage";
      if (agent) flash(agent, 900);
      if (agent) emit(linkByKey[agent.id + ">" + hub.id], color, small ? 1.6 : 2.6);
      if (!project) return;
      var delay = agent && hub && !REDUCED ? 950 : 0;
      setTimeout(function () {
        flash(hub, 1300);
        emit(hub && linkByKey[hub.id + ">" + project.id], color, small ? 1.6 : 2.6);
        var projectDelay = hub && !REDUCED ? 950 : 0;
        setTimeout(function () {
        flash(project, 1300);
        var mem = links.filter(function (l) { return l.kind === "pm" && (l.source === project || l.source === project.id); });
        for (var i = 0; i < Math.min(2, mem.length); i++) emit(mem[Math.floor(Math.random() * mem.length)], color, small ? 1.1 : 1.6);
        }, projectDelay);
      }, delay);
    }

    // ---------------------------------------------------------------- light animation loop (flash, bloom, twinkle)
    function startLoop() {
      if (loopOn || !active || document.hidden || !C) return;
      loopOn = true;
      requestAnimationFrame(loop);
    }

    function loop(now) {
      if (!active || document.hidden) { loopOn = false; return; }
      for (var i = anims.length - 1; i >= 0; i--) {
        var a = anims[i], t = Math.min(1, (now - a.start) / a.dur), n = a.node, parts = n.parts;
        if (!parts) { if (now - a.start > 4000) anims.splice(i, 1); continue; }
        if (a.kind === "bloom") {
          var s = t < 1 ? 1 + 2.2 * Math.pow(t - 1, 3) + 1.2 * Math.pow(t - 1, 2) : 1;   // ease-out-back
          parts.group.scale.setScalar(Math.max(0.001, s));
          parts.halo.material.opacity = haloOpacity(n) + (1 - t) * 1.2;
          parts.halo.scale.setScalar(haloBaseSize(n) * (1 + (1 - t) * 2.5));
        } else {
          var f = 1 - t;
          parts.core.material.emissiveIntensity = (n.type === "hub" ? 1.5 : 0.85) + f * 1.6;
          parts.halo.material.opacity = haloOpacity(n) + f * 0.9;
          var base = haloBaseSize(n);
          parts.halo.scale.setScalar(base * (1 + f * 1.4));
        }
        if (t >= 1) {
          if (a.kind === "bloom") { parts.group.scale.setScalar(1); n.bloomAt = 0; }
          anims.splice(i, 1);
        }
      }
      // subtle twinkle and active-agent breathing at ~15 fps (halo opacity only)
      var twinkle = !REDUCED && now - lastTwinkle > 66;
      if (twinkle) {
        lastTwinkle = now;
        var tt = now / 1000;
        for (var k = 0; k < nodes.length; k++) {
          var m = nodes[k];
          if (!m.parts || m.bloomAt || isAnimating(m)) continue;
          if (m.type === "memory") m.parts.halo.material.opacity = haloOpacity(m) * (0.65 + 0.35 * Math.sin(tt * (0.7 + (m.phase % 1)) + m.phase));
          else if (m.type === "agent" && m.active) m.parts.halo.material.opacity = haloOpacity(m) + (bloomActive() ? 0.15 : 0.35) * Math.sin(tt * 3 + m.phase);
        }
      }
      if (anims.length || !REDUCED) requestAnimationFrame(loop);
      else loopOn = false;
    }

    function isAnimating(n) {
      for (var i = 0; i < anims.length; i++) if (anims[i].node === n) return true;
      return false;
    }

    // ---------------------------------------------------------------- lifecycle
    function resize() {
      var r = container.getBoundingClientRect();
      if (r.width > 10 && r.height > 10) {
        Graph.width(r.width).height(r.height);
        resizeBloom(r.width, r.height);
      }
    }

    function setActive(on) {
      active = on;
      if (on && !document.hidden) { Graph.resumeAnimation(); resize(); startLoop(); }
      else Graph.pauseAnimation();
    }

    document.addEventListener("visibilitychange", function () {
      if (!active) return;
      if (document.hidden) Graph.pauseAnimation();
      else { Graph.resumeAnimation(); startLoop(); }
    });
    if (window.ResizeObserver) new ResizeObserver(resize).observe(container);
    resize();
    bootstrap();

    return { setGraph: setGraph, pulse: pulse, resize: resize, setActive: setActive, graph: Graph };
  }

  window.DevmemBrain3D = { available: function () { return !!window.ForceGraph3D && webglAvailable(); }, create: create };
})();
