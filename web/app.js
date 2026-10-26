/* Fourth Official screens: TV panel, stadium board, phone "Why?".
 *
 * index.html?screen=tv        the broadcast panel (16:9), the hero screen
 * index.html?screen=stadium   the stadium LED board
 * index.html?screen=phone     the supporter's phone
 * index.html?screen=wall      the three side by side, driven by one control bar
 *
 * Options: &incident=INC-01  &phase=checking|decided  &tour (plays every incident in turn)
 *          &clean (no control bar, for recording)  &live=<Web PubSub client URL> (live cards)
 *
 * Replay mode reads data/bundle.json (built by web/build_data.py). Every screen shows, for each
 * (incident, phase, audience), the card with the highest revision: 1 = template, 2 = agents,
 * 3 = narrator. Tabs of the same browser stay in sync through a BroadcastChannel.
 */
(() => {
  "use strict";

  const params = new URLSearchParams(location.search);
  const SCREEN = params.get("screen") || "tv";
  const CLEAN = params.has("clean");
  const EMBED = params.has("embed");
  const LIVE_URL = params.get("live");
  const TOUR = params.has("tour");
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SIZES = { tv: [1920, 1080], stadium: [1920, 400], phone: [430, 884] };
  const app = document.getElementById("app");
  const channel = "BroadcastChannel" in window ? new BroadcastChannel("fourth-official") : null;

  let B = null;                     // the bundle
  const store = new Map();          // "INC-01|decided|tv" -> [cards by revision]
  const state = { incident: null, phase: "checking", dt: 0, playing: false, maxRev: 1, phoneTab: "club" };
  let playStartedAt = 0, playFrom = 0, revealTimer = null, tourTimer = null;

  // ------------------------------------------------------------------ helpers
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
    return n;
  };
  const svg = (tag, attrs = {}) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const incident = () => B.incidents.find((i) => i.id === state.incident);
  const surname = (pid) => (B.players[pid]?.name || pid).split(" ").slice(-1)[0];
  const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);

  function addCard(c) {
    const k = `${c.incident_id}|${c.phase}|${c.audience}`;
    const list = store.get(k) || [];
    list.push(c);
    list.sort((a, b) => (a.revision || 1) - (b.revision || 1));
    store.set(k, list);
  }
  // The card a screen shows: highest revision not above what has been "revealed" so far.
  function card(phase, audience, incId = state.incident) {
    const list = store.get(`${incId}|${phase}|${audience}`) || [];
    const shown = list.filter((c) => (c.revision || 1) <= state.maxRev);
    return shown[shown.length - 1] || null;
  }
  const claim = (c, id) => c?.claims.find((x) => x.id === id);
  const cleanQuote = (q) => q.replace(/^[\s•]+/, "").replace(/\s+/g, " ");

  // ------------------------------------------------------------------ positions
  function pos(inc, entity, dt) {
    const w = inc.window, track = w.positions[entity];
    if (!track) return null;
    const f = Math.max(0, Math.min(track.length - 1, (dt - w.dt[0]) * w.hz));
    const i = Math.floor(f), j = Math.min(track.length - 1, i + 1), a = f - i;
    return [track[i][0] + (track[j][0] - track[i][0]) * a, track[i][1] + (track[j][1] - track[i][1]) * a];
  }
  const observerId = (inc) => ("AR1" in inc.observers ? "AR1" : "REF");
  function observerPos(inc, dt) {
    const id = observerId(inc);
    return id === "REF" ? pos(inc, "REF", dt) : inc.observers[id].position_at_key_moment;
  }
  // What the official could see at this instant, from the Referee's Eye frames.
  function visibleAt(inc, dt) {
    const o = inc.observers[observerId(inc)];
    const frames = o.frames || [];
    if (!frames.length) return { share: o.visible_share_at_key_moment, covering: {} };
    let best = frames[0];
    for (const f of frames) if (Math.abs(f.dt - dt) < Math.abs(best.dt - dt)) best = f;
    if (Math.abs(dt) < 0.06 || (dt > 0 && frames[0].dt > 0.5)) return { share: o.visible_share_at_key_moment, covering: best.covering || {} };
    if (dt < frames[0].dt - 0.4) return { share: o.visible_share_at_key_moment, covering: {}, early: true };
    return { share: best.visible, covering: best.covering || {} };
  }

  // ------------------------------------------------------------------ sync between tabs
  function broadcast() {
    if (channel) channel.postMessage({ type: "state", state: { ...state }, playStartedAt, playFrom });
  }
  if (channel) {
    channel.onmessage = (e) => {
      if (e.data?.type === "hello") { if (!EMBED && B) broadcast(); return; }
      if (e.data?.type !== "state" || !B) return;
      const changed = e.data.state.incident !== state.incident || e.data.state.phase !== state.phase;
      const wasPlaying = state.playing;
      Object.assign(state, e.data.state);
      playStartedAt = e.data.playStartedAt; playFrom = e.data.playFrom;
      if (changed) mount();
      else update(true);
      if (state.playing && !wasPlaying) requestAnimationFrame(tick);
    };
  }

  // ------------------------------------------------------------------ playback
  function clipRange(inc) { const d = inc.window.dt; return [d[0], d[d.length - 1]]; }
  // Real time, except slow motion (x0.25) through the critical window around the key moment.
  function advance(dt, seconds, inc) {
    const [a, b] = inc.critical_window;
    const lo = a - 0.3, hi = b + 0.3;
    let t = dt, left = seconds;
    while (left > 0) {
      const slow = t >= lo && t < hi;
      const rate = slow ? 0.25 : 1;
      const edge = slow ? hi : (t < lo ? lo : Infinity);
      const step = Math.min(left * rate, edge - t);
      t += step; left -= step / rate;
      if (step <= 0) break;
    }
    return t;
  }
  function play(from) {
    const inc = incident();
    state.playing = true;
    playFrom = from ?? clipRange(inc)[0];
    playStartedAt = Date.now();  // wall-clock time: the same in every tab
    state.dt = playFrom;
    broadcast();
    requestAnimationFrame(tick);
  }
  function tick() {
    if (!state.playing) return;
    const inc = incident();
    const end = clipRange(inc)[1];
    state.dt = advance(playFrom, (Date.now() - playStartedAt) / 1000, inc);
    if (state.dt >= end) {          // freeze on the key moment, the way a VAR replay does
      state.dt = 0; state.playing = false;
      update(); broadcast();
      return;
    }
    update();
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ choosing what to show
  function select(incId, phase, { autoplay = true, sync = true } = {}) {
    state.incident = incId;
    state.phase = phase;
    state.dt = 0;
    state.playing = false;
    // Replay the arrival of the cards: template first, then the agents' revision.
    clearTimeout(revealTimer);
    state.maxRev = LIVE_URL ? 99 : 1;
    if (!LIVE_URL) revealTimer = setTimeout(() => { state.maxRev = 99; update(true); broadcast(); }, 2200);
    mount();
    if (sync) broadcast();
    if (autoplay && phase === "checking") play();
  }

  function startTour() {
    const order = B.incidents.map((i) => i.id);
    let k = 0;
    const step = () => {
      const id = order[k % order.length];
      select(id, "checking");
      const inc = incident();
      const [a, b] = clipRange(inc);
      const clip = (b - a) + (inc.critical_window[1] - inc.critical_window[0] + 0.6) * 3;
      tourTimer = setTimeout(() => {
        select(id, "decided", { autoplay: false });
        tourTimer = setTimeout(() => { k += 1; step(); }, 11000);
      }, (clip + 3) * 1000);
    };
    step();
  }

  // ------------------------------------------------------------------ the TV panel
  const tv = {};

  // Zoom on the incident, keeping the left column free for the score bug and the visibility figure.
  const REGION = { x: 0.33, w: 0.6, y: 0.1, h: 0.74 };
  function viewBox(inc) {
    const pts = [[inc.focal.x, inc.focal.y], observerPos(inc, 0), inc.best_view.best.position, inc.best_view.actual.position];
    for (const pid of [...Object.values(inc.involved), ...(inc.observers[observerId(inc)].blockers || [])]) {
      const p = pos(inc, pid, 0); if (p) pts.push(p);
    }
    const pad = 3, xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const minx = Math.min(...xs) - pad, maxx = Math.max(...xs) + pad, miny = Math.min(...ys) - pad, maxy = Math.max(...ys) + pad;
    const wc = Math.max(24, maxx - minx), hc = Math.max(14, maxy - miny);
    const aspect = 1340 / 1080;
    const H = Math.max(hc / REGION.h, wc / REGION.w / aspect), W = H * aspect;
    const x0 = (minx + maxx) / 2 - (REGION.x + REGION.w / 2) * W;
    const y0 = (miny + maxy) / 2 - (REGION.y + REGION.h / 2) * H;
    return [x0, y0, W, H];
  }

  function pitchMarkings(g) {
    const L = 105, W = 68, line = { fill: "none", stroke: "var(--chalk)", "stroke-width": 0.14, "stroke-opacity": 0.9 };
    g.append(svg("rect", { x: -20, y: -20, width: L + 40, height: W + 40, fill: "var(--surround)" }));
    for (let i = 0; i < 20; i++) {
      g.append(svg("rect", { x: i * 5.25, y: 0, width: 5.25, height: W, fill: i % 2 ? "var(--grass)" : "var(--grass-2)" }));
    }
    g.append(svg("rect", { x: 0, y: 0, width: L, height: W, ...line }));
    g.append(svg("line", { x1: L / 2, y1: 0, x2: L / 2, y2: W, ...line }));
    g.append(svg("circle", { cx: L / 2, cy: W / 2, r: 9.15, ...line }));
    for (const side of [0, 1]) {
      const x0 = side ? L : 0, s = side ? -1 : 1;
      g.append(svg("rect", { x: side ? L - 16.5 : 0, y: 13.84, width: 16.5, height: 40.32, ...line }));
      g.append(svg("rect", { x: side ? L - 5.5 : 0, y: 24.84, width: 5.5, height: 18.32, ...line }));
      g.append(svg("circle", { cx: x0 + s * 11, cy: W / 2, r: 0.22, fill: "var(--chalk)" }));
      const dx = 16.5 - 11, dy = Math.sqrt(9.15 ** 2 - dx ** 2);
      g.append(svg("path", { d: `M ${x0 + s * 16.5} ${W / 2 - dy} A 9.15 9.15 0 0 ${side ? 0 : 1} ${x0 + s * 16.5} ${W / 2 + dy}`, ...line }));
      g.append(svg("rect", { x: side ? L : -2, y: 30.34, width: 2, height: 7.32, ...line, "stroke-width": 0.2 }));
    }
  }

  function mountTV() {
    const inc = incident();
    const root = el("div", { class: `tv ${state.phase}` });
    const pitch = el("div", { class: "tv-pitch" });
    const s = svg("svg", { viewBox: viewBox(inc).join(" "), "aria-label": `Pitch view of ${inc.headline}`, role: "img" });
    const defs = svg("defs");
    const grad = svg("linearGradient", { id: "cone", gradientUnits: "userSpaceOnUse" });
    grad.append(svg("stop", { offset: "0", "stop-color": "#e5f23c", "stop-opacity": "0.05" }));
    grad.append(svg("stop", { offset: "1", "stop-color": "#e5f23c", "stop-opacity": "0.42" }));
    defs.append(grad);
    s.append(defs);
    const field = svg("g"); pitchMarkings(field); s.append(field);

    tv.offside = svg("g"); s.append(tv.offside);
    tv.cone = svg("polygon", { fill: "url(#cone)" });
    tv.sightClear = svg("line", { stroke: "#e5f23c", "stroke-width": 0.12, "stroke-dasharray": "0.5 0.35" });
    tv.sightBlocked = svg("line", { stroke: "#e5352b", "stroke-width": 0.18 });
    s.append(tv.cone, tv.sightClear, tv.sightBlocked);

    // Target: what the official needed to see.
    const F = [inc.focal.x, inc.focal.y];
    const target = svg("g", { transform: `translate(${F[0]} ${F[1]})` });
    target.append(svg("circle", { r: 0.7, fill: "none", stroke: "#fff", "stroke-width": 0.1 }));
    for (const [x1, y1, x2, y2] of [[-1, 0, -0.4, 0], [0.4, 0, 1, 0], [0, -1, 0, -0.4], [0, 0.4, 0, 1]]) {
      target.append(svg("line", { x1, y1, x2, y2, stroke: "#fff", "stroke-width": 0.1 }));
    }
    s.append(target);

    // Ghost referee at the best reachable view (coaching, shown with the decision).
    tv.ghost = svg("g", { opacity: 0 });
    const bvp = inc.best_view.best.position, act = inc.best_view.actual.position;
    tv.ghost.append(svg("line", { x1: act[0], y1: act[1], x2: bvp[0], y2: bvp[1], stroke: "#e5f23c", "stroke-width": 0.12, "stroke-dasharray": "0.4 0.3" }));
    tv.ghost.append(svg("circle", { cx: bvp[0], cy: bvp[1], r: 0.65, fill: "none", stroke: "#e5f23c", "stroke-width": 0.16, "stroke-dasharray": "0.45 0.3" }));
    const mv = inc.best_view.move_m, parts = [];
    if (Math.abs(mv.left) >= 1) parts.push(`${Math.round(Math.abs(mv.left))} m to his ${mv.left > 0 ? "left" : "right"}`);
    if (Math.abs(mv.closer) >= 1) parts.push(`${Math.round(Math.abs(mv.closer))} m ${mv.closer > 0 ? "closer" : "back"}`);
    const gl = svg("text", { x: bvp[0], y: bvp[1] + 1.9, "text-anchor": "middle", fill: "#e5f23c", "font-size": 0.9, class: "lbl" });
    const t1 = svg("tspan", { x: bvp[0], class: "strong" }); t1.textContent = `Best view: ${pct(inc.best_view.best.visible_share)} visible`;
    const t2 = svg("tspan", { x: bvp[0], dy: 1.1 }); t2.textContent = parts.join(", ") || "where he stood";
    gl.append(t1, t2);
    tv.ghost.append(gl);
    s.append(tv.ghost);

    // Players, officials, ball.
    tv.dots = {};
    const named = new Set([...Object.values(inc.involved), ...(inc.observers[observerId(inc)].blockers || [])]);
    for (const ent of Object.keys(inc.window.positions)) {
      const g = svg("g");
      if (ent === "BALL") {
        g.append(svg("circle", { r: 0.32, fill: "#fff", stroke: "#13212b", "stroke-width": 0.08 }));
      } else if (ent === "REF") {
        g.append(svg("circle", { r: 0.62, fill: "#e5f23c", stroke: "#13212b", "stroke-width": 0.14 }));
        const t = svg("text", { y: -1.05, "text-anchor": "middle", fill: "#e5f23c", "font-size": 0.85, class: "lbl strong" });
        t.textContent = "Referee"; g.append(t);
      } else {
        const p = B.players[ent] || {}, team = B.match.teams[p.team] || { colour: "#888" };
        const ring = svg("circle", { r: 1.0, fill: "none", stroke: "#e5352b", "stroke-width": 0.2, "stroke-dasharray": "0.55 0.25", opacity: 0, class: "blocker-ring" });
        g.append(ring);
        g.append(svg("circle", { r: 0.58, fill: team.colour, stroke: "#fff", "stroke-width": 0.11 }));
        const n = svg("text", { y: 0.24, "text-anchor": "middle", fill: "#fff", "font-size": 0.66, class: "num" });
        n.textContent = p.number ?? ""; g.append(n);
        if (named.has(ent)) {
          const t = svg("text", { y: -1.25, "text-anchor": "middle", fill: "var(--chalk)", "font-size": 0.78, class: "lbl" });
          t.textContent = surname(ent); g.append(t); g.label = t;
        }
        g.ring = ring;
      }
      s.append(g);
      tv.dots[ent] = g;
    }
    if (observerId(inc) === "AR1") {
      const p = inc.observers.AR1.position_at_key_moment, g = svg("g", { transform: `translate(${p[0]} ${p[1]})` });
      g.append(svg("circle", { r: 0.62, fill: "#e5f23c", stroke: "#13212b", "stroke-width": 0.14 }));
      const t = svg("text", { y: 1.9, "text-anchor": "middle", fill: "#e5f23c", "font-size": 0.85, class: "lbl strong" });
      t.textContent = "Assistant referee"; g.append(t); s.append(g);
    }
    pitch.append(s, el("div", { class: "shade" }));
    tv.svg = s;

    // Score bug.
    const [home, away] = Object.keys(B.match.teams);
    const team = (code) => el("span", { class: "team" }, el("i", { class: "swatch", style: `background:${B.match.teams[code].colour}` }), B.match.teams[code].short);
    pitch.append(el("div", { class: "bug" }, team(home),
      el("span", { class: "score", text: `${inc.score_before[home]}–${inc.score_before[away]}` }), team(away),
      el("span", { class: "clock", text: inc.clock })));

    // How much the official could see.
    const o = inc.observers[observerId(inc)];
    tv.pct = el("span", { class: "pct" });
    const who = observerId(inc) === "AR1" ? "assistant referee" : "referee";
    pitch.append(el("div", { class: "seen" }, tv.pct,
      el("div", { class: "what" }, `of ${inc.focal.what} visible to the ${who}, `, el("b", { text: `${Math.round(o.distance_to_focal_m)} m away` }))));

    // Scrubber.
    const [a, b] = clipRange(inc);
    tv.range = el("input", { type: "range", min: a, max: b, step: 0.05, value: state.dt, "aria-label": "Replay position" });
    tv.range.addEventListener("input", () => { state.playing = false; state.dt = +tv.range.value; update(); broadcast(); });
    const span = b - a, at = (x) => `${((x - a) / span) * 100}%`;
    const [c0, c1] = inc.critical_window;
    tv.fill = el("div", { class: "fill" });
    tv.head = el("div", { class: "head" });
    tv.playBtn = el("button", { "aria-label": "Play the replay", onclick: () => (state.playing ? (state.playing = false, broadcast(), update()) : play()) });
    tv.time = el("span", { class: "time" });
    pitch.append(el("div", { class: "scrub" }, tv.playBtn,
      el("div", { class: "track" }, el("div", { class: "bar" }),
        el("div", { class: "window", style: `left:${at(c0)};width:calc(${at(c1)} - ${at(c0)})` }),
        tv.fill, el("div", { class: "key", style: `left:${at(0)}` }), tv.head, tv.range),
      tv.time));

    root.append(pitch);
    tv.panel = el("section", { class: "panel", "aria-label": "Decision card" });
    root.append(tv.panel);
    tv.lastCardKey = null;
    return root;
  }

  // Names above players, nudged so they never sit on top of each other.
  function placeLabels(inc, dt) {
    const placed = [];
    const items = Object.entries(tv.dots).filter(([, g]) => g.label).map(([ent, g]) => ({ g, p: pos(inc, ent, dt) }))
      .filter((i) => i.p).sort((a, b) => a.p[1] - b.p[1]);
    for (const { g, p } of items) {
      const w = g.label.textContent.length * 0.42 + 0.6;
      const options = [[0, -1.25], [0, 2.0], [w / 2 + 0.9, 0.3], [-(w / 2 + 0.9), 0.3], [0, -2.3], [0, 3.1]];
      let chosen = options[0];
      for (const o of options) {
        const x = p[0] + o[0], y = p[1] + o[1];
        if (!placed.some((q) => Math.abs(q.x - x) < (q.w + w) / 2 && Math.abs(q.y - y) < 0.95)) { chosen = o; break; }
      }
      placed.push({ x: p[0] + chosen[0], y: p[1] + chosen[1], w });
      g.label.setAttribute("x", chosen[0]); g.label.setAttribute("y", chosen[1]);
    }
  }

  const EVIDENCE_LABEL = { measure: "Measured by the Referee's Eye", law: "IFAB Laws of the Game 2025/26", event: "Match feed", player: "Line of sight" };

  function renderPanel(force) {
    const inc = incident();
    const c = card(state.phase, "tv");
    const k = c ? `${c.incident_id}|${c.phase}|${c.revision}` : "none";
    if (!force && k === tv.lastCardKey) return;
    const fresh = tv.lastCardKey && tv.lastCardKey !== "none" && k !== tv.lastCardKey;
    tv.lastCardKey = k;
    const p = tv.panel;
    p.replaceChildren();
    p.className = `panel ${state.phase}`;
    const decided = state.phase === "decided";
    p.append(el("div", { class: "status" }, el("i", { class: "dot" }), decided ? "Decision" : "VAR check in progress"));
    p.append(el("p", { class: "headline", text: inc.headline }));
    if (!c) { p.append(el("p", { class: "decision", text: decided ? "Waiting for the officials." : "Checking." })); return; }
    const top = claim(c, decided ? "verdict" : "check");
    p.append(el("p", { class: `decision${fresh ? " fresh" : ""}`, text: top ? top.text : "" }));
    if (c.framing === "referees_call") p.append(el("span", { class: "badge", text: "Referee's call" }));
    const list = el("ul", { class: "claims" });
    for (const cl of c.claims) {
      if (cl === top) continue;
      const types = [...new Set(cl.evidence.map((e) => e.type))];
      const label = types.includes("measure") ? EVIDENCE_LABEL.measure : EVIDENCE_LABEL[types[0]] || "";
      const li = el("li", { class: fresh ? "fresh" : "" }, cl.text);
      const q = cl.evidence.find((e) => e.type === "law" && e.quote);
      if (q) li.append(el("blockquote", { text: `“${cleanQuote(q.quote)}”` }));
      li.append(el("span", { class: "src", text: label }));
      list.append(li);
    }
    p.append(list);
    const rev = c.revision || 1;
    p.append(el("p", { class: "provenance", text: rev >= 2
      ? "Written by the agents on Microsoft Foundry. Every sentence was checked against its evidence by the verifier, then by the Contradicteur."
      : "First card from the measurements. The agents are improving it." }));
  }

  function updateTV(forcePanel) {
    const inc = incident(), dt = state.dt;
    for (const [ent, g] of Object.entries(tv.dots)) {
      const p = pos(inc, ent, dt);
      if (p) g.setAttribute("transform", `translate(${p[0].toFixed(2)} ${p[1].toFixed(2)})`);
    }
    const O = observerPos(inc, dt), F = [inc.focal.x, inc.focal.y];
    const dx = F[0] - O[0], dy = F[1] - O[1], L = Math.hypot(dx, dy) || 1;
    const nx = -dy / L, ny = dx / L, hw = Math.max(0.9, L * Math.tan((4 * Math.PI) / 180));
    tv.cone.setAttribute("points", `${O[0]},${O[1]} ${F[0] + nx * hw},${F[1] + ny * hw} ${F[0] - nx * hw},${F[1] - ny * hw}`);
    const grad = document.getElementById("cone");
    grad.setAttribute("x1", O[0]); grad.setAttribute("y1", O[1]); grad.setAttribute("x2", F[0]); grad.setAttribute("y2", F[1]);

    const { share, covering, early } = visibleAt(inc, dt);
    let main = null, mainCover = 0.15;
    for (const [pid, cov] of Object.entries(covering)) if (cov > mainCover && tv.dots[pid]) { main = pid; mainCover = cov; }
    const hidden = share != null && share < 0.95;   // nobody is "in the way" if the official saw it all
    for (const [ent, g] of Object.entries(tv.dots)) if (g.ring) g.ring.setAttribute("opacity", hidden && (covering[ent] || 0) >= 0.15 ? 1 : 0);
    let cut = F;
    if (main && share != null && share < 0.5) {
      const bp = pos(inc, main, dt);
      const t = Math.max(0, Math.min(1, ((bp[0] - O[0]) * dx + (bp[1] - O[1]) * dy) / (L * L)));
      cut = [O[0] + dx * t, O[1] + dy * t];
    }
    tv.sightClear.setAttribute("x1", O[0]); tv.sightClear.setAttribute("y1", O[1]);
    tv.sightClear.setAttribute("x2", cut[0]); tv.sightClear.setAttribute("y2", cut[1]);
    tv.sightBlocked.setAttribute("x1", cut[0]); tv.sightBlocked.setAttribute("y1", cut[1]);
    tv.sightBlocked.setAttribute("x2", F[0]); tv.sightBlocked.setAttribute("y2", F[1]);
    tv.sightBlocked.setAttribute("opacity", cut === F ? 0 : 1);

    placeLabels(inc, dt);
    tv.pct.textContent = pct(share);
    tv.pct.classList.toggle("blind", share != null && share < 0.5);
    tv.pct.style.opacity = early ? 0.3 : 1;   // before the measured frames: the key-moment figure, dimmed

    const decided = state.phase === "decided";
    tv.ghost.setAttribute("opacity", decided && !inc.best_view.already_good && (!state.playing || dt >= 0) ? 1 : 0);
    tv.offside.replaceChildren();
    if (decided && inc.offside) {
      const o = inc.offside;
      tv.offside.append(svg("line", { x1: o.line_x, y1: 0, x2: o.line_x, y2: 68, stroke: "#e5352b", "stroke-width": 0.12 }));
      tv.offside.append(svg("line", { x1: o.attacker_x, y1: 0, x2: o.attacker_x, y2: 68, stroke: "#e5f23c", "stroke-width": 0.12 }));
      const t = svg("text", { x: Math.min(o.line_x, o.attacker_x) - 0.6, y: inc.focal.y - 3, "text-anchor": "end", fill: "#e5f23c", "font-size": 1.1, class: "num" });
      t.textContent = `${Math.round(o.margin_m * 100)} cm`; tv.offside.append(t);
    }

    const [a, b] = clipRange(inc);
    tv.range.value = dt;
    const at = `${((dt - a) / (b - a)) * 100}%`;
    tv.fill.style.width = at; tv.head.style.left = at;
    tv.playBtn.textContent = state.playing ? "❚❚" : "▶";
    tv.playBtn.setAttribute("aria-label", state.playing ? "Pause the replay" : "Play the replay");
    const slow = dt >= inc.critical_window[0] - 0.3 && dt < inc.critical_window[1] + 0.3 && state.playing;
    tv.time.textContent = Math.abs(dt) < 0.03 ? "Key moment" : `${dt > 0 ? "+" : "−"}${Math.abs(dt).toFixed(1)} s${slow ? ", slow motion" : ""}`;
    renderPanel(forcePanel);
  }

  // ------------------------------------------------------------------ the stadium board
  function mountStadium() { return el("div", { class: `stadium ${state.phase}` }); }
  function updateStadium(root) {
    const inc = incident();
    const [home, away] = Object.keys(B.match.teams);
    let kicker, text;
    if (state.phase === "checking") {
      const c = card("checking", "tv");
      kicker = "VAR check";
      const t = claim(c, "check")?.text || "VAR is checking.";
      text = t.replace(/^VAR is checking /, "").replace(/\.$/, "");
      text = text.charAt(0).toUpperCase() + text.slice(1);
    } else {
      const c = card("decided", "stadium");
      kicker = c?.framing === "referees_call" ? "Decision, referee's call" : "Decision";
      if (!c) text = "Waiting for the officials.";
      else if ((c.revision || 1) >= 3) text = c.claims.map((x) => x.text).join(" ");   // the Narrator's short line
      else {   // before the Narrator's line: the decision and the Law, nothing longer on a big screen
        const law = c.claims.flatMap((x) => x.evidence).find((e) => e.type === "law");
        text = `${claim(c, "verdict")?.text || ""}${law ? ` ${law.law}.` : ""}`;
      }
    }
    const k = `${state.incident}|${state.phase}|${kicker}|${text}`;
    if (root.dataset.k === k) return;
    root.dataset.k = k;
    root.className = `stadium ${state.phase}`;
    root.replaceChildren(
      el("div", { class: "board-score" },
        el("div", { class: "row" }, el("span", { text: B.match.teams[home].short }), el("span", { text: inc.score_before[home] })),
        el("div", { class: "row" }, el("span", { text: B.match.teams[away].short }), el("span", { text: inc.score_before[away] })),
        el("div", { class: "clock", text: inc.clock })),
      el("div", { class: "line" }, el("div", { class: "kicker", text: kicker }), el("div", { class: "text", text })));
    // Long lines shrink until they fit the board, the way an LED board scales its text.
    const kick = root.querySelector(".kicker"), t = root.querySelector(".text");
    const room = root.clientHeight - 48 - 20 - kick.offsetHeight;   // padding, gap, kicker
    for (let size = 84; size > 36 && t.offsetHeight > room; size -= 6) t.style.fontSize = `${size - 6}px`;
  }

  // ------------------------------------------------------------------ the phone
  const LANGS = { es: "Español", fr: "Français", en: "English", pt: "Português", de: "Deutsch", it: "Italiano", nl: "Nederlands" };
  function mountPhone() {
    const bare = window.innerWidth < 500;
    const ph = el("div", { class: `phone${bare ? " bare" : ""}` });
    return bare ? ph : el("div", { class: "phone-frame" }, ph);
  }
  function updatePhone(root) {
    const ph = root.classList.contains("phone") ? root : root.querySelector(".phone");
    const inc = incident(), decided = state.phase === "decided";
    const tvc = card(state.phase, "tv"), fan = decided ? card("decided", "fan") : null, audio = decided ? card("decided", "audio") : null;
    const k = [state.incident, state.phase, state.phoneTab, tvc?.revision, fan?.revision, audio?.revision].join("|");
    if (ph.dataset.k === k) return;
    ph.dataset.k = k;
    const [home, away] = Object.keys(B.match.teams);
    const clubName = fan?.club;
    const clubCode = Object.keys(B.match.teams).find((c) => B.match.teams[c].name === clubName);
    const header = el("header", {},
      el("div", { class: "match", text: `${B.match.teams[home].name} ${inc.score_before[home]}–${inc.score_before[away]} ${B.match.teams[away].name}, ${inc.clock}` }),
      el("h1", { text: decided ? "Why this decision?" : "VAR is checking" }),
      clubCode ? el("div", { class: "club" }, el("i", { style: `background:${B.match.teams[clubCode].colour}` }), `For ${clubName} supporters`) : null);
    const tabs = el("div", { class: "tabs", role: "tablist" });
    for (const [id, label] of [["club", "My club"], ["analyst", "Analyst"], ["audio", "Audio"]]) {
      tabs.append(el("button", { role: "tab", "aria-selected": state.phoneTab === id ? "true" : "false",
        onclick: () => { state.phoneTab = id; updatePhone(root); broadcast(); } }, label));
    }
    const body = el("div", { class: "body", role: "tabpanel" });
    const verdict = claim(tvc, decided ? "verdict" : "check");
    if (state.phoneTab === "club") {
      if (fan) {
        const [first, ...rest] = fan.claims;
        body.append(el("p", { class: "verdict", text: first.text }));
        for (const c of rest) body.append(el("p", { text: c.text }));
        body.append(el("p", { class: "note" }, "Written for you by the Narrator, checked like every card.", el("span", { class: "lang", text: LANGS[fan.language] || fan.language })));
      } else {
        body.append(el("p", { class: "verdict", text: verdict?.text || "" }));
        for (const c of tvc?.claims || []) if (c !== verdict) body.append(el("p", { text: c.text }));
        body.append(el("p", { class: "note", text: decided ? "Your club's version is being written." : "You will get the decision as soon as the officials make it, not before." }));
      }
    } else if (state.phoneTab === "analyst") {
      const o = inc.observers[observerId(inc)];
      body.append(el("div", { class: "meter" }, el("b", { text: pct(o.visible_share_at_key_moment) }), `of ${inc.focal.what} visible at the key moment`));
      for (const c of tvc?.claims || []) {
        body.append(el("p", { text: c.text }));
        const q = c.evidence.find((e) => e.type === "law" && e.quote);
        if (q) body.append(el("p", { class: "note", text: `“${cleanQuote(q.quote)}” IFAB Laws of the Game 2025/26` }));
      }
    } else {
      const text = audio ? audio.claims.map((c) => c.text).join(" ") : (tvc?.claims || []).map((c) => c.text).join(" ");
      body.append(el("p", { text }));
      const file = audio?.audio_file;  // recorded with Azure AI Speech (web/build_audio.py)
      const btn = el("button", { class: "play", onclick: () => (file ? play(file, text, btn) : speak(text, btn)) }, "▶ Play");
      body.append(btn);
      if (file) body.append(el("p", { class: "note", text: "Voice: Azure AI Speech, neural voice." }));
      else if (!("speechSynthesis" in window)) body.append(el("p", { class: "note", text: "This browser cannot read text aloud." }));
    }
    ph.replaceChildren(header, tabs, body);
  }
  let player = null;
  function play(file, text, btn) {
    if (player && !player.paused) { player.pause(); player.currentTime = 0; btn.textContent = "▶ Play"; return; }
    player = new Audio(file);
    player.onended = () => { btn.textContent = "▶ Play"; };
    player.play().then(() => { btn.textContent = "■ Stop"; }).catch(() => speak(text, btn));  // no file: browser voice
  }
  function speak(text, btn) {
    if (!("speechSynthesis" in window)) return;
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.textContent = "▶ Play"; return; }
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-GB"; u.rate = 1;
    u.onend = () => { btn.textContent = "▶ Play"; };
    btn.textContent = "■ Stop";
    speechSynthesis.speak(u);
  }

  // ------------------------------------------------------------------ the wall: three screens, one control bar
  function mountWall() {
    const q = (s) => `?screen=${s}&embed&clean${LIVE_URL ? `&live=${encodeURIComponent(LIVE_URL)}` : ""}`;
    return el("div", { class: "wall" },
      el("iframe", { class: "w-tv", src: q("tv"), title: "TV panel" }),
      el("iframe", { class: "w-stadium", src: q("stadium"), title: "Stadium board" }),
      el("iframe", { class: "w-phone", src: q("phone"), title: "Phone" }));
  }

  // ------------------------------------------------------------------ control bar
  const TYPE_LABEL = { penalty_claim: "penalty shout", offside_goal: "offside goal", handball: "handball", dogso: "goal chance denied", penalty_claim_holding: "holding" };
  function controls() {
    const bar = el("nav", { class: "controls pinned", "aria-label": "Replay controls" });
    const incs = el("div", { class: "group" });
    for (const i of B.incidents) {
      incs.append(el("button", { "aria-pressed": i.id === state.incident ? "true" : "false", onclick: () => { stopTour(); select(i.id, state.phase); } },
        `${i.clock} ${TYPE_LABEL[i.type] || i.type}`));
    }
    const phases = el("div", { class: "group" },
      el("button", { "aria-pressed": state.phase === "checking" ? "true" : "false", onclick: () => { stopTour(); select(state.incident, "checking"); } }, "VAR check"),
      el("button", { "aria-pressed": state.phase === "decided" ? "true" : "false", onclick: () => { stopTour(); select(state.incident, "decided", { autoplay: false }); } }, "Decision"),
      el("button", { onclick: () => play() }, "Replay"),
      el("button", { onclick: () => { stopTour(); startTour(); } }, "Play all"));
    const links = el("div", { class: "group" },
      ...["tv", "stadium", "phone", "wall"].filter((s) => s !== SCREEN).map((s) =>
        el("a", { href: `?screen=${s}${LIVE_URL ? `&live=${encodeURIComponent(LIVE_URL)}` : ""}`, target: "_blank", rel: "noopener" },
          { tv: "TV panel", stadium: "Stadium", phone: "Phone", wall: "All screens" }[s])));
    const live = el("span", { class: `live${liveOn ? " on" : ""}` }, el("i"), LIVE_URL ? (liveOn ? "Live" : "Connecting") : `Replay of ${B.cards_source}`);
    bar.append(incs, phases, el("span", { class: "spacer" }), live, links);
    setTimeout(() => bar.classList.remove("pinned"), 5000);
    return bar;
  }
  function stopTour() { clearTimeout(tourTimer); tourTimer = null; }

  // ------------------------------------------------------------------ mounting
  let screenRoot = null, stage = null;
  function fit() {
    if (!stage) return;
    const [w, h] = SIZES[SCREEN];
    const bare = SCREEN === "phone" && window.innerWidth < 500;
    const s = bare ? 1 : Math.min(window.innerWidth / w, window.innerHeight / h);
    stage.style.width = `${w}px`; stage.style.height = `${h}px`;
    stage.style.transform = bare ? "none" : `translate(-50%, -50%) scale(${s})`;
  }
  function mount() {
    if (!B || !state.incident) return;
    app.replaceChildren();
    if (SCREEN === "wall") {
      if (!screenRoot) screenRoot = mountWall();
      app.append(screenRoot);
    } else {
      screenRoot = SCREEN === "stadium" ? mountStadium() : SCREEN === "phone" ? mountPhone() : mountTV();
      const bare = SCREEN === "phone" && window.innerWidth < 500;
      if (bare) { stage = null; app.append(screenRoot); }
      else {
        stage = el("div", { class: "stage" }, screenRoot);
        app.append(el("div", { class: "stage-wrap" }, stage));
        fit();
      }
    }
    if (!CLEAN && !EMBED) app.append(controls());
    update(true);
  }
  function update(force) {
    if (!screenRoot || SCREEN === "wall") return;
    if (SCREEN === "tv") updateTV(force);
    else if (SCREEN === "stadium") updateStadium(screenRoot);
    else updatePhone(screenRoot);
  }
  window.addEventListener("resize", () => (SCREEN === "phone" ? mount() : fit()));

  // ------------------------------------------------------------------ live cards (Azure Web PubSub)
  let liveOn = false;
  function connectLive() {
    let ws;
    try { ws = new WebSocket(LIVE_URL); } catch { return; }
    ws.onopen = () => { liveOn = true; mount(); };
    ws.onclose = () => { liveOn = false; setTimeout(connectLive, 3000); };
    ws.onmessage = (e) => {
      let c;
      try { c = JSON.parse(e.data); } catch { return; }
      if (!c || !c.incident_id || !c.claims) return;
      addCard(c);
      // Follow the match: jump to the incident and phase of the newest card.
      if (c.incident_id !== state.incident || c.phase !== state.phase) select(c.incident_id, c.phase, { sync: false });
      else update(true);
    };
  }

  // ------------------------------------------------------------------ start
  app.append(el("div", { class: "loading", text: "Loading the match…" }));
  fetch("data/bundle.json").then((r) => {
    if (!r.ok) throw new Error(`data/bundle.json: ${r.status}`);
    return r.json();
  }).then((bundle) => {
    B = bundle;
    if (!LIVE_URL) for (const c of B.cards) addCard(c);
    const first = params.get("incident") || B.incidents[0].id;
    const phase = params.get("phase") === "decided" ? "decided" : "checking";
    if (EMBED) { state.incident = first; state.phase = phase; state.maxRev = 99; mount(); if (channel) channel.postMessage({ type: "hello" }); }
    else if (TOUR) { startTour(); }
    else select(first, phase, { autoplay: phase === "checking" });
    if (LIVE_URL) connectLive();
  }).catch((err) => {
    app.replaceChildren(el("div", { class: "loading", text: `The match data could not be loaded (${err.message}). Run py web/build_data.py, then serve the web folder.` }));
  });
})();
