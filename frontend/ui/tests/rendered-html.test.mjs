import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the Cricbot chat-first archive and agent workspace", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Cricbot — IPL Archive &amp; Cricket Intelligence<\/title>/i);
  assert.match(html, /CRIC/);
  assert.match(html, /HISTORICAL MATCH EXPLORER/);
  assert.match(html, /IPL season/);
  assert.match(html, /Search RCB, Chennai, Wankhede/);
  assert.match(html, /Ask about any IPL match/);
  assert.match(html, /Living documentation/i);
  assert.match(html, /API patterns, ownership, state, and provenance/);
  assert.match(html, /Agent graph/);
  assert.match(html, /Toggle agent trace/);
  assert.match(html, /Understand request/);
  assert.match(html, /Load match data/);
  assert.match(html, /LLM synthesis/);
  assert.doesNotMatch(html, /Loading graph definition/i);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});
