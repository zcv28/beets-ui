// beets-ui page behaviour. Plain JS, no build step; every block checks that
// its page elements exist, so this one file serves every page.
"use strict";

// ------------------------------------------------------------------ toasts
// Job pop-ups arrive through the polled nav badge; drop them after they fade.
document.addEventListener("animationend", (e) => {
  if (e.target.classList && e.target.classList.contains("toast")) e.target.remove();
});

// ------------------------------------------------------------ audio player
(() => {
  const bar = document.getElementById("player-bar");
  const player = document.getElementById("player");
  if (!bar || !player) return;
  const name = document.getElementById("player-name");
  let current = null;

  const mark = (btn, playing) => { if (btn) btn.textContent = playing ? "⏸" : "▶"; };
  player.addEventListener("pause", () => mark(current, false));
  player.addEventListener("ended", () => mark(current, false));
  player.addEventListener("play", () => mark(current, true));

  window.beetsPlay = (btn) => {
    if (btn === current && !player.paused) { player.pause(); return; }
    if (btn !== current) {
      mark(current, false);
      current = btn;
      player.src = btn.dataset.src;
      const row = btn.closest("tr, li");
      name.textContent = row ? row.textContent.replace(/[▶⏸]/g, "").trim().slice(0, 80) : "";
    }
    bar.hidden = false;
    player.play().catch(() => mark(current, false));
  };
  window.beetsTogglePlayer = () => {
    if (current) window.beetsPlay(current);
  };
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("button.play");
    if (btn) { e.preventDefault(); window.beetsPlay(btn); }
  });
})();

// ----------------------------------------------------- inbox review page
(() => {
  const form = document.getElementById("import-form");
  if (!form) return;
  const decided = document.getElementById("decided");

  const summarise = () => {
    let apply = 0, asis = 0, skip = 0, dupOpen = 0;
    for (const task of form.querySelectorAll("section.task")) {
      const ti = task.dataset.task;
      const choice = form.querySelector(`input[name="choice-${ti}"]:checked`);
      const v = choice ? choice.value : "skip";
      if (v.startsWith("c:")) apply++; else if (v === "asis") asis++; else skip++;
      const dups = form.querySelectorAll(`input[name="dup-${ti}"]`);
      if (dups.length && v !== "skip" && !form.querySelector(`input[name="dup-${ti}"]:checked`)) dupOpen++;
    }
    const parts = [`${apply + asis} of ${decided.dataset.tasks} to import`];
    if (asis) parts.push(`${asis} as-is`);
    if (skip) parts.push(`${skip} skipped`);
    if (dupOpen) parts.push(`${dupOpen} duplicate${dupOpen > 1 ? "s" : ""} to resolve`);
    decided.textContent = parts.join(" · ");
    decided.classList.toggle("warnc", dupOpen > 0);
  };
  form.addEventListener("change", summarise);
  summarise();

  // Picking a candidate marks it like the server does on reload.
  form.addEventListener("change", (e) => {
    if (!e.target.name || !e.target.name.startsWith("choice-")) return;
    for (const r of form.querySelectorAll(`input[name="${e.target.name}"]`)) {
      const cand = r.closest("details.cand");
      if (cand) cand.classList.toggle("chosen", r.checked);
    }
  });

  // Keyboard: j/k move between candidates, x/Enter choose, o open, p play, i import.
  const cands = () => [...form.querySelectorAll("details.cand")];
  let idx = -1;
  const focus = (i) => {
    const list = cands();
    if (!list.length) return;
    idx = Math.max(0, Math.min(list.length - 1, i));
    list.forEach((c, n) => c.classList.toggle("kfocus", n === idx));
    list[idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    const cur = cands()[idx];
    switch (e.key) {
      case "j": focus(idx + 1); break;
      case "k": focus(idx < 0 ? 0 : idx - 1); break;
      case "o": if (cur) cur.open = !cur.open; break;
      case "x":
      case "Enter": {
        if (!cur || (e.key === "Enter" && t !== document.body)) return;
        const r = cur.querySelector('input[type="radio"]');
        if (r) { r.checked = true; r.dispatchEvent(new Event("change", { bubbles: true })); }
        break;
      }
      case "p": {
        const btn = (cur && cur.open && cur.querySelector("button.play"))
          || (cur && cur.closest("section.task").querySelector("button.play"));
        if (btn) window.beetsPlay(btn); else if (window.beetsTogglePlayer) window.beetsTogglePlayer();
        break;
      }
      case "i": form.requestSubmit(); break;
      default: return;
    }
    e.preventDefault();
  });
})();

// ------------------------------------------------------------ artists page
(() => {
  const grid = document.getElementById("artist-grid");
  if (!grid) return;
  const input = document.getElementById("artist-filter");
  const letters = document.getElementById("artist-letters");
  const none = document.getElementById("artist-none");
  const cards = [...grid.querySelectorAll(".card")];
  let letter = decodeURIComponent(location.hash.slice(1));

  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let shown = 0;
    for (const c of cards) {
      const ok = (!q || c.dataset.name.includes(q)) && (!letter || c.dataset.letter === letter);
      c.hidden = !ok;
      shown += ok;
    }
    none.hidden = shown > 0;
    for (const a of letters.querySelectorAll("a")) a.classList.toggle("on", a.dataset.letter === letter);
  };
  input.addEventListener("input", apply);
  letters.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-letter]");
    if (!a) return;
    e.preventDefault();
    letter = a.dataset.letter;
    history.replaceState(null, "", letter ? `#${encodeURIComponent(letter)}` : location.pathname);
    apply();
  });
  apply();

  // Merge: ticking two or more artists shows the merge bar.
  const bar = document.getElementById("merge-bar");
  const target = document.getElementById("merge-target");
  const count = document.getElementById("merge-count");
  const update = () => {
    const picked = [...grid.querySelectorAll('input[name="name"]:checked')];
    bar.hidden = picked.length < 2;
    count.textContent = `Merge ${picked.length} artists`;
    const keep = target.value;
    target.replaceChildren(...picked.map((p) => new Option(p.value, p.value, false, p.value === keep)));
    for (const c of cards) c.classList.toggle("picked", !!c.querySelector("input:checked"));
  };
  grid.addEventListener("change", update);
  update();
})();
