---
name: diff2png
description: Developer evidence tool for turning selected git diff hunks into reviewer-ready PNGs.
colors:
  ink-bg: "#0f1117"
  panel: "#1a1d27"
  panel-raised: "#21253a"
  rule: "#2a2f45"
  action-blue: "#5b8af0"
  action-blue-dim: "#3d5fad"
  success-green: "#3ecf6e"
  warning-yellow: "#f5c842"
  text-primary: "#c8cfe8"
  text-muted: "#5a6080"
  danger-red: "#e05c5c"
  white: "#ffffff"
typography:
  title:
    fontFamily: "JetBrains Mono, monospace"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.04em"
  body:
    fontFamily: "Noto Sans JP, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Noto Sans JP, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.08em"
  mono:
    fontFamily: "JetBrains Mono, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  xs: "3px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  modal: "10px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  panel-gap: "24px"
components:
  button-primary:
    backgroundColor: "{colors.action-blue}"
    textColor: "{colors.white}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "7px 14px"
  button-success:
    backgroundColor: "{colors.success-green}"
    textColor: "#0a1a10"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "7px 14px"
  button-ghost:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.text-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "7px 14px"
  input-text:
    backgroundColor: "{colors.ink-bg}"
    textColor: "{colors.text-primary}"
    typography: "{typography.mono}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
  hunk-item-active:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.md}"
    padding: "8px 10px"
---

# Design System: diff2png

## 1. Overview

**Creative North Star: "The Diff Workbench"**

diff2png is a compact developer workbench, not a presentation surface. The interface should feel like a focused review-prep tool: dark, dense, quiet, and immediately legible while a developer moves from repository path to hunk selection to reviewer-ready PNG output.

The system uses a restrained product palette: deep ink backgrounds, two panel layers, one blue action accent, and semantic green/yellow/red states. Visual interest comes from hierarchy, alignment, and state clarity, not decoration. It rejects the PRODUCT.md anti-references directly: marketing-site heroes, excessive ornament, flashy gradients, card-heavy SaaS styling, cuteness that reduces density, and custom controls that break familiar development-tool behavior.

**Key Characteristics:**
- Dense two-pane workspace with a persistent header, sidebar controls, and a large preview surface.
- Dark developer-tool theme with a single blue action channel used for primary actions, focus, active state, and adjusted ranges.
- Mono typography for product identity, file paths, counters, code-like values, and commit/diff metadata.
- Flat panel layering by default; shadows appear only for floating UI such as drawers, modals, iframes, and toasts.
- Short state transitions that communicate opening, selection, feedback, and loading without choreography.

## 2. Colors

The palette is an ink-and-panel system with one restrained action blue and clear semantic feedback colors.

### Primary
- **Workbench Blue** (`action-blue`): The only primary action and focus color. Use it for analyze/apply actions, active ranges, focus borders, and the product name. Its scarcity is part of the design.
- **Muted Selection Blue** (`action-blue-dim`): Active hunk border and selected-state reinforcement. It supports `action-blue` without creating a second accent system.

### Secondary
- **Evidence Green** (`success-green`): Successful export, additive hunk badges, and positive completion feedback.
- **Review Yellow** (`warning-yellow`): Reserved for warning or attention states. It exists in the token set but should stay rare until the UI has a genuine warning state.
- **Failure Red** (`danger-red`): Destructive actions, failed operations, and error toasts.

### Neutral
- **Ink Background** (`ink-bg`): Application canvas and input field fill. It keeps the preview and panels grounded.
- **Panel Surface** (`panel`): Header, sidebar, drawer, modal, and toolbar surfaces.
- **Raised Panel** (`panel-raised`): Ghost buttons, hover rows, active hunk backgrounds, and toast fill.
- **Rule Line** (`rule`): One-pixel dividers, input strokes, panel borders, scroll thumbs, and disabled-state structure.
- **Primary Text** (`text-primary`): Main readable UI text on dark surfaces.
- **Muted Text** (`text-muted`): Labels, subtitles, counters, secondary descriptions, and disabled hints. Use carefully; if text becomes task-critical, promote it to `text-primary`.
- **Preview White** (`white`): The iframe preview surface only. Do not use white as an app surface color.

### Named Rules

**The One Accent Rule.** Workbench Blue is the only non-semantic accent. Do not introduce purple, cyan, magenta, or gradient accents for decoration.

**The Dark Workbench Rule.** Keep the application shell dark. A light theme may exist later, but the default identity is a developer tool used for focused review preparation.

## 3. Typography

**Display Font:** none  
**Body Font:** Noto Sans JP, sans-serif  
**Label/Mono Font:** JetBrains Mono, monospace

**Character:** The typography is technical but not terminal-only. Noto Sans JP carries Japanese UI labels with compact readability; JetBrains Mono marks paths, counters, configuration keys, and the `diff2png` identity.

### Hierarchy
- **Title** (600, 15px, 1.2): Header product name only. It uses mono type and blue color, with light tracking for tool identity.
- **Panel Title** (600, 13px, 1.3): Drawer and modal titles. Keep it plain; no display styling.
- **Body** (400, 14px, 1.5): Default application text. Product UI should stay fixed-size, not fluid.
- **Control Text** (500, 12-13px, 1.3): Buttons, selects, setting labels, source controls, and dense toolbar text.
- **Label** (500-600, 10-11px, 0.08-0.1em): Uppercase section labels such as repository path, preview, and setting groups.
- **Mono Data** (400-600, 10-12px, 1.4): File paths, line ranges, badges, status counters, config code labels, and range values.

### Named Rules

**The Data Is Mono Rule.** Anything that behaves like a code artifact, path, line number, ref, config key, or count uses JetBrains Mono.

**The No Display Type Rule.** This is a tool, not a landing page. Do not add oversized headings, fluid `clamp()` display type, or decorative font pairings.

## 4. Elevation

Depth is conveyed primarily through tonal layering and one-pixel borders. Static panels are flat. Shadows are reserved for UI that leaves the base plane: the settings drawer, modal dialogs, preview iframe, and toast feedback. This keeps the workspace crisp while making transient layers unambiguous.

### Shadow Vocabulary
- **Drawer Shadow** (`box-shadow: -8px 0 32px rgba(0, 0, 0, 0.4)`): Right-side settings drawer only.
- **Modal Shadow** (`box-shadow: 0 16px 48px rgba(0, 0, 0, 0.45)`): Centered modal surfaces such as diff conditions and output folder.
- **Preview Shadow** (`box-shadow: 0 4px 32px rgba(0, 0, 0, 0.5)`): Iframe preview surface to separate rendered evidence from the app shell.
- **Toast Shadow** (`box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4)`): Bottom-right feedback messages.

### Named Rules

**The Flat-Until-Floating Rule.** Do not put broad soft shadows on normal buttons, list items, panels, or cards. If an element is not floating over the app, it gets a tonal surface and a one-pixel border.

## 5. Components

### Buttons

Buttons are compact, rectangular, and utilitarian. They are commands, not decorative chips.

- **Shape:** Compact rounded rectangle (6px radius).
- **Primary:** Workbench Blue fill with white text; used for analyze, apply, browse, and confirm actions.
- **Success:** Evidence Green fill with dark text; used for final export.
- **Danger:** Failure Red fill with white text; used only for destructive actions such as clearing generated PNGs.
- **Ghost:** Raised Panel fill with Rule Line border and Primary Text; used for secondary actions, navigation, and range controls.
- **Hover / Focus:** Hover reduces opacity to 0.85. Active press scales to 0.97. Focus should use Workbench Blue border or outline; do not rely on opacity alone.
- **Disabled:** Opacity 0.35 with default cursor disabled. Disabled labels must remain understandable from context.

### Chips

The current chip-like element is the hunk badge.

- **Style:** Mono 10px label, 4px radius, 2px 6px padding.
- **Positive:** Evidence Green text on a low-alpha green background for added-line counts.
- **Danger:** Red text on a low-alpha red background for deletion-only or destructive status.
- **Rule:** Badges explain diff facts. Do not turn them into decorative tags.

### Cards / Containers

The app should not grow a card-grid aesthetic. Containers are functional panels, rows, drawers, modals, iframe preview surfaces, and toasts.

- **Corner Style:** Most controls and rows use 6px; iframe and toast use 8px; modal uses 10px.
- **Background:** Ink Background for canvas and inputs; Panel Surface for persistent chrome; Raised Panel for selected rows, ghost buttons, and toasts.
- **Shadow Strategy:** Follow Elevation. Shadows are only for floating or preview surfaces.
- **Border:** One-pixel Rule Line border for dividers, controls, modal edges, and selected hunk reinforcement.
- **Internal Padding:** Dense controls use 7-10px; panels use 16-20px; drawer groups use 24px vertical rhythm.

### Inputs / Fields

Inputs are quiet, code-aware controls optimized for paths, refs, and configuration values.

- **Style:** Ink Background fill, Rule Line stroke, 6px radius, Primary Text color.
- **Typography:** Text paths and numeric values use JetBrains Mono at 12px; selects use Noto Sans JP at 12px.
- **Focus:** Border shifts to Workbench Blue. Add a visible `focus-visible` outline before shipping keyboard-heavy refinements.
- **Error / Disabled:** Use Failure Red for error text and border only when the field is directly invalid; otherwise use toast feedback.

### Navigation

Navigation is local and task-based rather than site-like.

- **Header:** 52px tall global strip with product name, subtitle, status, and settings command.
- **Sidebar:** 340px fixed work panel for repository, hunk list, and export actions.
- **Preview Toolbar:** Dense inline controls for current file, range adjustment, hunk navigation, and count. It should remain a toolbar, not a breadcrumb or tab bar.
- **Mobile Treatment:** Not yet implemented. If added, collapse the sidebar structurally; do not simply shrink typography.

### Hunk List

The hunk list is the signature component of the product.

- **Default:** Transparent row, checkbox, mono file path, muted line range, and badge.
- **Hover:** Raised Panel background.
- **Active:** Raised Panel background plus Muted Selection Blue border.
- **Selection:** Checkbox uses Workbench Blue accent. Selected export count must stay visible in the export footer.
- **Overflow:** File paths truncate with ellipsis; never wrap paths into multi-line rows unless a dedicated expanded state exists.

### Drawer / Modal

Drawers and modals are utilitarian overlays for secondary configuration.

- **Drawer:** Right side, 320px width, Panel Surface fill, Rule Line border-left, Drawer Shadow, 250ms transform.
- **Modal:** Centered, max 680px wide, Panel Surface fill, 10px radius, Rule Line border, Modal Shadow, 200ms opacity/transform.
- **Overlay:** Black 45% opacity. It exists to focus attention, not to create a glass effect.

## 6. Do's and Don'ts

### Do:
- **Do** keep the default shell dark and developer-tool-like; the product's credibility comes from focused utility.
- **Do** use Workbench Blue only for primary actions, focus, active selection, and adjusted range signals.
- **Do** preserve density: 52px header, 340px sidebar, compact 6px controls, and 10-13px control labels are part of the system.
- **Do** use JetBrains Mono for file paths, line ranges, counters, hunk badges, config keys, and status values.
- **Do** distinguish success, danger, active, disabled, and loading states with both color and text where possible.
- **Do** keep panels flat unless they are truly floating overlays or preview surfaces.

### Don't:
- **Don't** create a marketing-site hero, oversized headline area, or explanatory landing screen for the main app.
- **Don't** add excessive ornament, flashy gradients, purple/blue gradient accents, glassmorphism, or decorative background effects.
- **Don't** turn the interface into a card-heavy SaaS layout. Repeated cards are the wrong structure for this workflow.
- **Don't** make the tool cute, playful, or friendly at the cost of density and scan speed.
- **Don't** invent non-standard form controls, modals, dropdowns, or scroll behavior for flavor.
- **Don't** use broad shadows on normal controls or combine one-pixel borders with large decorative shadows on buttons.
- **Don't** rely on color alone to communicate additions, deletions, export success, or errors.
