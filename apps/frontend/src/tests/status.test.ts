import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { renderToStaticMarkup } from "react-dom/server";

import nextConfig from "../../next.config.ts";
import { StatusBanner } from "../lib/status.ts";

test("status banner keeps unsafe states and their evidence visible", () => {
  const blocked = renderToStaticMarkup(
    StatusBanner({
      state: "BLOCKED",
      sourceAt: "2026-08-10T04:00:00Z",
      reason: "REQUIRED_LIVE_STATE_UNAVAILABLE",
    }),
  );
  const stale = renderToStaticMarkup(
    StatusBanner({
      state: "STALE",
      sourceAt: "2026-08-10T04:00:00Z",
      reason: "FEED_STALE",
    }),
  );

  assert.match(blocked, /BLOCKED/);
  assert.match(stale, /STALE/);
  assert.match(blocked, /2026-08-10T04:00:00Z/);
  assert.match(blocked, /REQUIRED_LIVE_STATE_UNAVAILABLE/);
  assert.match(stale, /FEED_STALE/);
});

test("polling cannot preserve LIVE behind a hung request", () => {
  const page = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(page, /signal: AbortSignal\.timeout\(2_500\)/);
  assert.match(page, /catch \{[\s\S]*reason: "API_UNAVAILABLE"/);
});

test("browser API calls stay on the dashboard origin", async () => {
  const rewrites = await nextConfig.rewrites?.();

  assert.deepEqual(rewrites, [
    {
      source: "/backend/:path*",
      destination: "http://127.0.0.1:8000/:path*",
    },
  ]);
});
