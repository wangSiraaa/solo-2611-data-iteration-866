"""运输学校资格复核的端到端测试：练习还原 / 届规则窗口 / 补录纠正 / 签证。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import time
import unittest

from weighbridge import (
    GUIDED,
    IN,
    INDEPENDENT,
    IN_PROGRESS,
    LOAD,
    OUT,
    PARTIAL,
    QUALIFIED,
    UNVERIFIED,
    WAITING_GUIDED,
    Catalog,
    CatalogError,
    School,
    SchoolError,
    Snapshot,
    Warehouse,
    epoch_day,
    verify_certificate,
)

KEY = b"school-test-key"


def T(day: int, hour: int = 0) -> int:
    """第 day 天 hour 时的整数秒（epoch_day 记日）。"""
    return day * 86400 + hour * 3600


def D(n: int) -> str:
    """第 n 天的 ISO 日期。"""
    return epoch_day(n * 86400)


def ticket(tid, tractor, seq, ts, gross, direction=IN):
    return {
        "ticket_id": tid, "tractor": tractor, "catalog_seq": seq,
        "ts": ts, "direction": direction, "gross": gross,
    }


def product_dict(tractor, trailer, seq):
    return {
        "Kind": "composition_snapshot",
        "Version": 1,
        "seq": seq,
        "product": {
            "product_id": f"P{seq}-{tractor}-{trailer}",
            "action_seq": seq,
            "tractor_id": f"tractor:{tractor}",
            "trailer_id": f"trailer:{trailer}" if trailer else None,
            "mode": "EMPTY",
            "relation_src": "attach",
            "attach_seq": 1,
            "predecessor": None,
            "live": True,
            "display": tractor,
            "masses": [
                {"kind": "tractor", "entity_id": f"tractor:{tractor}",
                 "lo": 7000, "hi": 7200},
                *([{"kind": "trailer", "entity_id": f"trailer:{trailer}",
                    "lo": 1500, "hi": 1600}] if trailer else []),
            ],
        },
    }


class WorldBuilder:
    """合成运输世界：T1 在 RA/RB 之间换挂，所有运输 LOAD 可行。"""

    def __init__(self) -> None:
        self.c = Catalog()
        self.c.define_mass("tractor", "T1", 7000, 7200)
        self.c.define_mass("trailer", "RA", 1500, 1600)
        self.c.define_mass("trailer", "RB", 2000, 2200)
        self.w = Warehouse(self.c)
        self.c.apply_attach("T1", "RA")  # seq 1
        self.current = "RA"
        self.n = 0

    def _confirm(self, tin: str, tout: str) -> None:
        ev = self.w.preview(LOAD, tin, tout)
        assert ev.feasible, f"{tin}/{tout}: {ev.reason}"
        self.w.confirm(ev.trip_id)

    def trip(self, day: int, *, swap: bool = False) -> tuple[str, str]:
        """一趟单程运输；swap=True 时途中换一次挂。"""
        self.n += 1
        tin, tout = f"IN{self.n}", f"OUT{self.n}"
        seq_in = self.c.last_seq()
        if swap:
            other = "RB" if self.current == "RA" else "RA"
            self.c.apply_detach("T1", self.current)
            self.c.apply_attach("T1", other)
            self.current = other
        seq_out = self.c.last_seq()
        self.w.add_ticket(ticket(tin, "T1", seq_in, T(day, 9), 9000))
        self.w.add_ticket(ticket(tout, "T1", seq_out, T(day, 15), 12500, OUT))
        self._confirm(tin, tout)
        return tin, tout

    def round_trip_swap(self, day: int) -> tuple[str, str]:
        """途中 A→B→A：首尾组合相同，但稳定身份换过两次。"""
        self.n += 1
        tin, tout = f"IN{self.n}", f"OUT{self.n}"
        seq_in = self.c.last_seq()
        other = "RB" if self.current == "RA" else "RA"
        self.w.add_ticket(ticket(tin, "T1", seq_in, T(day, 9), 9000))
        self.c.apply_detach("T1", self.current)
        self.c.apply_attach("T1", other)
        self.c.apply_detach("T1", other)
        self.c.apply_attach("T1", self.current)
        seq_out = self.c.last_seq()
        self.w.add_ticket(ticket(tout, "T1", seq_out, T(day, 15), 12500, OUT))
        self._confirm(tin, tout)
        return tin, tout

    def relabel_trip(self, day: int, displays=("PLATE-66", "PLATE-88")) -> tuple[str, str]:
        """同一挂车只改展示号；票面与净重和真换挂完全相同。"""
        self.n += 1
        tin, tout = f"IN{self.n}", f"OUT{self.n}"
        seq_in = self.c.last_seq()
        self.w.add_ticket(ticket(tin, "T1", seq_in, T(day, 9), 9000))
        for disp in displays:
            self.c.apply_set_display("T1", disp)
        seq_out = self.c.last_seq()
        self.w.add_ticket(ticket(tout, "T1", seq_out, T(day, 15), 12500, OUT))
        self._confirm(tin, tout)
        return tin, tout


def build_basic():
    """题面案例：甲 D1 经 U 指导 A→B→A，D2 独立直行，D3 独立换挂。"""
    b = WorldBuilder()
    b.round_trip_swap(1)     # IN1/OUT1  D1 指导 A→B→A
    b.trip(2)                # IN2/OUT2  D2 独立直行
    b.trip(3, swap=True)     # IN3/OUT3  D3 独立换挂
    s = School(signing_key=KEY)
    s.record_shift("SH-U-D1", "U", T(1, 8), T(1, 18))
    s.record_report("C1", "IN1", "OUT1", "甲", "U")
    s.record_report("C2", "IN2", "OUT2", "甲")
    s.record_report("C3", "IN3", "OUT3", "甲")
    s.set_cohort("CohortA", calendar=[D(n) for n in range(1, 11)],
                 instructors=["U"])
    s.ingest_warehouse(b.w, b.c)
    return b, s


def extend_overlap(b: WorldBuilder, s: School) -> None:
    """追加 D4 指导 B→A→B、D5 独立直行、D6 独立换挂（窗口重叠）。"""
    b.round_trip_swap(4)     # IN4/OUT4  D4 指导 B→A→B
    b.trip(5)                # IN5/OUT5  D5 独立直行
    b.trip(6, swap=True)     # IN6/OUT6  D6 独立换挂
    s.record_shift("SH-U-D4", "U", T(4, 8), T(4, 18))
    s.record_report("C4", "IN4", "OUT4", "甲", "U")
    s.record_report("C5", "IN5", "OUT5", "甲")
    s.record_report("C6", "IN6", "OUT6", "甲")
    s.ingest_warehouse(b.w, b.c)


class CatalogDisplayTests(unittest.TestCase):
    def test_set_display_keeps_stable_identity(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.apply_attach("T1", "R1")          # seq 1
        c.apply_set_display("T1", "P-1")    # seq 2
        p = c.product_at(2, "T1")
        self.assertEqual(p.stable_identity(), ("T1", "R1"))
        self.assertEqual(p.display, "P-1")
        self.assertEqual(p.relation_src, "display_change")
        self.assertEqual(p.shown_identity(), ("T1", "R1", "P-1"))
        kinds = [a.kind for a in c.actions_between("T1", 1, 2)]
        self.assertEqual(kinds, ["set_display"])
        self.assertEqual(c.verify(), [])

    def test_display_inherited_across_swap(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.define_mass("trailer", "R2", 2000, 2200)
        c.apply_attach("T1", "R1")
        c.apply_set_display("T1", "P-9")
        c.apply_detach("T1", "R1")
        c.apply_attach("T1", "R2")
        self.assertEqual(c.product_at(4, "T1").shown_identity(), ("T1", "R2", "P-9"))
        self.assertEqual(c.product_at(3, "T1").shown_identity(), ("T1", None, "P-9"))

    def test_display_snapshot_roundtrip_and_legacy_default(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.apply_set_display("T1", "P-7")
        snap = c.snapshot_at(1, "T1").to_json()
        self.assertEqual(Snapshot.from_json(snap).product.display, "P-7")
        legacy = json.loads(snap)
        del legacy["product"]["display"]      # 旧格式快照没有 display 字段
        self.assertEqual(Snapshot.from_dict(legacy).product.display, "")

    def test_set_display_validation(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        with self.assertRaises(CatalogError):
            c.apply_set_display("T1", "   ")
        with self.assertRaises(CatalogError):
            c.apply_set_display("T9", "P-1")

    def test_batch_with_set_display_is_atomic(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.define_mass("trailer", "R2", 2000, 2200)
        c.apply_attach("T1", "R1")
        with self.assertRaises(CatalogError):
            c.apply_batch([
                {"kind": "set_display", "tractor": "T1", "display": "P-3"},
                {"kind": "attach", "tractor": "T1", "trailer": "R2"},  # 违反单挂
            ])
        self.assertEqual(c.last_seq(), 1)   # 整批回滚，序号不消耗
        self.assertEqual(c.product_at(1, "T1").display, "T1")

    def test_actions_between_excludes_mass_corrections(self):
        c = Catalog()
        c.define_mass("tractor", "T1", 7000, 7200)
        c.define_mass("trailer", "R1", 1500, 1600)
        c.apply_attach("T1", "R1")
        c.apply_set_mass("trailer", "R1", 1500, 1550)   # 质量更正不是换挂
        kinds = [a.kind for a in c.actions_between("T1", 1, 2)]
        self.assertEqual(kinds, [])


class PracticeDerivationTests(unittest.TestCase):
    def test_basic_practices_classified(self):
        b, s = build_basic()
        practices = {p.ticket_in: p for p in s.practices("甲")}
        p1, p2, p3 = practices["IN1"], practices["IN2"], practices["IN3"]
        self.assertEqual((p1.klass, p1.swapped), (GUIDED, True))
        self.assertEqual((p2.klass, p2.swapped), (INDEPENDENT, False))
        self.assertEqual((p3.klass, p3.swapped), (INDEPENDENT, True))
        # A→B→A：四次稳定身份变化都记为换挂事件
        self.assertEqual(len(p1.evidence["swap_events"]), 4)
        self.assertEqual(p1.evidence["display_events"], [])
        self.assertEqual(s.reconcile(), [])

    def test_display_relabel_is_not_a_swap(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # 甲 D1 真换挂 A→B→A
        b.relabel_trip(2)        # 乙 D2 同一挂车两次改号
        ev_swap = b.w.evaluation(b.w.trips_for_ticket("IN1")[0])
        ev_disp = b.w.evaluation(b.w.trips_for_ticket("IN2")[0])
        # 票面（9000→12500）与净重完全相同
        self.assertEqual((ev_swap.net_lo, ev_swap.net_hi),
                         (ev_disp.net_lo, ev_disp.net_hi))
        s = School(signing_key=KEY)
        s.record_shift("SH-U-D1", "U", T(1, 8), T(1, 18))
        s.record_shift("SH-U-D2", "U", T(2, 8), T(2, 18))
        s.record_report("C1", "IN1", "OUT1", "甲", "U")
        s.record_report("C2", "IN2", "OUT2", "乙", "U")
        s.set_cohort("CohortA", calendar=[D(n) for n in range(1, 11)],
                     instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        p_swap = s.practices("甲")[0]
        p_disp = s.practices("乙")[0]
        self.assertTrue(p_swap.swapped)
        self.assertFalse(p_disp.swapped)
        self.assertEqual(len(p_disp.evidence["display_events"]), 2)
        self.assertEqual(p_disp.klass, GUIDED)   # 指导属实，但没换挂
        self.assertEqual(s.evaluate("CohortA", "甲").stage, 2)
        self.assertEqual(s.evaluate("CohortA", "乙").status, WAITING_GUIDED)

    def test_duplicate_copies_count_once(self):
        b, s = build_basic()
        s.record_report("C1-副本", "IN1", "OUT1", "甲", "U")   # 同票对同内容
        s.record_report("C1", "IN1", "OUT1", "甲", "U")        # 同 copy_id 幂等
        self.assertEqual(len(s.practices("甲")), 3)
        summary = s.nightly_review()
        self.assertEqual(summary["duplicate_copies_dropped"], 1)
        self.assertEqual(s.evaluate("CohortA", "甲").earliest_day, D(3))
        self.assertEqual(s.reconcile(), [])

    def test_conflicting_copies_block_pair_until_corrected(self):
        b, s = build_basic()
        s.record_report("C1-冲突", "IN1", "OUT1", "甲", "V")
        p1 = [p for p in s.practices("甲") if p.ticket_in == "IN1"][0]
        self.assertTrue(p1.conflict)
        self.assertEqual(s.evaluate("CohortA", "甲").status, WAITING_GUIDED)
        self.assertTrue(any("conflict" in e for e in s.reconcile()))
        s.retract_report("C1-冲突")
        p1 = [p for p in s.practices("甲") if p.ticket_in == "IN1"][0]
        self.assertFalse(p1.conflict)
        self.assertEqual(s.evaluate("CohortA", "甲").earliest_day, D(3))
        self.assertEqual(s.reconcile(), [])

    def test_partial_accompaniment_counts_for_neither(self):
        b = WorldBuilder()
        b.round_trip_swap(1)
        b.trip(2)
        b.trip(3, swap=True)
        s = School(signing_key=KEY)
        s.record_shift("SH-U-D1", "U", T(1, 8), T(1, 12))   # 只覆盖半程
        s.record_report("C1", "IN1", "OUT1", "甲", "U")
        s.record_report("C2", "IN2", "OUT2", "甲")
        s.record_report("C3", "IN3", "OUT3", "甲")
        s.set_cohort("CohortA", calendar=[D(n) for n in range(1, 11)],
                     instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        p1 = s.practices("甲")[0]
        self.assertEqual(p1.klass, PARTIAL)
        self.assertAlmostEqual(p1.coverage, 3 / 6)
        r = s.evaluate("CohortA", "甲")
        self.assertEqual(r.status, WAITING_GUIDED)   # 部分陪同不能充抵指导
        self.assertIsNone(r.earliest_day)

    def test_unverified_claim_counts_for_neither(self):
        b = WorldBuilder()
        b.round_trip_swap(1)
        b.trip(2)
        b.trip(3, swap=True)
        s = School(signing_key=KEY)
        s.record_report("C1", "IN1", "OUT1", "甲", "V")   # V 没有任何班次
        s.record_report("C2", "IN2", "OUT2", "甲")
        s.record_report("C3", "IN3", "OUT3", "甲")
        s.set_cohort("CohortA", calendar=[D(n) for n in range(1, 11)],
                     instructors=["V"])
        s.ingest_warehouse(b.w, b.c)
        p1 = s.practices("甲")[0]
        self.assertEqual(p1.klass, UNVERIFIED)
        self.assertEqual(s.evaluate("CohortA", "甲").status, WAITING_GUIDED)

    def test_facts_without_report_are_orphaned_then_adopted(self):
        s = School(signing_key=KEY)
        pid = s.register_practice_facts(
            ticket_in="X1", ticket_out="X2", tractor="T1",
            ts_in=T(1, 9), ts_out=T(1, 15), seq_in=1, seq_out=1,
            product_in=product_dict("T1", "RA", 1),
            product_out=product_dict("T1", "RA", 1),
        )
        self.assertIsNone(pid)
        self.assertEqual(len(s.orphans()), 1)
        s.record_report("R1", "X1", "X2", "甲")
        pid = s.register_practice_facts(
            ticket_in="X1", ticket_out="X2", tractor="T1",
            ts_in=T(1, 9), ts_out=T(1, 15), seq_in=1, seq_out=1,
            product_in=product_dict("T1", "RA", 1),
            product_out=product_dict("T1", "RA", 1),
        )
        self.assertIsNotNone(pid)
        self.assertEqual(s.practices("甲")[0].klass, INDEPENDENT)

    def test_reconcile_flags_seq_inversion(self):
        s = School()
        s.record_report("R1", "X1", "X2", "甲")
        s.register_practice_facts(
            ticket_in="X1", ticket_out="X2", tractor="T1",
            ts_in=T(1, 9), ts_out=T(1, 15), seq_in=5, seq_out=3,
            product_in=product_dict("T1", "RA", 5),
            product_out=product_dict("T1", "RA", 3),
        )
        errs = s.reconcile()
        self.assertTrue(any("seq_out" in e for e in errs))


class QualificationTests(unittest.TestCase):
    def test_textbook_case_qualifies_on_day3(self):
        b, s = build_basic()
        r = s.evaluate("CohortA", "甲")
        self.assertEqual(r.status, QUALIFIED)
        self.assertEqual(r.earliest_day, D(3))
        self.assertEqual(
            [link["role"] for link in r.chain],
            ["guided", "first_independent", "second_independent"],
        )
        self.assertEqual(r.chain[0]["day"], D(1))
        self.assertEqual(r.chain[0]["instructor"], "U")
        self.assertEqual(r.chain[1]["day"], D(2))
        self.assertEqual(r.chain[2]["day"], D(3))
        self.assertTrue(r.chain[2]["swapped"])
        self.assertEqual(s.reconcile(), [])

    def test_cohort_with_other_designated_instructor_waits(self):
        b, s = build_basic()
        s.set_cohort("CohortV", calendar=[D(n) for n in range(1, 11)],
                     instructors=["V"])
        r = s.evaluate("CohortV", "甲")
        self.assertEqual(r.status, WAITING_GUIDED)
        self.assertEqual(r.stage, 1)
        self.assertIsNone(r.earliest_day)
        # 同一批练习在指定 U 的一届已达标
        self.assertEqual(s.evaluate("CohortA", "甲").status, QUALIFIED)

    def test_overlapping_windows_keep_the_old_one_alive(self):
        b, s = build_basic()
        extend_overlap(b, s)
        r = s.evaluate("CohortA", "甲")
        self.assertEqual(r.status, QUALIFIED)
        self.assertEqual(r.earliest_day, D(3))   # 旧窗口给出最早日
        self.assertEqual(len(r.windows), 2)
        w1, w2 = r.windows
        self.assertEqual(w1["guided_day"], D(1))
        self.assertEqual(w1["window"], [D(2), D(3), D(4)])
        self.assertEqual(w1["status"], "satisfied")
        self.assertEqual(w1["second_independent_day"], D(3))
        self.assertEqual(w2["guided_day"], D(4))
        self.assertEqual(w2["window"], [D(5), D(6), D(7)])
        self.assertEqual(w2["status"], "satisfied")
        self.assertEqual(w2["second_independent_day"], D(6))
        # 支持链走最早窗口：D1 指导 → D2 独立 → D3 独立换挂
        self.assertEqual([l["day"] for l in r.chain], [D(1), D(2), D(3)])

    def test_window_is_exactly_three_school_days(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # D1 指导换挂 → 窗口 [D2,D3,D4]
        b.trip(2)                # D2 独立直行
        b.trip(5, swap=True)     # D5 独立换挂：窗口之外
        s = School(signing_key=KEY)
        s.record_shift("SH1", "U", T(1, 8), T(1, 18))
        s.record_report("R1", "IN1", "OUT1", "甲", "U")
        s.record_report("R2", "IN2", "OUT2", "甲")
        s.record_report("R3", "IN3", "OUT3", "甲")
        s.set_cohort("CA", calendar=[D(n) for n in range(1, 11)], instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").status, IN_PROGRESS)
        # 补上 D4 独立换挂 → 窗口内 D2→D4 成立
        b.trip(4, swap=True)
        s.record_report("R4", "IN4", "OUT4", "甲")
        s.ingest_warehouse(b.w, b.c)
        r = s.evaluate("CA", "甲")
        self.assertEqual(r.status, QUALIFIED)
        self.assertEqual(r.earliest_day, D(4))

    def test_two_independents_must_be_on_different_days(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # D1 指导
        b.trip(2)                # D2 独立直行
        b.trip(2, swap=True)     # D2 又一次独立换挂：同日不算
        s = School(signing_key=KEY)
        s.record_shift("SH1", "U", T(1, 8), T(1, 18))
        for i in range(1, 4):
            s.record_report(f"R{i}", f"IN{i}", f"OUT{i}", "甲", "U" if i == 1 else None)
        s.set_cohort("CA", calendar=[D(n) for n in range(1, 11)], instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").status, IN_PROGRESS)
        b.trip(3, swap=True)     # D3 独立换挂 → 不同日满足
        s.record_report("R4", "IN4", "OUT4", "甲")
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").earliest_day, D(3))

    def test_second_independent_must_be_the_swap(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # D1 指导
        b.trip(2, swap=True)     # D2 独立换挂（可作第一次）
        b.trip(3)                # D3 独立直行（第二次须换挂 → 不算）
        s = School(signing_key=KEY)
        s.record_shift("SH1", "U", T(1, 8), T(1, 18))
        for i in range(1, 4):
            s.record_report(f"R{i}", f"IN{i}", f"OUT{i}", "甲", "U" if i == 1 else None)
        s.set_cohort("CA", calendar=[D(n) for n in range(1, 11)], instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").status, IN_PROGRESS)
        b.trip(4, swap=True)     # D4 独立换挂 → 第一次 D2、第二次 D4
        s.record_report("R4", "IN4", "OUT4", "甲")
        s.ingest_warehouse(b.w, b.c)
        r = s.evaluate("CA", "甲")
        self.assertEqual(r.earliest_day, D(4))
        self.assertEqual([l["day"] for l in r.chain], [D(1), D(2), D(4)])

    def test_calendar_gaps_skip_non_school_days(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # D1 指导 → 窗口 [D2,D4,D5]（D3 非校历日）
        b.trip(2)                # D2 独立直行
        b.trip(3, swap=True)     # D3 独立换挂：非校历日，不在窗口
        s = School(signing_key=KEY)
        s.record_shift("SH1", "U", T(1, 8), T(1, 18))
        for i in range(1, 4):
            s.record_report(f"R{i}", f"IN{i}", f"OUT{i}", "甲", "U" if i == 1 else None)
        calendar = [D(1), D(2), D(4), D(5), D(6), D(7)]
        s.set_cohort("CA", calendar=calendar, instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").status, IN_PROGRESS)
        b.trip(4, swap=True)     # D4 独立换挂 → 窗口内成立
        s.record_report("R4", "IN4", "OUT4", "甲")
        s.ingest_warehouse(b.w, b.c)
        self.assertEqual(s.evaluate("CA", "甲").earliest_day, D(4))

    def test_open_stages_report_what_is_still_completable(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # D1 指导
        b.trip(2)                # D2 独立直行（第一次已就位）
        s = School(signing_key=KEY)
        s.record_shift("SH1", "U", T(1, 8), T(1, 18))
        s.record_report("R1", "IN1", "OUT1", "甲", "U")
        s.record_report("R2", "IN2", "OUT2", "甲")
        s.set_cohort("CA", calendar=[D(n) for n in range(1, 11)], instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        r = s.evaluate("CA", "甲", today=D(3))
        self.assertEqual(r.status, IN_PROGRESS)
        win = r.windows[0]
        self.assertEqual(win["status"], "open")
        self.assertEqual(win["first_independent_day"], D(2))
        self.assertEqual(win["need"], ["second_independent_swap"])
        self.assertEqual(win["remaining_days"], [D(3), D(4)])
        self.assertTrue(win["completable"])
        # 窗口彻底过去后：不再可能完成
        r2 = s.evaluate("CA", "甲", today=D(5))
        self.assertEqual(r2.windows[0]["status"], "expired")
        self.assertFalse(r2.windows[0]["completable"])


class CorrectionTests(unittest.TestCase):
    def test_late_correction_postpones_earliest_day(self):
        b, s = build_basic()
        extend_overlap(b, s)
        self.assertEqual(s.evaluate("CohortA", "甲").earliest_day, D(3))
        cert = s.issue_certificate("CohortA", "甲")
        # 晚到更正：D1 的班次撤销，改为只覆盖前两个小时 → 部分陪同
        s.retract_shift("SH-U-D1")
        s.record_shift("SH-U-D1B", "U", T(1, 8), T(1, 11))
        p1 = [p for p in s.practices("甲") if p.ticket_in == "IN1"][0]
        self.assertEqual(p1.klass, PARTIAL)
        r = s.evaluate("CohortA", "甲")
        self.assertEqual(r.status, QUALIFIED)
        self.assertEqual(r.earliest_day, D(6))   # D4 指导的窗口顶上
        self.assertEqual([l["day"] for l in r.chain], [D(4), D(5), D(6)])
        # 结论历史保留：D3 与 D6 都在
        days = [h["earliest_day"] for h in s.ruling_history("CohortA", "甲")
                if h["status"] == QUALIFIED]
        self.assertIn(D(3), days)
        self.assertIn(D(6), days)
        # 已签证的原判（D3）仍可离线复核
        self.assertEqual(cert["qualified_day"], D(3))
        self.assertEqual(verify_certificate(cert, key=KEY), [])
        self.assertEqual(s.reconcile(), [])

    def test_late_correction_eliminates_earliest_day(self):
        b, s = build_basic()
        self.assertEqual(s.evaluate("CohortA", "甲").earliest_day, D(3))
        s.retract_shift("SH-U-D1")
        s.record_shift("SH-U-D1B", "U", T(1, 8), T(1, 11))
        r = s.evaluate("CohortA", "甲")
        self.assertEqual(r.status, WAITING_GUIDED)
        self.assertIsNone(r.earliest_day)
        self.assertEqual(r.stage, 1)
        self.assertEqual(s.reconcile(), [])

    def test_late_backfill_creates_earlier_qualification(self):
        b = WorldBuilder()
        b.round_trip_swap(1)     # 丙 D1 声称 U 指导 A→B→A，但 U 当时无班次
        b.trip(2)                # D2 独立直行
        b.trip(3, swap=True)     # D3 独立换挂
        s = School(signing_key=KEY)
        s.record_report("P1", "IN1", "OUT1", "丙", "U")
        s.record_report("P2", "IN2", "OUT2", "丙")
        s.record_report("P3", "IN3", "OUT3", "丙")
        s.set_cohort("CohortA", calendar=[D(n) for n in range(1, 11)],
                     instructors=["U"])
        s.ingest_warehouse(b.w, b.c)
        p1 = s.practices("丙")[0]
        self.assertEqual(p1.klass, UNVERIFIED)
        self.assertEqual(s.evaluate("CohortA", "丙").status, WAITING_GUIDED)
        # 教练日间补报多天前的班次 → 过去可证明的最早日期被改写
        s.record_shift("SH-U-D1", "U", T(1, 8), T(1, 18))
        p1 = s.practices("丙")[0]
        self.assertEqual(p1.klass, GUIDED)
        r = s.evaluate("CohortA", "丙")
        self.assertEqual(r.status, QUALIFIED)
        self.assertEqual(r.earliest_day, D(3))
        self.assertEqual(s.reconcile(), [])


class CertificateTests(unittest.TestCase):
    def test_certificate_verifies_offline_and_detects_tampering(self):
        b, s = build_basic()
        cert = s.issue_certificate("CohortA", "甲")
        # 离线复核：纯函数，不碰任何库
        self.assertEqual(verify_certificate(cert, key=KEY), [])
        self.assertEqual(verify_certificate(json.dumps(cert), key=KEY), [])
        # 篡改达标日
        bad = json.loads(json.dumps(cert))
        bad["qualified_day"] = D(4)
        self.assertTrue(verify_certificate(bad, key=KEY))
        # 篡改链上分类
        bad2 = json.loads(json.dumps(cert))
        bad2["chain"][1]["klass"] = "GUIDED"
        self.assertTrue(verify_certificate(bad2, key=KEY))
        # 抹掉换挂证据
        bad3 = json.loads(json.dumps(cert))
        bad3["chain"][0]["evidence"]["actions"] = []
        errs = verify_certificate(bad3, key=KEY)
        self.assertTrue(any("swapped" in e for e in errs))
        # 错钥 / 有签名不给钥 / 无签名却给钥
        self.assertTrue(verify_certificate(cert, key=b"wrong"))
        self.assertTrue(verify_certificate(cert))
        unsigned = dict(cert, signature=None)
        self.assertTrue(verify_certificate(unsigned, key=KEY))

    def test_certificate_requires_qualification(self):
        b, s = build_basic()
        with self.assertRaises(SchoolError):
            s.issue_certificate("CohortA", "乙")

    def test_certificate_chain_recomputes_from_embedded_evidence(self):
        b, s = build_basic()
        cert = s.issue_certificate("CohortA", "甲")
        guided = cert["chain"][0]
        # 证据内嵌：票对、组合快照、动作流水、班次
        self.assertEqual(guided["ticket_in"], "IN1")
        self.assertEqual(len(guided["evidence"]["swap_events"]), 4)
        self.assertEqual(len(guided["evidence"]["shifts"]), 1)
        self.assertEqual(guided["product_in"]["product"]["trailer_id"], "trailer:RA")
        self.assertEqual(guided["product_out"]["product"]["trailer_id"], "trailer:RA")
        # 关掉所有库连接后仍可复核
        b.c.close()
        b.w.close()
        s.close()
        self.assertEqual(verify_certificate(cert, key=KEY), [])


class ScaleTests(unittest.TestCase):
    def test_nightly_review_scales_to_tens_of_thousands(self):
        s = School(signing_key=KEY)
        instructors = [f"U{k}" for k in range(5)]
        for instr in instructors:
            s.record_shift(f"SH-{instr}", instr, 0, 41 * 86400)
        s.set_cohort("ALL", calendar=[D(n) for n in range(1, 41)],
                     instructors=instructors)
        s.set_cohort("ONLY-U0", calendar=[D(n) for n in range(1, 41)],
                     instructors=["U0"])
        s.set_cohort("GAP", calendar=[D(n) for n in range(1, 41) if n % 4 != 0],
                     instructors=["U1", "U2"])
        n_trainees = 400
        t0 = time.monotonic()
        for i in range(n_trainees):
            trainee = f"S{i:04d}"
            tid = f"T{i % 20}"
            instr = instructors[i % 5]
            s.register_practice_facts(           # D1 指导换挂
                ticket_in=f"A{i}-IN", ticket_out=f"A{i}-OUT", tractor=tid,
                ts_in=T(1, 8), ts_out=T(1, 16), seq_in=1, seq_out=2,
                product_in=product_dict(tid, "RA", 1),
                product_out=product_dict(tid, "RB", 2),
                actions=[{"seq": 2, "kind": "attach", "tractor": tid,
                          "trailer": "RB"}],
                report={"copy_id": f"R{i}-A", "trainee": trainee,
                        "instructor": instr},
                defer=True,
            )
            for d in range(2, 31):               # D2..D30 独立练习，单日换挂
                swap = d % 2 == 1
                s.register_practice_facts(
                    ticket_in=f"P{i}-{d}-IN", ticket_out=f"P{i}-{d}-OUT",
                    tractor=tid, ts_in=T(d, 8), ts_out=T(d, 16),
                    seq_in=1, seq_out=2 if swap else 1,
                    product_in=product_dict(tid, "RA", 1),
                    product_out=product_dict(tid, "RB" if swap else "RA",
                                             2 if swap else 1),
                    actions=([{"seq": 2, "kind": "attach", "tractor": tid,
                               "trailer": "RB"}] if swap else []),
                    report={"copy_id": f"R{i}-{d}", "trainee": trainee},
                    defer=True,
                )
        summary = s.nightly_review(today=D(35))
        dt = time.monotonic() - t0
        self.assertEqual(summary["practices"], n_trainees * 30)
        # ALL 400 人、ONLY-U0 80 人、GAP 160 人达标
        self.assertEqual(len(summary["qualified"]), 400 + 80 + 160)
        r0 = s.evaluate("ALL", "S0000")
        self.assertEqual(r0.status, QUALIFIED)
        self.assertEqual(r0.earliest_day, D(3))
        self.assertEqual(s.evaluate("ONLY-U0", "S0001").status, WAITING_GUIDED)
        self.assertEqual(s.reconcile(), [])
        print(f"\n  nightly_review: {summary['practices']} practices, "
              f"{summary['trainees']} trainees x {summary['cohorts']} cohorts "
              f"in {dt:.2f}s")
        self.assertLess(dt, 60)


if __name__ == "__main__":
    unittest.main()
