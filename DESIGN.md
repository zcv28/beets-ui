---
name: beets-ui
description: A calm, neutral working tool for getting, reviewing and keeping a music library.
colors:
  background: "oklch(1 0 0)"
  foreground: "oklch(0.145 0 0)"
  card: "oklch(1 0 0)"
  primary: "oklch(0.205 0 0)"
  primary-foreground: "oklch(0.985 0 0)"
  muted: "oklch(0.97 0 0)"
  muted-foreground: "oklch(0.556 0 0)"
  accent: "oklch(0.97 0 0)"
  border: "oklch(0.922 0 0)"
  ring: "oklch(0.556 0 0)"
  sidebar: "oklch(0.985 0 0)"
  destructive: "oklch(0.577 0.245 27.325)"
  success: "oklch(0.448 0.119 151.328)"
  warning: "oklch(0.476 0.114 61.907)"
  info: "oklch(0.488 0.243 264.376)"
  dark-background: "oklch(0.145 0 0)"
  dark-foreground: "oklch(0.985 0 0)"
  dark-card: "oklch(0.205 0 0)"
  dark-primary: "oklch(0.922 0 0)"
  dark-muted: "oklch(0.269 0 0)"
  dark-muted-foreground: "oklch(0.708 0 0)"
  dark-border: "oklch(0.275 0 0)"
  dark-destructive: "oklch(0.704 0.191 22.216)"
  dark-success: "oklch(0.792 0.209 151.711)"
  dark-warning: "oklch(0.828 0.189 84.429)"
  dark-info: "oklch(0.707 0.165 254.624)"
typography:
  h1:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Noto Sans, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  h2:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  h3:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  hint:
    fontFamily: "ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.5
  data:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
rounded:
  sm: "0.375rem"
  md: "0.5rem"
  lg: "0.625rem"
  xl: "0.875rem"
spacing:
  unit: "0.25rem"
  page-x: "2rem"
  page-x-phone: "1rem"
  bar-gap: "0.5rem"
  section: "2rem"
components:
  button-default:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
    height: "2.25rem"
  button-outline:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    height: "2.25rem"
  button-ghost:
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
  card:
    backgroundColor: "{colors.card}"
    rounded: "{rounded.xl}"
    padding: "1rem"
  table-head:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
  empty-state:
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.xl}"
    padding: "2rem 1.5rem"
  sidebar:
    backgroundColor: "{colors.sidebar}"
    width: "16rem"
---

## Overview

beets-ui is an Operate surface end to end: one owner, at a desktop, working through a library session (see PRODUCT.md). The look is the shadcn-derived design system shipped in `src/beets_ui/static/ds/`: a neutral greyscale with colour reserved for status, system fonts, quiet borders and soft shadows. Ambition shows as precision and calm, not decoration. Light is the default; dark follows the system, or the owner's choice from the theme button in the sidebar footer (Light, Dark, Same as the browser; kept in a cookie so pages arrive in it).

Cascade: `@layer base, components, app` (declared in `ds/tokens.css`). `ds/bundle.css` is the components layer; `static/app.css` adds element defaults (base) and the app's own pieces (app), built only from the tokens.

## Colors

Neutral greys carry everything; hue appears only as meaning.

- **Status hues** (`success`, `warning`, `info`, `destructive`) colour status dots, alert borders and icons, and progress bars for a finished state. Text stays in the foreground colour beside them.
- **Destructive** controls on a page are outlines (or ghost icons) with a red label; only the confirming dialog button is filled. In dark mode, filled destructive buttons become outlines, because the filled version reads below 3:1.
- **Muted foreground** is for hints and secondary text only. Table headers sit on `muted` and use the full foreground (muted on muted reads 4.3:1).
- The light focus ring is `oklch(0.556 0 0)`, overriding the design system's lighter ring to reach 3:1.

## Typography

System sans for everything (`--font-sans`); monospace only for data: numbers in tables, durations, sizes, paths, IDs and codes. Sizes are small and dense: body 14px, hints 13px, h1 24px, h2 20px, h3 16px, with negative tracking on headings only. Names in Japanese, Chinese or Korean get `lang` attributes (the `|lang` filter) so they render with the right font.

## Layout

- Sidebar (16rem) plus a content column capped at 80rem with 2rem side padding (1rem on phones). Below 768px the sidebar becomes a top bar and a bottom-reaching sheet.
- Page anatomy: a back link where there is a parent, h1 with a muted description line, an optional tab list, then content in bars (`.bar`, `.toolbar`) and tables. The page header keeps one height with or without the description line, so tabs never move between sibling pages. A page reached from several places (a catalogue artist or release) links back to where you came from.
- Tables are the default container for lists. On phones, tables with actions use `.stack-sm`: each row stacks, the main cell first and the actions on the right.
- Links in tables: a row's bold name is its link and underlines on hover; secondary links (an artist, the release a track is from) carry a quiet underline so they read as links.
- Long-running pages use a sticky action bar at the bottom (`.sticky-bar`) for the primary decision (import, download).

## Elevation & Depth

Flat by default. Borders separate; shadows are soft, offset and small (`--shadow-xs` to `--shadow-lg`), used for popovers, menus, toasts, the sticky bar and the player. No glow, no zero-offset halos.

## Shapes

Radii from the tokens: inputs and buttons `md` (0.5rem), cards, boxes, tables and empty states `xl` (0.875rem), badges round. Album covers are square with `sm` radius; artist pictures are circles.

## Components

- **Buttons:** `default` (filled, one primary per area), `outline` (secondary), `ghost` (tertiary, row actions), sizes `xs`/`sm`/default, icon-only with an `aria-label`. `data-tone="destructive"` for destructive triggers. Row actions are `outline`; in a list of rows that each need a decision, only the first row's action is filled. A search bar's submit is `outline` everywhere.
- **Status:** `.status` is a coloured dot and a word (`data-status` success, warning, info, destructive). Job, inbox and request states use it. A request's state links to its page. Ownership of a catalogue release is one of three: **In your library** (success; by ID, or artist and title), **Maybe your …** (warning; another edition or credit, or the same artist and year under its other script), or nothing. A maybe names the library album it suspects, linked, on the row itself ("Maybe your Elma (2019)"); it keeps its Get, but bulk Get leaves it out.
- **Alerts:** `alert(kind, title)` macro: icon, title, body, optional bar of actions. For things that need attention, never for routine empty results.
- **Empty states:** a dashed, neutral box with one sentence saying what will appear and how. Never a status colour.
- **Toasts:** bottom right; successes leave after 4s (8s with Undo), failures stay. Server responses send them with an `HX-Trigger: {"toast": …}` header.
- **Menus:** `details.menu` with `.menu-item`s (label plus a hint line); "More actions" and "Tidy up".
- **Tabs:** line tabs under the page header for sibling views (Get: Missing / Discover / Coming in / Find; Library: Albums / Artists / Tracks / Covers / Artist pictures; Insights). A section page's h1 is the section; the tab names the page in `<title>`.
- **Row menus:** `row_menu()` macro, a ghost "more" button and a `[popover]` menu in the top layer, so a scrolling table never clips it. For a row's secondary actions; the row's one main action stays visible. The two sit together in `.row-actions`, right-aligned on one line, whether the main action is a button or the request's state.
- **Release path:** `release_path(stage)` macro, Missing → Getting → Review → In your library, on a request's page and on the review page of a folder that came from one.
- **Tiles:** square cover, title, muted sub-lines; used for album and artist grids.
- **Covers in lists:** `cover()` macro, a 48px square that leads a row about a release (Missing, Discover, Coming in, Find, the inbox). A release with no cover, or a row with no release yet, shows the quiet placeholder so rows stay aligned.
- **Icons:** Lucide, inline SVG through the `icon()` macro, 16px, stroke 2.

## Do's and Don'ts

- Do lead every page with what needs the owner, then what is in progress, then the rest.
- Do say what a control does ("Get a better copy", "Download this one"), in sentence case.
- Do keep the download source unnamed outside its details line, its error and the home status line.
- Do update rows in place (htmx) with a toast, and offer Undo where an undo exists.
- Do confirm any action that creates more than one request, with the count and "anything it isn't sure of waits for you", then offer Undo in its message.
- Don't use colour for anything but status; don't put monospace on prose.
- Don't use cards as page structure, hero metrics, eyebrows above headings, or gradient text.
- Don't make a modal for anything but confirming a destructive action.
