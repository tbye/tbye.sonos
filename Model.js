var MAX_SPEAKERS = 32
var MAX_NAME = 64
var MAX_UID = 80
var MAX_RAW = 128 * 1024

function plainLabel(value, fallback) {
  var s = String(value == null ? "" : value)
  s = s.replace(/[\x00-\x1F\x7F]/g, "").replace(/[<>]/g, "")
  s = s.replace(/\s+/g, " ").trim()
  if (s.length > MAX_NAME) s = s.slice(0, MAX_NAME)
  return s || (fallback || "")
}

function plainUid(value) {
  var s = plainLabel(value, "")
  if (s.length > MAX_UID) s = s.slice(0, MAX_UID)
  return /^RINCON_[0-9A-Fa-f]{12,32}$/.test(s) ? s : ""
}

function plainMac(value) {
  var s = String(value == null ? "" : value).toLowerCase().replace(/[^0-9a-f]/g, "")
  return s.length === 12 ? s : ""
}

function sanitizeSpeaker(row) {
  if (!row || typeof row !== "object") return null
  var uid = plainUid(row.uid)
  if (!uid) return null
  var volume = Number(row.volume)
  if (!isFinite(volume)) volume = 0
  volume = Math.max(0, Math.min(100, volume))
  return {
    uid: uid,
    name: plainLabel(row.name, "Speaker"),
    ip: plainLabel(row.ip, "").slice(0, 15),
    mac: plainMac(row.mac),
    invisible: row.invisible === true,
    coordinator: row.coordinator === true,
    airplay: row.airplay === true,
    selectable: row.selectable !== false,
    groupCoordinator: plainUid(row.groupCoordinator),
    muted: row.muted === true,
    volume: volume,
    volumeReady: row.volumeReady === true,
    sinkName: plainLabel(row.sinkName, "").slice(0, 128),
    sinkId: plainLabel(row.sinkId, "").slice(0, 32),
    sinkReady: row.sinkReady === true,
    selected: row.selected === true
  }
}

function sanitizeLocal(local) {
  var row = local && typeof local === "object" ? local : {}
  return {
    id: "local",
    name: plainLabel(row.name, "This computer"),
    description: plainLabel(row.description, "Built-in Audio"),
    selected: row.selected === true,
    sinkName: plainLabel(row.sinkName, "").slice(0, 128),
    sinkId: plainLabel(row.sinkId, "").slice(0, 32)
  }
}

function parseList(raw) {
  var text = String(raw || "")
  if (text.length > MAX_RAW) return { ok: false, error: "Sonos helper output was too large" }
  text = text.trim()
  if (!text) return { ok: false, error: "No response from Sonos helper" }
  try {
    var data = JSON.parse(text)
  } catch (e) {
    return { ok: false, error: "Could not parse Sonos status" }
  }
  if (!data || data.ok === false)
    return { ok: false, error: plainLabel((data && data.error) || "Sonos lookup failed", "Sonos lookup failed") }

  var speakers = Array.isArray(data.speakers) ? data.speakers.slice(0, MAX_SPEAKERS) : []
  var cleaned = []
  for (var i = 0; i < speakers.length; i++) {
    var row = sanitizeSpeaker(speakers[i])
    if (row) cleaned.push(row)
  }
  var outputId = String(data.outputId || "local")
  if (outputId !== "local" && !plainUid(outputId)) outputId = "local"
  return {
    ok: true,
    error: "",
    outputId: outputId,
    raopReady: data.raopReady === true,
    local: sanitizeLocal(data.local),
    speakers: cleaned
  }
}

function parseAction(raw) {
  var text = String(raw || "")
  if (text.length > MAX_RAW) return { ok: false, error: "Response too large" }
  text = text.trim()
  if (!text) return { ok: false, error: "No response" }
  try {
    return JSON.parse(text)
  } catch (e) {
    return { ok: false, error: "Could not parse response" }
  }
}

if (typeof module !== "undefined") {
  module.exports = {
    parseList: parseList,
    parseAction: parseAction,
    plainLabel: plainLabel,
    sanitizeSpeaker: sanitizeSpeaker,
    MAX_SPEAKERS: MAX_SPEAKERS,
    MAX_RAW: MAX_RAW
  }
}
