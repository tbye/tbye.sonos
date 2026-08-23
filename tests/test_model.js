#!/usr/bin/env node
const assert = require("assert")
const { parseList, plainLabel, MAX_SPEAKERS, MAX_RAW } = require("../Model.js")

function speaker(i) {
  return {
    uid: `RINCON_${String(i).padStart(12, "0")}01400`,
    name: `Room ${i}`,
    ip: "192.168.1.10",
    mac: "000e5824d1a0",
    selectable: true,
    volume: 12,
    volumeReady: true
  }
}

assert.strictEqual(plainLabel("<img src=\"http://evil.example/x.png\">Kitchen", "Speaker"), "img src=\"http://evil.example/x.png\"Kitchen")
assert.ok(!plainLabel("<b>x</b>", "Speaker").includes("<"))

const huge = parseList("x".repeat(MAX_RAW + 1))
assert.strictEqual(huge.ok, false)
assert.match(huge.error, /too large/)

const markup = parseList(JSON.stringify({
  ok: true,
  outputId: "RINCON_000E5824D1A001400",
  raopReady: true,
  local: { name: "<i>This computer</i>", description: "Built-in Audio" },
  speakers: [{
    uid: "RINCON_000E5824D1A001400",
    name: "<img src=\"http://evil.example/x.png\">Kitchen",
    ip: "192.168.1.10",
    mac: "000e5824d1a0",
    volume: 40,
    selectable: true
  }]
}))
assert.strictEqual(markup.ok, true)
assert.strictEqual(markup.speakers.length, 1)
assert.ok(!markup.speakers[0].name.includes("<"))
assert.ok(!markup.local.name.includes("<"))

const many = parseList(JSON.stringify({
  ok: true,
  outputId: "local",
  speakers: Array.from({ length: 80 }, (_, i) => speaker(i + 1))
}))
assert.strictEqual(many.speakers.length, MAX_SPEAKERS)

const badUid = parseList(JSON.stringify({
  ok: true,
  speakers: [{ uid: "nope", name: "X", ip: "192.168.1.10" }]
}))
assert.strictEqual(badUid.speakers.length, 0)

console.log("test_model.js ok")
