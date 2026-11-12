// beets-ui page behaviour. Plain JS, no build step.
//
// Signed in, links and forms swap the page body in place (htmx boost) instead of
// reloading it, so this file runs once per visit. It has two kinds of code:
//  - document-level handlers, set up once, that find what they need when they run;
//  - init(root), run for the first page and for every piece of page htmx swaps in.
"use strict";

// Elements matching sel inside root, root itself included.
const within = (root, sel) => [
  ...(root.matches && root.matches(sel) ? [root] : []),
  ...(root.querySelectorAll ? root.querySelectorAll(sel) : []),
];

// Lucide icons (ISC), as in partials/macros.html.
const ICON = {
  success: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  destructive: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
};
const svg = (name, cls) => `<svg class="${cls}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

// ------------------------------------------------------------------ toasts
// Pop-ups come from the jobs panel (finished jobs and downloads) and from in-place
// actions (an HX-Trigger "toast" header). Successes leave after four seconds, eight
// when they offer Undo, never while hovered; failures stay until dismissed.
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
    const wait = Number(t.dataset.timeout || 4000);
    let timer = setTimeout(() => dismiss(t), wait);
    t.addEventListener("mouseenter", () => clearTimeout(timer));
    t.addEventListener("mouseleave", () => { timer = setTimeout(() => dismiss(t), 2000); });
  };
  new MutationObserver(() => box.querySelectorAll(".toast").forEach(arm)).observe(box, { childList: true, subtree: true });

  // {title, text, kind: success | destructive | info, undo: {url, vals}}
  window.beetsToast = (d) => {
    const kind = d.kind || "success";
    const t = document.createElement("div");
    t.className = "toast";
    t.setAttribute("role", kind === "destructive" ? "alert" : "status");
    if (kind !== "info") t.dataset.variant = kind;
    if (kind === "destructive") t.setAttribute("data-sticky", "");
    if (d.undo) t.dataset.timeout = "8000";
    t.innerHTML = `<div class="toast-content">${svg(kind, "toast-icon")}<div class="toast-text"><p class="toast-title"></p></div><button class="toast-close" type="button" aria-label="Dismiss">${svg("x", "")}</button></div>`;
    t.querySelector(".toast-title").textContent = d.title || "";
    const text = t.querySelector(".toast-text");
    if (d.text) {
      const p = document.createElement("p");
      p.className = "toast-description";
      p.textContent = d.text;
      text.append(p);
    }
    if (d.undo) {
      const actions = document.createElement("div");
      actions.className = "toast-actions";
      const b = document.createElement("button");
      b.className = "btn";
      b.type = "button";
      b.dataset.variant = "outline";
      b.dataset.size = "xs";
      b.textContent = "Undo";
      b.addEventListener("click", () => {
        b.disabled = true;
        htmx.ajax("POST", d.undo.url, { values: d.undo.vals || {}, swap: "none" })
          .then(() => htmx.ajax("GET", location.pathname + location.search, { target: "body", swap: "innerHTML" }))
          .finally(() => dismiss(t));
      });
      actions.append(b);
      text.append(actions);
    }
    box.append(t);
  };
  document.body.addEventListener("toast", (e) => window.beetsToast(e.detail.value || e.detail));
  box.addEventListener("click", (e) => {
    const close = e.target.closest(".toast-close");
    if (close) dismiss(close.closest(".toast"));
  });
})();

// ------------------------------------------------------------------- theme
// Light, dark or the browser's setting, chosen from the sidebar. The choice applies at
// once and is kept in a cookie, so the server sends every later page already in it.
(() => {
  const BAR = { light: "#ffffff", dark: "#0a0a0a" };
  const apply = (theme) => {
    const root = document.documentElement;
    const fixed = theme === "light" || theme === "dark";
    if (fixed) root.dataset.theme = theme; else delete root.dataset.theme;
    document.cookie = `beetsui_theme=${fixed ? theme : ""}; path=/; max-age=${fixed ? 31536000 : 0}; samesite=lax`;
    // The browser's own bar (and the installed app's title bar) follows.
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.remove());
    for (const t of fixed ? [theme] : ["light", "dark"]) {
      const m = document.createElement("meta");
      m.name = "theme-color";
      m.content = BAR[t];
      if (!fixed) m.media = `(prefers-color-scheme: ${t})`;
      document.head.append(m);
    }
    for (const b of document.querySelectorAll("[data-theme-set]")) {
      b.setAttribute("aria-pressed", String(b.dataset.themeSet === (fixed ? theme : "")));
    }
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-theme-set]");
    if (!b) return;
    apply(b.dataset.themeSet);
    const menu = b.closest("[popover]");
    if (menu) menu.hidePopover();
  });
})();

// ---------------------------------------------------------- mobile sidebar
// The sheet is part of the swapped page, so its buttons are found when clicked.
document.addEventListener("click", (e) => {
  const sheet = document.getElementById("sidebar-mobile");
  if (!sheet || typeof sheet.showModal !== "function") return;
  if (e.target.closest("#sidebar-open")) sheet.showModal();
  else if (e.target.closest(".sidebar-mobile-close")) sheet.close();
  // A click on the backdrop lands on the dialog itself.
  else if (e.target === sheet) sheet.close();
});

// ----------------------------------------------------------- confirmations
// Forms with data-confirm ask first, in the page's dialog:
//   data-confirm="text"  data-confirm-button="Delete"  data-confirm-kind="default"
//   data-confirm-checked="delete" data-confirm-alt="text when that checkbox is ticked"
//   data-confirm-alt-button="button label when that checkbox is ticked"
// The dialog is titled with the action, and Cancel has focus, so Enter never destroys.
// The submit is stopped here, before htmx (boosted and hx-post forms) sees it.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.dataset || !form.dataset.confirm) return;
  if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
  e.preventDefault();
  e.stopPropagation();
  const box = form.dataset.confirmChecked && form.elements[form.dataset.confirmChecked];
  const alt = box && box.checked;
  const text = alt ? form.dataset.confirmAlt : form.dataset.confirm;
  const label = (alt && form.dataset.confirmAltButton) || form.dataset.confirmButton || "Continue";
  const go = () => { form.dataset.confirmed = "1"; form.requestSubmit(e.submitter || undefined); };
  const dialog = document.getElementById("confirm-dialog");
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

// ------------------------------------------------------------ audio player
// The player sits outside the swapped page (hx-preserve), so it keeps playing
// while you move around.
(() => {
  const bar = document.getElementById("player-bar");
  const player = document.getElementById("player");
  if (!bar || !player) return;
  const name = document.getElementById("player-name");
  let current = null;
  let currentSrc = "";

  // Play buttons on a freshly swapped page show the state of what is playing.
  const mark = (btn, playing) => {
    if (!btn) return;
    btn.classList.toggle("playing", playing);
    btn.setAttribute("aria-pressed", playing ? "true" : "false");
  };
  const markAll = () => {
    for (const b of document.querySelectorAll("button.play")) mark(b, b.dataset.src === currentSrc && !player.paused);
  };
  player.addEventListener("pause", markAll);
  player.addEventListener("ended", markAll);
  player.addEventListener("play", markAll);
  document.addEventListener("htmx:load", markAll);

  window.beetsPlay = (btn) => {
    if (btn.dataset.src === currentSrc && !player.paused) { player.pause(); return; }
    if (btn.dataset.src !== currentSrc) {
      currentSrc = btn.dataset.src;
      player.src = currentSrc;
      name.textContent = (btn.getAttribute("aria-label") || "").replace(/^Play /, "");
    }
    current = btn;
    bar.hidden = false;
    player.play().catch(markAll);
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

// ------------------------------------------------------------- keyboard
const typing = (t, allowChoice) => t.closest && t.closest(
  allowChoice
    ? "textarea, select, dialog, [contenteditable], input:not([type=radio]):not([type=checkbox])"
    : "input, textarea, select, dialog, [contenteditable]",
);

// Review queue: n / N open the next and previous folder.
// Single-key shortcuts can be turned off (a screen reader or voice control may type them).
const keysOff = () => { try { return localStorage.getItem("beetsui-keys") === "off"; } catch { return false; } };
const showKeys = () => {
  const off = keysOff();
  for (const el of document.querySelectorAll("[data-keys-toggle]")) {
    el.textContent = off ? "Turn shortcuts on" : "Turn shortcuts off";
    el.setAttribute("aria-pressed", String(!off));
  }
  for (const el of document.querySelectorAll(".keys-list")) el.hidden = off;
};
document.addEventListener("click", (e) => {
  const t = e.target.closest && e.target.closest("[data-keys-toggle]");
  if (!t) return;
  try { localStorage.setItem("beetsui-keys", keysOff() ? "on" : "off"); } catch { /* stays as it was */ }
  showKeys();
});
document.addEventListener("DOMContentLoaded", showKeys);
document.addEventListener("htmx:load", showKeys);

document.addEventListener("keydown", (e) => {
  if (keysOff() || e.ctrlKey || e.metaKey || e.altKey || typing(e.target, true)) return;
  const next = document.getElementById("queue-next");
  const prev = document.getElementById("queue-prev");
  if (e.key === "n" && next) { e.preventDefault(); next.click(); }
  if (e.key === "N" && prev) { e.preventDefault(); prev.click(); }
});

// Inbox review page: j/k move between candidates, x/Enter choose, o files, p play, i import.
document.addEventListener("keydown", (e) => {
  const form = document.getElementById("import-form");
  if (!form || keysOff() || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  // A candidate's radio has focus after j/k: the shortcuts keep working from there.
  const onCand = t.matches && t.matches("tr.cand input[type=radio]");
  if (!onCand && typing(t, false)) return;
  const rows = [...form.querySelectorAll("tr.cand")];
  const idx = form.reviewIdx ?? -1;
  const focus = (i) => {
    if (!rows.length) return;
    form.reviewIdx = Math.max(0, Math.min(rows.length - 1, i));
    rows.forEach((r, n) => r.classList.toggle("kfocus", n === form.reviewIdx));
    rows[form.reviewIdx].querySelector('input[type="radio"]').focus({ preventScroll: true });
    rows[form.reviewIdx].scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const cur = rows[idx];
  const task = cur ? cur.closest("section.task") : form.querySelector("section.task");
  switch (e.key) {
    case "j": focus(idx + 1); break;
    case "k": focus(idx < 0 ? 0 : idx - 1); break;
    case "o": {
      const files = task && task.querySelector("details.current");
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

// A row menu ([popover].menu-content) opens in the top layer, under its button,
// right-aligned to it and flipped above when there is no room below.
document.addEventListener("toggle", (e) => {
  const pop = e.target;
  if (!pop.matches || !pop.matches(".menu-content[popover]") || e.newState !== "open") return;
  const btn = document.querySelector(`[popovertarget="${CSS.escape(pop.id)}"]`);
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight, gap = 4;
  const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
  const below = r.bottom + gap + h <= window.innerHeight - 8;
  pop.style.left = `${left}px`;
  pop.style.top = `${below ? r.bottom + gap : Math.max(8, r.top - gap - h)}px`;
}, true);
// It belongs to a spot on the page: scrolling closes it rather than leaving it adrift.
document.addEventListener("scroll", () => {
  for (const p of document.querySelectorAll(".menu-content[popover]:popover-open")) p.hidePopover();
}, { passive: true, capture: true });

// -------------------------------------------------------- auto-applied filters
// Forms with data-autosubmit apply as soon as a checkbox or select changes.
document.addEventListener("change", (e) => {
  const form = e.target.form;
  if (form && form.hasAttribute("data-autosubmit") && e.target.matches("select, input[type=checkbox], input[type=radio]")) {
    form.requestSubmit();
  }
});

// ------------------------------------------------- Soulseek: choose files
// The folder page's file list: a tick-all box, and a count and size on the button.
document.addEventListener("change", (e) => {
  const form = e.target.closest("#pick-form");
  if (!form) return;
  const boxes = [...form.querySelectorAll('input[name="file"]')];
  if (e.target.id === "pick-all") boxes.forEach((b) => { b.checked = e.target.checked; });
  const on = boxes.filter((b) => b.checked);
  const all = form.querySelector("#pick-all");
  all.checked = on.length === boxes.length;
  all.indeterminate = on.length > 0 && on.length < boxes.length;
  let bytes = on.reduce((n, b) => n + Number(b.dataset.bytes || 0), 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let u = 0;
  while (bytes >= 1024 && u < units.length - 1) { bytes /= 1024; u++; }
  const size = u ? `${bytes.toFixed(1)} ${units[u]}` : `${bytes} B`;
  form.querySelector("#pick-count").textContent = `${on.length} of ${boxes.length} files · ${size}`;
  const button = form.querySelector("#pick-submit");
  button.textContent = on.length ? `Download ${on.length} file${on.length === 1 ? "" : "s"}` : "Download";
  button.disabled = on.length === 0;
});

// ----------------------------------------------- polled lists keep open rows
// A refreshed region marked data-keep-open re-opens the details that were open before the
// swap, and gives focus back to the row it was in (its summary) so the keyboard keeps its place.
document.addEventListener("htmx:beforeSwap", (e) => {
  const el = e.detail.target;
  if (!el.hasAttribute || !el.hasAttribute("data-keep-open")) return;
  el.dataset.openIds = [...el.querySelectorAll("details[open][id]")].map((d) => d.id).join(" ");
  const active = document.activeElement;
  const row = active && el.contains(active) ? active.closest("details[id]") : null;
  el.dataset.focusId = row ? row.id : "";
});
document.addEventListener("htmx:afterSwap", (e) => {
  const el = e.detail.target;
  if (!el.dataset || !el.dataset.openIds && !el.dataset.focusId) return;
  for (const id of (el.dataset.openIds || "").split(" ")) {
    const d = id && document.getElementById(id);
    if (d) d.open = true;
  }
  const focus = el.dataset.focusId && document.getElementById(el.dataset.focusId);
  if (focus) focus.querySelector("summary").focus({ preventScroll: true });
  el.dataset.openIds = el.dataset.focusId = "";
});

// A row that removes itself (hx-swap="delete") hands focus to the next row, or the one before.
const FOCUSABLE = "a[href], button:not([disabled]), summary, input:not([type=hidden]), select";
document.addEventListener("htmx:beforeSwap", (e) => {
  const target = e.detail.target;
  const active = document.activeElement;
  if (!target || !active || !target.contains(active)) return;
  const swap = e.detail.requestConfig && e.detail.requestConfig.elt && e.detail.requestConfig.elt.getAttribute("hx-swap");
  if (swap !== "delete") return;
  const next = target.nextElementSibling || target.previousElementSibling;
  const into = next && (next.matches(FOCUSABLE) ? next : next.querySelector(FOCUSABLE));
  if (into) setTimeout(() => into.focus({ preventScroll: true }), 0);
});

// A refreshed list may carry a one-line summary for screen readers ([data-announce]);
// it is read out when it changes.
document.addEventListener("htmx:afterSwap", (e) => {
  const note = e.detail.target.querySelector && e.detail.target.querySelector("[data-announce]");
  const region = document.getElementById("live-status");
  if (!note || !region) return;
  const text = note.dataset.announce;
  if (region.dataset.last !== undefined && region.dataset.last !== text) region.textContent = text;
  region.dataset.last = text;
});

// ------------------------------------------------------- tabs that overflow
// A tab row too wide for the screen scrolls; a fade marks the side with more tabs,
// and the current tab is kept in view.
const tabOverflow = (list) => {
  const max = list.scrollWidth - list.clientWidth;
  const x = list.scrollLeft;
  list.dataset.overflow = max <= 1 ? "" : x < 2 ? "end" : x >= max - 2 ? "start" : "both";
};
window.addEventListener("resize", () => document.querySelectorAll(".tab-list").forEach(tabOverflow));

// ----------------------------------------------------------- per-page setup
const initReview = (form) => {
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
};

const initArtists = (grid) => {
  const input = document.getElementById("artist-filter");
  const letters = document.getElementById("artist-letters");
  const none = document.getElementById("artist-none");
  const cards = [...grid.querySelectorAll(".tile")];
  // The filter and the letter are kept in the address (?q=…#letter), so they are still
  // there after opening an artist and coming back, by the Back button or the page's link.
  let letter = decodeURIComponent(location.hash.slice(1));
  input.value = new URLSearchParams(location.search).get("q") || "";
  const remember = () => {
    const q = input.value.trim();
    const url = location.pathname + (q ? `?q=${encodeURIComponent(q)}` : "") + (letter ? `#${encodeURIComponent(letter)}` : "");
    history.replaceState(history.state, "", url);
    try { sessionStorage.setItem("beetsui-artists", url); } catch { /* the address still has it */ }
  };

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
  input.addEventListener("input", () => { remember(); apply(); });
  letters.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-letter]");
    if (!a) return;
    e.preventDefault();
    letter = a.dataset.letter;
    remember();
    apply();
  });
  remember();
  apply();

  // Rename one ticked artist, or merge several into one name (typed or picked).
  const bar = document.getElementById("merge-bar");
  const target = document.getElementById("merge-target");
  const names = document.getElementById("merge-names");
  const count = document.getElementById("merge-count");
  const verb = document.getElementById("merge-verb");
  const button = document.getElementById("merge-button");
  const form = document.getElementById("merge-form");
  const confirmText = () => {
    const picked = [...grid.querySelectorAll('input[name="name"]:checked')].map((p) => p.value);
    const name = target.value.trim() || "the new name";
    const one = picked.length === 1;
    form.dataset.confirm = (one ? `Rename ${picked[0]} to ${name}?` : `Merge ${picked.length} artists into ${name}?`)
      + " Tags are rewritten and files move to that artist's folder. You can undo it from the job page.";
    form.dataset.confirmButton = one ? "Rename artist" : `Merge ${picked.length} artists`;
  };
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
  target.addEventListener("input", confirmText);
  grid.addEventListener("change", update);
  update();
};

const init = (root) => {
  // Fields marked data-autofocus take focus only with a mouse or trackpad: on a phone
  // it would pop the keyboard up over the page.
  const field = within(root, "[data-autofocus]")[0];
  if (field && window.matchMedia("(pointer: fine)").matches) field.focus({ preventScroll: true });

  for (const form of within(root, "#import-form")) initReview(form);
  for (const grid of within(root, "#artist-grid")) initArtists(grid);
  // An artist's Back link returns to the list as it was left: its filter and letter.
  for (const a of within(root, 'a.back[href="/library/artists"]')) {
    try {
      const url = sessionStorage.getItem("beetsui-artists") || "";
      if (!url.startsWith("/library/artists") || url === a.getAttribute("href")) continue;
      a.setAttribute("href", url);
      // htmx read the link's address when the page came in: have it read the new one.
      if (window.htmx) htmx.process(a);
    } catch { /* the plain list */ }
  }
  for (const list of within(root, ".tab-list")) {
    const current = list.querySelector('[aria-current="page"]');
    if (current) list.scrollLeft = current.offsetLeft + current.offsetWidth - list.clientWidth + 24;
    list.addEventListener("scroll", () => tabOverflow(list), { passive: true });
    tabOverflow(list);
  }
};

if (window.htmx) htmx.onLoad(init);
else document.addEventListener("DOMContentLoaded", () => init(document.body));

// ---------------------------------------------------------------- navigation
if (window.htmx) {
  // Back and forward fetch the page again instead of restoring a stale copy.
  htmx.config.historyCacheSize = 0;

  document.addEventListener("htmx:beforeSwap", (e) => {
    const d = e.detail;
    if (!d.boosted) return;
    // A page change cross-fades (View Transitions) and starts at the top.
    d.swapOverride = "innerHTML transition:true show:window:top";
    // An error page still replaces the page; anything that is not a page says what failed.
    if (d.isError) {
      const html = (d.xhr.getResponseHeader("content-type") || "").includes("text/html");
      if (html) {
        d.shouldSwap = true;
        d.isError = false;
      } else {
        let text = d.xhr.responseText;
        try { text = JSON.parse(text).detail || text; } catch (_) { /* plain text */ }
        window.beetsToast({ title: `Couldn't open that (${d.xhr.status})`, text: String(text).slice(0, 200), kind: "destructive" });
      }
    }
  });
  // After a page change, focus moves to the new content for keyboard and screen reader users.
  document.addEventListener("htmx:afterSettle", (e) => {
    if (!e.detail.boosted) return;
    const main = document.getElementById("main");
    if (main && (!document.activeElement || document.activeElement === document.body)) main.focus({ preventScroll: true });
  });
  document.addEventListener("htmx:sendError", () => {
    window.beetsToast({ title: "Can't reach beets-ui", text: "Check the connection and try again.", kind: "destructive" });
  });
}

// ------------------------------------------------------------- live updates
// One event stream tells the page when jobs, the inbox or downloads change; parts
// marked data-refresh="<topic>" then reload themselves. Their slow timers stay as a
// fallback for when the stream is down.
(() => {
  if (!window.EventSource || !window.htmx || !document.getElementById("main")) return;
  const stream = new EventSource("/events");
  for (const topic of ["jobs", "inbox", "downloads", "wanted"]) {
    stream.addEventListener(topic, () => {
      if (document.hidden) { pending.add(topic); return; }
      refresh(topic);
    });
  }
  const pending = new Set();
  const refresh = (topic) => {
    for (const el of document.querySelectorAll(`[data-refresh~="${topic}"]`)) htmx.trigger(el, "refresh");
  };
  // A hidden tab catches up when it is shown again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    pending.forEach(refresh);
    pending.clear();
  });
})();

// --------------------------------------------------------------- install
// The service worker only makes beets-ui installable as an app; it caches nothing.
if ("serviceWorker" in navigator && document.getElementById("main")) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
