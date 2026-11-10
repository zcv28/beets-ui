// beets-ui page behaviour. Plain JS, no build step; every block checks that
// its page elements exist, so this one file serves every page.
"use strict";

// ------------------------------------------------------------------ toasts
// Job pop-ups arrive through the polled jobs panel. Finished jobs leave after four
// seconds (not while hovered); failures stay until dismissed.
(() => {
  const box = document.getElementById("toasts");
  if (!box) return;
  const dismiss = (t) => {
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 200);
  };
  const arm = (t) => {
    if (t.dataset.armed) return;
    t.dataset.armed = "1";
    if (t.hasAttribute("data-sticky")) return;
    let timer = setTimeout(() => dismiss(t), 4000);
    t.addEventListener("mouseenter", () => clearTimeout(timer));
    t.addEventListener("mouseleave", () => { timer = setTimeout(() => dismiss(t), 2000); });
  };
  new MutationObserver(() => box.querySelectorAll(".toast").forEach(arm)).observe(box, { childList: true, subtree: true });
  box.addEventListener("click", (e) => {
    const close = e.target.closest(".toast-close");
    if (close) dismiss(close.closest(".toast"));
  });
})();

// ---------------------------------------------------------- mobile sidebar
(() => {
  const sheet = document.getElementById("sidebar-mobile");
  const open = document.getElementById("sidebar-open");
  if (!sheet || !open || typeof sheet.showModal !== "function") return;
  open.addEventListener("click", () => sheet.showModal());
  sheet.querySelector(".sidebar-mobile-close").addEventListener("click", () => sheet.close());
  // A click on the backdrop lands on the dialog itself.
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });
})();

// ----------------------------------------------------------- confirmations
// Forms with data-confirm ask first, in the page's dialog:
//   data-confirm="text"  data-confirm-button="Delete"  data-confirm-kind="default"
//   data-confirm-checked="delete" data-confirm-alt="text when that checkbox is ticked"
//   data-confirm-alt-button="button label when that checkbox is ticked"
// The dialog is titled with the action, and Cancel has focus, so Enter never destroys.
(() => {
  const dialog = document.getElementById("confirm-dialog");
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.dataset || !form.dataset.confirm) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    const box = form.dataset.confirmChecked && form.elements[form.dataset.confirmChecked];
    const alt = box && box.checked;
    const text = alt ? form.dataset.confirmAlt : form.dataset.confirm;
    const label = (alt && form.dataset.confirmAltButton) || form.dataset.confirmButton || "Continue";
    const go = () => { form.dataset.confirmed = "1"; form.requestSubmit(e.submitter || undefined); };
    if (!dialog || typeof dialog.showModal !== "function") {
      if (window.confirm(text)) go();
      return;
    }
    document.getElementById("confirm-title").textContent = `${label}?`;
    document.getElementById("confirm-text").textContent = text;
    const ok = document.getElementById("confirm-ok");
    ok.textContent = label;
    ok.dataset.variant = form.dataset.confirmKind === "default" ? "default" : "destructive";
    dialog.returnValue = "";
    dialog.onclose = () => { if (dialog.returnValue === "ok") go(); };
    dialog.showModal();
    document.getElementById("confirm-cancel").focus();
  }, true);

  // A submit button with data-label-alt says what it will do once the form's checkbox is ticked.
  document.addEventListener("change", (e) => {
    const form = e.target.form;
    if (!form || !form.dataset.confirmChecked || e.target.name !== form.dataset.confirmChecked) return;
    for (const btn of form.querySelectorAll("button[data-label-alt]")) {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.textContent = e.target.checked ? btn.dataset.labelAlt : btn.dataset.label;
    }
  });
})();

// -------------------------------------------------------------- autofocus
// Fields marked data-autofocus take focus only with a mouse or trackpad: on a phone
// it would pop the keyboard up over the page.
(() => {
  const field = document.querySelector("[data-autofocus]");
  if (field && window.matchMedia("(pointer: fine)").matches) field.focus();
})();

// ---------------------------------------------------- review queue: n / N
(() => {
  const next = document.getElementById("queue-next");
  const prev = document.getElementById("queue-prev");
  if (!next && !prev) return;
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest("textarea, select, dialog, [contenteditable], input:not([type=radio]):not([type=checkbox])")) return;
    if (e.key === "n" && next) { e.preventDefault(); next.click(); }
    if (e.key === "N" && prev) { e.preventDefault(); prev.click(); }
  });
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
    list[idx].querySelector('input[type="radio"]').focus({ preventScroll: true });
    list[idx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const focusedTask = () => {
    const row = rows()[idx];
    return row ? row.closest("section.task") : form.querySelector("section.task");
  };
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    // A candidate's radio has focus after j/k: the shortcuts keep working from there.
    const onCand = t.matches && t.matches("tr.cand input[type=radio]");
    if (!onCand && t.closest && t.closest("input, textarea, select, dialog, [contenteditable]")) return;
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
        if (!cur || (e.key === "Enter" && t !== document.body && !onCand)) return;
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
      case "i": form.requestSubmit(document.getElementById("import-next") || undefined); break;
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
  const cards = [...grid.querySelectorAll(".tile")];
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
    target.placeholder = one ? "New name" : "Pick or type a name";
    names.replaceChildren(...picked.map((n) => new Option(n)));
    if (!one && !target.value && picked.length) target.value = picked[0];
    if (one && target.value === picked[0]) target.value = "";
    for (const c of cards) c.classList.toggle("picked", !!c.querySelector("input:checked"));
    confirmText();
  };
  const form = document.getElementById("merge-form");
  const confirmText = () => {
    const picked = [...grid.querySelectorAll('input[name="name"]:checked')].map((p) => p.value);
    const name = target.value.trim() || "the new name";
    const one = picked.length === 1;
    form.dataset.confirm = (one ? `Rename ${picked[0]} to ${name}?` : `Merge ${picked.length} artists into ${name}?`)
      + " Tags are rewritten and files move to that artist's folder. You can undo it from the job page.";
    form.dataset.confirmButton = one ? "Rename artist" : `Merge ${picked.length} artists`;
  };
  target.addEventListener("input", confirmText);
  grid.addEventListener("change", update);
  update();
})();

// ------------------------------------------------------------- menus
// A details menu closes on Escape (focus back on its button) and on a click elsewhere.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  for (const m of document.querySelectorAll("details.menu[open]")) {
    m.open = false;
    m.querySelector("summary").focus();
  }
});
document.addEventListener("click", (e) => {
  for (const m of document.querySelectorAll("details.menu[open]")) {
    if (!m.contains(e.target)) m.open = false;
  }
});

// -------------------------------------------------------- auto-applied filters
// Forms with data-autosubmit apply as soon as a checkbox or select changes.
document.addEventListener("change", (e) => {
  const form = e.target.form;
  if (form && form.hasAttribute("data-autosubmit") && e.target.matches("select, input[type=checkbox], input[type=radio]")) {
    form.requestSubmit();
  }
});

// ------------------------------------------------------- tabs that overflow
// A tab row too wide for the screen scrolls; a fade marks the side with more tabs,
// and the current tab is kept in view.
for (const list of document.querySelectorAll(".tab-list")) {
  const update = () => {
    const max = list.scrollWidth - list.clientWidth;
    const x = list.scrollLeft;
    list.dataset.overflow = max <= 1 ? "" : x < 2 ? "end" : x >= max - 2 ? "start" : "both";
  };
  const current = list.querySelector('[aria-current="page"]');
  if (current) list.scrollLeft = current.offsetLeft + current.offsetWidth - list.clientWidth + 24;
  list.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  update();
}
