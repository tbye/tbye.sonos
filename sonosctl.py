#!/usr/bin/env python3
"""Discover Sonos speakers, set volume, and switch this PC's audio output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.dont_write_bytecode = True

PLUGIN_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy-sonos"
STATE_PATH = STATE_DIR / "state.json"
SOAP_TIMEOUT = 1.6
HTTP_TIMEOUT = 1.6
USER_AGENT = "omarchy-sonos/1.0"

RENDERING_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
TOPOLOGY_URN = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"


def emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0 if payload.get("ok", False) else 1


def fail(message: str, **extra) -> int:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return emit(payload)


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def http_post(url: str, body: bytes, headers: dict, timeout: float = SOAP_TIMEOUT) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def soap(ip: str, path: str, urn: str, action: str, inner_xml: str = "") -> str:
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{urn}">{inner_xml}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    return http_post(
        f"http://{ip}:1400{path}",
        envelope.encode("utf-8"),
        {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{urn}#{action}"',
            "User-Agent": USER_AGENT,
        },
    )


def xml_text(blob: str, local_name: str) -> str | None:
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.split("}")[-1] == local_name:
            return el.text
    return None


def rendering(ip: str, action: str, inner_xml: str) -> str:
    return soap(ip, "/MediaRenderer/RenderingControl/Control", RENDERING_URN, action, inner_xml)


def get_volume(ip: str) -> int:
    raw = rendering(
        ip,
        "GetVolume",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    text = xml_text(raw, "CurrentVolume") or "0"
    try:
        return max(0, min(100, int(text)))
    except ValueError:
        return 0


def set_volume(ip: str, level: int) -> int:
    level = max(0, min(100, int(level)))
    rendering(
        ip,
        "SetVolume",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>"
        f"<DesiredVolume>{level}</DesiredVolume>",
    )
    try:
        return get_volume(ip)
    except Exception:
        return level


def get_mute(ip: str) -> bool:
    raw = rendering(
        ip,
        "GetMute",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>",
    )
    text = (xml_text(raw, "CurrentMute") or "0").strip()
    return text not in ("0", "false", "False")


def set_mute(ip: str, muted: bool) -> bool:
    rendering(
        ip,
        "SetMute",
        "<InstanceID>0</InstanceID><Channel>Master</Channel>"
        f"<DesiredMute>{1 if muted else 0}</DesiredMute>",
    )
    try:
        return get_mute(ip)
    except Exception:
        return muted


def uid_mac(uid: str) -> str:
    raw = uid.replace("RINCON_", "").replace("rincon_", "")
    hex_part = "".join(ch for ch in raw if ch in "0123456789abcdefABCDEF")
    return hex_part[:12].lower()


def host_from_location(location: str) -> str:
    try:
        return urlparse(location).hostname or ""
    except ValueError:
        return ""


def avahi_ips() -> list[str]:
    try:
        proc = subprocess.run(
            ["timeout", "2.5", "avahi-browse", "-prt", "_sonos._tcp"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    ips: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split(";")
        if len(parts) < 8:
            continue
        if parts[0] != "=" or parts[2] != "IPv4":
            continue
        ip = parts[7].strip()
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def zone_group_state(ip: str) -> ET.Element:
    raw = soap(ip, "/ZoneGroupTopology/Control", TOPOLOGY_URN, "GetZoneGroupState")
    inner = xml_text(raw, "ZoneGroupState")
    if not inner:
        raise RuntimeError("empty zone group state")
    return ET.fromstring(inner)


def collect_members(tree: ET.Element) -> list[dict]:
    speakers: list[dict] = []
    seen: set[str] = set()
    for group in tree.findall(".//ZoneGroup"):
        coordinator = group.get("Coordinator") or ""
        parent_name = ""
        for child in list(group):
            if child.tag.split("}")[-1] == "ZoneGroupMember":
                parent_name = child.get("ZoneName") or parent_name
                row = member_row(child, coordinator, parent_name)
                if row and row["uid"] not in seen:
                    speakers.append(row)
                    seen.add(row["uid"])
                for sat in list(child):
                    if sat.tag.split("}")[-1] != "Satellite":
                        continue
                    sat_row = member_row(sat, coordinator, parent_name)
                    if sat_row and sat_row["uid"] not in seen:
                        speakers.append(sat_row)
                        seen.add(sat_row["uid"])
    speakers.sort(key=lambda s: (not s["selectable"], s["name"].lower(), s["uid"]))
    return speakers


def member_row(el: ET.Element, coordinator: str, parent_name: str) -> dict | None:
    uid = el.get("UUID") or ""
    name = (el.get("ZoneName") or "").strip() or "Speaker"
    invisible = el.get("Invisible") == "1"
    if invisible and parent_name and name == parent_name:
        return None
    location = el.get("Location") or ""
    ip = host_from_location(location)
    if not uid or not ip:
        return None
    airplay = el.get("AirPlayEnabled") == "1"
    is_coordinator = uid == coordinator
    selectable = (not invisible) and (airplay or is_coordinator)
    return {
        "uid": uid,
        "name": name,
        "ip": ip,
        "mac": uid_mac(uid),
        "invisible": invisible,
        "coordinator": is_coordinator,
        "airplay": airplay,
        "selectable": selectable,
        "groupCoordinator": coordinator,
        "muted": False,
        "volume": 0,
        "sinkName": "",
        "sinkId": "",
        "sinkReady": False,
        "selected": False,
    }


def fill_levels(speakers: list[dict]) -> None:
    if not speakers:
        return

    def one(spk: dict) -> tuple[str, int, bool]:
        try:
            return spk["uid"], get_volume(spk["ip"]), get_mute(spk["ip"])
        except Exception:
            return spk["uid"], 0, False

    with ThreadPoolExecutor(max_workers=min(8, len(speakers))) as pool:
        futures = [pool.submit(one, spk) for spk in speakers]
        by_uid = {uid: (vol, muted) for uid, vol, muted in (f.result() for f in as_completed(futures))}
    for spk in speakers:
        vol, muted = by_uid.get(spk["uid"], (0, False))
        spk["volume"] = vol
        spk["muted"] = muted


def seed_ips(state: dict) -> list[str]:
    ips: list[str] = []
    for ip in state.get("seedIps") or []:
        if isinstance(ip, str) and ip and ip not in ips:
            ips.append(ip)
    for ip in avahi_ips():
        if ip not in ips:
            ips.append(ip)
    return ips


def discover_speakers(state: dict) -> list[dict]:
    last_error = None
    for ip in seed_ips(state):
        try:
            tree = zone_group_state(ip)
            speakers = collect_members(tree)
            if speakers:
                fill_levels(speakers)
                state["seedIps"] = [s["ip"] for s in speakers]
                state["speakers"] = [
                    {"uid": s["uid"], "ip": s["ip"], "name": s["name"], "mac": s["mac"]}
                    for s in speakers
                ]
                return speakers
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise RuntimeError(f"could not reach Sonos speakers ({last_error})")
    return []


def run_json(command: list[str], timeout: float = 2.0):
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def list_sinks() -> list[dict]:
    data = run_json(["pactl", "--format=json", "list", "sinks"])
    if not isinstance(data, list):
        return []
    sinks = []
    for item in data:
        props = item.get("properties") or {}
        name = str(item.get("name") or "")
        desc = str(item.get("description") or props.get("device.description") or name)
        blob = " ".join(
            [
                name,
                desc,
                str(props.get("node.nick") or ""),
                str(props.get("node.name") or ""),
                str(item.get("driver") or ""),
                str(props.get("device.description") or ""),
            ]
        ).lower()
        serial = str(props.get("object.serial") or item.get("index") or "")
        node_id = str(props.get("object.id") or serial)
        sinks.append(
            {
                "name": name,
                "description": desc,
                "nick": str(props.get("node.nick") or ""),
                "serial": serial,
                "id": node_id,
                "isRaop": "raop" in blob or "airplay" in blob,
                "isInternal": "internal" in str(props.get("device.form_factor") or "").lower()
                or "analog" in blob
                or "built-in" in blob,
                "blob": blob,
            }
        )
    return sinks


def default_sink_name() -> str:
    try:
        proc = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def sink_blob_compact(blob: str) -> str:
    return blob.replace(":", "").replace("-", "").replace("_", "").replace(".", "")


def sink_matches_speaker(sink: dict, speaker: dict) -> bool:
    """Prefer the Sonos MAC encoded in raop_sink.Sonos-<MAC> over room-name hits.

    Rooms like Basement also have an Apple TV advertising the same AirPlay name.
    """
    if not sink.get("isRaop"):
        return False
    blob = sink.get("blob") or ""
    compact = sink_blob_compact(blob)
    mac = speaker.get("mac") or ""
    if mac and mac in compact:
        return True
    name = (speaker.get("name") or "").lower()
    return bool(name) and "sonos" in compact and name in blob


def attach_sinks(speakers: list[dict], sinks: list[dict]) -> None:
    for spk in speakers:
        for sink in sinks:
            if sink_matches_speaker(sink, spk):
                spk["sinkName"] = sink["name"]
                spk["sinkId"] = sink["id"] or sink["serial"]
                spk["sinkReady"] = True
                break


def find_local_sink(sinks: list[dict], state: dict) -> dict | None:
    wanted = state.get("localSink") or ""
    if wanted:
        for sink in sinks:
            if sink["name"] == wanted and not sink["isRaop"]:
                return sink
    internals = [s for s in sinks if s["isInternal"] and not s["isRaop"]]
    if internals:
        return internals[0]
    for sink in sinks:
        if not sink["isRaop"]:
            return sink
    return None


def remember_local(state: dict, sinks: list[dict]) -> None:
    current = default_sink_name()
    for sink in sinks:
        if sink["name"] == current and not sink["isRaop"]:
            state["localSink"] = sink["name"]
            state["localSinkId"] = sink["id"] or sink["serial"]
            return
    if not state.get("localSink"):
        local = find_local_sink(sinks, state)
        if local:
            state["localSink"] = local["name"]
            state["localSinkId"] = local["id"] or local["serial"]


def apply_sink(sink: dict) -> None:
    node_id = sink.get("id") or sink.get("serial") or ""
    name = sink.get("name") or ""
    if not node_id or not name:
        raise RuntimeError("missing sink identity")
    subprocess.run(
        ["omarchy-audio-output-set-default", str(node_id), str(name)],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )


def current_output_id(speakers: list[dict], sinks: list[dict], state: dict) -> str:
    current = default_sink_name()
    for sink in sinks:
        if sink["name"] != current:
            continue
        if sink["isRaop"]:
            for spk in speakers:
                if sink_matches_speaker(sink, spk):
                    return spk["uid"]
            return str(state.get("output") or "sonos")
        return "local"
    saved = str(state.get("output") or "local")
    return saved if saved else "local"


def local_payload(sinks: list[dict], state: dict, output_id: str) -> dict:
    local = find_local_sink(sinks, state)
    return {
        "id": "local",
        "name": "This computer",
        "description": (local or {}).get("description") or "Built-in Audio",
        "sinkName": (local or {}).get("name") or "",
        "sinkId": (local or {}).get("id") or (local or {}).get("serial") or "",
        "selected": output_id == "local",
    }


def mark_selected(speakers: list[dict], output_id: str) -> None:
    for spk in speakers:
        spk["selected"] = spk["uid"] == output_id


def snapshot() -> dict:
    state = load_state()
    speakers = discover_speakers(state)
    sinks = list_sinks()
    remember_local(state, sinks)
    attach_sinks(speakers, sinks)
    output_id = current_output_id(speakers, sinks, state)
    state["output"] = output_id
    save_state(state)
    mark_selected(speakers, output_id)
    raop_ready = any(s["isRaop"] for s in sinks)
    return {
        "ok": True,
        "error": "",
        "outputId": output_id,
        "raopReady": raop_ready,
        "local": local_payload(sinks, state, output_id),
        "speakers": speakers,
    }


def find_speaker(speakers: list[dict], uid: str) -> dict:
    for spk in speakers:
        if spk["uid"] == uid:
            return spk
    raise KeyError(uid)


def cached_speaker(state: dict, uid: str) -> dict | None:
    for spk in state.get("speakers") or []:
        if isinstance(spk, dict) and spk.get("uid") == uid and spk.get("ip"):
            return spk
    return None


def resolve_speaker(state: dict, uid: str) -> dict:
    cached = cached_speaker(state, uid)
    if cached:
        return cached
    speakers = discover_speakers(state)
    save_state(state)
    return find_speaker(speakers, uid)


def cmd_list(_args: list[str]) -> int:
    try:
        return emit(snapshot())
    except Exception as exc:
        return fail(str(exc))


def cmd_volume(args: list[str]) -> int:
    if len(args) < 2:
        return fail("usage: volume <uid> <0-100>")
    uid, raw_level = args[0], args[1]
    try:
        level = max(0, min(100, int(float(raw_level))))
    except ValueError:
        return fail("volume must be a number")
    state = load_state()
    try:
        spk = resolve_speaker(state, uid)
        volume = set_volume(spk["ip"], level)
        return emit({"ok": True, "uid": uid, "volume": volume, "muted": get_mute(spk["ip"])})
    except KeyError:
        return fail(f"unknown speaker {uid}")
    except Exception as exc:
        return fail(str(exc), uid=uid)


def cmd_mute(args: list[str]) -> int:
    if not args:
        return fail("usage: mute <uid> [on|off|toggle]")
    uid = args[0]
    mode = (args[1] if len(args) > 1 else "toggle").lower()
    state = load_state()
    try:
        spk = resolve_speaker(state, uid)
        current = get_mute(spk["ip"])
        if mode in ("on", "1", "true"):
            wanted = True
        elif mode in ("off", "0", "false"):
            wanted = False
        else:
            wanted = not current
        muted = set_mute(spk["ip"], wanted)
        return emit({"ok": True, "uid": uid, "muted": muted, "volume": get_volume(spk["ip"])})
    except KeyError:
        return fail(f"unknown speaker {uid}")
    except Exception as exc:
        return fail(str(exc), uid=uid)


def cmd_set_output(args: list[str]) -> int:
    if not args:
        return fail("usage: set-output local|<uid>")
    target = args[0]
    state = load_state()
    try:
        sinks = list_sinks()
        remember_local(state, sinks)
        if target == "local":
            sink = find_local_sink(sinks, state)
            if not sink:
                return fail("no local audio sink found")
            apply_sink(sink)
            state["output"] = "local"
            save_state(state)
            return emit({"ok": True, "outputId": "local", "sinkName": sink["name"]})
        speakers = discover_speakers(state)
        attach_sinks(speakers, sinks)
        spk = find_speaker(speakers, target)
        sink = None
        for item in sinks:
            if sink_matches_speaker(item, spk):
                sink = item
                break
        if not sink:
            state["output"] = spk["uid"]
            save_state(state)
            return fail(
                "AirPlay output for this speaker is not ready yet",
                uid=spk["uid"],
                pending=True,
            )
        apply_sink(sink)
        state["output"] = spk["uid"]
        save_state(state)
        return emit({"ok": True, "outputId": spk["uid"], "sinkName": sink["name"]})
    except KeyError:
        return fail(f"unknown speaker {target}")
    except Exception as exc:
        return fail(str(exc))


def cmd_status(_args: list[str]) -> int:
    try:
        data = snapshot()
        return emit(
            {
                "ok": True,
                "outputId": data["outputId"],
                "raopReady": data["raopReady"],
                "speakerCount": len(data["speakers"]),
                "local": data["local"],
            }
        )
    except Exception as exc:
        return fail(str(exc))


COMMANDS = {
    "list": cmd_list,
    "volume": cmd_volume,
    "mute": cmd_mute,
    "set-output": cmd_set_output,
    "status": cmd_status,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stderr.write(
            "usage: sonosctl.py list|status|volume <uid> <0-100>|mute <uid> [on|off|toggle]|set-output local|<uid>\n"
        )
        return 2
    command = argv[0]
    handler = COMMANDS.get(command)
    if handler is None:
        return fail(f"unknown command {command}")
    # Avoid leaving a stale umask-restricted state file in a world-readable config dir.
    os.umask(0o077)
    return handler(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
