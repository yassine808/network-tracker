---
version: alpha
name: Graphite Instrument
description: DataGuard dark theme. Graphite grey surfaces with a navy accent family, semantic status colors reserved for state.
colors:
  bg: "#131619"
  surface: "#1B1E24"
  sidebar: "#171A20"
  input: "#181B20"
  border: "#2D3239"
  track: "#262B31"
  ink: "#E9EBEF"
  muted: "#9AA0A8"
  accent: "#4384D8"
  accentDeep: "#214E82"
  onAccent: "#F2F4F7"
  success: "#45D194"
  warning: "#F5B942"
  danger: "#FF6F6B"
  chartDown: "#4CC4FF"
  chartUp: "#A78BFA"
  axisText: "#85888F"
typography:
  ui:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.5
  display:
    fontFamily: "Segoe UI Variable Display, Segoe UI Light, Segoe UI, sans-serif"
    fontSize: 78px
    fontWeight: 300
    letterSpacing: -0.035em
  heading:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: 14px
    fontWeight: 650
    lineHeight: 1.4
  caption:
    fontFamily: "Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, Cascadia Mono, Consolas, monospace"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.4
rounded:
  control: 10px
  card: 16px
  overlay: 22px
  pill: 99px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
---

# DataGuard Design System

## Overview

DataGuard is a quiet instrument panel for one job: tell the owner how many GB are left on a metered
hotspot plan, and whether today's pace is safe, in a two-second glance. The aesthetic family is
instrument panel, not analytics product: graphite surfaces the color of a dark desk, a single navy
accent that marks whatever is interactive or selected, and semantic color that only appears when
something changed. Nothing at rest is saturated.

The base is graphite grey with a whisper of cool tint, deliberately not the blue-black that every
developer tool defaults to. Navy is kept, but demoted from the whole background to the accent role:
primary buttons, active navigation, focus rings, hover borders. The screen reads as grey first,
blue only where you can act.

Emotional target: calm certainty. The owner should feel the app is telling the truth, not selling
anything.

## Colors

Strategy: restrained. Tinted graphite neutrals plus one navy accent family that never exceeds about
10 percent of the visible surface. Status colors are semantic vocabulary, not decoration.

- **bg** `#131619`: app background, the dark desk. Slightly cool, clearly grey.
- **surface** `#1B1E24`: content cards. One step lighter than bg; elevation comes from this step,
  not from shadows.
- **sidebar** `#171A20`: nav rail. Between bg and surface, so the pane it frames reads as primary.
- **input** `#181B20`: form field and editable-value backgrounds.
- **border** `#2D3239`: the 1px line that separates everything. Card outlines, row dividers, control
  outlines.
- **track** `#262B31`: unfilled meter and ring track.
- **ink** `#E9EBEF`: primary text. Off-white, never pure white.
- **muted** `#9AA0A8`: labels, captions, secondary text.
- **accent** `#4384D8`: mid navy. Active nav text, focus rings, hover borders, links, editable-value
  hover.
- **accentDeep** `#214E82`: deep navy fill. Primary buttons only, with onAccent text.
- **onAccent** `#F2F4F7`: text on deep navy and danger fills.
- **success** `#45D194`, **warning** `#F5B942`, **danger** `#FF6F6B`: the only saturated colors.
  Usage state, warnings, destructive actions, ring and meter states. Never decorative.
- **chartDown** `#4CC4FF`, **chartUp** `#A78BFA`: live traffic lines, one hue per direction, used
  nowhere else.
- **axisText** `#85888F`: chart axis and tick labels.

In code the same values are authored in OKLCH (chroma 0.010 to 0.014 on neutrals, hue 254 to 260)
with these hex values as the no-OKLCH fallback. Source of truth: the CSS custom properties in
dataguard/dashboard.html.

Rules: tint every neutral, never use `#000` or `#fff`, one accent family only, status color always
paired with a word or icon (never color alone).

## Typography

One system stack carries everything, per product convention: "Segoe UI Variable Text" with Segoe UI
and system-ui fallbacks. A display stack (Segoe UI Variable Display, weight 300) is reserved for the
two numbers that anchor the main screen: the ring percent and the GB-left figure.

- Fixed scale, no fluid sizing: hero 48 to 78px (clamp by viewport), fact values 22px/600,
  section heads 14px/650, body 15px/400, captions 13 to 13.5px, chart ticks 10 to 12px.
- Ratios are tight (about 1.125 to 1.2 between steps); hierarchy comes from weight and size
  contrast, not from new families.
- All data uses tabular numerals so digits never jitter while polling.
- Prose lines cap at 65 to 75ch; data rows may run denser.
- No display font in labels, buttons, or table text.

## Layout

Fixed 212px sidebar rail plus an internally scrolling content pane, content capped at 1040px and
centered. Panels switch in place (Main, Apps, Alerts, Settings) without navigation.

- Spacing scale: 4, 8, 12, 16, 20, 24, 40px. Cards use 20 to 26px padding; control clusters gap at
  10 to 14px. Rhythm varies by density: hero is airy, tables and strips are tight.
- Responsive is structural: under 700px the hero stacks and the facts strip becomes rows; under
  820px the three-up strip stacks with dividers switching from vertical to horizontal.
- Standard patterns only: side nav, forms with labels above controls, tables with a header row,
  details rows that expand.

## Elevation & Depth

Elevation is surface lightness steps (bg → sidebar → surface) plus the 1px border token. Drop
shadows exist only on things that float above the app: the toast and the warning veil card. Cards
on the page have no shadow. The veil is the one blur in the product: it is a functional modal
dimmer, not decoration.

## Shapes

Control radius 10px (buttons, inputs), card radius 16px, overlay card 22px, pills and chips 99px,
chart bars 2px. Borders are always 1px except the focus ring (2px) and meter/ring strokes.

## Components

- **Card**: surface bg, 1px border, 16px radius, 20 to 22px padding. No shadow, no stripe.
- **Side nav item**: transparent rest, muted text; hover = track bg and ink text; active = 15%
  accent tint bg with accent text. Icons are 1.8px stroke line icons inheriting currentColor.
- **Status chip**: pill with a leading dot. Dot color carries state (success pulses only when
  counting); the word beside it always states the state in text.
- **Primary button**: accentDeep fill, onAccent text, weight 650. Hover brightens 8%.
  **Secondary button**: surface fill, 1px border, ink text; hover border turns accent.
  **Danger button**: 1px danger outline with danger text; hover fills danger with onAccent text.
- **Editable display value**: the plan size and the GB-left figure are number inputs styled as
  display text (transparent at rest). Hover draws the 1px accent border; focus shows the ring;
  commit on Enter or click-away; invalid input reverts with a toast. Same vocabulary in both spots.
- **Meter**: 8px pill, track bg, fill colored by semantic state, animates width only.
- **Ring**: dashed track circle, solid arc colored by state, soft glow at 75 percent and above.
- **Tag**: pill with currentColor at 12 percent background, state word inside.
- **Alerts list**: full 1px border, warning tint background, leading dot; no left stripe.
- **Inputs and selects**: input bg, 1px border, accent border on focus, 9 to 11px padding.
- **Toast**: bottom right, surface bg, border, one shadow; error variant uses danger tint and
  border.
- **Veil**: full-screen dimmer with functional blur, one card, state icon, title, one paragraph,
  at most two actions.

## Do's and Don'ts

- Do keep saturation under 10 percent of the screen at rest; don't color anything for decoration.
- Do earn elevation with surface steps and 1px borders; don't stack cards inside cards or add
  shadows to page content.
- Do put words next to every status color; don't encode state by hue alone.
- Do keep motion 150 to 250ms and state-driven; don't animate layout properties or add entrance
  choreography on load.
- Do use the deep navy for primary fills and the mid navy for interactive text and borders; don't
  let navy creep back into large surfaces.
- Do write short, plain, factual copy; don't use em dashes, marketing voice, or emoji in the UI.
- Do blur only the warning veil; don't add glass or gradient effects anywhere else.
