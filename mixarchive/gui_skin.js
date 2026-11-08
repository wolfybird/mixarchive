// Shared logic for the workflow-first test UIs (/radio, /panel). Pages supply the markup
// (ids below) and their own CSS; this file talks to the same /api endpoints as gui.html.
//
// Navigation model: mixarchive is a pipeline, so the main nav is its five steps
// (add -> update -> sync -> analyze -> usb) plus the library and the output log.
// Each step shows a live count, and "next" points at the first step with work waiting.
(() => {
  const token = new URLSearchParams(location.search).get("t") || "";
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = n => (n || 0).toLocaleString();
  const plural = (n, word) => `${num(n)} ${word}${n === 1 ? "" : "s"}`;
  const size = b => b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : Math.round((b || 0) / 1e6) + " MB";
  const set = (sel, html) => $$(sel).forEach(e => e.innerHTML = html);
  const KIND = { picks: "picks", label: "label", channel: "channel", playlist: "playlist", "local folder": "local folder" };
  const S = { cols: [], rbOpen: false, rb: null, job: null, view: null, col: null };

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? { headers: { "X-Token": token } }
      : { method: "POST", headers: { "X-Token": token, "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }
  let tt;
  function say(m) { const t = $("#toast"); if (!t) return; t.textContent = m; t.hidden = false; clearTimeout(tt); tt = setTimeout(() => t.hidden = true, 5000); }

  // stable artwork per name (there's no per-collection cover yet)
  function hue(s) { let h = 0; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 2654435761) >>> 0; return (h >>> 7) % 360; }
  const art = name => `background:linear-gradient(135deg,hsl(${hue(name)} 60% 50%),hsl(${(hue(name) + 50) % 360} 70% 22%))`;
  const tracksOf = c => (c.playlists || []).reduce((a, p) => a + p.tracks, 0);
  const bytesOf = c => (c.playlists || []).reduce((a, p) => a + p.bytes, 0);
  const singlesOf = c => Object.values(c.singles || {}).reduce((a, b) => a + b, 0);

  // ------------------------------------------------------------------ derived workflow state
  function rbPlaylists() { return (S.rb && S.rb.status && S.rb.status.mixarchive) || []; }
  function waiting() { const p = rbPlaylists().find(p => p.name === "Not analyzed yet"); return p ? p.not_analyzed : 0; }
  function syncRows() {   // tracks on disk vs tracks rekordbox has, per mixarchive playlist
    const inRb = Object.fromEntries(rbPlaylists().map(p => [p.name, p.tracks]));
    return S.cols.flatMap(c => (c.playlists || []).map(p => ({ col: c.name, name: p.name, disk: p.tracks, rb: inRb[p.name] ?? 0, bytes: p.bytes })));
  }
  const toSync = () => syncRows().reduce((a, r) => a + Math.max(0, r.disk - r.rb), 0);
  const toFetch = () => S.cols.reduce((a, c) => a + Math.max(0, (c.mixes || 0) - (c.mixes_fetched || 0)), 0);
  const unsplit = () => S.cols.filter(c => c.mixes_fetched && !tracksOf(c)).length;

  function steps() {
    const rbKnown = !!(S.rb && S.rb.status);
    return {
      add: { n: 1, title: "Add", sub: "channels, playlists, videos", count: `${S.cols.length} collections`, todo: S.cols.length === 0 },
      update: { n: 2, title: "Update", sub: "download, split, tag", count: toFetch() ? `${num(toFetch())} mixes to fetch` : unsplit() ? `${unsplit()} to split` : "up to date", todo: toFetch() > 0 || unsplit() > 0 },
      sync: { n: 3, title: "Sync", sub: "into rekordbox", count: !rbKnown ? "checking…" : toSync() ? `${num(toSync())} new tracks` : "in sync", todo: rbKnown && toSync() > 0, blocked: S.rbOpen && "close rekordbox first" },
      analyze: { n: 4, title: "Analyze", sub: "in rekordbox", count: !rbKnown ? "checking…" : waiting() ? `${num(waiting())} waiting` : "all analyzed", todo: waiting() > 0 },
      usb: { n: 5, title: "USB", sub: "export to the stick", count: plural(rbPlaylists().filter(p => p.name !== "Not analyzed yet").length, "playlist"), todo: false },
    };
  }
  function next() {
    const st = steps();
    return Object.keys(st).find(k => st[k].todo) || "usb";
  }

  // ------------------------------------------------------------------ navigation
  function go(view, col) {
    if (!$("#v-" + view)) view = "home";
    S.view = view; S.col = col || null;
    $$(".view").forEach(v => v.classList.toggle("on", v.id === "v-" + view));
    $$("[data-go]").forEach(b => b.dataset.go === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
    history.replaceState(null, "", location.search + "#" + view + (col ? "/" + encodeURIComponent(col) : ""));
    render();
    window.scrollTo(0, 0);
  }
  document.addEventListener("click", e => {
    const g = e.target.closest("[data-go]");
    if (g) { e.preventDefault(); return go(g.dataset.go); }
    const r = e.target.closest("[data-run]");
    if (r) { e.stopPropagation(); if (!r.disabled) run(JSON.parse(r.dataset.run)); return; }
    const c = e.target.closest("[data-col]");
    if (c) return go("col", c.dataset.col);
    const o = e.target.closest("[data-open]");
    if (o) api("/api/open", o.dataset.open === "rekordbox" ? { what: "rekordbox" } : { what: "folder", name: o.dataset.open }).catch(err => say(err.message));
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.matches("[data-col]")) go("col", e.target.dataset.col);
  });
  const runAttr = args => esc(JSON.stringify(args));
  const updArgs = name => ["update", "--no-rbsync", ...(name ? [name] : [])];

  // ------------------------------------------------------------------ render
  function render() {
    const st = steps(), nx = next(), running = !!(S.job && S.job.running);
    // step nav: counts + which one is next
    for (const [k, s] of Object.entries(st)) {
      set(`[data-count="${k}"]`, esc(s.blocked && s.todo ? s.blocked : s.count));
      $$(`[data-step="${k}"]`).forEach(e => { e.classList.toggle("is-next", k === nx && !running); e.classList.toggle("is-todo", !!s.todo); });
    }
    // next-step prompt
    const s = st[nx];
    set("[data-next-n]", s.n);
    set("[data-next-title]", running ? "Working…" : `Next: ${esc(s.title)}`);
    set("[data-next-sub]", running ? "wait for the current command to finish" : esc(s.blocked && s.todo ? `${s.count} · ${s.blocked}` : s.count));
    $$("[data-next-go]").forEach(b => { b.dataset.go = nx; b.disabled = running; });

    set("[data-rb]", `<span class="dot ${S.rbOpen ? "warn" : "good"}"></span>rekordbox ${S.rbOpen ? "open" : "closed"}`);
    renderCollections(); renderUpdate(); renderSync(); renderAnalyze(); renderUsb(); renderCol();
    gate();
  }
  function card(c) {
    const tags = [KIND[c.kind] || c.kind, c.genre, c.label, c.singles && c.singles.check ? `${c.singles.check} to check` : ""].filter(Boolean);
    return `<article class="col-card" data-col="${esc(c.name)}" tabindex="0" aria-label="${esc(c.name)}">
      <div class="col-art" style="${art(c.name)}"><button class="col-play" data-run="${runAttr(updArgs(c.name))}" aria-label="Update ${esc(c.name)}" title="Update: new uploads → download → split">▶</button></div>
      <div class="col-meta"><span>${esc(KIND[c.kind] || c.kind)}</span><span>${num(c.mixes_fetched)}/${num(c.mixes)} mixes</span></div>
      <h3 class="col-title">${esc(c.name)}</h3>
      <div class="col-sub">${plural(tracksOf(c), "track")} · ${plural(singlesOf(c), "single")} · ${size(bytesOf(c))}</div>
      <div class="tags">${tags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </article>`;
  }
  function renderCollections() {
    set("#colGrid", S.cols.length ? S.cols.map(card).join("") : `<p class="empty">No collections yet. <a href="#" data-go="add">Add a channel or a few videos</a> to start.</p>`);
    set("[data-lib-size]", `${S.cols.length} collections · ${num(S.cols.reduce((a, c) => a + tracksOf(c), 0))} tracks · ${size(S.cols.reduce((a, c) => a + bytesOf(c), 0))}`);
  }
  function renderCol() {
    if (S.view !== "col") return;
    const c = S.cols.find(x => x.name === S.col);
    if (!c) return set("#colDetail", `<p class="empty">That collection isn't in the library.</p>`);
    const s = c.singles || {};
    set("#colDetail", `
      <div class="col-hero"><div class="col-art big" style="${art(c.name)}"></div><div>
        <div class="eyebrow">${esc(KIND[c.kind] || c.kind)}${c.label ? " · " + esc(c.label) : ""}${c.genre ? " · " + esc(c.genre) : ""}</div>
        <h2>${esc(c.name)}</h2>
        <dl class="spec">
          <dt>mixes</dt><dd>${num(c.mixes_fetched)} / ${num(c.mixes)} downloaded${c.skipped ? ` · ${num(c.skipped)} skipped` : ""}</dd>
          <dt>singles</dt><dd>${num(s.ok)} ok${s.check ? ` · ${num(s.check)} to check` : ""}${s.skip ? ` · ${num(s.skip)} skipped` : ""}</dd>
          <dt>on disk</dt><dd>${num(tracksOf(c))} tracks · ${size(bytesOf(c))}</dd>
          <dt>folder</dt><dd class="path">${esc(c.root)}</dd>
        </dl>
        <div class="actions">
          <button class="btn primary" data-run="${runAttr(updArgs(c.name))}">Update</button>
          <button class="btn" data-run="${runAttr(["status", c.name])}">Status</button>
          <button class="btn" data-open="${esc(c.name)}">Show folder</button>
        </div>
      </div></div>
      <h3 class="sec">Playlists</h3>
      <table class="list"><thead><tr><th>Name</th><th class="r">On disk</th><th class="r">In rekordbox</th><th class="r">Size</th></tr></thead><tbody>
      ${syncRows().filter(r => r.col === c.name).map(r => `<tr><td>${esc(r.name)}</td><td class="r">${num(r.disk)}</td><td class="r">${num(r.rb)}</td><td class="r">${size(r.bytes)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">none yet</td></tr>`}
      </tbody></table>
      <h3 class="sec">Sources</h3>
      <ul class="links">${c.links.map(u => `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u.replace(/^https?:\/\/(www\.)?/, ""))}</a></li>`).join("")}</ul>`);
  }
  function renderUpdate() {
    set("#updList", S.cols.map(c => {
      const pct = c.mixes ? Math.round(100 * (c.mixes_fetched || 0) / c.mixes) : 100;
      return `<tr><td><a href="#" data-col="${esc(c.name)}">${esc(c.name)}</a><div class="muted small">${esc(KIND[c.kind] || c.kind)}</div></td>
        <td class="meter-cell"><div class="meter" role="img" aria-label="${pct}% of mixes downloaded"><i style="width:${pct}%"></i></div><div class="muted small">${num(c.mixes_fetched)} / ${num(c.mixes)} mixes</div></td>
        <td class="r">${num(tracksOf(c))}</td>
        <td class="r"><button class="btn small" data-run="${runAttr(updArgs(c.name))}">Update</button></td></tr>`;
    }).join("") || `<tr><td colspan="4" class="muted">No collections yet.</td></tr>`);
  }
  function renderSync() {
    set("#syncList", syncRows().map(r => `<tr><td>${esc(r.name)}<div class="muted small">${esc(r.col)}</div></td><td class="r">${num(r.disk)}</td><td class="r">${num(r.rb)}</td><td class="r">${r.disk > r.rb ? `<b class="hl">+${num(r.disk - r.rb)}</b>` : "–"}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">Nothing split yet.</td></tr>`);
    set("[data-sync-note]", S.rbOpen ? "rekordbox is open. Close it, then sync: mixarchive only writes to its database while it's closed, after saving a copy." : "rekordbox is closed, so it's safe to sync.");
  }
  function renderAnalyze() {
    const st = S.rb && S.rb.status;
    set("#rbTiles", st ? [[st.tracks, "tracks in rekordbox"], [st.analyzed, "analyzed"], [waiting(), "mixarchive tracks waiting"], [st.missing, "files missing"]]
      .map(([v, l]) => `<div class="tile"><b>${num(v)}</b><span>${l}</span></div>`).join("") : `<p class="muted">${S.rb && S.rb.error ? esc(S.rb.error) : "Reading rekordbox…"}</p>`);
  }
  function renderUsb() {
    set("#usbList", rbPlaylists().filter(p => p.name !== "Not analyzed yet").map(p => `<tr><td>${esc(p.name)}</td><td class="r">${num(p.tracks)}</td><td class="r">${p.not_analyzed ? `<b class="hl">${num(p.not_analyzed)}</b>` : "0"}</td><td class="r">${size(p.bytes)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">No mixarchive playlists in rekordbox yet. Sync first.</td></tr>`);
  }

  // ------------------------------------------------------------------ data
  async function loadOverview() {
    try { const o = await api("/api/overview"); S.cols = o.collections; S.rbOpen = o.rekordbox_running; renderJob(o.job); render(); }
    catch (e) { say(e.message); }
  }
  async function loadRb(refresh) {
    try {
      S.rb = await api("/api/rekordbox" + (refresh ? "?refresh=1" : ""));
      set("[data-rb-at]", S.rb.loading ? "reading rekordbox…" : S.rb.at ? "checked " + new Date(S.rb.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
      if (S.rb.loading) setTimeout(loadRb, 1500);
      render();
    } catch (e) { say(e.message); }
  }
  $$("[data-rb-refresh]").forEach(b => b.addEventListener("click", () => loadRb(true)));

  // ------------------------------------------------------------------ commands
  let from = 0, polling = null, tick = null;
  async function run(args) {
    try { const s = await api("/api/run", { args }); set("#logOut", ""); from = 0; renderJob(s); poll(); say("Started: mixarchive " + args.join(" ")); }
    catch (e) { say(e.message); }
  }
  function poll() {
    clearTimeout(polling);
    polling = setTimeout(async () => {
      try {
        const s = await api("/api/job?from=" + from); renderJob(s);
        if (s.running) poll(); else { loadOverview(); if (["rbsync", "update", "files", "merge"].includes((s.args || [])[0])) setTimeout(() => loadRb(true), 800); }
      } catch (e) { say(e.message); }
    }, 700);
  }
  const clock = s => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  function renderJob(s) {
    if (!s) return;
    const wasRunning = S.job && S.job.running;
    S.job = s;
    const pre = $("#logOut");
    if (pre && s.lines && s.lines.length) {
      const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      pre.textContent += s.lines.join("\n") + "\n"; from = s.total;
      if (atEnd) pre.scrollTop = pre.scrollHeight;
    }
    const args = s.args || [], name = args.filter(a => !a.startsWith("-")).slice(1).join(", ");
    const last = pre ? (pre.textContent.trim().split("\n").pop() || "") : "";
    set("[data-np-title]", esc(args.length ? `${args[0]} · ${name || "all collections"}` : "Nothing running"));
    set("[data-np-sub]", esc(s.running ? (last || "starting…") : args.length ? (s.code === 0 ? "finished" : `stopped (exit ${s.code})`) : "idle"));
    $$("[data-np]").forEach(e => { e.classList.toggle("running", !!s.running); e.classList.toggle("failed", !s.running && s.code != null && s.code !== 0); });
    $$("[data-np-stop]").forEach(b => b.disabled = !s.running);
    clearInterval(tick);
    const upd = () => set("[data-np-time]", s.started ? clock((s.ended || Date.now() / 1000) - s.started) : "0:00");
    upd(); if (s.running) tick = setInterval(upd, 1000);
    renderProgress(s, args, pre ? pre.textContent : "");
    if (wasRunning !== s.running) render();
  }
  // progress, drawn by each page's CSS:
  //   [data-prog]              gets .on while running, .indet when there's no count, --p = 0..100
  //   [data-prog-cells=steps]  one .cell per pipeline step, each with its own --f fill
  //   [data-prog-cells=N]      N .cell LEDs lit up to the overall percentage
  //   [data-prog-text], [data-prog-pct], [data-prog-step]
  function renderProgress(s, args, text) {
    const p = s.running && window.mxProgress ? window.mxProgress(text, args, S.cols.length) : null;
    const pct = p && p.frac != null ? Math.round(p.frac * 100) : null;
    $$("[data-prog]").forEach(e => {
      e.classList.toggle("on", !!p); e.classList.toggle("indet", !!p && pct == null);
      e.style.setProperty("--p", pct ?? 0);
      e.setAttribute("role", "progressbar"); e.setAttribute("aria-label", "Progress");
      pct == null ? e.removeAttribute("aria-valuenow") : e.setAttribute("aria-valuenow", pct);
    });
    $$("[data-prog-cells]").forEach(e => {
      const n = e.dataset.progCells;
      const fills = !p ? [] : n === "steps" ? p.cells : Array.from({ length: +n }, (_, i) => pct == null ? 0 : Math.max(0, Math.min(1, pct / 100 * +n - i)));
      if (e.children.length !== fills.length) e.innerHTML = fills.map(() => `<i class="cell"></i>`).join("");
      fills.forEach((f, i) => { e.children[i].style.setProperty("--f", f); e.children[i].classList.toggle("lit", f >= 1); e.children[i].classList.toggle("cur", f > 0 && f < 1); });
    });
    const where = p ? [p.collections > 1 ? `${p.collection} (${p.collectionNo}/${p.collections})` : p.collection, p.stepNo ? `step ${p.stepNo}/${p.steps}` : ""].filter(Boolean).join(" · ") : "";
    set("[data-prog-step]", esc(p ? p.step + (p.count ? " " + p.count : "") : ""));
    set("[data-prog-text]", esc(p ? [where, p.step + (p.count ? " " + p.count : "")].filter(Boolean).join(" · ") : ""));
    set("[data-prog-pct]", p ? (pct == null ? "…" : pct + "%") : "");
  }
  $$("[data-np-stop]").forEach(b => b.addEventListener("click", async () => { try { renderJob(await api("/api/stop", {})); } catch (e) { say(e.message); } }));
  function gate() {   // one command at a time; writes to rekordbox only while it's closed
    const busy = !!(S.job && S.job.running);
    $$("[data-run], #addBtn").forEach(b => {
      const writes = (b.dataset.run || "").includes('"rbsync"');
      b.disabled = busy || (writes && S.rbOpen);
      b.title = writes && S.rbOpen ? "Close rekordbox first" : busy ? "Another command is running" : (b.title || "");
    });
  }
  $$("[data-sync-btn]").forEach(b => b.dataset.run = JSON.stringify(["rbsync"]));
  $$("[data-update-all]").forEach(b => b.dataset.run = JSON.stringify(updArgs()));

  // ------------------------------------------------------------------ add
  const urls = () => ($("#urls") ? $("#urls").value : "").split(/\s+/).map(s => s.trim()).filter(Boolean);
  function kindOf(u) {
    if (!/^https?:\/\/(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//i.test(u)) return "not a YouTube link";
    if (/[?&]list=/.test(u)) return "playlist → its own collection";
    if (/youtube\.com\/(@|c\/|channel\/|user\/)/i.test(u)) return "channel → its own collection";
    return "video → YouTube picks";
  }
  if ($("#urls")) {
    $("#urls").addEventListener("input", () => set("#addPreview", urls().map(u => `<li><span>${esc(u)}</span><em>${kindOf(u)}</em></li>`).join("")));
    $("#addBtn").addEventListener("click", () => {
      const u = urls();
      if (!u.length) return say("Paste at least one link.");
      if (u.some(x => kindOf(x).startsWith("not"))) return say("One of the lines isn't a YouTube channel, playlist or video link.");
      const args = ["add", ...u];
      if ($("#addLabel").value.trim()) args.push("--label", $("#addLabel").value.trim());
      if ($("#addGenre").value.trim()) args.push("--genre", $("#addGenre").value.trim());
      if ($("#addRun").checked) args.push("--run");
      run(args); go("output");
    });
  }

  const fromHash = () => { const [v, c] = location.hash.slice(1).split("/"); if (v !== S.view || (c && decodeURIComponent(c)) !== (S.col || undefined)) go(v || "home", c && decodeURIComponent(c)); };
  window.addEventListener("hashchange", fromHash);   // back/forward and edited addresses
  fromHash();
  loadOverview(); loadRb();
  api("/api/job?from=0").then(s => { renderJob(s); if (s.running) poll(); }).catch(() => {});
  setInterval(() => { if (!(S.job && S.job.running)) loadOverview(); }, 15000);
})();
