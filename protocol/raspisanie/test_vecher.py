"""Проверки того, что в vecher.py решается без сети и без vast.ai."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import vecher

KEY = "ab" * 32


class KeyHex(unittest.TestCase):
    def file(self, data):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_plain(self):
        self.assertEqual(vecher.key_hex(self.file(KEY.encode())), KEY)

    def test_windows_bom_crlf(self):
        self.assertEqual(vecher.key_hex(self.file(b"\xef\xbb\xbf" + KEY.encode() + b"\r\n")), KEY)

    def test_utf16(self):
        self.assertEqual(vecher.key_hex(self.file(KEY.encode("utf-16"))), KEY)

    def test_key_file_lines(self):
        data = ("address=axm1abcdef\npubkey_hex=02" + "cd" * 32 +
                "\nprivkey_hex=" + KEY + "\n").encode()
        self.assertEqual(vecher.key_hex(self.file(data)), KEY)

    def test_wrong_length_refused(self):
        with self.assertRaises(RuntimeError):
            vecher.key_hex(self.file(KEY[:-2].encode()))
        # лишний hex — тоже отказ, а не обрезка
        with self.assertRaises(RuntimeError):
            vecher.key_hex(self.file(("node1 " + KEY).encode()))


class Instance(unittest.TestCase):
    INST = {"public_ipaddr": "1.2.3.4 ",
            "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40022"}],
                      "8081/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40081"}]}}

    def test_addr(self):
        self.assertEqual(vecher.addr(self.INST), ("1.2.3.4", 40022, 40081))

    def test_addr_without_bundle_port(self):
        inst = {**self.INST, "ports": {"22/tcp": self.INST["ports"]["22/tcp"]}}
        self.assertIsNone(vecher.addr(inst))

    def test_addr_stopped(self):
        self.assertIsNone(vecher.addr({"public_ipaddr": "1.2.3.4", "ports": None}))

    def test_stale_image(self):
        conf = {"image": "ghcr.io/x/node@sha256:new"}
        self.assertEqual(vecher.stale_image({"image_uuid": "ghcr.io/x/node@sha256:old"}, conf),
                         "ghcr.io/x/node@sha256:old")
        self.assertIsNone(vecher.stale_image({"image_uuid": "ghcr.io/x/node@sha256:new"}, conf))
        # поле не пришло — машину не губим из-за неизвестности
        self.assertIsNone(vecher.stale_image({}, conf))


class Offers(unittest.TestCase):
    OFFERS = [{"id": 1, "machine_id": 10, "dph_total": 0.30},
              {"id": 2, "machine_id": 11, "dph_total": 0.40},
              {"id": 3, "machine_id": 12, "dph_total": 0.70}]

    def test_cheapest(self):
        self.assertEqual(vecher.pick_offer(self.OFFERS, 0.6, set(), set())["id"], 1)

    def test_skips_used_machine_and_tried_offer(self):
        self.assertEqual(vecher.pick_offer(self.OFFERS, 0.6, {10}, set())["id"], 2)
        self.assertEqual(vecher.pick_offer(self.OFFERS, 0.6, set(), {1})["id"], 2)

    def test_price_cap(self):
        self.assertIsNone(vecher.pick_offer(self.OFFERS, 0.6, {10, 11}, set()))


class Unfinished(unittest.TestCase):
    def test_states(self):
        jobs = [
            {"job_id": 1, "job": {"state": "settled", "paid": True}},
            {"job_id": 2, "job": {"state": "claimed", "paid": False}},
            {"job_id": 3, "job": {"state": "answered", "paid": False}},
            {"job_id": 4, "job": {"state": "answered", "paid": True}},
            {"job_id": 5, "job": {"state": "open", "paid": False}},
        ]
        self.assertEqual(vecher.unfinished(jobs), [2, 3])


class Launch(unittest.TestCase):
    def test_key_not_in_command(self):
        conf = {"contract": "axm1x"}
        cmd = vecher.launch_cmd(conf, "http://1.2.3.4:40081")
        self.assertIn("read -r KEY_HEX", cmd)
        self.assertIn("ENDPOINT=http://1.2.3.4:40081", cmd)
        self.assertNotIn(KEY, cmd)

    def test_pgrep_patterns_do_not_match_own_shell(self):
        # шаблон «[l]oop.py» не должен встречаться буквально как «loop.py»
        for cmd in (vecher.RUNNING, vecher.WIPE):
            self.assertNotIn("'loop.py", cmd)


class State(unittest.TestCase):
    def test_save_and_clear(self):
        d = tempfile.mkdtemp()
        old = vecher.HOME
        vecher.HOME = Path(d)
        try:
            vecher.save_instance("node1", 123)
            vecher.save_instance("node2", 456)
            vecher.save_instance("node1", None)
            self.assertEqual(json.loads((Path(d) / "state.json").read_text()),
                             {"node2": {"instance": 456}})
        finally:
            vecher.HOME = old


class ConfScript(unittest.TestCase):
    def test_formats(self):
        sh = vecher.CONF_SH.format(contract="axm1p", rpc="https://r")
        self.assertIn("C=axm1p;", sh)
        self.assertIn('{"config":{}}', sh)
        self.assertIn("ref_version", sh)


class StakeScript(unittest.TestCase):
    def test_formats(self):
        sh = vecher.STAKE_SH.format(contract="axm1t", bartr="axm1b", rpc="https://r", amount=300000)
        self.assertIn('{"deposit_stake":{}}', sh)
        self.assertIn('\\"amount\\":\\"300000\\"', sh)


if __name__ == "__main__":
    unittest.main()
