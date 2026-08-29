/* ─────────────────────────────────────────────────────────────
   Arkendpoint welcome page — settings.
   Edit this file in any text editor and reload the page.
   ───────────────────────────────────────────────────────────── */

/* "cycle"  → crossfade through the IMAGES list below
   "static" → show STATIC_IMAGE only, no cycling            */
export const MODE = "static";

/* Used when MODE is "static". */
export const STATIC_IMAGE = "assets/backdrops/bg-1.png";

/* Used when MODE is "cycle". Add, remove or reorder freely —
   any path that fails to load is simply skipped. */
export const IMAGES = [
  "assets/backdrops/bg-1.png",
  "assets/backdrops/bg-2.png",
  "assets/backdrops/bg-3.png",
  "assets/backdrops/bg-4.png",
  "assets/backdrops/bg-5.png",
  "assets/backdrops/bg-6.png",
];

/* Seconds each image holds before the crossfade. */
export const HOLD_SECONDS = 8;

/* true  → a random image from IMAGES on every page load
   false → always start at the first image                  */
export const RANDOM_ON_LOAD = true;

/* true  → the accent colors follow each image's dominant hue
   false → keep the fixed Nocturne blurple                  */
export const HUE_FOLLOWS_IMAGE = true;

/* Fallback hue (0–360) used before sampling, or when
   HUE_FOLLOWS_IMAGE is false. 262 is the Nocturne blurple. */
export const BASE_HUE = 262;

/* ── Overlay & motion ─────────────────────────────────────── */

/* Darkness of the flat black scrim over the image, 0–1.
   Higher = darker image, more readable type. */
export const OVERLAY_OPACITY = 0.3; /* 22 */

/* Brightness of the background image itself, applied as a CSS filter.
   1 = unchanged, below 1 darkens, above 1 lightens (e.g. 0.7, 1.3). */
export const BG_BRIGHTNESS = 1.15; /* 067 */

/* Strength of the colored bloom behind the wordmark, 0–1.
   0 turns the tint off and leaves a plain vignette. */
export const BLOOM_STRENGTH = 0.18;

/* Darkness of the vignette at the edges of the frame, 0–1. */
export const VIGNETTE_OPACITY = 0.25;

/* Seconds the crossfade (and the color retint) takes. */
export const CROSSFADE_SECONDS = 2.2;

/* true → the background slowly zooms while it holds. */
export const DRIFT_ZOOM = false;

/* Faint concentric rings, three-quarters off the bottom.
   RINGS_OPACITY 0 hides them entirely. */
export const RINGS = true;
export const RINGS_OPACITY = 0.025;

/* Bottom-right corner: the visitor's own local time and date,
   from their device clock and timezone. */
export const SHOW_CLOCK = true;
export const CLOCK_24_HOUR = false;
