"""Проверки сторожа без vast.ai и Telegram: машины подставные, время задано."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import storozh
import vecher

IMG = "ghcr.io/x/node@sha256:aa"
CONF = {"image": IMG, "nodes": [{"name": "node1"}, {"name": "node2"}]}


def at(hhmm, day=18):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, day, h, m, tzinfo=storozh.MSK).timestamp()


def inst(i, status="running", start=None, label=None, image=IMG, chat=False, dph=0.5):
    d = {"id": i, "actual_status": status, "start_date": start, "label": label,
         "image_uuid": image, "dph_total": dph, "public_ipaddr": "1.2.3.4"}
    if chat:
        d["ports"] = {"8080/tcp": [{"HostPort": "40000"}]}
    return d


class Tick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(vecher, "HOME", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.nodes = {}
        q = mock.patch.object(vecher, "load_state", lambda: self.nodes)
        q.start()
        self.addCleanup(q.stop)

    def run_ticks(self, steps, chat=lambda url: False):
        st, out = None, []
        for now, insts in steps:
            st, msgs = storozh.tick(CONF, {str(i["id"]): i for i in insts}, st, now, chat)
            out.append(msgs)
        return out

    def test_first_run_silent(self):
        out = self.run_ticks([(at("19:10"), [inst(1)])])
        self.assertEqual(out, [[]])

    def test_up_and_destroyed(self):
        t0 = at("12:00")
        a = inst(5, start=t0, label="chat", image="webui", dph=0.2)
        out = self.run_ticks([(t0, []), (t0 + 300, [a]), (t0 + 7200, [])])
        self.assertTrue(out[1][0].startswith("▶️ Поднялась chat (5)"))
        self.assertIn("Уничтожена chat (5), работала 2.0 ч, ≈$0.40", out[2][0])

    def test_stopped(self):
        t0 = at("12:00")
        out = self.run_ticks([(t0, [inst(5, image="w")]),
                              (t0 + 60, [inst(5, image="w", status="exited", start=t0)])])
        self.assertIn("⏹ Остановилась", out[1][0])
        self.assertIn("Диск", out[1][0])

    def test_chat_ready_and_not_ready(self):
        t0 = at("12:00")
        c = inst(7, start=t0, label="chat", image="webui", chat=True)
        ok = self.run_ticks([(t0, []), (t0 + 60, [c]), (t0 + 360, [c])],
                            chat=lambda url: True)
        self.assertTrue(any("✅ Чат отвечает: http://1.2.3.4:40000" in m for m in ok[1]))
        self.assertEqual(ok[2], [])
        bad = self.run_ticks([(t0, []), (t0 + 60, [c]), (t0 + 42 * 60, [c]),
                              (t0 + 50 * 60, [c])])
        self.assertTrue(any("не отвечает" in m for m in bad[2]))
        self.assertFalse(any("не отвечает" in m for m in bad[3]))  # один раз

    def test_long_run_hourly(self):
        t0 = at("08:00")
        a = inst(9, start=t0, label="chat", image="w", dph=0.21)
        steps = [(t0, [])] + [(t0 + m * 60, [a]) for m in range(5, 6 * 60, 5)]
        out = self.run_ticks(steps)
        longs = [m for msgs in out for m in msgs if m.startswith("⏰")]
        # 3, 4, 5 часов — по одному в час
        self.assertEqual(len(longs), 3)
        self.assertIn("работает уже 3.0 ч, ≈$0.63", longs[0])

    def test_schedule_all_up_then_down(self):
        self.nodes.update({"node1": {"instance": 1}, "node2": {"instance": 2}})
        n = [inst(1), inst(2)]
        out = self.run_ticks([(at("18:30"), []), (at("18:50"), n),
                              (at("19:05"), n), (at("19:10"), n),
                              (at("20:05"), []), (at("20:40"), [])])
        self.assertIn("✅ По расписанию поднялись все узлы: 2 из 2", out[2])
        self.assertEqual(out[3], [])
        self.assertIn("✅ Узлы по расписанию остановлены", out[5])

    def test_schedule_missing_and_not_stopped(self):
        self.nodes.update({"node1": {"instance": 1}})
        out = self.run_ticks([(at("18:30"), []), (at("19:05"), [inst(1)]),
                              (at("20:40"), [inst(1)]), (at("20:45"), [inst(1)])])
        self.assertTrue(any("поднялись 1 из 2, нет: node2" in m for m in out[1]))
        self.assertTrue(any("не остановились" in m and "node1 (1)" in m for m in out[2]))
        self.assertFalse(any("не остановились" in m for m in out[3]))  # раз в день

    def test_node_image_without_state_counts_for_down(self):
        out = self.run_ticks([(at("18:30"), []), (at("20:40"), [inst(3)])])
        self.assertTrue(any("не остановились" in m for m in out[1]))


if __name__ == "__main__":
    unittest.main()
