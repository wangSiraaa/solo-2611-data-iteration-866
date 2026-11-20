"""运输学校资格复核的可复跑合成演示。

运行::

    python3 -m demo.run_school_demo          # 打印叙述式 JSON
    python3 -m demo.run_school_demo --json   # 另写 demo/out/school_*.json

覆盖题面案例：
1. 甲：D1 经 U 指导完成 A→B→A，D2 独立直行，D3 独立换挂 → D3 结业
2. 乙：同一挂车两次改号，票面与净重和甲完全相同 → 不具备资格
3. 另一届只认 V 教练 → 同一批练习，甲仍在等待第一阶段
4. 两次指导窗口重叠：新指导不抹掉旧窗口，最早日仍出自旧窗口
5. 晚到更正：D1 班次被纠正为部分陪同 → 最早结业日 D3 推迟到 D6
6. 继续纠正掉 D4 班次 → 最早结业日被消除，回到等待第一阶段
7. 丙的教练补报多天前的班次 → 过去可证明的最早日期被改写
8. 签证：达标证书内嵌全部证据，运输库离线也能复核原判
9. 每晚全量复核上万份练习（计时）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time

from weighbridge import (
    GUIDED,
    IN,
    INDEPENDENT,
    LOAD,
    OUT,
    PARTIAL,
    QUALIFIED,
    UNVERIFIED,
    WAITING_GUIDED,
    Catalog,
    School,
    Warehouse,
    epoch_day,
    verify_certificate,
)

KEY = b"school-demo-key"


def T(day: int, hour: int = 0) -> int:
    return day * 86400 + hour * 3600


def D(n: int) -> str:
    return epoch_day(n * 86400)


def ticket(tid, tractor, seq, ts, gross, direction=IN):
    return {
        "ticket_id": tid, "tractor": tractor, "catalog_seq": seq,
        "ts": ts, "direction": direction, "gross": gross,
    }


def build_world():
    """合成运输世界：T1 在 RA/RB 间的全部练习运输（LOAD 均可行）。"""
    c = Catalog()
    c.define_mass("tractor", "T1", 7000, 7200)
    c.define_mass("trailer", "RA", 1500, 1600)
    c.define_mass("trailer", "RB", 2000, 2200)
    w = Warehouse(c)
    c.apply_attach("T1", "RA")                                   # seq 1

    def confirm(tin, tout):
        ev = w.preview(LOAD, tin, tout)
        assert ev.feasible, ev.reason
        w.confirm(ev.trip_id)
        return ev

    # 甲 D1：指导 A→B→A（首尾同挂 RA，途中换过两次）
    w.add_ticket(ticket("IN1", "T1", 1, T(1, 9), 9000))
    c.apply_detach("T1", "RA"); c.apply_attach("T1", "RB")       # 2,3
    c.apply_detach("T1", "RB"); c.apply_attach("T1", "RA")       # 4,5
    w.add_ticket(ticket("OUT1", "T1", 5, T(1, 15), 12500, OUT))
    ev_jia_d1 = confirm("IN1", "OUT1")
    # 甲 D2：独立直行（无动作）；甲 D3：独立换挂 A→B
    w.add_ticket(ticket("IN2", "T1", 5, T(2, 9), 9000))
    w.add_ticket(ticket("OUT2", "T1", 5, T(2, 15), 12400, OUT))
    confirm("IN2", "OUT2")
    c.apply_detach("T1", "RA"); c.apply_attach("T1", "RB")       # 6,7
    w.add_ticket(ticket("IN3", "T1", 5, T(3, 9), 9000))
    w.add_ticket(ticket("OUT3", "T1", 7, T(3, 15), 12600, OUT))
    confirm("IN3", "OUT3")
    # 甲 D4：又一次指导换挂 B→A→B（窗口重叠）；D5 独立直行；D6 独立换挂
    w.add_ticket(ticket("IN4", "T1", 7, T(4, 9), 9300))
    c.apply_detach("T1", "RB"); c.apply_attach("T1", "RA")       # 8,9
    c.apply_detach("T1", "RA"); c.apply_attach("T1", "RB")       # 10,11
    w.add_ticket(ticket("OUT4", "T1", 11, T(4, 15), 12800, OUT))
    confirm("IN4", "OUT4")
    w.add_ticket(ticket("IN5", "T1", 11, T(5, 9), 9300))
    w.add_ticket(ticket("OUT5", "T1", 11, T(5, 15), 12900, OUT))
    confirm("IN5", "OUT5")
    c.apply_detach("T1", "RB"); c.apply_attach("T1", "RA")       # 12,13
    w.add_ticket(ticket("IN6", "T1", 11, T(6, 9), 9300))
    w.add_ticket(ticket("OUT6", "T1", 13, T(6, 15), 12900, OUT))
    confirm("IN6", "OUT6")
    # 乙 D8：同一挂车 RA 两次改展示号（票面/净重与甲 D1 相同）
    w.add_ticket(ticket("IN8", "T1", 13, T(8, 9), 9000))
    c.apply_set_display("T1", "PLATE-66")                        # 14
    c.apply_set_display("T1", "PLATE-88")                        # 15
    w.add_ticket(ticket("OUT8", "T1", 15, T(8, 15), 12500, OUT))
    ev_yi = confirm("IN8", "OUT8")
    # 丙 D9 指导换挂（声称 U，班次后补）、D10 独立直行、D11 独立换挂
    w.add_ticket(ticket("IN9", "T1", 15, T(9, 9), 9000))
    c.apply_detach("T1", "RA"); c.apply_attach("T1", "RB")       # 16,17
    c.apply_detach("T1", "RB"); c.apply_attach("T1", "RA")       # 18,19
    w.add_ticket(ticket("OUT9", "T1", 19, T(9, 15), 12500, OUT))
    confirm("IN9", "OUT9")
    w.add_ticket(ticket("IN10", "T1", 19, T(10, 9), 9000))
    w.add_ticket(ticket("OUT10", "T1", 19, T(10, 15), 12400, OUT))
    confirm("IN10", "OUT10")
    c.apply_detach("T1", "RA"); c.apply_attach("T1", "RB")       # 20,21
    w.add_ticket(ticket("IN11", "T1", 19, T(11, 9), 9000))
    w.add_ticket(ticket("OUT11", "T1", 21, T(11, 15), 12600, OUT))
    confirm("IN11", "OUT11")
    return c, w, ev_jia_d1, ev_yi


def build_school(c: Catalog, w: Warehouse) -> School:
    s = School(signing_key=KEY)
    # 排班：U 在 D1/D4/D8 全天在场（丙的 D9 班次故意先不登，留待补报）
    s.record_shift("SH-U-D1", "U", T(1, 8), T(1, 18))
    s.record_shift("SH-U-D4", "U", T(4, 8), T(4, 18))
    s.record_shift("SH-U-D8", "U", T(8, 8), T(8, 18))
    # 考评簿抄本（甲 D1 有一份重复抄本，只计一次）
    s.record_report("C1", "IN1", "OUT1", "甲", "U")
    s.record_report("C1-副本", "IN1", "OUT1", "甲", "U")
    s.record_report("C2", "IN2", "OUT2", "甲")
    s.record_report("C3", "IN3", "OUT3", "甲")
    s.record_report("C4", "IN4", "OUT4", "甲", "U")
    s.record_report("C5", "IN5", "OUT5", "甲")
    s.record_report("C6", "IN6", "OUT6", "甲")
    s.record_report("C8", "IN8", "OUT8", "乙", "U")
    s.record_report("C9", "IN9", "OUT9", "丙", "U")
    s.record_report("C10", "IN10", "OUT10", "丙")
    s.record_report("C11", "IN11", "OUT11", "丙")
    # 两届：A 届指定 U，B 届只认 V
    s.set_cohort("A届", calendar=[D(n) for n in range(1, 15)], instructors=["U"])
    s.set_cohort("B届", calendar=[D(n) for n in range(1, 15)], instructors=["V"])
    s.ingest_warehouse(w, c)
    return s


def ruling_view(s: School, cohort: str, trainee: str) -> dict:
    r = s.evaluate(cohort, trainee)
    return {
        "status": r.status,
        "stage": r.stage,
        "earliest_day": r.earliest_day,
        "chain": [
            {"role": l["role"], "day": l["day"], "ticket_in": l["ticket_in"],
             "klass": l["klass"], "swapped": l["swapped"],
             "instructor": l["instructor"]}
            for l in r.chain
        ],
        "windows": r.windows,
    }


def product_dict(tractor, trailer, seq):
    return {
        "Kind": "composition_snapshot", "Version": 1, "seq": seq,
        "product": {
            "product_id": f"P{seq}-{tractor}-{trailer}", "action_seq": seq,
            "tractor_id": f"tractor:{tractor}",
            "trailer_id": f"trailer:{trailer}" if trailer else None,
            "mode": "EMPTY", "relation_src": "attach", "attach_seq": 1,
            "predecessor": None, "live": True, "display": tractor,
            "masses": [
                {"kind": "tractor", "entity_id": f"tractor:{tractor}",
                 "lo": 7000, "hi": 7200},
                *([{"kind": "trailer", "entity_id": f"trailer:{trailer}",
                    "lo": 1500, "hi": 1600}] if trailer else []),
            ],
        },
    }


def perf_nightly() -> dict:
    """每晚全量复核：400 学员 × 30 份练习 = 12000 份，3 届规则各异。"""
    s = School()
    instructors = [f"U{k}" for k in range(5)]
    for instr in instructors:
        s.record_shift(f"SH-{instr}", instr, 0, 41 * 86400)
    s.set_cohort("ALL", calendar=[D(n) for n in range(1, 41)],
                 instructors=instructors)
    s.set_cohort("ONLY-U0", calendar=[D(n) for n in range(1, 41)],
                 instructors=["U0"])
    s.set_cohort("GAP", calendar=[D(n) for n in range(1, 41) if n % 4 != 0],
                 instructors=["U1", "U2"])
    t0 = time.monotonic()
    for i in range(400):
        trainee, tid = f"S{i:04d}", f"T{i % 20}"
        instr = instructors[i % 5]
        s.register_practice_facts(
            ticket_in=f"A{i}-IN", ticket_out=f"A{i}-OUT", tractor=tid,
            ts_in=T(1, 8), ts_out=T(1, 16), seq_in=1, seq_out=2,
            product_in=product_dict(tid, "RA", 1),
            product_out=product_dict(tid, "RB", 2),
            actions=[{"seq": 2, "kind": "attach", "tractor": tid, "trailer": "RB"}],
            report={"copy_id": f"R{i}-A", "trainee": trainee, "instructor": instr},
            defer=True,
        )
        for d in range(2, 31):
            swap = d % 2 == 1
            s.register_practice_facts(
                ticket_in=f"P{i}-{d}-IN", ticket_out=f"P{i}-{d}-OUT", tractor=tid,
                ts_in=T(d, 8), ts_out=T(d, 16),
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
    summary["seconds"] = round(dt, 3)
    summary["qualified_count"] = len(summary.pop("qualified"))
    summary["reconcile"] = s.reconcile()
    return summary


def report() -> dict:
    c, w, ev_jia_d1, ev_yi = build_world()
    s = build_school(c, w)
    out: dict = {"cases": {}, "checks": {}}

    # 1) 甲：题面案例 → D3 结业（D4 的第二次指导开了重叠窗口，但不改最早日）
    out["cases"]["1_甲_题面案例"] = ruling_view(s, "A届", "甲")

    # 2) 乙：同一挂车两次改号，票面与净重和甲 D1 完全相同 → 不具备资格
    out["cases"]["2_乙_只改展示号"] = {
        "net_甲_D1": [ev_jia_d1.net_lo, ev_jia_d1.net_hi],
        "net_乙_D8": [ev_yi.net_lo, ev_yi.net_hi],
        "乙_D8_练习": s.practices("乙")[0].to_dict(),
        "ruling": ruling_view(s, "A届", "乙"),
    }

    # 3) B 届只认 V：同一批练习，甲仍在等待第一阶段
    out["cases"]["3_甲_在B届"] = ruling_view(s, "B届", "甲")

    # 4) 丙：教练补报多天前的班次，过去可证明的最早日期被改写
    before = ruling_view(s, "A届", "丙")
    s.record_shift("SH-U-D9", "U", T(9, 8), T(9, 18))   # 日间补报
    after = ruling_view(s, "A届", "丙")
    out["cases"]["4_丙_补报班次"] = {"补报前": before, "补报后": after}

    # 5) 签证：为甲签发 D3 达标证书
    cert = s.issue_certificate("A届", "甲")
    out["cases"]["5_签证"] = {
        "qualified_day": cert["qualified_day"],
        "payload_hash": cert["payload_hash"],
        "offline_verify": verify_certificate(cert, key=KEY),
    }

    # 6) 晚到更正：D1 班次被纠正为部分陪同 → 最早日 D3 推迟到 D6
    s.retract_shift("SH-U-D1")
    s.record_shift("SH-U-D1B", "U", T(1, 8), T(1, 11))
    out["cases"]["6_晚到更正_推迟"] = {
        "甲_D1_练习_klass": [p.klass for p in s.practices("甲")
                          if p.ticket_in == "IN1"][0],
        "ruling": ruling_view(s, "A届", "甲"),
        "ruling_history": s.ruling_history("A届", "甲"),
        "旧证书仍可离线复核原判": verify_certificate(cert, key=KEY),
    }

    # 7) 继续纠正掉 D4 班次 → 最早结业日被消除
    s.retract_shift("SH-U-D4")
    out["cases"]["7_晚到更正_消除"] = ruling_view(s, "A届", "甲")

    # 8) 每晚全量复核上万份练习
    out["cases"]["8_夜间全量复核"] = perf_nightly()

    out["checks"] = {
        "school_reconcile": s.reconcile(),
        "warehouse_reconcile": w.reconcile(),
        "catalog_verify": c.verify(),
    }
    return out, cert


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="写出 demo/out/school_*.json")
    args = ap.parse_args()

    data, cert = report()
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if args.json:
        out = pathlib.Path(__file__).parent / "out"
        out.mkdir(exist_ok=True)
        (out / "school_report.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out / "school_certificate.json").write_text(
            json.dumps(cert, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwrote {out}/school_report.json", flush=True)


if __name__ == "__main__":
    main()
