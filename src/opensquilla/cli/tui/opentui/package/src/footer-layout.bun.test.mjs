// Renderer-level regression tests for the footer (composer + router HUD).
//
// Two guarantees:
//   1. The footer container paints an OPAQUE background. A transparent footer
//      leaves cells it vacates on resize/reflow uncleared by the terminal diff,
//      so stale glyphs from a prior layout linger (the router model text bleeding
//      into the composer placeholder, stray edge characters, etc.). The tmux/PTY
//      text-snapshot harness cannot see cell background alpha, so this class of
//      bug slipped past it; here we inspect captured span backgrounds (RGBA).
//   2. The composer box and router HUD box never overlap, at several widths and
//      after a resize — locking the layout contract.
//
// Must run under bun: @opentui/core/testing needs bun FFI. Run with:
//   bun test src/footer-layout.bun.test.mjs
import { test, expect } from "bun:test";
import { createTestRenderer } from "@opentui/core/testing";
import { BoxRenderable, TextRenderable } from "@opentui/core";

import { createComposer } from "./composer.mjs";
import { THEME } from "./theme.mjs";

const FOOTER_HEIGHT = 6;
const HEIGHT = 12;
const LONG_MODEL = "deepseek/deepseek-v4-pro-20260423";

async function renderFooter({ width, withFooterBg = true, resizeTo = null }) {
  const setup = await createTestRenderer({ width, height: HEIGHT });
  const { renderer, renderOnce, captureSpans, resize } = setup;

  const conversationBox = new BoxRenderable(renderer, {
    id: "conversation",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    height: HEIGHT - FOOTER_HEIGHT,
  });
  renderer.root.add(conversationBox);

  const inputOptions = {
    id: "input-region",
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    height: FOOTER_HEIGHT,
  };
  if (withFooterBg) inputOptions.backgroundColor = THEME.footerBg;
  const inputBox = new BoxRenderable(renderer, inputOptions);
  renderer.root.add(inputBox);

  const overlayLayer = new BoxRenderable(renderer, {
    id: "overlay-layer",
    position: "absolute",
    left: 0,
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 1000,
    shouldFill: false,
    visible: false,
  });
  renderer.root.add(overlayLayer);

  const composer = createComposer({
    renderer,
    BoxRenderable,
    TextRenderable,
    conversationBox,
    inputBox,
    overlayLayer,
    footerHeight: FOOTER_HEIGHT,
    sendHostMessage: () => {},
  });
  try {
    composer.install();
  } catch {
    composer.rerender();
  }
  composer.setComposerState({ placeholder: "send a message" });
  composer.setRouterState({ model: LONG_MODEL, route: "route c1", saving: "-", context: "-" });

  if (resizeTo) {
    const doResize = resize || ((w, h) => renderer.resize(w, h));
    await doResize(resizeTo, HEIGHT);
    composer.onResize();
  }

  await renderOnce();
  const frame = captureSpans();
  renderer.destroy?.();
  return frame;
}

function rowText(frame, rowIndex) {
  const line = frame.lines[rowIndex];
  return line ? line.spans.map((span) => span.text).join("") : "";
}

// Background of the inputBox left margin (column 0). The composer box starts at
// left:1, so column 0 in a footer row is the container's own fill.
function footerMarginBg(frame) {
  const line = frame.lines[HEIGHT - 3];
  if (!line) return null;
  let col = 0;
  for (const span of line.spans) {
    const width = Math.max(1, span.width || 1);
    for (let i = 0; i < width; i += 1) {
      if (col === 0) return span.bg;
      col += 1;
    }
  }
  return null;
}

test("footer paints an opaque background so it self-clears on reflow", async () => {
  const bg = footerMarginBg(await renderFooter({ width: 80, withFooterBg: true }));
  expect(bg).not.toBeNull();
  // Opaque: every footer cell is rewritten each frame, so stale glyphs cannot
  // survive a composer/router box moving on resize.
  expect(bg.a).toBeGreaterThan(0);
});

test("a footer without backgroundColor is transparent (contrast case)", async () => {
  // Proves the assertion above discriminates: drop the fix and the margin cell is
  // transparent (alpha 0) — the bug that lets ghosts linger.
  const bg = footerMarginBg(await renderFooter({ width: 80, withFooterBg: false }));
  expect(bg).not.toBeNull();
  expect(bg.a).toBe(0);
});

test("composer and router boxes never overlap across widths", async () => {
  for (const width of [60, 80, 120, 160]) {
    const top = rowText(await renderFooter({ width }), HEIGHT - FOOTER_HEIGHT);
    const composerOpen = top.indexOf("╭");
    const composerClose = top.indexOf("╮");
    const routerOpen = top.lastIndexOf("╭");
    const routerClose = top.lastIndexOf("╮");
    expect(composerOpen).toBeGreaterThanOrEqual(0); // composer box present
    expect(routerOpen).toBeGreaterThan(composerOpen); // a distinct second box
    expect(composerClose).toBeGreaterThan(composerOpen);
    // composer fully closes before the router box opens — i.e. no overlap.
    expect(composerClose).toBeLessThan(routerOpen);
    expect(routerClose).toBeGreaterThan(routerOpen);
  }
});

test("router model value stays out of the composer box after a resize", async () => {
  const frame = await renderFooter({ width: 100, resizeTo: 60 });
  const top = rowText(frame, HEIGHT - FOOTER_HEIGHT);
  const composerClose = top.indexOf("╮");
  const routerOpen = top.lastIndexOf("╭");
  expect(composerClose).toBeLessThan(routerOpen);
  // The model text must render inside the router box, never bleeding left into
  // the composer placeholder region.
  const modelRow = rowText(frame, HEIGHT - 5);
  const modelIndex = modelRow.indexOf("deepseek-v4-pro");
  expect(modelIndex).toBeGreaterThan(composerClose);
});
