function parseList(raw) {
  var text = String(raw || "").trim()
  if (!text) return { ok: false, error: "No response from Sonos helper" }
  try {
    var data = JSON.parse(text)
  } catch (e) {
    return { ok: false, error: "Could not parse Sonos status" }
  }
  if (!data || data.ok === false)
    return { ok: false, error: String((data && data.error) || "Sonos lookup failed") }

  var speakers = Array.isArray(data.speakers) ? data.speakers : []
  var local = data.local && typeof data.local === "object" ? data.local : {
    id: "local",
    name: "This computer",
    description: "Built-in Audio",
    selected: true,
    sinkName: "",
    sinkId: ""
  }
  return {
    ok: true,
    error: "",
    outputId: String(data.outputId || "local"),
    raopReady: data.raopReady === true,
    local: local,
    speakers: speakers
  }
}

function parseAction(raw) {
  var text = String(raw || "").trim()
  if (!text) return { ok: false, error: "No response" }
  try {
    return JSON.parse(text)
  } catch (e) {
    return { ok: false, error: "Could not parse response" }
  }
}

if (typeof module !== "undefined") {
  module.exports = { parseList: parseList, parseAction: parseAction }
}
