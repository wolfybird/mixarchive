// Progress of the running command, read from its output. Shared by every GUI page; each
// page draws the result in its own style. The CLI marks its structure with
//   "===== <collection>"  (update / add --run: one per collection, then "===== rbsync")
//   "--- <step>"          (the per-collection pipeline, see cli._pipeline)
//   "<i>/<n> ..."         (counters inside a step: mixes, singles, tracks added)
window.mxProgress = (() => {
  const STEPS = ["resolve", "tracklists", "fetch", "tracklists", "art", "split"];
  const WEIGHT = [1, 1, 4, 0.5, 1, 3];        // rough share of the time each step takes
  const NAME = { resolve: "finding mixes + tracks", tracklists: "reading tracklists", fetch: "downloading",
    art: "cover art", split: "splitting + tagging", modmatch: "matching modules", modrender: "rendering modules",
    rbsync: "adding to rekordbox" };
  const COUNT = /^\s+(?:added\s+)?(\d[\d,]*)\/(\d[\d,]*)(?:\s|$)/;   // indented counters only, not titles
  const W = WEIGHT.reduce((a, b) => a + b, 0);

  // text: everything printed so far; args: the command; nCollections: library size
  return function (text, args, nCollections) {
    args = args || [];
    const lines = text.split("\n");
    let cols = 0, col = "", rb = false, step = -1, stepName = "", count = null;
    for (const l of lines) {
      let m;
      if ((m = /^===== (.+)$/.exec(l))) {
        if (m[1] === "rbsync") { rb = true; stepName = "rbsync"; } else { cols++; col = m[1]; stepName = ""; }
        step = -1; count = null;
      } else if ((m = /^--- (\w+)/.exec(l))) {
        step++; stepName = m[1]; count = null;
      } else if ((m = COUNT.exec(l))) {
        count = [+m[1].replace(/,/g, ""), +m[2].replace(/,/g, "")];
      }
    }
    const cmd = args[0] || "";
    const named = args.slice(1).filter(a => !a.startsWith("-"));
    const piped = cmd === "update" || cmd === "run" || (cmd === "add" && args.includes("--run"));
    const inner = count && count[1] ? Math.min(1, count[0] / count[1]) : null;
    if (!piped) {      // rbsync, files, merge...: only the counter, if there is one
      return { cmd, collection: "", step: NAME[cmd] || cmd, stepNo: 0, steps: 0,
        count: count ? `${count[0].toLocaleString()}/${count[1].toLocaleString()}` : "", frac: inner, cells: [], counted: inner != null };
    }
    // how many units of work: each collection, plus rbsync at the end of update
    const total = cmd === "run" ? 1 : cmd === "update" ? (named.length || nCollections || 1) : Math.max(cols, 1);
    const units = total + (cmd === "update" && !args.includes("--no-rbsync") ? 1 : 0);
    const s = Math.min(step, STEPS.length - 1);
    let done = 0;
    for (let i = 0; i < s; i++) done += WEIGHT[i];
    const within = s < 0 ? 0 : (done + WEIGHT[s] * (inner ?? 0.3)) / W;   // 0.3: "under way", no counter yet
    const frac = rb ? (units - 1 + (inner ?? 0.3)) / units : Math.min(1, (Math.max(cols, 1) - 1 + within) / units);
    // one cell per pipeline step: 1 done, 0..1 the one running, 0 not yet
    const cells = rb ? STEPS.map(() => 1) : STEPS.map((_, i) => i < s ? 1 : i === s ? (inner ?? 0.3) : 0);
    return {
      cmd, collection: rb ? "rekordbox" : col, collectionNo: Math.max(cols, 1), collections: total,
      step: NAME[stepName] || stepName || "starting", stepNo: rb ? STEPS.length : s + 1, steps: STEPS.length,
      count: count ? `${count[0].toLocaleString()}/${count[1].toLocaleString()}` : "", frac, cells,
      counted: inner != null,     // false: frac leans on a guessed "under way" share
    };
  };
})();

// Seconds left for the running job, or null. Estimated from the pace so far
// (elapsed / done * left), only while a step reports a real count; in between it keeps
// counting the last estimate down instead of guessing from a step with no counter.
window.mxEta = (() => {
  let last = { started: null, secs: null, at: 0 };
  return function (p, started, now) {
    if (!p || !started) return null;
    if (last.started !== started) last = { started, secs: null, at: 0 };   // a new job
    const elapsed = now - started;
    if (p.counted && p.frac > 0.02 && p.frac < 1 && elapsed > 10) {
      last.secs = elapsed * (1 - p.frac) / p.frac;
      last.at = now;
    }
    return last.secs == null ? null : Math.max(0, last.secs - (now - last.at));
  };
})();

// "~4 min left", "under a minute left", "~1 h 20 min left"
window.mxLeft = secs => {
  if (secs == null) return "";
  if (secs < 60) return "under a minute left";
  const m = Math.round(secs / 60);
  return m < 60 ? `~${m} min left` : `~${Math.floor(m / 60)} h ${m % 60} min left`;
};
