#!/usr/bin/env python3
"""Discover Sonos speakers, set volume, and switch this PC's audio output."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import subprocess
import sys
import time
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
USER_AGENT = "omarchy-sonos/1.0"

# LAN responders control Avahi and SOAP bodies. Bound every hop before the
# helper prints JSON or the shell collects it.
MAX_HTTP_BYTES = 256 * 1024
MAX_AVAHI_BYTES = 64 * 1024
MAX_PACTL_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 128 * 1024
MAX_SEED_IPS = 16
MAX_SPEAKERS = 32
MAX_SINKS = 64
MAX_NAME_CHARS = 64
MAX_UID_CHARS = 80
MAX_SINK_CHARS = 128
LIST_DEADLINE_S = 8.0

RENDERING_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
TOPOLOGY_URN = "urn:schemas-upnp-org:service:ZoneGroupTopology:1"
SOAP_PATHS = {
    "rendering": "/MediaRenderer/RenderingControl/Control",
    "topology": "/ZoneGroupTopology/Control",
}

_LAN_NETS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)
_CTRL = dict.fromkeys(list(range(32)) + [127])
_UID_RE = re.compile(r"^RINCON_[0-9A-Fa-f]{12,32}$")
_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def emit(payload: dict) -> int:
    blob = json.dumps(payload, ensure_ascii=True)
    if len(blob.encode("utf-8")) > MAX_STDOUT_BYTES:
        blob = json.dumps({"ok": False, "error": "response too large"}, ensure_ascii=True)
        sys.stdout.write(blob + "\n")
        return 1
    sys.stdout.write(blob + "\n")
    return 0 if payload.get("ok", False) else 1


def fail(message: str, **extra) -> int:
    payload = {"ok": False, "error": clean_text(message, 200, "error")}
    for key, value in extra.items():
        if isinstance(value, str):
            payload[key] = clean_text(value, MAX_UID_CHARS)
        else:
            payload[key] = value
    return emit(payload)


def is_lan_ipv4(value: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(str(value).strip())
    except (ipaddress.AddressValueError, ValueError):
        return False
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return False
    if str(addr) == "169.254.169.254":
        return False
    return any(addr in net for net in _LAN_NETS)


def clean_text(value: object, max_chars: int, fallback: str = "") -> str:
    text = str(value or "").translate(_CTRL).replace("<", "").replace(">", "")
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text or fallback


def clean_uid(value: object) -> str:
    uid = clean_text(value, MAX_UID_CHARS)
    return uid if _UID_RE.match(uid) else ""


def clean_mac(value: object) -> str:
    mac = clean_text(value, 12).lower()
    return mac if _MAC_RE.match(mac) else ""


def deadline_passed(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def read_limited(resp, limit: int) -> bytes:
    headers = getattr(resp, "headers", None)
    if headers is not None:
        raw_len = headers.get("Content-Length")
        if raw_len is not None:
            try:
                declared = int(raw_len)
            except ValueError:
                declared = -1
            if declared > limit:
                raise ValueError("response too large")
    buf = bytearray()
    while len(buf) <= limit:
        chunk = resp.read(min(65536, limit + 1 - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    if len(buf) > limit:
        raise ValueError("response too large")
    return bytes(buf)


def run_capped(command: list[str], timeout: float, max_bytes: int) -> bytes:
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return b""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    buf = bytearray()
    end = time.monotonic() + timeout
    try:
        while len(buf) <= max_bytes:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(buf)))
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass
    return bytes(buf[:max_bytes])


def sanitize_state(data: dict) -> dict:
    seeds: list[str] = []
    for ip in data.get("seedIps") or []:
        if isinstance(ip, str) and is_lan_ipv4(ip) and ip not in seeds:
            seeds.append(ip)
        if len(seeds) >= MAX_SEED_IPS:
            break
    data["seedIps"] = seeds
    speakers: list[dict] = []
    for spk in data.get("speakers") or []:
        if not isinstance(spk, dict):
            continue
        ip = str(spk.get("ip") or "")
        row = {
            "uid": clean_uid(spk.get("uid")),
            "ip": ip if is_lan_ipv4(ip) else "",
            "name": clean_text(spk.get("name"), MAX_NAME_CHARS, "Speaker"),
            "mac": clean_mac(spk.get("mac")),
        }
        if row["uid"] and row["ip"]:
            speakers.append(row)
        if len(speakers) >= MAX_SPEAKERS:
            break
    data["speakers"] = speakers
    local_sink = data.get("localSink")
    if isinstance(local_sink, str):
        data["localSink"] = clean_text(local_sink, MAX_SINK_CHARS)
    output = data.get("output")
    if isinstance(output, str) and output != "local":
        data["output"] = clean_uid(output) or "local"
    return data


def load_state() -> dict:
    try:
        text = STATE_PATH.read_text(encoding="utf-8")
        if len(text) > MAX_STDOUT_BYTES:
            return {}
        data = json.loads(text)
        if isinstance(data, dict):
            return sanitize_state(data)
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
    with _OPENER.open(req, timeout=timeout) as resp:
        return read_limited(resp, MAX_HTTP_BYTES).decode("utf-8", "replace")


def soap(ip: str, path: str, urn: str, action: str, inner_xml: str = "") -> str:
    if not is_lan_ipv4(ip):
        raise ValueError("refusing non-LAN speaker address")
    if path not in SOAP_PATHS.values():
        raise ValueError("invalid SOAP path")
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
    if len(blob) > MAX_HTTP_BYTES:
        return None
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.split("}")[-1] == local_name:
            return el.text
    return None


def rendering(ip: str, action: str, inner_xml: str) -> str:
    return soap(ip, SOAP_PATHS["rendering"], RENDERING_URN, action, inner_xml)


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
    return clean_mac(hex_part[:12].lower())


def host_from_location(location: str) -> str:
    try:
        parsed = urlparse(location)
    except ValueError:
        return ""
    if parsed.scheme != "http":
        return ""
    if parsed.port not in (None, 1400):
        return ""
    host = parsed.hostname or ""
    return host if is_lan_ipv4(host) else ""


def avahi_ips() -> list[str]:
    raw = run_capped(
        ["timeout", "2.5", "avahi-browse", "-prt", "_sonos._tcp"],
        timeout=4,
        max_bytes=MAX_AVAHI_BYTES,
    )
    ips: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        parts = line.split(";")
        if len(parts) < 8:
            continue
        if parts[0] != "=" or parts[2] != "IPv4":
            continue
        ip = parts[7].strip()
        if is_lan_ipv4(ip) and ip not in ips:
            ips.append(ip)
        if len(ips) >= MAX_SEED_IPS:
            break
    return ips


def zone_group_state(ip: str) -> ET.Element:
    raw = soap(ip, SOAP_PATHS["topology"], TOPOLOGY_URN, "GetZoneGroupState")
    inner = xml_text(raw, "ZoneGroupState")
    if not inner:
        raise RuntimeError("empty zone group state")
    if len(inner) > MAX_HTTP_BYTES:
        raise RuntimeError("zone group state too large")
    return ET.fromstring(inner)


def collect_members(tree: ET.Element) -> list[dict]:
    speakers: list[dict] = []
    seen: set[str] = set()
    for group in tree.findall(".//ZoneGroup"):
        coordinator = clean_uid(group.get("Coordinator") or "")
        parent_name = ""
        for child in list(group):
            if child.tag.split("}")[-1] == "ZoneGroupMember":
                parent_name = clean_text(child.get("ZoneName"), MAX_NAME_CHARS) or parent_name
                row = member_row(child, coordinator, parent_name)
                if row and row["uid"] not in seen:
                    speakers.append(row)
                    seen.add(row["uid"])
                    if len(speakers) >= MAX_SPEAKERS:
                        speakers.sort(key=lambda s: (not s["selectable"], s["name"].lower(), s["uid"]))
                        return speakers
                for sat in list(child):
                    if sat.tag.split("}")[-1] != "Satellite":
                        continue
                    sat_row = member_row(sat, coordinator, parent_name)
                    if sat_row and sat_row["uid"] not in seen:
                        speakers.append(sat_row)
                        seen.add(sat_row["uid"])
                        if len(speakers) >= MAX_SPEAKERS:
                            speakers.sort(key=lambda s: (not s["selectable"], s["name"].lower(), s["uid"]))
                            return speakers
    speakers.sort(key=lambda s: (not s["selectable"], s["name"].lower(), s["uid"]))
    return speakers


def member_row(el: ET.Element, coordinator: str, parent_name: str) -> dict | None:
    uid = clean_uid(el.get("UUID") or "")
    name = clean_text(el.get("ZoneName"), MAX_NAME_CHARS, "Speaker")
    invisible = el.get("Invisible") == "1"
    if invisible and parent_name and name == parent_name:
        return None
    ip = host_from_location(el.get("Location") or "")
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
        "volumeReady": False,
        "sinkName": "",
        "sinkId": "",
        "sinkReady": False,
        "selected": False,
    }


def fill_levels(speakers: list[dict], deadline: float | None = None) -> None:
    if not speakers or deadline_passed(deadline):
        return

    def one(spk: dict) -> tuple[str, int, bool, bool]:
        try:
            return spk["uid"], get_volume(spk["ip"]), get_mute(spk["ip"]), True
        except Exception:
            return spk["uid"], 0, False, False

    submitted: list = []
    with ThreadPoolExecutor(max_workers=min(8, len(speakers))) as pool:
        for spk in speakers:
            if deadline_passed(deadline):
                break
            submitted.append(pool.submit(one, spk))
        by_uid = {}
        for future in as_completed(submitted):
            if deadline_passed(deadline):
                break
            uid, vol, muted, ok = future.result()
            by_uid[uid] = (vol, muted, ok)
    for spk in speakers:
        vol, muted, ok = by_uid.get(spk["uid"], (0, False, False))
        spk["volumeReady"] = ok
        if ok:
            spk["volume"] = vol
            spk["muted"] = muted


def seed_ips(state: dict) -> list[str]:
    ips: list[str] = []
    for ip in state.get("seedIps") or []:
        if isinstance(ip, str) and is_lan_ipv4(ip) and ip not in ips:
            ips.append(ip)
        if len(ips) >= MAX_SEED_IPS:
            return ips
    for ip in avahi_ips():
        if ip not in ips:
            ips.append(ip)
        if len(ips) >= MAX_SEED_IPS:
            break
    return ips


def discover_speakers(state: dict, deadline: float | None = None) -> list[dict]:
    if deadline is None:
        deadline = time.monotonic() + LIST_DEADLINE_S
    last_error = None
    for ip in seed_ips(state):
        if deadline_passed(deadline):
            break
        try:
            tree = zone_group_state(ip)
            speakers = collect_members(tree)
            if speakers:
                fill_levels(speakers, deadline)
                state["seedIps"] = [s["ip"] for s in speakers][:MAX_SEED_IPS]
                state["speakers"] = [
                    {"uid": s["uid"], "ip": s["ip"], "name": s["name"], "mac": s["mac"]}
                    for s in speakers
                ]
                return speakers
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise RuntimeError(f"could not reach Sonos speakers ({clean_text(str(last_error), 160)})")
    return []


def run_json(command: list[str], timeout: float = 2.0):
    raw = run_capped(command, timeout=timeout, max_bytes=MAX_PACTL_BYTES)
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def list_sinks() -> list[dict]:
    data = run_json(["pactl", "--format=json", "list", "sinks"])
    if not isinstance(data, list):
        return []
    sinks = []
    for item in data:
        if not isinstance(item, dict):
            continue
        props = item.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        name = clean_text(item.get("name") or "", MAX_SINK_CHARS)
        desc = clean_text(
            item.get("description") or props.get("device.description") or name,
            MAX_NAME_CHARS,
            name,
        )
        blob = " ".join(
            [
                name,
                desc,
                clean_text(props.get("node.nick") or "", MAX_SINK_CHARS),
                clean_text(props.get("node.name") or "", MAX_SINK_CHARS),
                clean_text(item.get("driver") or "", MAX_SINK_CHARS),
                clean_text(props.get("device.description") or "", MAX_NAME_CHARS),
            ]
        ).lower()
        serial = clean_text(props.get("object.serial") or item.get("index") or "", 32)
        node_id = clean_text(props.get("object.id") or serial, 32)
        if len(sinks) >= MAX_SINKS:
            break
        sinks.append(
            {
                "name": name,
                "description": desc,
                "nick": clean_text(props.get("node.nick") or "", MAX_NAME_CHARS),
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
    return clean_text(proc.stdout.strip(), MAX_SINK_CHARS)


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


def run_quiet(command: list[str], timeout: float = 2.0) -> None:
    try:
        subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def speaker_sink(spk: dict, sinks: list[dict]) -> dict | None:
    for item in sinks:
        if sink_matches_speaker(item, spk):
            return item
    wanted = spk.get("sinkName") or ""
    if wanted:
        for item in sinks:
            if item.get("name") == wanted:
                return item
    return None


def set_sink_level(sink: dict, level: int, muted: bool | None = None) -> None:
    """Keep the PipeWire RAOP sink at the speaker's volume, not 100%.

    AirPlay starts a session by sending the sink volume to the device. New
    RAOP sinks default to 100%, which would blast the room.
    """
    name = sink.get("name") or ""
    node_id = str(sink.get("id") or sink.get("serial") or "")
    level = max(0, min(100, int(level)))
    vol = f"{level}%"
    if name:
        run_quiet(["pactl", "set-sink-volume", name, vol])
        if muted is not None:
            run_quiet(["pactl", "set-sink-mute", name, "1" if muted else "0"])
    if node_id:
        run_quiet(["wpctl", "set-volume", node_id, vol])
        if muted is not None:
            run_quiet(["wpctl", "set-mute", node_id, "1" if muted else "0"])


def match_sink_to_speaker(sink: dict, spk: dict) -> tuple[int, bool]:
    ip = spk.get("ip") or ""
    volume = int(spk.get("volume") or 0)
    muted = bool(spk.get("muted"))
    if ip:
        try:
            volume = get_volume(ip)
            muted = get_mute(ip)
        except Exception:
            pass
    set_sink_level(sink, volume, muted)
    return volume, muted


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
        muted = get_mute(spk["ip"])
        if str(state.get("output") or "") == uid:
            sink = speaker_sink(spk, list_sinks())
            if sink:
                set_sink_level(sink, volume, muted)
        return emit({"ok": True, "uid": uid, "volume": volume, "muted": muted})
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
        volume = get_volume(spk["ip"])
        if str(state.get("output") or "") == uid:
            sink = speaker_sink(spk, list_sinks())
            if sink:
                set_sink_level(sink, volume, muted)
        return emit({"ok": True, "uid": uid, "muted": muted, "volume": volume})
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
        sink = speaker_sink(spk, sinks)
        if not sink:
            state["output"] = spk["uid"]
            save_state(state)
            return fail(
                "AirPlay output for this speaker is not ready yet",
                uid=spk["uid"],
                pending=True,
            )
        volume, muted = match_sink_to_speaker(sink, spk)
        apply_sink(sink)
        state["output"] = spk["uid"]
        save_state(state)
        return emit(
            {
                "ok": True,
                "outputId": spk["uid"],
                "sinkName": sink["name"],
                "volume": volume,
                "muted": muted,
            }
        )
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
