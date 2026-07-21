# Graveyard Squad — Style Guide

(250_IDEAS.md #178: internal style-guide documentation for the color tokens,
spacing scale, and type scale used across `roster.html`, `player.html`,
`link.html`, and `admin.html`.)

This is documentation only — the actual enforceable source of truth is
`static/theme.css`, loaded by all four live templates via
`<link rel="stylesheet" href="/static/theme.css">`. Each template keeps its
own local `:root` block for page-specific extras, but the canonical palette
below should not be redeclared with different values.

## Color tokens (idea #176)

| Token | Value | Use |
|---|---|---|
| `--gy-bg` | `#0b0c10` | Page background |
| `--gy-surface` | `#111418` | Topbar / raised chrome |
| `--gy-panel` | `#111820` | Card backgrounds |
| `--gy-border` | `#1e2530` | Borders, dividers |
| `--gy-accent` | `#00e5ff` | Brand accent — links, active states, highlights |
| `--gy-ok` | `#00e096` | Success / healthy status |
| `--gy-warn` | `#ffaa00` | Warning / degraded status |
| `--gy-err` | `#ff3d71` | Error / destructive status |
| `--gy-text` | `#c5c6c7` | Default body text |
| `--gy-dim` | `#888` | Secondary/meta text |
| `--gy-text-bright` | `#f0f0f0` | Off-white emphasis text (idea #171 — never pure `#fff`, which is jarring against this dark palette) |

Dark is the default and only "designed" theme; `html[data-theme="light"]`
(idea #187) is a lighter override of the same token set, toggled by the
🌙/☀️ button in each page's topbar and persisted to `localStorage['gy_theme']`.

## Spacing scale (idea #185)

| Token | Value |
|---|---|
| `--space-1` | 4px |
| `--space-2` | 8px |
| `--space-3` | 12px |
| `--space-4` | 16px |
| `--space-5` | 24px |
| `--space-6` | 32px |

The pre-existing per-page CSS has ad hoc 8/10/12/14/16/20/24px values scattered
through it; a full rewrite of every declaration was judged too risky to do in
one pass, but any *new* component should use these tokens, and existing rules
should migrate to them opportunistically when touched for other reasons.

## Type scale (idea #178)

| Token | Value |
|---|---|
| `--text-xs` | 11px |
| `--text-sm` | 13px |
| `--text-md` | 15px |
| `--text-lg` | 20px |
| `--text-xl` | 28px |

Fonts: `--font-mono` (`Share Tech Mono`) for data/numbers/timestamps,
`--font-ui` (`Barlow Condensed`) for headings and UI chrome, system sans for
long-form body copy (e.g. `how_it_works.html`).

## Shared components (idea #177, #182)

- `.card` / `.card-title` — the consolidated panel pattern. `admin.html`'s
  `.diag-card` and the public pages' various dashboard panels were built
  independently but are visually the same thing; new panels should use `.card`
  rather than inventing another one-off class.
- `.btn` — a shared button base (padding, radius, type scale, focus/active
  handling). Existing button classes (`.btn-refresh`, `.btn-danger`,
  `.btn-strike`, `.btn-dm`, `.btn-save`, `.lfg-btn`, ...) are left in place to
  avoid regressions, but can be combined with `.btn` (`class="btn btn-refresh"`)
  and new buttons should prefer `.btn` plus a semantic color modifier over a
  fully bespoke class.
- `.empty-state` / `.empty-state-icon` / `.empty-state-title` / `.empty-state-sub`
  — idea #180's friendlier "nothing here yet" pattern (icon + heading + one
  line of context) instead of a bare text string.
- `.skeleton` / `.skeleton-line` — idea #181's shimmer loading placeholder,
  used in place of a spinner-and-text combo for async content.
- `.freshness-label` / `data-fresh="true|false"` — idea #186's subtle glow +
  label indicating whether a stat card's underlying data is recent.
- `.icon` — a normalizing wrapper for the emoji-as-icon approach (see below).

## Icon conventions (idea #179)

This project uses emoji as its icon set rather than an icon font, to keep the
playful tone. To keep sizing/weight consistent, wrap standalone icon emoji in
`<span class="icon">`. The agreed emoji-to-concept mapping so far:

| Emoji | Meaning |
|---|---|
| ☠ | Brand mark |
| ⚔️ | War / battles |
| 🏆 | Trophies / rank |
| 🎁 | Donations |
| 🥇 | MVP / #1 |
| 📈 | Growth / rising stat |
| 🎯 | Recruiting |
| 🔔 | Notifications |
| 🛡️ | Admin / roster |
| 🔍 | Search / diagnostics |
| 📖 | Documentation / how-it-works |
| 🖼️ | Shareable image/card export |

## Favicon & branding (idea #183)

`static/favicon.svg` (a `☠` glyph on the dark background) is linked from every
template's `<head>`, alongside a standardized `<title>` pattern:
`{Page Name} | Graveyard Squad` (the admin panel is the one exception in
spirit — its title is `Admin | Graveyard Squad` even though the page itself
still calls itself "Graveyard HQ" in the UI).
