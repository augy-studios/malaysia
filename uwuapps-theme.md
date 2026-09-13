# Theme system reference (portable)

Canonical source: the uwuFlights PWA. This file is the drop-in spec for
every other project. Copy it into the repo as `uwuapps-theme.md` (or paste it
into the Claude Code prompt) and follow it exactly. Update this file only
when the canonical implementation changes.

Two independent axes that combine freely:
**brand colour** (7 options, `data-color-theme` on `<html>`) and
**mode** (light/dark, `data-mode` on `<html>`). 14 valid combinations.

Mode has three settings a person can pick from and two values it can resolve
to. `data-mode` is only ever `light` or `dark`; the third setting, `time`,
is a rule for choosing between them. See Time based mode below.

| Axis | Attribute | Values |
|---|---|---|
| Brand colour | `data-color-theme` | 7 swatches, see below |
| Mode | `data-mode` | `light`, `dark` |
| Mode preference | `data-mode-preference` | `light` (default), `dark`, `time` |

`data-color-theme` and `data-mode` are always present, both on `<html>`.
Every colour block selects on those two and on nothing else.

`data-mode-preference` records which of the three settings the person chose.
No stylesheet reads it. It is there so the theme modal can show the right
button pressed after a reload, and so anything inspecting the page can tell
"dark because you asked" apart from "dark because it is nine in the evening".

## Time based mode

A third setting on the mode axis: follow the device clock. Light from 09:00
up to but not including 18:00, dark the rest of the day.

Optional per app. An app that does not want it ships the two button toggle
and nothing below applies. An app that does want it must take all of it,
because the parts that look skippable are the ones that break.

**The split that makes it safe.** Two things the naive version conflates:

| | What it is | Values |
|---|---|---|
| Preference | what the person chose | `light`, `dark`, `time` |
| Mode | what the document is in | `light`, `dark` |

`getModePreference()` returns the first and decides which button is pressed.
`getStoredMode()` resolves it and is what the theme button's icon and
`withLightMode`-style helpers want. Not one line of colour CSS changes.

**The hours are duplicated in the pre-paint script, and have to be.**
`theme.js` is a module and runs after first paint, so resolving only there
shows an evening reader a white page that turns dark a moment later. The
pre-paint script cannot import anything, so the two hours appear in both
places. Change them together, and say so in a comment in both.

**A tab left open across a boundary re-resolves itself.** Somebody who opens
the app at 17:55 and looks up at 18:10 should not still be in light mode.
Schedule one timer to the next 09:00 or 18:00 rather than polling, so an idle
tab costs one wakeup rather than 1,440 a day. Also re-check on
`visibilitychange`: a device that sleeps through the boundary fires its
timer late, and coming back to the tab is the moment to notice.

When the resolved mode does change, dispatch `uwu:modechange` on `document`
with `{ mode, preference }`, so the theme modal can redraw rather than
showing the answer from before dinner.

**The device clock is the only input.** No timezone is asked for, sent, or
stored, and there is no sunrise or sunset lookup, which would need a
location. Somebody's evening is their evening wherever they are.

**Accessibility.** This is the one thing in the theme system that changes
the page without the person touching anything. It is opt in, it never moves
focus, and it never animates the transition beyond the ordinary token
change. An app adding it should include it in its accessibility pass rather
than assume the two button toggle's result carries over.

## Non-negotiable rules

- **No gradients, orbs, or blobs.** The page background is one flat colour:
  `color-mix(in srgb, var(--brand) 10%, var(--bg))` on `body`. If something
  seems to call for a gradient or a soft blob, use a flat tint or a glass
  card.
- **Glassmorphism, not flat cards.** Card-like surfaces use the `.glass`
  primitive. Smaller nested controls (buttons, badges, chips) use
  `--surface-strong` directly so they read one level above the card.
- **Never hardcode a hex in component CSS.** Reference the variable so it
  stays correct across all 14 combinations. The one allowed exception is a
  fixed-meaning accent such as the footer heart (`#34c759`), which must
  read as a heart regardless of brand colour.
- **Font is Jua everywhere**, set once on `*` with a `sans-serif` fallback.
  No per-component `font-family`.
- **No emoji, ever.** All icons are inline SVG from `js/icons.js`,
  referenced as `data-icon="name"` and hydrated by `hydrateIcons()`.
- **No em dashes** in UI copy, code comments, or docs. Use a comma,
  semicolon, colon, or period.
- **Light mode is the default.** Do not read `prefers-color-scheme` on
  first load. Users opt into dark mode explicitly in the theme modal.
- **Every text pair meets WCAG AA**, 4.5:1 for body text and 3:1 for large
  text and UI boundaries, in all 14 combinations. Text on a brand tint uses
  `--on-brand`, never `--brand-ink`: brand swatches are pale in both modes,
  so `--brand-ink` on a tint only reaches 1.9:1 in dark mode.
- **Everything that opens, closes, or switches state animates**, 150-220ms.
  Show/hide is a single `.hidden` class flip in JS; all timing lives in CSS.
  Respect `prefers-reduced-motion: reduce`.

## Brand colours

| id | Label | `--brand` |
|---|---|---|
| `classic` (default) | Classic | `#ccffcc` |
| `not-green-1` | Not green 1 | `#ffcccc` |
| `not-green-2` | Not green 2 | `#ccccff` |
| `not-green-3` | Not green 3 | `#ffffcc` |
| `not-green-4` | Not green 4 | `#ffccff` |
| `not-green-5` | Not green 5 | `#ccffff` |
| `really-light-green` | Really really light green | `#ffffff` |

Each also sets `--brand-rgb` as comma-separated `r, g, b` for `rgba()` mixes.

## Mode tokens

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--bg` | `#eef2f0` | `#0c100e` | page background base, before brand tint |
| `--surface` | `rgba(255,255,255,.55)` | `rgba(255,255,255,.06)` | `.glass` background |
| `--surface-strong` | `rgba(255,255,255,.78)` | `rgba(255,255,255,.11)` | buttons, active tab, badges, modal |
| `--surface-border` | `rgba(255,255,255,.65)` | `rgba(255,255,255,.14)` | borders, dividers |
| `--ink` | `#121815` | `#eef2ef` | primary text |
| `--muted` | `#5b665f` | `#9aa7a0` | secondary text |
| `--brand-ink` | `#1f6b3d` | `#bff5cf` | headings, links, active states |
| `--on-brand` | `#121815` | `#121815` | text and icons **on a brand tint** |
| `--shadow` | `0 10px 30px rgba(20,40,30,.10)` | `0 10px 34px rgba(0,0,0,.5)` | `.glass` box-shadow |
| `--ok` | `#0f7234` | `#4ade80` | success dot/text |
| `--warn` | `#a24b08` | `#fbbf24` | warning dot/text |
| `--busy` | `#627288` | `#94a3b8` | in-progress dot |
| `--error` | `#b91c1c` | `#fa9696` | error text/toast |

`--brand-ink` stays green-tinted on purpose. It is readable against all
seven brand tints as a **foreground on glass**, so it does not change with
the swatch.

`--on-brand` is the same dark ink in both modes. Brand swatches are pale
pastels regardless of mode, so anything sitting on a brand tint needs dark
text in dark mode too. Using `--brand-ink` there gives 1.9:1.

### Brand-derived fills

| Token | Value | Used for |
|---|---|---|
| `--brand-fill` | `color-mix(in srgb, var(--brand) 55%, var(--surface-strong))` | active tabs, primary buttons, hover fills, toasts |
| `--brand-fill-strong` | `color-mix(in srgb, var(--brand) 75%, var(--surface-strong))` | the hover step above `--brand-fill`, badges |

**Any fill that tracked the brand swatch before the mode axis existed must
still track it after.** If a surface changed colour when the user picked a
different swatch, it keeps doing that in both light and dark. Use
`--brand-fill` rather than a flat `--surface-strong`, and pair it with
`--on-brand` text. This is the recipe `.mode-btn.active` uses; naming it
keeps every brand-tracking surface on one definition instead of repeating
the `color-mix` inline.

Both fills resolve against `--surface-strong`, so they darken correctly in
dark mode while the brand hue stays recognisable, and `--on-brand` clears
4.5:1 on both in all 14 combinations.

### Secondary text on a strong surface

| Token | Value | Used for |
|---|---|---|
| `--muted-strong` | `color-mix(in srgb, var(--ink) 70%, transparent)` | secondary text and icons sitting on `--surface-strong` |

`--muted` is tuned against `--surface` and the page background. When a
`--surface-strong` control is nested inside a `.glass` card, the two white
overlays stack and lighten the backdrop, and `--muted` drops to **3.5-3.8:1
in dark mode** across all seven swatches. Use `--muted-strong` for
secondary text on chips, badges, input placeholders and icon buttons that
sit on `--surface-strong`. It clears 4.86:1 dark and 6.62:1 light while
still reading a step below `--ink`.

Set `opacity: 1` on placeholders using it. A stacked `opacity` multiplier
on top of the token puts the pair back under AA.

The exception is **foregrounds**, which do not track the brand. Text,
links, small icons and focus borders stay on `--brand-ink`. A pale pastel
foreground cannot reach 4.5:1 on a pale surface, which is the whole reason
`--brand-ink` is fixed. Brand tracking applies to fills and to the
`--on-brand` text sitting on them.

The status colours are tuned so that each reads at 4.5:1 against a 14% tint
of itself, which is the standard banner and badge treatment:

```css
.banner.error {
  background: color-mix(in srgb, var(--error) 14%, transparent);
  color: var(--error);
}
```

## Shape conventions

- `.glass` surfaces: `border-radius: 20px`
- Cards and modals: `16-20px`
- Chip-style buttons: `999px`; rectangular buttons: `12-14px`
- Icon buttons: `42px` square, `34px` with `.small`
- Layout column: `max-width: 720px`, padding `14px`, `10px` under 480px
- Single breakpoint: `@media (max-width: 480px)`

---

## 1. CSS to add (`css/theme.css`, or the top of `style.css`)

```css
/* ---- base ---- */
* {
  padding: 0;
  margin: 0;
  box-sizing: border-box;
  font-family: "Jua", "Segoe UI", sans-serif;
  -webkit-user-select: none;
  user-select: none;
}

html, body { height: 100%; }
input, textarea { user-select: text; }

:link, :visited { color: var(--brand-ink); }
a:hover { text-decoration: none; }

button {
  cursor: pointer;
  border: none;
  background: none;
  color: inherit;
}
button:disabled { cursor: not-allowed; opacity: 0.6; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
}

/* ---- brand colour tokens (7 swatches) ---- */
:root,
:root[data-color-theme="classic"] {
  --brand: #ccffcc;
  --brand-rgb: 204, 255, 204;
}
:root[data-color-theme="not-green-1"] { --brand: #ffcccc; --brand-rgb: 255, 204, 204; }
:root[data-color-theme="not-green-2"] { --brand: #ccccff; --brand-rgb: 204, 204, 255; }
:root[data-color-theme="not-green-3"] { --brand: #ffffcc; --brand-rgb: 255, 255, 204; }
:root[data-color-theme="not-green-4"] { --brand: #ffccff; --brand-rgb: 255, 204, 255; }
:root[data-color-theme="not-green-5"] { --brand: #ccffff; --brand-rgb: 204, 255, 255; }
:root[data-color-theme="really-light-green"] { --brand: #ffffff; --brand-rgb: 255, 255, 255; }

/* ---- light / dark surface tokens ---- */
:root,
:root[data-mode="light"] {
  --bg: #eef2f0;
  --surface: rgba(255, 255, 255, 0.55);
  --surface-strong: rgba(255, 255, 255, 0.78);
  --surface-border: rgba(255, 255, 255, 0.65);
  --ink: #121815;
  --muted: #5b665f;
  --shadow: 0 10px 30px rgba(20, 40, 30, 0.10);
  --brand-ink: #1f6b3d;
  --ok: #0f7234;
  --warn: #a24b08;
  --busy: #627288;
  --error: #b91c1c;
}

:root[data-mode="dark"] {
  --bg: #0c100e;
  --surface: rgba(255, 255, 255, 0.06);
  --surface-strong: rgba(255, 255, 255, 0.11);
  --surface-border: rgba(255, 255, 255, 0.14);
  --ink: #eef2ef;
  --muted: #9aa7a0;
  --shadow: 0 10px 34px rgba(0, 0, 0, 0.5);
  --brand-ink: #bff5cf;
  --ok: #4ade80;
  --warn: #fbbf24;
  --busy: #94a3b8;
  --error: #fa9696;
}

/* Same in both modes: brand tints are pale whatever the mode. */
:root {
  --on-brand: #121815;
  /* Brand-derived fills. Any surface that tracked the brand swatch before
     still tracks it, in both modes. Text on these is always --on-brand. */
  --brand-fill: color-mix(in srgb, var(--brand) 55%, var(--surface-strong));
  --brand-fill-strong: color-mix(in srgb, var(--brand) 75%, var(--surface-strong));
  /* Secondary text on --surface-strong. Plain --muted only reaches 3.6:1
     there in dark mode, since the strong surface lightens the backdrop. */
  --muted-strong: color-mix(in srgb, var(--ink) 70%, transparent);
}

/* flat, static colour derived from theme, no gradients */
body {
  background-color: color-mix(in srgb, var(--brand) 10%, var(--bg));
  color: var(--ink);
  min-height: 100vh;
  overflow-x: hidden;
  transition: background-color 0.25s ease, color 0.25s ease;
}

/* ---- glass primitive ---- */
.glass {
  background: var(--surface);
  backdrop-filter: blur(18px) saturate(150%);
  -webkit-backdrop-filter: blur(18px) saturate(150%);
  border: 1px solid var(--surface-border);
  border-radius: 20px;
  box-shadow: var(--shadow);
}

/* ---- icon button (theme trigger, modal close) ---- */
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 14px;
  background: var(--surface-strong);
  border: 1px solid var(--surface-border);
  color: var(--ink);
  transition: transform 0.15s ease, background 0.15s ease;
}
.icon-btn:hover { transform: translateY(-1px); }
.icon-btn svg { width: 20px; height: 20px; }
.icon-btn.small { width: 34px; height: 34px; }
.icon-btn.small svg { width: 16px; height: 16px; }

/* ---- modal shell ---- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  background: rgba(10, 15, 12, 0.35);
  padding: 12px;
  opacity: 1;
  visibility: visible;
  transition: opacity 0.2s ease, visibility 0s linear 0s;
}
.modal-backdrop.hidden {
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity 0.18s ease, visibility 0s linear 0.18s;
}

@media (min-width: 640px) {
  .modal-backdrop { align-items: center; }
}

.modal {
  width: 100%;
  max-width: 420px;
  max-height: 85vh;
  overflow-y: auto;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--surface-strong);
  opacity: 1;
  transform: translateY(0) scale(1);
  transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.18s ease;
}
.modal-backdrop.hidden .modal {
  opacity: 0;
  transform: translateY(14px) scale(0.97);
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.modal-head h2 { font-size: 1.1rem; color: var(--brand-ink); }

/* Section labels inside the modal: "Mode", "Brand colour". */
.modal-section-label {
  font-size: 0.85rem;
  color: var(--muted);
  margin-top: 4px;
}

body.modal-open { overflow: hidden; }

/* ---- mode toggle ---- */
.mode-toggle {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
  padding: 4px;
  border-radius: 14px;
  background: var(--surface);
}
.mode-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px;
  border-radius: 10px;
  font-size: 0.85rem;
  color: var(--muted);
  transition: background 0.15s ease, color 0.15s ease;
}
.mode-btn svg { width: 16px; height: 16px; }
.mode-btn.active {
  background: var(--brand-fill);
  color: var(--on-brand);
}

/* Time based mode only. Light and dark stay side by side and the third
   spans the row beneath them: two equal columns would put the longer label
   in a box too narrow to read at 320px, and the full width row also signals
   this is a different kind of choice, a rule for picking a mode rather than
   a mode. Omit both rules in an app that ships the two button toggle. */
.mode-btn-wide { grid-column: 1 / -1; }
.mode-note {
  margin-top: 0.5rem;
  font-size: 0.85rem;
  color: var(--muted);
}

/* ---- swatch grid ---- */
.swatch-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}
.swatch {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-radius: 14px;
  background: var(--surface);
  border: 1px solid var(--surface-border);
  font-size: 0.78rem;
  text-align: left;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
/* Ringed in its own colour, plus a containment ring. On a light modal the
   pale swatches reach only 1:1 against the surface on their own, so the
   outer ring is what actually carries the active state, at 3.4:1. */
.swatch.active {
  border-color: var(--swatch-color);
  box-shadow:
    0 0 0 2px var(--swatch-color),
    0 0 0 3px color-mix(in srgb, var(--ink) 50%, transparent);
}
.swatch-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--swatch-color);
  border: 1px solid color-mix(in srgb, var(--ink) 30%, transparent);
  flex-shrink: 0;
}

@media (max-width: 480px) {
  .swatch-grid { grid-template-columns: 1fr; }
}
```

## 2. `js/theme.js` (copy verbatim, change `APP_KEY` only)

```js
// Theme system: 7 brand colour swatches + light/dark mode.
// Default is always light + classic (#ccffcc), regardless of OS preference.
// Once the user picks something, it is persisted.

const APP_KEY = "appname"; // change per project, e.g. "uwuflights"

export const COLOR_THEMES = [
  { id: "classic", label: "Classic", hex: "#ccffcc" },
  { id: "not-green-1", label: "Not green 1", hex: "#ffcccc" },
  { id: "not-green-2", label: "Not green 2", hex: "#ccccff" },
  { id: "not-green-3", label: "Not green 3", hex: "#ffffcc" },
  { id: "not-green-4", label: "Not green 4", hex: "#ffccff" },
  { id: "not-green-5", label: "Not green 5", hex: "#ccffff" },
  { id: "really-light-green", label: "Really really light green", hex: "#ffffff" },
];

const STORAGE_KEY_COLOR = `${APP_KEY}.colorTheme`;
const STORAGE_KEY_MODE = `${APP_KEY}.mode`;

function hexToRgb(hex) {
  const n = parseInt(hex.replace("#", ""), 16);
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

export function getStoredColorTheme() {
  return localStorage.getItem(STORAGE_KEY_COLOR) || "classic";
}

/* Mode preference and mode are different things. The preference is what the
   person chose and can be "time"; the mode is what the document is in and
   is only ever light or dark. An app that does not ship the time based
   option can drop MODE_PREFERENCES down to two entries and delete
   everything under "Keeping the time based mode honest" below. */

export const MODE_PREFERENCES = ["light", "dark", "time"];

/* The daylight window. Duplicated in the pre-paint script in every head,
   which has to resolve this before first paint and cannot import anything.
   Change both together. */
export const LIGHT_FROM_HOUR = 9;
export const LIGHT_UNTIL_HOUR = 18;

export function getModePreference() {
  const v = localStorage.getItem(STORAGE_KEY_MODE);
  return MODE_PREFERENCES.includes(v) ? v : "light";
}

export function isDaylightHours(now = new Date()) {
  const hour = now.getHours();
  return hour >= LIGHT_FROM_HOUR && hour < LIGHT_UNTIL_HOUR;
}

export function resolveMode(preference) {
  if (preference === "time") return isDaylightHours() ? "light" : "dark";
  return preference === "dark" ? "dark" : "light";
}

// The mode the document is in right now, resolved. What the theme button
// icon and anything else reading the active mode wants.
export function getStoredMode() {
  return resolveMode(getModePreference());
}

export function applyColorTheme(id) {
  const theme = COLOR_THEMES.find((t) => t.id === id) || COLOR_THEMES[0];
  document.documentElement.setAttribute("data-color-theme", theme.id);
  document.documentElement.style.setProperty("--brand", theme.hex);
  document.documentElement.style.setProperty("--brand-rgb", hexToRgb(theme.hex));
  localStorage.setItem(STORAGE_KEY_COLOR, theme.id);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme.hex);
  return theme;
}

export function applyMode(preference) {
  const chosen = MODE_PREFERENCES.includes(preference) ? preference : "light";
  const resolved = resolveMode(chosen);

  document.documentElement.setAttribute("data-mode", resolved);
  document.documentElement.setAttribute("data-mode-preference", chosen);
  localStorage.setItem(STORAGE_KEY_MODE, chosen);

  scheduleModeCheck();

  return resolved;
}

/* Keeping the time based mode honest while the page stays open. Delete this
   block in an app that ships the two button toggle. */

let modeTimer = null;
let watchingVisibility = false;

// Milliseconds until the next 09:00 or 18:00, whichever comes first.
function msUntilNextBoundary(now = new Date()) {
  const next = new Date(now);
  next.setMinutes(0, 0, 0);

  const hour = now.getHours();
  if (hour < LIGHT_FROM_HOUR) {
    next.setHours(LIGHT_FROM_HOUR);
  } else if (hour < LIGHT_UNTIL_HOUR) {
    next.setHours(LIGHT_UNTIL_HOUR);
  } else {
    next.setDate(next.getDate() + 1);
    next.setHours(LIGHT_FROM_HOUR);
  }

  // A second of slack, so a timer that fires a fraction early does not land
  // back in the hour it just left and reschedule itself in a tight loop.
  return Math.max(1000, next.getTime() - now.getTime() + 1000);
}

function scheduleModeCheck() {
  if (modeTimer !== null) {
    clearTimeout(modeTimer);
    modeTimer = null;
  }

  if (getModePreference() !== "time") return;

  modeTimer = setTimeout(() => {
    modeTimer = null;
    refreshTimeMode();
  }, msUntilNextBoundary());

  if (!watchingVisibility && typeof document !== "undefined") {
    watchingVisibility = true;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshTimeMode();
    });
  }
}

export function refreshTimeMode() {
  if (getModePreference() !== "time") return;

  const resolved = resolveMode("time");
  const current = document.documentElement.getAttribute("data-mode");

  if (resolved !== current) {
    document.documentElement.setAttribute("data-mode", resolved);
    document.dispatchEvent(
      new CustomEvent("uwu:modechange", {
        detail: { mode: resolved, preference: "time" },
      })
    );
  }

  scheduleModeCheck();
}

export function initTheme() {
  applyColorTheme(getStoredColorTheme());
  // The preference, not the resolved mode. Passing the resolved one would
  // quietly rewrite a stored "time" into "dark" the first evening.
  applyMode(getModePreference());
}
```

## 3. `js/icons.js` (add these three if missing)

`viewBox "0 0 24 24"`, `stroke="currentColor"`, `stroke-width="1.8"`,
round caps and joins, `fill="none"`. Icons inherit colour via
`currentColor`, so never hardcode fill or stroke colours.

```js
export const icons = {
  sun: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>`,
  moon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>`,
  clock: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>`,
};

export function icon(name) {
  return icons[name] || "";
}
```

## 4. `js/ui.js` helpers (add if missing)

```js
import { icon } from "./icons.js";

// Safe to call repeatedly; re-renders when data-icon changes.
export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    const name = el.dataset.icon;
    if (el.dataset.iconRendered === name) return;
    el.innerHTML = icon(name);
    el.dataset.iconRendered = name;
  });
}

export function openModal(id) {
  document.getElementById(id).classList.remove("hidden");
  document.body.classList.add("modal-open");
}

export function closeModal(id) {
  document.getElementById(id).classList.add("hidden");
  if (!document.querySelector(".modal-backdrop:not(.hidden)")) {
    document.body.classList.remove("modal-open");
  }
}
```

## 5. HTML

In `<head>`:

```html
<meta name="theme-color" content="#ccffcc" />
<link href="https://fonts.googleapis.com/css2?family=Jua&display=swap" rel="stylesheet" />
```

Trigger button in the topbar:

```html
<button class="icon-btn" id="themeBtn" type="button" aria-label="Theme">
  <span data-icon="sun"></span>
</button>
```

Modal, placed at the end of `<body>`, starting with `.hidden`:

```html
<div class="modal-backdrop hidden" id="themeModal">
  <div class="modal glass" role="dialog" aria-modal="true" aria-labelledby="themeModalTitle">
    <div class="modal-head">
      <h2 id="themeModalTitle">Theme</h2>
      <button class="icon-btn small" type="button" data-close-modal="themeModal" aria-label="Close">
        <span data-icon="close"></span>
      </button>
    </div>
    <p class="modal-section-label">Mode</p>
    <div class="mode-toggle" id="modeToggle">
      <button class="mode-btn" type="button" data-mode="light" aria-pressed="false"><span data-icon="sun"></span>Light</button>
      <button class="mode-btn" type="button" data-mode="dark" aria-pressed="false"><span data-icon="moon"></span>Dark</button>
      <button class="mode-btn mode-btn-wide" type="button" data-mode="time" aria-pressed="false"><span data-icon="clock"></span>Time-based</button>
    </div>
    <p class="mode-note" id="modeNote" hidden></p>
    <p class="modal-section-label">Brand colour</p>
    <div class="swatch-grid" id="swatchGrid"></div>
  </div>
</div>
```

The third mode button and `#modeNote` belong to the time based option only.
Drop both in an app that ships the two button toggle. Where it is shipped,
the button pressed state comes from `getModePreference()` and the note says
which mode the clock has currently chosen, so the two are never confused.

## 6. Wiring in `app.js`

If the project has no `app.js`, for example one script per page, put this
wiring at the bottom of `js/theme.js` instead, next to the state it drives.
If the project does not use ES modules, keep every function name and body
as written and publish them on `window` rather than adding `type="module"`,
which would silently defer scripts that are not written to be deferred.

```js
import { COLOR_THEMES, applyColorTheme, applyMode, getStoredColorTheme, getStoredMode, getModePreference, initTheme } from "./theme.js";
import { hydrateIcons, openModal, closeModal } from "./ui.js";

function buildThemeModal() {
  const grid = document.getElementById("swatchGrid");
  grid.innerHTML = COLOR_THEMES.map(
    (t) => `
      <button class="swatch" data-theme-id="${t.id}" style="--swatch-color:${t.hex}" type="button" aria-label="${t.label}">
        <span class="swatch-dot"></span>
        <span class="swatch-label">${t.label}</span>
      </button>`
  ).join("");

  syncThemeModalState();

  grid.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-id]");
    if (!btn) return;
    applyColorTheme(btn.dataset.themeId);
    syncThemeModalState();
  });

  document.getElementById("modeToggle").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-mode]");
    if (!btn) return;
    applyMode(btn.dataset.mode);
    syncThemeModalState();
  });

  // A tab left open across 09:00 or 18:00 re-resolves itself; redraw the
  // modal so the note and pressed state stay in step with the change.
  document.addEventListener("uwu:modechange", syncThemeModalState);
}

function syncThemeModalState() {
  const activeTheme = getStoredColorTheme();
  const activePreference = getModePreference();
  const resolvedMode = getStoredMode();

  document.querySelectorAll("#swatchGrid .swatch").forEach((el) => {
    el.classList.toggle("active", el.dataset.themeId === activeTheme);
  });
  document.querySelectorAll("#modeToggle .mode-btn").forEach((el) => {
    const isActive = el.dataset.mode === activePreference;
    el.classList.toggle("active", isActive);
    el.setAttribute("aria-pressed", String(isActive));
  });

  const note = document.getElementById("modeNote");
  if (note) {
    note.hidden = activePreference !== "time";
    if (activePreference === "time") {
      note.textContent = `Following the clock. Currently ${resolvedMode}.`;
    }
  }

  updateThemeButtonIcon();
}

function updateThemeButtonIcon() {
  const span = document.querySelector("#themeBtn [data-icon]");
  span.setAttribute("data-icon", getStoredMode() === "dark" ? "moon" : "sun");
  hydrateIcons(document.getElementById("themeBtn"));
}

function wireModals() {
  document.querySelectorAll("[data-close-modal]").forEach((btn) => {
    btn.addEventListener("click", () => closeModal(btn.dataset.closeModal));
  });
  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeModal(backdrop.id);
    });
  });
  document.getElementById("themeBtn").addEventListener("click", () => openModal("themeModal"));
}
```

Call order in `boot()`:

```js
initTheme();
hydrateIcons();
updateThemeButtonIcon();
buildThemeModal();
wireModals();
```

Selecting a swatch or mode updates the modal in place. It never closes the
modal; closing is a separate explicit action (close button, or clicking the
backdrop).

## 7. Optional: kill the first-paint flash

If the app's JS is deferred or bundled, add this before the stylesheet so
the saved theme lands before first paint. Keep `APP_KEY` in sync.

```html
<script>
  (function () {
    var k = "appname";
    var m = localStorage.getItem(k + ".mode") || "light";

    // Time based mode. Resolved here rather than in theme.js because
    // theme.js runs after first paint, and an evening reader would
    // otherwise watch a white page turn dark. Keep the two hours in step
    // with LIGHT_FROM_HOUR and LIGHT_UNTIL_HOUR in theme.js. Omit this
    // block in an app that ships the two button toggle.
    document.documentElement.setAttribute("data-mode-preference", m);
    if (m === "time") {
      var h = new Date().getHours();
      m = h >= 9 && h < 18 ? "light" : "dark";
    }

    var c = localStorage.getItem(k + ".colorTheme") || "classic";
    document.documentElement.setAttribute("data-mode", m);
    document.documentElement.setAttribute("data-color-theme", c);
  })();
</script>
```

This runs before first paint and guarantees `data-mode`, `data-mode-preference`,
and `data-color-theme` all exist, which is what lets every colour block
select on both axes without a fallback. Keep the key in sync with `APP_KEY`.

## localStorage

`.mode` stores the **preference**, not the resolved mode, so its value is
`light`, `dark`, or `time`. An app that does not ship the time based option
will never write `time`, and one that does must not write the resolved
value back over it: storing `dark` on a winter evening would silently end
the setting the person actually chose.

## Acceptance checklist

- [ ] All 7 swatches render, with the active one ringed in its own colour
- [ ] Light and dark both work with all 7 swatches, 14 combinations, no
      unreadable text
- [ ] Choice survives a reload, and the theme button icon matches the mode
- [ ] Fresh profile with OS set to dark still loads in light mode
- [ ] `meta[name="theme-color"]` updates when the swatch changes
- [ ] No hardcoded hex values left in component CSS
- [ ] Every text pair clears WCAG AA 4.5:1 in all 14 combinations, and
      anything on a brand tint uses `--on-brand`, not `--brand-ink`
- [ ] Every fill that tracked the brand swatch before still tracks it, in
      both modes, via `--brand-fill` or `--brand-fill-strong`
- [ ] The active `really-light-green` swatch is still visibly ringed
- [ ] No gradients, no emoji, no em dashes
- [ ] Modal opens and closes with animation, and with reduced motion enabled
      it is instant

Time based mode, where the app ships it:

- [ ] `data-mode` is still only ever `light` or `dark`, in the DOM and in
      localStorage's resolved reads
- [ ] `.mode` in localStorage holds the preference, and picking `time` and
      reloading in the evening still shows `time` pressed rather than `dark`
- [ ] No flash: loading in the evening with `time` chosen paints dark from
      the first frame, which means the pre-paint script resolves it
- [ ] The two hours in the pre-paint script match `LIGHT_FROM_HOUR` and
      `LIGHT_UNTIL_HOUR` in `theme.js`
- [ ] A tab held open across 09:00 or 18:00 changes mode by itself, and the
      theme modal's note updates with it
- [ ] A device woken from sleep past a boundary corrects on the next look at
      the tab, not on the next reload
- [ ] Changing the device clock or the timezone is reflected without
      clearing storage
- [ ] Nothing asks for, sends, or stores a timezone or a location