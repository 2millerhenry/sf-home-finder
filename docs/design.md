---
name: SF Home Finder
description: A calm daily shortlist for finding the right San Francisco home.
colors:
  accent-forest: "oklch(43% 0.09 162)"
  accent-forest-hover: "oklch(37% 0.085 162)"
  accent-mist: "oklch(95% 0.022 155)"
  canvas-fog: "oklch(97.5% 0.006 85)"
  surface-warm: "oklch(99.2% 0.004 85)"
  ink-pine: "oklch(21% 0.018 160)"
  text-stone: "oklch(48% 0.014 150)"
  divider-sage: "oklch(89% 0.01 145)"
  caution-amber: "oklch(51% 0.11 73)"
  caution-mist: "oklch(96% 0.035 82)"
  danger-red: "oklch(48% 0.14 25)"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', sans-serif"
    fontSize: "2.25rem"
    fontWeight: 720
    lineHeight: 1.08
    letterSpacing: "-0.035em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif"
    fontSize: "1.125rem"
    fontWeight: 680
    lineHeight: 1.3
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', sans-serif"
    fontSize: "0.75rem"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "0.015em"
rounded:
  control: "10px"
  surface: "18px"
  pill: "999px"
spacing:
  xs: "6px"
  sm: "10px"
  md: "16px"
  lg: "24px"
  xl: "36px"
components:
  button-primary:
    backgroundColor: "{colors.accent-forest}"
    textColor: "{colors.surface-warm}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.accent-forest-hover}"
    textColor: "{colors.surface-warm}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-secondary:
    backgroundColor: "{colors.surface-warm}"
    textColor: "{colors.ink-pine}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  listing-row:
    backgroundColor: "{colors.surface-warm}"
    textColor: "{colors.ink-pine}"
    rounded: "{rounded.surface}"
    padding: "24px"
---

# Design System: SF Home Finder

## Overview

**Creative North Star: "The Quiet Ledger"**

This interface is used on a MacBook in ordinary daylight, often for a quick morning or evening review. It should feel calm, native, and unusually clear: more like a focused Apple utility than a dashboard. The listing is the product; controls and source machinery should recede until requested.

The primary workflow uses a premium data-grid vocabulary: aligned columns, compact rows, a sticky header, and quiet hairline rules. It keeps the scanning speed of Excel while rejecting raw spreadsheet chrome, dense admin controls, and generic analytics styling. At least five complete homes should be visible in a standard laptop viewport.

**Key Characteristics:**

- Light, warm, and quietly SF rather than sterile white.
- One forest accent reserved for actions, selection, and confirmed success.
- Strong recommendation hierarchy with scores, prices, neighborhoods, evidence, and concerns aligned for comparison.
- Familiar native controls, subtle separators, and almost no decorative effects.
- Breakpoint-driven columns that preserve a compact table at every useful width.

## Colors

Warm fog neutrals create a daylight canvas; a muted forest green provides the single active voice.

### Primary

- **Presidio Forest** (`oklch(43% 0.09 162)`): primary actions, active navigation, strong match labels, and useful links.
- **Presidio Forest Hover** (`oklch(37% 0.085 162)`): direct interaction feedback only.
- **Eucalyptus Mist** (`oklch(95% 0.022 155)`): selected and saved backgrounds.

### Neutral

- **Morning Fog** (`oklch(97.5% 0.006 85)`): page canvas.
- **Warm Paper** (`oklch(99.2% 0.004 85)`): primary surfaces and controls.
- **Pine Ink** (`oklch(21% 0.018 160)`): primary text.
- **Sidewalk Stone** (`oklch(48% 0.014 150)`): metadata and supporting text.
- **Sage Divider** (`oklch(89% 0.01 145)`): borders and separators.

### Named Rules

**The One Voice Rule.** Presidio Forest stays under ten percent of the screen and always communicates action, selection, or success.

**The Honest State Rule.** Caution and failure always include words or symbols; color is supporting evidence, never the only evidence.

## Typography

**Display Font:** SF Pro Display through the Apple system stack
**Body Font:** SF Pro Text through the Apple system stack
**Label/Mono Font:** SFMono only for advanced configuration

**Character:** Native, precise, and comfortable. One system family keeps the interface familiar while weight and spacing establish hierarchy.

### Hierarchy

- **Headline** (720, 2.25rem, 1.08): one page-level outcome statement.
- **Title** (680, 1.125rem, 1.3): listing names and section titles.
- **Body** (400, 0.9375rem, 1.5): summaries and explanatory copy, capped near 70 characters where practical.
- **Label** (650, 0.75rem, 0.015em): metadata, field labels, and compact state text.

### Named Rules

**The Sentence Case Rule.** Use sentence case throughout. Uppercase is reserved for tiny nonessential metadata, never navigation or actions.

## Elevation

The system is flat by default. Tonal layers and one-pixel dividers define structure; a low ambient shadow may lift the main result surface or an interactive control, but stacked cards and floating panels are avoided.

### Shadow Vocabulary

- **Ambient Low** (`0 1px 2px oklch(21% 0.018 160 / 0.05)`): main surfaces at rest.
- **Ambient Focus** (`0 8px 24px oklch(21% 0.018 160 / 0.08)`): focused elevated content only.

### Named Rules

**The Flat-by-Default Rule.** Shadows never decorate; they only clarify the current layer or interaction.

## Components

Components are refined and restrained, with predictable native behavior and no invented interaction patterns.

### Buttons

- **Shape:** softly squared (`10px`), never a novelty capsule for primary actions.
- **Primary:** Presidio Forest on Warm Paper, `10px 16px`, medium-strong weight.
- **Hover / Focus:** darker forest on hover; a visible two-pixel focus ring with offset.
- **Secondary / Ghost:** Warm Paper with Sage Divider border; ghost actions use text only when hierarchy is obvious.

### Chips

- **Style:** compact pill for passive metadata and segmented view navigation.
- **State:** unselected uses transparent neutral; selected uses Warm Paper with Pine Ink and a subtle border or Eucalyptus Mist for saved state.

### Cards / Containers

- **Corner Style:** `18px` on the main result surface.
- **Background:** Warm Paper on Morning Fog.
- **Shadow Strategy:** Ambient Low only on the outer result surface.
- **Border:** one-pixel Sage Divider.
- **Internal Padding:** `24px` desktop, `16px` narrow screens.

### Inputs / Fields

- **Style:** Warm Paper, one-pixel Sage Divider, `10px` radius, at least `42px` high.
- **Focus:** Presidio Forest border plus a visible translucent focus ring.
- **Error / Disabled:** explicit message; muted fill and readable text.

### Navigation

A compact solid top bar contains the product name and four text destinations. The current destination receives a subtle filled treatment and `aria-current`; mobile navigation scrolls horizontally rather than collapsing into a hidden menu.

### Listing Table

Each listing occupies one compact row. Score, price, neighborhood, source, title, three supported matches, one concern, found date, and actions align under sticky column labels. Rows target roughly 80–90px on desktop so five or more complete recommendations remain visible. Saved rows receive only a quiet green tint. At compact widths, lower-priority evidence columns fold into a native disclosure inside the listing cell instead of turning into cards.

## Do's and Don'ts

### Do:

- **Do** put the strongest recommendations first by default.
- **Do** align comparable values in stable columns and keep at least five rows visible on a standard laptop screen.
- **Do** use responsive column hiding and an inline detail disclosure before allowing horizontal scrolling.
- **Do** explain the score with three supported matches and one clearly labeled concern.
- **Do** keep buttons at least 42px high and provide a visible `:focus-visible` state.
- **Do** move source health and scan history into progressive disclosure below the shortlist.
- **Do** honor reduced-motion preferences and keep transitions between 150 and 200 milliseconds.

### Don't:

- **Don't** resemble a raw default spreadsheet, a generic analytics dashboard, or an admin panel with controls bolted on.
- **Don't** use excessive panels, nested cards, decorative metrics, or technical copy in the primary workflow.
- **Don't** use side-stripe borders, gradient text, glassmorphism, decorative motion, or full-saturation inactive states.
- **Don't** make color the only indicator of score, success, warning, or failure.
- **Don't** expose YAML before the plain-language preference controls.
- **Don't** repeat the same listing link in multiple competing actions.
