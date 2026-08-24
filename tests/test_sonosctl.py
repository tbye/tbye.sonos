#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sonosctl", ROOT / "sonosctl.py")
sonosctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sonosctl)


class FakeResp:
    def __init__(self, data: bytes, length=None):
        self._buf = io.BytesIO(data)
        self.headers = {}
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


def member_xml(index: int, name: str = "", ip: str = "192.168.1.10") -> str:
    uid = f"RINCON_{index:012X}01400"
    zone = name or f"Room {index}"
    return (
        f'<ZoneGroup Coordinator="{uid}">'
        f'<ZoneGroupMember UUID="{uid}" ZoneName="{zone}" '
        f'Location="http://{ip}:1400/xml/device_description.xml" '
        f'AirPlayEnabled="1"/>'
        f"</ZoneGroup>"
    )


class LanIpTests(unittest.TestCase):
    def test_allows_rfc1918_and_link_local(self):
        self.assertTrue(sonosctl.is_lan_ipv4("192.168.1.5"))
        self.assertTrue(sonosctl.is_lan_ipv4("10.0.0.8"))
        self.assertTrue(sonosctl.is_lan_ipv4("172.16.4.1"))
        self.assertTrue(sonosctl.is_lan_ipv4("169.254.10.2"))

    def test_rejects_loopback_public_and_metadata(self):
        self.assertFalse(sonosctl.is_lan_ipv4("127.0.0.1"))
        self.assertFalse(sonosctl.is_lan_ipv4("8.8.8.8"))
        self.assertFalse(sonosctl.is_lan_ipv4("169.254.169.254"))
        self.assertFalse(sonosctl.is_lan_ipv4("not-an-ip"))
        self.assertFalse(sonosctl.is_lan_ipv4("sonos.local"))


class SanitizeTests(unittest.TestCase):
    def test_strips_markup_controls_and_length(self):
        raw = "<img src=\"http://evil.example/x.png\">" + ("A" * 200)
        cleaned = sonosctl.clean_text(raw, sonosctl.MAX_NAME_CHARS, "Speaker")
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertLessEqual(len(cleaned), sonosctl.MAX_NAME_CHARS)

    def test_uid_must_look_like_sonos(self):
        self.assertEqual(sonosctl.clean_uid("RINCON_000E5824D1A001400"), "RINCON_000E5824D1A001400")
        self.assertEqual(sonosctl.clean_uid("not a uid"), "")
        self.assertEqual(sonosctl.clean_uid("<script>"), "")


class LocationTests(unittest.TestCase):
    def test_only_http_lan_ipv4_on_port_1400(self):
        self.assertEqual(
            sonosctl.host_from_location("http://192.168.1.9:1400/xml/device_description.xml"),
            "192.168.1.9",
        )
        self.assertEqual(sonosctl.host_from_location("http://127.0.0.1:1400/xml"), "")
        self.assertEqual(sonosctl.host_from_location("http://8.8.8.8:1400/xml"), "")
        self.assertEqual(sonosctl.host_from_location("http://evil.example:1400/xml"), "")
        self.assertEqual(sonosctl.host_from_location("https://192.168.1.9:1400/xml"), "")
        self.assertEqual(sonosctl.host_from_location("http://192.168.1.9:80/xml"), "")


class HttpBoundTests(unittest.TestCase):
    def test_read_limited_rejects_oversize_body(self):
        resp = FakeResp(b"x" * 50, length=None)
        with self.assertRaises(ValueError):
            sonosctl.read_limited(resp, 16)

    def test_read_limited_rejects_content_length(self):
        resp = FakeResp(b"ok", length=sonosctl.MAX_HTTP_BYTES + 1)
        with self.assertRaises(ValueError):
            sonosctl.read_limited(resp, sonosctl.MAX_HTTP_BYTES)

    def test_soap_refuses_non_lan_and_unknown_path(self):
        with self.assertRaises(ValueError):
            sonosctl.soap("8.8.8.8", sonosctl.SOAP_PATHS["topology"], sonosctl.TOPOLOGY_URN, "GetZoneGroupState")
        with self.assertRaises(ValueError):
            sonosctl.soap("192.168.1.5", "/etc/passwd", sonosctl.TOPOLOGY_URN, "GetZoneGroupState")


class TopologyCapTests(unittest.TestCase):
    def test_collect_members_caps_count_and_names(self):
        groups = "".join(member_xml(i, name=f"&lt;img src=&quot;http://e/{i}&quot;&gt;") for i in range(80))
        tree = ET.fromstring(f"<ZoneGroupState><ZoneGroups>{groups}</ZoneGroups></ZoneGroupState>")
        speakers = sonosctl.collect_members(tree)
        self.assertEqual(len(speakers), sonosctl.MAX_SPEAKERS)
        self.assertTrue(all(s["uid"].startswith("RINCON_") for s in speakers))
        self.assertTrue(all("<" not in s["name"] and ">" not in s["name"] for s in speakers))
        self.assertTrue(all(s["ip"] == "192.168.1.10" for s in speakers))


class AvahiCapTests(unittest.TestCase):
    def test_avahi_ips_validates_and_caps(self):
        lines = []
        for i in range(40):
            lines.append(f"=;eth0;IPv4;Sonos {i};_sonos._tcp;local;speaker.local;192.168.1.{i};1400;")
        lines.append("=;eth0;IPv4;loop;_sonos._tcp;local;x.local;127.0.0.1;1400;")
        lines.append("=;eth0;IPv4;pub;_sonos._tcp;local;x.local;8.8.8.8;1400;")
        blob = "\n".join(lines).encode()
        with mock.patch.object(sonosctl, "run_capped", return_value=blob):
            ips = sonosctl.avahi_ips()
        self.assertEqual(len(ips), sonosctl.MAX_SEED_IPS)
        self.assertNotIn("127.0.0.1", ips)
        self.assertNotIn("8.8.8.8", ips)

    def test_run_capped_stops_at_max_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dump.py"
            path.write_text("import sys\nsys.stdout.write('A' * 200000)\nsys.stdout.flush()\n", encoding="utf-8")
            data = sonosctl.run_capped([sys.executable, str(path)], timeout=2, max_bytes=64)
        self.assertEqual(len(data), 64)


class EmitCapTests(unittest.TestCase):
    def test_emit_refuses_huge_payload(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = sonosctl.emit({"ok": True, "blob": "x" * (sonosctl.MAX_STDOUT_BYTES + 10)})
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"], "response too large")


class StateSanitizeTests(unittest.TestCase):
    def test_load_state_drops_bad_seeds_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "seedIps": ["127.0.0.1", "192.168.0.4", "not-ip", "8.8.8.8"],
                        "speakers": [
                            {
                                "uid": "RINCON_000E5824D1A001400",
                                "ip": "192.168.0.4",
                                "name": "<b>Kitchen</b>",
                                "mac": "000e5824d1a0",
                            },
                            {"uid": "nope", "ip": "192.168.0.5", "name": "X"},
                        ],
                        "output": "<img>",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(sonosctl, "STATE_PATH", state_path):
                state = sonosctl.load_state()
        self.assertEqual(state["seedIps"], ["192.168.0.4"])
        self.assertEqual(len(state["speakers"]), 1)
        self.assertEqual(state["speakers"][0]["name"], "bKitchen/b")
        self.assertEqual(state["output"], "local")


class StateFileTests(unittest.TestCase):
    def test_load_state_rejects_oversize_before_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_bytes(b"{" + b"x" * (sonosctl.MAX_STATE_BYTES + 8))
            with mock.patch.object(sonosctl, "STATE_PATH", state_path):
                self.assertEqual(sonosctl.load_state(), {})

    def test_load_state_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "other.json"
            target.write_text(json.dumps({"seedIps": ["192.168.0.4"]}), encoding="utf-8")
            state_path = Path(tmp) / "state.json"
            state_path.symlink_to(target)
            with mock.patch.object(sonosctl, "STATE_PATH", state_path):
                self.assertEqual(sonosctl.load_state(), {})

    def test_load_state_rejects_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            os.mkfifo(state_path)
            with mock.patch.object(sonosctl, "STATE_PATH", state_path):
                self.assertEqual(sonosctl.load_state(), {})

    def test_save_state_does_not_follow_predictable_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            state_path = state_dir / "state.json"
            victim = state_dir / "victim.txt"
            victim.write_text("keep me", encoding="utf-8")
            predictable = state_dir / "state.json.tmp"
            predictable.symlink_to(victim)
            with mock.patch.object(sonosctl, "STATE_DIR", state_dir), mock.patch.object(
                sonosctl, "STATE_PATH", state_path
            ):
                sonosctl.save_state({"output": "local"})
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
            self.assertTrue(predictable.is_symlink())
            self.assertTrue(state_path.is_file())
            self.assertFalse(state_path.is_symlink())
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["output"], "local")

    def test_save_state_replaces_state_path_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            victim = state_dir / "other.json"
            victim.write_text('{"keep": true}\n', encoding="utf-8")
            state_path = state_dir / "state.json"
            state_path.symlink_to(victim)
            with mock.patch.object(sonosctl, "STATE_DIR", state_dir), mock.patch.object(
                sonosctl, "STATE_PATH", state_path
            ):
                sonosctl.save_state({"output": "local"})
            self.assertFalse(state_path.is_symlink())
            self.assertEqual(json.loads(victim.read_text(encoding="utf-8")), {"keep": True})
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["output"], "local")


if __name__ == "__main__":
    unittest.main()
