// beets-ui page behaviour. Plain JS, no build step; every block checks that
// its page elements exist, so this one file serves every page.
"use strict";

// ------------------------------------------------------------------ toasts
// Job pop-ups arrive through the polled status bar cell; drop them after they fade.
document.addEventListener("animationend", (e) => {
  if (e.target.classList && e.target.classList.contains("toast")) e.target.remove();
});

// ----------------------------------------------------------- confirmations
// Forms with data-confirm ask first, in the page's dialog:
//   data-confirm="text"  data-confirm-button="Delete"  data-confirm-kind="accent"
//   data-confirm-checked="delete" data-confirm-alt="text when that checkbox is ticked"
(() => {
  const dialog = document.getElementById("confirm-dialog");
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.dataset || !form.dataset.confirm) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    const box = form.dataset.confirmChecked && form.elements[form.dataset.confirmChecked];
    const text = box && box.checked ? form.dataset.confirmAlt : form.dataset.confirm;
    const go = () => { form.dataset.confirmed = "1"; form.requestSubmit(e.submitter || undefined); };
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(text)) go();
      return;
    }
    document.getElementById("confirm-text").textContent = text;
    const ok = document.getElementById("confirm-ok");
    ok.textContent = form.dataset.confirmButton || "OK";
    ok.className = "nc-btn " + (form.dataset.confirmKind === "accent" ? "nc-btn--accent" : "nc-btn--danger");
    dialog.returnValue = "";
    dialog.onclose = () => { if (dialog.returnValue === "ok") go(); };
    dialog.showModal();
    ok.focus();
  }, true);
})();

// ------------------------------------------------------------ audio player
(() => {
  const bar = document.getElementById("player-bar");
  const player = document.getElementById("player");
  if (!bar || !player) return;
  const name = document.getElementById("player-name");
  let current = null;

  const mark = (btn, playing) => {
    if (!btn) return;
    btn.classList.toggle("playing", playing);
    btn.setAttribute("aria-pressed", playing ? "true" : "false");
  };
  player.addEventListener("pause", () => mark(current, false));
  player.addEventListener("ended", () => mark(current, false));
  player.addEventListener("play", () => mark(current, true));

  window.beetsPlay = (btn) => {
    if (btn === current && !player.paused) { player.pause(); return; }
    if (btn !== current) {
      mark(current, false);
      current = btn;
      player.src = btn.dataset.src;
      name.textContent = (btn.getAttribute("aria-label") || "").replace(/^Play /, "");
    }
    bar.hidden = false;
    player.play().catch(() => mark(current, false));
  };
  window.beetsTogglePlayer = () => { if (current) window.beetsPlay(current); };
  document.getElementById("player-close").addEventListener("click", () => {
    player.pause();
    bar.hidden = true;
  });
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

  // The chosen candidate's row is selected and its track changes are shown.
  const showChoice = (task) => {
    for (const row of task.querySelectorAll("tr.cand")) {
      const on = row.querySelector('input[type="radio"]').checked;
      if (on) row.setAttribute("aria-selected", "true"); else row.removeAttribute("aria-selected");
      const diff = document.getElementById(row.dataset.diff);
      if (diff) diff.hidden = !on;
    }
  };
  form.addEventListener("change", (e) => {
    const task = e.target.closest("section.task");
    if (task && e.target.name && e.target.name.startsWith("choice-")) showChoice(task);
    summarise();
  });
  // Clicking anywhere on a candidate row chooses it.
  form.addEventListener("click", (e) => {
    const row = e.target.closest("tr.cand");
    if (!row || e.target.closest("a, button, input, label")) return;
    const radio = row.querySelector('input[type="radio"]');
    if (!radio.checked) { radio.checked = true; radio.dispatchEvent(new Event("change", { bubbles: true })); }
  });
  summarise();

  // Keyboard: j/k move between candidates, x/Enter choose, o files, p play, i import.
  const rows = () => [...form.querySelectorAll("tr.cand")];
  let idx = -1;
  const focus = (i) => {
    const list = rows();
    if (!list.length) return;
    idx = Math.max(0, Math.min(list.length - 1, i));
    list.forEach((r, n) => r.classList.toggle("kfocus", n === idx));
    list[idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const focusedTask = () => {
    const row = rows()[idx];
    return row ? row.closest("section.task") : form.querySelector("section.task");
  };
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, dialog, [contenteditable]")) return;
    const cur = rows()[idx];
    switch (e.key) {
      case "j": focus(idx + 1); break;
      case "k": focus(idx < 0 ? 0 : idx - 1); break;
      case "o": {
        const files = focusedTask() && focusedTask().querySelector("details.current");
        if (files) files.open = !files.open;
        break;
      }
      case "x":
      case "Enter": {
        if (!cur || (e.key === "Enter" && t !== document.body)) return;
        const r = cur.querySelector('input[type="radio"]');
        r.checked = true;
        r.dispatchEvent(new Event("change", { bubbles: true }));
        break;
      }
      case "p": {
        const task = focusedTask();
        const btn = task && (task.querySelector(".diff:not([hidden]) button.play")
          || task.querySelector("details.current button.play"));
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
    for (const a of letters.querySelectorAll("a")) {
      const on = a.dataset.letter === letter;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    }
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

  // Rename one ticked artist, or merge several into one name (typed or picked).
  const bar = document.getElementById("merge-bar");
  const target = document.getElementById("merge-target");
  const names = document.getElementById("merge-names");
  const count = document.getElementById("merge-count");
  const verb = document.getElementById("merge-verb");
  const button = document.getElementById("merge-button");
  const update = () => {
    const picked = [...grid.querySelectorAll('input[name="name"]:checked')].map((p) => p.value);
    bar.hidden = picked.length < 1;
    const one = picked.length === 1;
    count.textContent = one ? `Rename ${picked[0]}` : `Merge ${picked.length} artists`;
    verb.textContent = one ? "to" : "into";
    button.textContent = one ? "Rename" : "Merge";
    target.placeholder = one ? "new name" : "pick or type a name";
    names.replaceChildren(...picked.map((n) => new Option(n)));
    if (!one && !target.value && picked.length) target.value = picked[0];
    if (one && target.value === picked[0]) target.value = "";
    for (const c of cards) c.classList.toggle("picked", !!c.querySelector("input:checked"));
  };
  grid.addEventListener("change", update);
  update();
})();
