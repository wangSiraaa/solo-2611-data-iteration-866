"""运输学校资格复核 (driving-school qualification review).

在磅单事实之上还原实习司机的驾驶练习，并按届（校历 + 指定教练）判定
最早结业资格日。本模块不修改运输库；它把已确认运输、组合目录动作、
教练排班与考评簿抄本汇总成练习事实，结论可签证、可离线复核。

语义
----
* **练习** = 一趟已确认运输 (IN/OUT 票对) + 当时组合版 + 考评簿抄本。
  同一票对的多份抄本只计一次；内容冲突的抄本使该票对进入冲突态，
  冲突未消不计入任何资格。抄本指向的票对没有已确认运输（或未登记
  练习事实）时记为孤儿抄本，等待补齐。
* **换挂**：入票序号至出票序号之间 ``(seq_in, seq_out]``，该牵引车发生
  的任何 attach/detach（稳定挂车身份变化）都算练习期间换过挂，包括
  A→B→A；``set_display``（展示号变化）与 ``set_mass``（质量更正）不算。
* **指导/独立**：考评簿声称的教练经排班核实覆盖 ``[ts_in, ts_out]``
  全程 → ``GUIDED``；声称无教练 → ``INDEPENDENT``；覆盖不足 →
  ``PARTIAL``；声称有教练但排班查无在场 → ``UNVERIFIED``。
  PARTIAL 与 UNVERIFIED 另记，均不能充抵指导或独立。
* **届规则**：先完成指定教练的一次指导换挂（第一阶段）；每个这样的
  指导日各自开启一个窗口 = 校历上紧随其后 ``window_days`` 个校历日
  （默认 3）。窗口内按顺序完成两次独立练习、不同日、第二次须换挂，
  即结业于第二次的日期。各窗口分别考察，新的指导不抹掉尚有效的旧
  窗口；最早达标日取所有窗口的最小值。
* **补录与纠正**：排班按 ``(shift_id, version)`` 版本化，可补报多天前
  的班次、可撤销；受影响的练习自动重分类、相关学员自动重评，结论
  历史保留在 ``ruling`` 表，允许改变过去可证明的最早日期。
* **签证**：达标时签发内嵌全部证据（票对、组合快照、动作流水、班次）
  的证书；运输库离线时可用模块级 ``verify_certificate`` 重开原判。
  证书证明"该练习链在该届规则下支持此达标日"；全簿最小性由夜间
  复核在线保证。

容量：练习/抄本/班次不设硬上限（SQLite）；每晚全量复核上万份练习。
"""

from __future__ import annotations

import bisect
import hashlib
import hmac
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterator, Sequence

from .catalog import Action, Catalog, Product, Snapshot
from .warehouse import IN, OUT, STATUS_CONFIRMED, Warehouse

GUIDED = "GUIDED"
INDEPENDENT = "INDEPENDENT"
PARTIAL = "PARTIAL"
UNVERIFIED = "UNVERIFIED"

QUALIFIED = "QUALIFIED"
IN_PROGRESS = "IN_PROGRESS"
WAITING_GUIDED = "WAITING_GUIDED"

ROLE_GUIDED = "guided"
ROLE_FIRST = "first_independent"
ROLE_SECOND = "second_independent"

DEFAULT_WINDOW_DAYS = 3
EPOCH = date(1970, 1, 1)
DAY_SECONDS = 86400

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shift (
    shift_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    instructor TEXT NOT NULL,
    start_ts   INTEGER NOT NULL,
    end_ts     INTEGER NOT NULL,
    retracted  INTEGER NOT NULL DEFAULT 0,
    recorded_event INTEGER NOT NULL,
    PRIMARY KEY (shift_id, version),
    CHECK (end_ts >= start_ts)
);
CREATE INDEX IF NOT EXISTS shift_instr ON shift(instructor, start_ts, end_ts);

CREATE TABLE IF NOT EXISTS report_copy (
    copy_id    TEXT PRIMARY KEY,
    ticket_in  TEXT NOT NULL,
    ticket_out TEXT NOT NULL,
    trainee    TEXT NOT NULL,
    instructor TEXT,
    day        TEXT,
    retracted  INTEGER NOT NULL DEFAULT 0,
    recorded_event INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS report_pair ON report_copy(ticket_in, ticket_out);

CREATE TABLE IF NOT EXISTS practice (
    practice_id TEXT PRIMARY KEY,
    ticket_in   TEXT NOT NULL,
    ticket_out  TEXT NOT NULL,
    trip_id     TEXT,
    trainee     TEXT NOT NULL,
    instructor_claim TEXT,
    tractor     TEXT NOT NULL,
    ts_in       INTEGER NOT NULL,
    ts_out      INTEGER NOT NULL,
    seq_in      INTEGER NOT NULL,
    seq_out     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    day_rule    TEXT NOT NULL,
    swapped     INTEGER NOT NULL,
    klass       TEXT NOT NULL CHECK (klass IN ('GUIDED','INDEPENDENT','PARTIAL','UNVERIFIED')),
    coverage    REAL,
    conflict    INTEGER NOT NULL DEFAULT 0,
    product_in  TEXT NOT NULL,
    product_out TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    UNIQUE (ticket_in, ticket_out)
);
CREATE INDEX IF NOT EXISTS practice_trainee ON practice(trainee, day);
CREATE INDEX IF NOT EXISTS practice_claim ON practice(instructor_claim, ts_in, ts_out);

CREATE TABLE IF NOT EXISTS cohort (
    cohort_id   TEXT PRIMARY KEY,
    window_days INTEGER NOT NULL CHECK (window_days >= 1),
    config      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ruling (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id    TEXT NOT NULL,
    trainee      TEXT NOT NULL,
    event_seq    INTEGER NOT NULL,
    status       TEXT NOT NULL,
    earliest_day TEXT,
    chain        TEXT NOT NULL,
    stages       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ruling_latest ON ruling(cohort_id, trainee, id);

CREATE TABLE IF NOT EXISTS orphan (
    ticket_in  TEXT NOT NULL,
    ticket_out TEXT NOT NULL,
    reason     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    event_seq  INTEGER NOT NULL,
    PRIMARY KEY (ticket_in, ticket_out, reason)
);

CREATE TABLE IF NOT EXISTS school_event (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class SchoolError(ValueError):
    """学校簿记输入或状态错误。"""


def epoch_day(ts: int) -> str:
    """默认记日规则：整数秒 → 1970-01-01 起算的 ISO 日期。"""
    return (EPOCH + timedelta(days=int(ts) // DAY_SECONDS)).isoformat()


def day_to_ts(day: str) -> int:
    """ISO 日期 → 当日 00:00 的整数秒（epoch_day 的逆运算）。"""
    return (date.fromisoformat(day) - EPOCH).days * DAY_SECONDS


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sign(key: bytes, digest_hex: str) -> str:
    return hmac.new(key, digest_hex.encode("utf-8"), hashlib.sha256).hexdigest()


def _bare(eid: str | None) -> str | None:
    if eid is None:
        return None
    return eid.split(":", 1)[1] if ":" in eid else eid


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise SchoolError(f"{name} must be an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SchoolError(f"{name} must be an integer") from exc


def _check_day(day: str) -> str:
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError) as exc:
        raise SchoolError(f"day must be an ISO date, got {day!r}") from exc
    return day


def _next_school_days(calendar: Sequence[str], day: str, count: int) -> list[str]:
    """校历上严格晚于 ``day`` 的接下来 ``count`` 个校历日。"""
    i = bisect.bisect_right(calendar, day)
    return list(calendar[i : i + count])


def _coverage(
    shifts: Sequence[dict], ts_in: int, ts_out: int
) -> tuple[float, list[list[int]]]:
    """班次对 [ts_in, ts_out] 的并集覆盖率；零长行程按时间点判定。"""
    intervals: list[list[int]] = []
    for s in shifts:
        lo = max(int(s["start_ts"]), ts_in)
        hi = min(int(s["end_ts"]), ts_out)
        if lo <= hi:
            intervals.append([lo, hi])
    intervals.sort()
    merged: list[list[int]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    span = ts_out - ts_in
    if span == 0:
        ratio = 1.0 if any(lo <= ts_in <= hi for lo, hi in merged) else 0.0
    else:
        covered = sum(hi - lo for lo, hi in merged)
        ratio = min(1.0, covered / span)
    return ratio, merged


def _classify(
    claim: str | None, shifts: Sequence[dict], ts_in: int, ts_out: int
) -> tuple[str, float | None]:
    """按排班核实结果分类练习；返回 (klass, coverage)。"""
    if claim is None:
        return INDEPENDENT, None
    ratio, _ = _coverage(shifts, ts_in, ts_out)
    if ratio >= 1.0:
        return GUIDED, ratio
    if ratio <= 0.0:
        return UNVERIFIED, ratio
    return PARTIAL, ratio


def _events_from_actions(
    actions: Sequence[dict], seq_in: int, seq_out: int, tractor: str
) -> tuple[list[dict], list[dict]]:
    """从动作流水筛出该牵引车在 (seq_in, seq_out] 的换挂/改号事件。"""
    swaps: list[dict] = []
    displays: list[dict] = []
    for a in actions:
        if a.get("tractor") != tractor:
            continue
        seq = int(a["seq"])
        if not (seq_in < seq <= seq_out):
            continue
        if a["kind"] in ("attach", "detach"):
            swaps.append({"seq": seq, "kind": a["kind"], "trailer": a.get("trailer")})
        elif a["kind"] == "set_display":
            displays.append({"seq": seq, "display": a.get("display")})
    return swaps, displays


def _norm_action(a: Action | dict) -> dict:
    if isinstance(a, Action):
        return {
            "seq": int(a.seq),
            "kind": a.kind,
            "tractor": _bare(a.tractor_id),
            "trailer": _bare(a.trailer_id),
            "display": a.display,
        }
    return {
        "seq": _as_int(a["seq"], "action seq"),
        "kind": str(a["kind"]),
        "tractor": _bare(str(a["tractor"])) if a.get("tractor") else None,
        "trailer": _bare(str(a["trailer"])) if a.get("trailer") else None,
        "display": a.get("display"),
    }


def _norm_product(obj: Snapshot | Product | dict, seq: int) -> dict:
    """归一化为 composition_snapshot 字典（与目录/仓库相同的 JSON 格式）。"""
    if isinstance(obj, Snapshot):
        return obj.to_dict()
    if isinstance(obj, Product):
        return Snapshot(int(seq), obj).to_dict()
    if isinstance(obj, dict):
        if obj.get("Kind") == "composition_snapshot":
            return dict(obj)
        if "product_id" in obj:
            return {
                "Kind": "composition_snapshot",
                "Version": 1,
                "seq": int(seq),
                "product": dict(obj),
            }
    raise SchoolError("product must be a Snapshot, Product, or snapshot/product dict")


def _product_identity(snapshot: dict) -> tuple:
    product = snapshot.get("product", snapshot)
    return (product.get("tractor_id"), product.get("trailer_id"))


@dataclass(frozen=True)
class Practice:
    """一份还原出的练习：确认运输 + 当时组合版 + 考评与排班核实结果。"""

    practice_id: str
    ticket_in: str
    ticket_out: str
    trip_id: str | None
    trainee: str
    instructor_claim: str | None
    tractor: str
    ts_in: int
    ts_out: int
    seq_in: int
    seq_out: int
    day: str
    day_rule: str
    swapped: bool
    klass: str
    coverage: float | None
    conflict: bool
    product_in: dict
    product_out: dict
    evidence: dict

    def ref(self, role: str) -> dict:
        return {
            "practice_id": self.practice_id,
            "trip_id": self.trip_id,
            "ticket_in": self.ticket_in,
            "ticket_out": self.ticket_out,
            "role": role,
            "trainee": self.trainee,
            "instructor": self.instructor_claim,
            "klass": self.klass,
            "swapped": self.swapped,
            "day": self.day,
        }

    def to_dict(self) -> dict:
        return {
            "practice_id": self.practice_id,
            "ticket_in": self.ticket_in,
            "ticket_out": self.ticket_out,
            "trip_id": self.trip_id,
            "trainee": self.trainee,
            "instructor_claim": self.instructor_claim,
            "tractor": self.tractor,
            "ts_in": self.ts_in,
            "ts_out": self.ts_out,
            "seq_in": self.seq_in,
            "seq_out": self.seq_out,
            "day": self.day,
            "day_rule": self.day_rule,
            "swapped": self.swapped,
            "klass": self.klass,
            "coverage": self.coverage,
            "conflict": self.conflict,
            "swap_events": self.evidence.get("swap_events", []),
            "display_events": self.evidence.get("display_events", []),
        }


@dataclass(frozen=True)
class Ruling:
    """某届某学员在某次复核时刻的结论。"""

    cohort_id: str
    trainee: str
    status: str
    stage: int
    earliest_day: str | None
    chain: tuple[dict, ...]
    windows: tuple[dict, ...]
    today: str

    def to_dict(self) -> dict:
        return {
            "cohort_id": self.cohort_id,
            "trainee": self.trainee,
            "status": self.status,
            "stage": self.stage,
            "earliest_day": self.earliest_day,
            "chain": list(self.chain),
            "windows": list(self.windows),
            "today": self.today,
        }


class School:
    """学校复核簿：练习、排班、考评抄本、届规则、结论与签证。"""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        day_of: Callable[[int], str] | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        self.path = path
        self.day_of = day_of or epoch_day
        self._day_rule = "epoch_day" if day_of is None else "recorded"
        self.signing_key = signing_key
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "School":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _event(self, c: sqlite3.Connection, kind: str, detail: dict) -> int:
        cur = c.execute(
            "INSERT INTO school_event(kind, detail) VALUES (?,?)",
            (kind, _canon(detail)),
        )
        return int(cur.lastrowid)

    # ---------------------------------------------------------------- cohorts
    def set_cohort(
        self,
        cohort_id: str,
        *,
        calendar: Sequence[str],
        instructors: Sequence[str],
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> None:
        """登记/更新一届：校历日列表、指定教练、窗口长度（校历日数）。"""
        cid = str(cohort_id).strip()
        if not cid:
            raise SchoolError("cohort_id must be non-empty")
        cal = sorted({_check_day(str(d)) for d in calendar})
        if not cal:
            raise SchoolError("calendar must contain at least one school day")
        instr = sorted({str(i).strip() for i in instructors if str(i).strip()})
        if not instr:
            raise SchoolError("cohort needs at least one designated instructor")
        window_days = _as_int(window_days, "window_days")
        if window_days < 1:
            raise SchoolError("window_days must be >= 1")
        config = {"calendar": cal, "instructors": instr}
        with self.transaction() as c:
            ev = self._event(c, "cohort_configured", {"cohort": cid})
            c.execute(
                "INSERT INTO cohort(cohort_id, window_days, config) VALUES (?,?,?) "
                "ON CONFLICT(cohort_id) DO UPDATE SET window_days=excluded.window_days, "
                "config=excluded.config",
                (cid, window_days, _canon(config)),
            )
            for trainee in self._trainees(c):
                self._persist_ruling(c, ev, self.evaluate(cid, trainee))

    def _cohort_config(self, cohort_id: str) -> dict:
        row = self.conn.execute(
            "SELECT window_days, config FROM cohort WHERE cohort_id=?", (cohort_id,)
        ).fetchone()
        if row is None:
            raise SchoolError(f"unknown cohort {cohort_id}")
        config = json.loads(row["config"])
        config["window_days"] = int(row["window_days"])
        return config

    def cohorts(self) -> list[str]:
        return [
            r["cohort_id"]
            for r in self.conn.execute("SELECT cohort_id FROM cohort ORDER BY cohort_id")
        ]

    # ---------------------------------------------------------------- shifts
    def record_shift(
        self,
        shift_id: str,
        instructor: str,
        start_ts: int,
        end_ts: int,
        *,
        version: int | None = None,
    ) -> int:
        """登记/补报/纠正一个班次；同一 shift_id 的版本严格递增。"""
        sid = str(shift_id).strip()
        instr = str(instructor).strip()
        if not sid or not instr:
            raise SchoolError("shift_id and instructor must be non-empty")
        start_ts = _as_int(start_ts, "start_ts")
        end_ts = _as_int(end_ts, "end_ts")
        if end_ts < start_ts:
            raise SchoolError("shift end_ts must be >= start_ts")
        with self.transaction() as c:
            prev = c.execute(
                "SELECT version, instructor, start_ts, end_ts FROM shift "
                "WHERE shift_id=? ORDER BY version DESC LIMIT 1",
                (sid,),
            ).fetchone()
            cur = int(prev["version"]) if prev else 0
            version = cur + 1 if version is None else _as_int(version, "version")
            if version <= cur:
                raise SchoolError(f"shift {sid} version must exceed {cur}")
            ev = self._event(
                c, "shift_recorded",
                {"shift": sid, "version": version, "instructor": instr,
                 "start_ts": start_ts, "end_ts": end_ts},
            )
            c.execute(
                "INSERT INTO shift(shift_id, version, instructor, start_ts, end_ts, "
                "retracted, recorded_event) VALUES (?,?,?,?,?,0,?)",
                (sid, version, instr, start_ts, end_ts, ev),
            )
            spans = [(instr, start_ts, end_ts)]
            if prev is not None and not self._shift_retracted(c, sid, cur):
                spans.append(
                    (prev["instructor"], int(prev["start_ts"]), int(prev["end_ts"]))
                )
            affected = self._reclassify_spans(c, spans)
            self._refresh_trainees(c, ev, affected)
        return version

    def retract_shift(self, shift_id: str) -> None:
        """撤销班次（纠正）：保留全部版本，最新版标记 retracted。"""
        sid = str(shift_id).strip()
        with self.transaction() as c:
            prev = c.execute(
                "SELECT version, instructor, start_ts, end_ts, retracted FROM shift "
                "WHERE shift_id=? ORDER BY version DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if prev is None:
                raise SchoolError(f"unknown shift {sid}")
            if prev["retracted"]:
                raise SchoolError(f"shift {sid} is already retracted")
            ev = self._event(c, "shift_retracted", {"shift": sid})
            c.execute(
                "INSERT INTO shift(shift_id, version, instructor, start_ts, end_ts, "
                "retracted, recorded_event) VALUES (?,?,?,?,?,1,?)",
                (sid, int(prev["version"]) + 1, prev["instructor"],
                 int(prev["start_ts"]), int(prev["end_ts"]), ev),
            )
            affected = self._reclassify_spans(
                c, [(prev["instructor"], int(prev["start_ts"]), int(prev["end_ts"]))]
            )
            self._refresh_trainees(c, ev, affected)

    def _shift_retracted(self, c: sqlite3.Connection, shift_id: str, version: int) -> bool:
        row = c.execute(
            "SELECT retracted FROM shift WHERE shift_id=? AND version=?",
            (shift_id, version),
        ).fetchone()
        return bool(row["retracted"]) if row else False

    def _effective_shifts(
        self, c: sqlite3.Connection, instructors: Sequence[str] | None = None
    ) -> list[dict]:
        sql = (
            "SELECT s.shift_id, s.version, s.instructor, s.start_ts, s.end_ts "
            "FROM shift s JOIN (SELECT shift_id, MAX(version) AS v FROM shift "
            "GROUP BY shift_id) x ON x.shift_id=s.shift_id AND x.v=s.version "
            "WHERE s.retracted=0"
        )
        args: list[object] = []
        if instructors:
            sql += f" AND s.instructor IN ({','.join('?' for _ in instructors)})"
            args.extend(instructors)
        return [dict(r) for r in c.execute(sql, args)]

    def shifts(self, instructor: str | None = None) -> list[dict]:
        instr = [instructor] if instructor is not None else None
        return self._effective_shifts(self.conn, instr)

    # ---------------------------------------------------------------- reports
    def record_report(
        self,
        copy_id: str,
        ticket_in: str,
        ticket_out: str,
        trainee: str,
        instructor: str | None = None,
        *,
        day: str | None = None,
    ) -> None:
        """登记一份考评簿抄本；同一票对的多份抄本只计一次。"""
        with self.transaction() as c:
            ev = self._event(c, "report_recorded", {"copy": copy_id})
            self._record_report(
                c, ev, copy_id, ticket_in, ticket_out, trainee, instructor, day=day
            )

    def _record_report(
        self,
        c: sqlite3.Connection,
        ev: int,
        copy_id: str,
        ticket_in: str,
        ticket_out: str,
        trainee: str,
        instructor: str | None = None,
        *,
        day: str | None = None,
    ) -> None:
        cid = str(copy_id).strip()
        tin, tout = str(ticket_in), str(ticket_out)
        tr = str(trainee).strip()
        instr = None if instructor is None else str(instructor).strip()
        if not cid or not tin or not tout or not tr:
            raise SchoolError("copy_id, tickets and trainee must be non-empty")
        if tin == tout:
            raise SchoolError("a report needs two distinct tickets")
        if day is not None:
            day = _check_day(str(day))
        row = c.execute(
            "SELECT * FROM report_copy WHERE copy_id=?", (cid,)
        ).fetchone()
        if row is not None:
            same = (
                row["ticket_in"] == tin and row["ticket_out"] == tout
                and row["trainee"] == tr and row["instructor"] == instr
                and row["day"] == day and not row["retracted"]
            )
            if same:
                return
            raise SchoolError(f"report copy {cid} exists with different data")
        c.execute(
            "INSERT INTO report_copy(copy_id, ticket_in, ticket_out, trainee, "
            "instructor, day, retracted, recorded_event) VALUES (?,?,?,?,?,?,0,?)",
            (cid, tin, tout, tr, instr, day, ev),
        )
        self._sync_pair(c, ev, tin, tout)

    def retract_report(self, copy_id: str) -> None:
        """撤销一份抄本（纠正）；若同票对还有其他抄本则按剩余抄本重定。"""
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM report_copy WHERE copy_id=?", (str(copy_id),)
            ).fetchone()
            if row is None or row["retracted"]:
                raise SchoolError(f"unknown or already retracted copy {copy_id}")
            ev = self._event(c, "report_retracted", {"copy": copy_id})
            c.execute(
                "UPDATE report_copy SET retracted=1 WHERE copy_id=?", (str(copy_id),)
            )
            self._sync_pair(c, ev, row["ticket_in"], row["ticket_out"])

    def _active_copies(
        self, c: sqlite3.Connection, ticket_in: str, ticket_out: str
    ) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM report_copy WHERE ticket_in=? AND ticket_out=? "
            "AND retracted=0 ORDER BY recorded_event, copy_id",
            (ticket_in, ticket_out),
        ).fetchall()

    def _sync_pair(
        self, c: sqlite3.Connection, ev: int, ticket_in: str, ticket_out: str
    ) -> None:
        """抄本变化后同步该票对的练习：去重、冲突标记、学员归属纠正。"""
        copies = self._active_copies(c, ticket_in, ticket_out)
        distinct = {(r["trainee"], r["instructor"]) for r in copies}
        prac = c.execute(
            "SELECT * FROM practice WHERE ticket_in=? AND ticket_out=?",
            (ticket_in, ticket_out),
        ).fetchone()
        if prac is None:
            return  # 练习事实未登记：等确认运输或事实导入
        trainees = {prac["trainee"]}
        if not distinct:
            c.execute("DELETE FROM practice WHERE practice_id=?", (prac["practice_id"],))
            self._refresh_trainees(c, ev, trainees)
            return
        conflict = len(distinct) > 1
        if conflict:
            if not prac["conflict"]:
                c.execute(
                    "UPDATE practice SET conflict=1 WHERE practice_id=?",
                    (prac["practice_id"],),
                )
            self._refresh_trainees(c, ev, trainees)
            return
        trainee, instr = next(iter(distinct))
        trainees.add(trainee)
        if (trainee, instr) != (prac["trainee"], prac["instructor_claim"]) or prac["conflict"]:
            c.execute(
                "UPDATE practice SET trainee=?, instructor_claim=?, conflict=0 "
                "WHERE practice_id=?",
                (trainee, instr, prac["practice_id"]),
            )
        trainees |= self._reclassify(c, [prac["practice_id"]])
        self._refresh_trainees(c, ev, trainees)

    # ---------------------------------------------------------------- practices
    def _create_practice(
        self,
        c: sqlite3.Connection,
        *,
        ticket_in: str,
        ticket_out: str,
        trip_id: str | None,
        trainee: str,
        instructor: str | None,
        tractor: str,
        ts_in: int,
        ts_out: int,
        seq_in: int,
        seq_out: int,
        product_in: dict,
        product_out: dict,
        actions: Sequence[dict],
        day: str | None,
    ) -> str:
        swaps, displays = _events_from_actions(actions, seq_in, seq_out, tractor)
        pday = day if day is not None else self.day_of(ts_out)
        day_rule = "recorded" if day is not None else self._day_rule
        pid = f"PR-{ticket_in}-{ticket_out}"
        evidence = {
            "actions": list(actions),
            "shifts": [],
            "swap_events": swaps,
            "display_events": displays,
        }
        c.execute(
            "INSERT INTO practice(practice_id, ticket_in, ticket_out, trip_id, "
            "trainee, instructor_claim, tractor, ts_in, ts_out, seq_in, seq_out, "
            "day, day_rule, swapped, klass, coverage, conflict, product_in, "
            "product_out, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (
                pid, ticket_in, ticket_out, trip_id, trainee, instructor, tractor,
                ts_in, ts_out, seq_in, seq_out, pday, day_rule, int(bool(swaps)),
                UNVERIFIED, None, _canon(product_in), _canon(product_out),
                _canon(evidence),
            ),
        )
        self._reclassify(c, [pid])
        return pid

    def _reclassify(
        self, c: sqlite3.Connection, practice_ids: Sequence[str]
    ) -> set[str]:
        """按当前生效班次重算分类；返回分类发生变化的学员集合。"""
        if not practice_ids:
            return set()
        rows = {
            r["practice_id"]: r
            for r in c.execute(
                f"SELECT * FROM practice WHERE practice_id IN "
                f"({','.join('?' for _ in practice_ids)})",
                list(practice_ids),
            )
        }
        claims = {r["instructor_claim"] for r in rows.values() if r["instructor_claim"]}
        shifts_by_instr: dict[str, list[dict]] = {i: [] for i in claims}
        for s in self._effective_shifts(c, sorted(claims)):
            shifts_by_instr.setdefault(s["instructor"], []).append(s)
        changed: set[str] = set()
        for pid, row in rows.items():
            claim = row["instructor_claim"]
            klass, coverage = _classify(
                claim, shifts_by_instr.get(claim, []), int(row["ts_in"]), int(row["ts_out"])
            )
            evidence = json.loads(row["evidence"])
            evidence["shifts"] = [
                dict(s) for s in shifts_by_instr.get(claim, [])
                if int(s["end_ts"]) >= int(row["ts_in"])
                and int(s["start_ts"]) <= int(row["ts_out"])
            ]
            if (
                klass != row["klass"]
                or coverage != row["coverage"]
                or _canon(evidence) != row["evidence"]
            ):
                c.execute(
                    "UPDATE practice SET klass=?, coverage=?, evidence=? "
                    "WHERE practice_id=?",
                    (klass, coverage, _canon(evidence), pid),
                )
                if klass != row["klass"] or coverage != row["coverage"]:
                    changed.add(row["trainee"])
        return changed

    def _reclassify_spans(
        self, c: sqlite3.Connection, spans: Sequence[tuple[str, int, int]]
    ) -> set[str]:
        """重分类与任一 (教练, 时段) 相交的练习（排班补报/纠正的增量入口）。"""
        pids: set[str] = set()
        for instr, lo, hi in spans:
            for r in c.execute(
                "SELECT practice_id FROM practice WHERE instructor_claim=? "
                "AND ts_in<=? AND ts_out>=?",
                (instr, hi, lo),
            ):
                pids.add(r["practice_id"])
        return self._reclassify(c, sorted(pids))

    def ingest_warehouse(
        self, warehouse: Warehouse, catalog: Catalog | None = None
    ) -> list[str]:
        """从运输库还原练习：每条已确认运输 × 已有抄本的票对 → 一份练习。

        换挂证据取自组合目录 ``(seq_in, seq_out]`` 的动作流水；运输库离线
        或只有外部快照时请改用 ``register_practice_facts`` 直接登记事实。
        幂等：已存在的票对跳过。
        """
        cat = catalog or warehouse.catalog
        if cat is None:
            raise SchoolError(
                "ingest_warehouse needs a catalog for swap evidence; "
                "use register_practice_facts when the transport store is offline"
            )
        created: list[str] = []
        with self.transaction() as c:
            ev = self._event(c, "ingest", {"source": "warehouse"})
            for trip in warehouse.trips():
                if trip["status"] != STATUS_CONFIRMED:
                    continue
                tin_id = trip["tickets"][IN]["ticket_id"]
                tout_id = trip["tickets"][OUT]["ticket_id"]
                if c.execute(
                    "SELECT 1 FROM practice WHERE ticket_in=? AND ticket_out=?",
                    (tin_id, tout_id),
                ).fetchone():
                    continue
                copies = self._active_copies(c, tin_id, tout_id)
                if not copies:
                    continue  # 无考评抄本：只是运输，不是练习
                distinct = {(r["trainee"], r["instructor"]) for r in copies}
                if len(distinct) > 1:
                    continue  # 冲突抄本：reconcile 会列出，等纠正
                trainee, instr = next(iter(distinct))
                evw = warehouse.evaluation(trip["trip_id"])
                tin = warehouse.ticket(tin_id)
                tout = warehouse.ticket(tout_id)
                seq_in = evw.derived_in.product_seq
                seq_out = evw.derived_out.product_seq
                actions = [
                    _norm_action(a)
                    for a in cat.actions_between(tin.tractor, seq_in, seq_out)
                ]
                pid = self._create_practice(
                    c, ticket_in=tin_id, ticket_out=tout_id,
                    trip_id=trip["trip_id"], trainee=trainee, instructor=instr,
                    tractor=tin.tractor, ts_in=tin.ts, ts_out=tout.ts,
                    seq_in=seq_in, seq_out=seq_out,
                    product_in=Snapshot(seq_in, evw.derived_in.product).to_dict(),
                    product_out=Snapshot(seq_out, evw.derived_out.product).to_dict(),
                    actions=actions, day=copies[0]["day"],
                )
                created.append(pid)
            self._refresh_trainees(c, ev, self._trainees(c))
        return created

    def register_practice_facts(
        self,
        *,
        ticket_in: str,
        ticket_out: str,
        tractor: str,
        ts_in: int,
        ts_out: int,
        seq_in: int,
        seq_out: int,
        product_in: Snapshot | Product | dict,
        product_out: Snapshot | Product | dict,
        actions: Sequence[Action | dict] = (),
        trip_id: str | None = None,
        report: dict | None = None,
        defer: bool = False,
    ) -> str | None:
        """直接登记练习事实（运输库离线导入 / 批量补录通道）。

        ``report`` 给出时先登记考评抄本；票对无有效抄本则记孤儿并返回
        ``None``——练习必须绑定已确认运输与考评簿。``defer=True`` 跳过
        即时重评（留给夜间复核）。
        """
        tin, tout = str(ticket_in), str(ticket_out)
        with self.transaction() as c:
            ev = self._event(c, "practice_facts", {"pair": [tin, tout]})
            if report is not None:
                self._record_report(c, ev, ticket_in=tin, ticket_out=tout, **report)
            row = c.execute(
                "SELECT practice_id FROM practice WHERE ticket_in=? AND ticket_out=?",
                (tin, tout),
            ).fetchone()
            if row is not None:
                return row["practice_id"]
            copies = self._active_copies(c, tin, tout)
            if not copies:
                c.execute(
                    "INSERT OR IGNORE INTO orphan(ticket_in, ticket_out, reason, "
                    "detail, event_seq) VALUES (?,?,?,?,?)",
                    (tin, tout, "facts_without_report",
                     _canon({"tractor": str(tractor)}), ev),
                )
                return None
            distinct = {(r["trainee"], r["instructor"]) for r in copies}
            if len(distinct) > 1:
                c.execute(
                    "INSERT OR IGNORE INTO orphan(ticket_in, ticket_out, reason, "
                    "detail, event_seq) VALUES (?,?,?,?,?)",
                    (tin, tout, "conflicting_reports",
                     _canon({"claims": sorted(map(str, distinct))}), ev),
                )
                return None
            trainee, instr = next(iter(distinct))
            norm_actions = [_norm_action(a) for a in actions]
            pid = self._create_practice(
                c, ticket_in=tin, ticket_out=tout, trip_id=trip_id,
                trainee=trainee, instructor=instr, tractor=str(tractor),
                ts_in=_as_int(ts_in, "ts_in"), ts_out=_as_int(ts_out, "ts_out"),
                seq_in=_as_int(seq_in, "seq_in"), seq_out=_as_int(seq_out, "seq_out"),
                product_in=_norm_product(product_in, _as_int(seq_in, "seq_in")),
                product_out=_norm_product(product_out, _as_int(seq_out, "seq_out")),
                actions=norm_actions, day=copies[0]["day"],
            )
            if not defer:
                self._refresh_trainees(c, ev, {trainee})
            return pid

    # ---------------------------------------------------------------- queries
    def _row_to_practice(self, row: sqlite3.Row) -> Practice:
        return Practice(
            practice_id=row["practice_id"],
            ticket_in=row["ticket_in"],
            ticket_out=row["ticket_out"],
            trip_id=row["trip_id"],
            trainee=row["trainee"],
            instructor_claim=row["instructor_claim"],
            tractor=row["tractor"],
            ts_in=int(row["ts_in"]),
            ts_out=int(row["ts_out"]),
            seq_in=int(row["seq_in"]),
            seq_out=int(row["seq_out"]),
            day=row["day"],
            day_rule=row["day_rule"],
            swapped=bool(row["swapped"]),
            klass=row["klass"],
            coverage=row["coverage"],
            conflict=bool(row["conflict"]),
            product_in=json.loads(row["product_in"]),
            product_out=json.loads(row["product_out"]),
            evidence=json.loads(row["evidence"]),
        )

    def practice(self, practice_id: str) -> Practice:
        row = self.conn.execute(
            "SELECT * FROM practice WHERE practice_id=?", (practice_id,)
        ).fetchone()
        if row is None:
            raise SchoolError(f"unknown practice {practice_id}")
        return self._row_to_practice(row)

    def practices(self, trainee: str | None = None) -> list[Practice]:
        sql = "SELECT * FROM practice"
        args: list[object] = []
        if trainee is not None:
            sql += " WHERE trainee=?"
            args.append(trainee)
        sql += " ORDER BY day, ts_out, practice_id"
        return [self._row_to_practice(r) for r in self.conn.execute(sql, args)]

    def _trainees(self, c: sqlite3.Connection) -> set[str]:
        return {
            r["trainee"]
            for r in c.execute("SELECT DISTINCT trainee FROM practice")
        }

    def _default_today(self) -> str:
        row = self.conn.execute("SELECT MAX(day) AS m FROM practice").fetchone()
        return row["m"] or "9999-12-31"

    def orphans(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT ticket_in, ticket_out, reason, detail, event_seq FROM orphan "
                "ORDER BY ticket_in, ticket_out"
            )
        ]

    # ---------------------------------------------------------------- rulings
    def evaluate(
        self, cohort_id: str, trainee: str, *, today: str | None = None
    ) -> Ruling:
        """按届规则评估一名学员：最早达标日、支持链、尚可完成的阶段。"""
        co = self._cohort_config(cohort_id)
        cal: list[str] = co["calendar"]
        instr = set(co["instructors"])
        width = int(co["window_days"])
        today = today or self._default_today()
        practices = [p for p in self.practices(trainee) if not p.conflict]

        guided: dict[str, list[Practice]] = {}
        for p in practices:
            if p.klass == GUIDED and p.swapped and p.instructor_claim in instr:
                guided.setdefault(p.day, []).append(p)
        independents = [p for p in practices if p.klass == INDEPENDENT]

        windows: list[dict] = []
        best_day: str | None = None
        best_chain: tuple[dict, ...] | None = None
        for g in sorted(guided):
            window = _next_school_days(cal, g, width)
            wset = set(window)
            in_window = [p for p in independents if p.day in wset]
            first = in_window[0] if in_window else None
            second = next(
                (p for p in in_window
                 if first is not None and p.day > first.day and p.swapped),
                None,
            )
            g_prac = guided[g][0]
            remaining = [d for d in window if d >= today]
            if second is not None:
                status, need, completable = "satisfied", [], False
            else:
                need = (
                    ["first_independent", "second_independent_swap"]
                    if first is None
                    else ["second_independent_swap"]
                )
                status = "open" if remaining else "expired"
                completable = (
                    len(remaining) >= 2
                    if first is None
                    else any(d > first.day for d in remaining)
                )
            win = {
                "guided_day": g,
                "window": window,
                "status": status,
                "first_independent_day": first.day if first else None,
                "second_independent_day": second.day if second else None,
                "need": need,
                "remaining_days": remaining,
                "completable": completable,
            }
            windows.append(win)
            if second is not None and (best_day is None or second.day < best_day):
                best_day = second.day
                best_chain = (
                    g_prac.ref(ROLE_GUIDED),
                    first.ref(ROLE_FIRST),
                    second.ref(ROLE_SECOND),
                )
        if best_day is not None:
            status, stage = QUALIFIED, 3
        elif guided:
            status, stage = IN_PROGRESS, 2
        else:
            status, stage = WAITING_GUIDED, 1
        return Ruling(
            cohort_id=cohort_id, trainee=trainee, status=status, stage=stage,
            earliest_day=best_day, chain=tuple(best_chain or ()),
            windows=tuple(windows), today=today,
        )

    def _persist_ruling(
        self, c: sqlite3.Connection, event_seq: int, ruling: Ruling
    ) -> bool:
        row = c.execute(
            "SELECT status, earliest_day, chain, stages FROM ruling "
            "WHERE cohort_id=? AND trainee=? ORDER BY id DESC LIMIT 1",
            (ruling.cohort_id, ruling.trainee),
        ).fetchone()
        chain_j = _canon(list(ruling.chain))
        stages_j = _canon(list(ruling.windows))
        if (
            row is not None
            and row["status"] == ruling.status
            and row["earliest_day"] == ruling.earliest_day
            and row["chain"] == chain_j
            and row["stages"] == stages_j
        ):
            return False
        c.execute(
            "INSERT INTO ruling(cohort_id, trainee, event_seq, status, earliest_day, "
            "chain, stages) VALUES (?,?,?,?,?,?,?)",
            (ruling.cohort_id, ruling.trainee, event_seq, ruling.status,
             ruling.earliest_day, chain_j, stages_j),
        )
        return True

    def _refresh_trainees(
        self, c: sqlite3.Connection, event_seq: int, trainees: set[str]
    ) -> None:
        if not trainees:
            return
        for cohort_id in self.cohorts():
            for trainee in sorted(trainees):
                self._persist_ruling(c, event_seq, self.evaluate(cohort_id, trainee))

    def ruling(self, cohort_id: str, trainee: str) -> dict | None:
        """最近一次已落盘的结论（含事件序号），无则 None。"""
        row = self.conn.execute(
            "SELECT * FROM ruling WHERE cohort_id=? AND trainee=? "
            "ORDER BY id DESC LIMIT 1",
            (cohort_id, trainee),
        ).fetchone()
        if row is None:
            return None
        return {
            "cohort_id": row["cohort_id"],
            "trainee": row["trainee"],
            "event_seq": int(row["event_seq"]),
            "status": row["status"],
            "earliest_day": row["earliest_day"],
            "chain": json.loads(row["chain"]),
            "stages": json.loads(row["stages"]),
        }

    def ruling_history(self, cohort_id: str, trainee: str) -> list[dict]:
        return [
            {
                "event_seq": int(r["event_seq"]),
                "status": r["status"],
                "earliest_day": r["earliest_day"],
                "chain": json.loads(r["chain"]),
            }
            for r in self.conn.execute(
                "SELECT * FROM ruling WHERE cohort_id=? AND trainee=? ORDER BY id",
                (cohort_id, trainee),
            )
        ]

    def nightly_review(self, *, today: str | None = None) -> dict:
        """每晚全量复核：重分类全部练习，重评全部 (届, 学员) 并落盘。"""
        with self.transaction() as c:
            ev = self._event(c, "nightly_review", {"today": today})
            pids = [
                r["practice_id"]
                for r in c.execute("SELECT practice_id FROM practice")
            ]
            changed = self._reclassify(c, pids)
            trainees = self._trainees(c)
            cohorts = self.cohorts()
            rulings_changed = 0
            qualified: list[dict] = []
            for trainee in sorted(trainees):
                for cohort_id in cohorts:
                    r = self.evaluate(cohort_id, trainee, today=today)
                    if self._persist_ruling(c, ev, r):
                        rulings_changed += 1
                    if r.status == QUALIFIED:
                        qualified.append(
                            {"cohort": cohort_id, "trainee": trainee,
                             "day": r.earliest_day}
                        )
            copies = int(
                c.execute(
                    "SELECT COUNT(*) AS n FROM report_copy WHERE retracted=0"
                ).fetchone()["n"]
            )
            pairs = int(
                c.execute(
                    "SELECT COUNT(DISTINCT ticket_in || char(0) || ticket_out) AS n "
                    "FROM report_copy WHERE retracted=0"
                ).fetchone()["n"]
            )
            return {
                "event_seq": ev,
                "practices": len(pids),
                "trainees_reclassified": len(changed),
                "trainees": len(trainees),
                "cohorts": len(cohorts),
                "rulings_changed": rulings_changed,
                "qualified": qualified,
                "duplicate_copies_dropped": copies - pairs,
            }

    # ---------------------------------------------------------------- certificates
    def issue_certificate(
        self, cohort_id: str, trainee: str, *, key: bytes | None = None
    ) -> dict:
        """为当前已达标的 (届, 学员) 签发内嵌全部证据的证书。"""
        row = self.conn.execute(
            "SELECT * FROM ruling WHERE cohort_id=? AND trainee=? "
            "ORDER BY id DESC LIMIT 1",
            (cohort_id, trainee),
        ).fetchone()
        if row is None or row["status"] != QUALIFIED:
            raise SchoolError(f"{trainee} is not qualified in cohort {cohort_id}")
        co = self._cohort_config(cohort_id)
        links = []
        for ref in json.loads(row["chain"]):
            p = self.practice(ref["practice_id"])
            links.append({
                "role": ref["role"],
                "practice_id": p.practice_id,
                "trip_id": p.trip_id,
                "ticket_in": p.ticket_in,
                "ticket_out": p.ticket_out,
                "trainee": p.trainee,
                "instructor_claim": p.instructor_claim,
                "klass": p.klass,
                "swapped": p.swapped,
                "coverage": p.coverage,
                "day": p.day,
                "day_rule": p.day_rule,
                "tractor": p.tractor,
                "ts_in": p.ts_in,
                "ts_out": p.ts_out,
                "seq_in": p.seq_in,
                "seq_out": p.seq_out,
                "product_in": p.product_in,
                "product_out": p.product_out,
                "evidence": p.evidence,
            })
        payload = {
            "Kind": "school_qualification_certificate",
            "Version": 1,
            "cohort_id": cohort_id,
            "trainee": trainee,
            "qualified_day": row["earliest_day"],
            "rule": {
                "window_days": co["window_days"],
                "calendar": co["calendar"],
                "instructors": co["instructors"],
            },
            "chain": links,
            "issued_event": int(row["event_seq"]),
        }
        digest = _sha256_text(_canon(payload))
        sign_key = key if key is not None else self.signing_key
        cert = dict(payload)
        cert["payload_hash"] = digest
        cert["signature"] = _sign(sign_key, digest) if sign_key else None
        with self.transaction() as c:
            self._event(c, "certificate_issued",
                        {"cohort": cohort_id, "trainee": trainee,
                         "day": row["earliest_day"], "hash": digest})
        return cert

    # ---------------------------------------------------------------- reconcile
    def reconcile(self) -> list[str]:
        """核对入口：返回不一致列表（空 = 通过）。"""
        errs: list[str] = []
        shifts_by_instr: dict[str, list[dict]] = {}
        for s in self._effective_shifts(self.conn):
            shifts_by_instr.setdefault(s["instructor"], []).append(s)

        for p in self.practices():
            where = f"practice {p.practice_id}"
            if p.seq_out < p.seq_in:
                errs.append(f"{where}: seq_out {p.seq_out} < seq_in {p.seq_in}")
            if p.ts_out < p.ts_in:
                errs.append(f"{where}: ts_out {p.ts_out} < ts_in {p.ts_in}")
            swaps, _ = _events_from_actions(
                p.evidence.get("actions", []), p.seq_in, p.seq_out, p.tractor
            )
            if bool(swaps) != p.swapped:
                errs.append(f"{where}: swapped flag not reproducible from actions")
            if not p.swapped and _product_identity(p.product_in) != _product_identity(
                p.product_out
            ):
                errs.append(f"{where}: no swap but product identities differ")
            klass, coverage = _classify(
                p.instructor_claim,
                shifts_by_instr.get(p.instructor_claim, []),
                p.ts_in, p.ts_out,
            )
            if klass != p.klass:
                errs.append(f"{where}: stored klass {p.klass} != recomputed {klass}")
            elif coverage != p.coverage and (
                coverage is None
                or p.coverage is None
                or abs(coverage - p.coverage) > 1e-9
            ):
                errs.append(f"{where}: stored coverage != recomputed coverage")
            if p.day_rule == "epoch_day" and p.day != epoch_day(p.ts_out):
                errs.append(f"{where}: day {p.day} != epoch_day(ts_out)")

        pairs = self.conn.execute(
            "SELECT ticket_in, ticket_out FROM report_copy WHERE retracted=0 "
            "GROUP BY ticket_in, ticket_out"
        ).fetchall()
        for pair in pairs:
            copies = self._active_copies(self.conn, pair["ticket_in"], pair["ticket_out"])
            distinct = {(r["trainee"], r["instructor"]) for r in copies}
            prac = self.conn.execute(
                "SELECT practice_id, conflict FROM practice WHERE ticket_in=? "
                "AND ticket_out=?",
                (pair["ticket_in"], pair["ticket_out"]),
            ).fetchone()
            if len(distinct) > 1:
                errs.append(
                    f"ticket pair ({pair['ticket_in']},{pair['ticket_out']}) has "
                    f"conflicting report copies"
                )
                if prac is not None and not prac["conflict"]:
                    errs.append(f"practice {prac['practice_id']} not flagged conflict")
            elif prac is None:
                errs.append(
                    f"orphan report for pair ({pair['ticket_in']},"
                    f"{pair['ticket_out']}): no confirmed transport/facts"
                )

        for r in self.conn.execute(
            "SELECT DISTINCT cohort_id, trainee FROM ruling"
        ).fetchall():
            stored = self.ruling(r["cohort_id"], r["trainee"])
            fresh = self.evaluate(r["cohort_id"], r["trainee"])
            if (
                stored["status"] != fresh.status
                or stored["earliest_day"] != fresh.earliest_day
                or stored["chain"] != list(fresh.chain)
            ):
                errs.append(
                    f"latest ruling for {r['trainee']} in {r['cohort_id']} is stale"
                )
        return errs

    def verify(self) -> list[str]:
        return self.reconcile()


# ---------------------------------------------------------------- offline verify
def verify_certificate(cert: dict | str, *, key: bytes | None = None) -> list[str]:
    """离线复核已签证的证书：只需证书本身（与可选密钥），不碰运输库。

    重算：哈希与签名、每环练习的换挂事件（动作流水）、指导分类（班次
    覆盖）、记日规则、窗口与顺序规则、达标日。返回不一致列表（空 =
    原判成立）。
    """
    errs: list[str] = []
    if isinstance(cert, str):
        try:
            cert = json.loads(cert)
        except json.JSONDecodeError as exc:
            return [f"invalid certificate json: {exc}"]
    if not isinstance(cert, dict) or cert.get("Kind") != "school_qualification_certificate":
        return ["not a school_qualification_certificate"]
    if int(cert.get("Version", 0)) != 1:
        errs.append(f"unsupported certificate Version {cert.get('Version')!r}")

    payload = {k: v for k, v in cert.items() if k not in ("payload_hash", "signature")}
    digest = _sha256_text(_canon(payload))
    if cert.get("payload_hash") != digest:
        errs.append("payload hash mismatch")
    sig = cert.get("signature")
    if sig is None:
        if key is not None:
            errs.append("certificate is unsigned")
    else:
        if key is None:
            errs.append("certificate is signed but no key was supplied")
        elif not hmac.compare_digest(_sign(key, digest), str(sig)):
            errs.append("bad signature")

    rule = cert.get("rule", {})
    calendar = sorted(rule.get("calendar", []))
    width = int(rule.get("window_days", 0) or 0)
    instructors = set(rule.get("instructors", []))
    chain = cert.get("chain", [])
    if not isinstance(chain, list) or not chain:
        errs.append("certificate has no practice chain")
        return errs

    pairs: set[tuple] = set()
    for link in chain:
        where = f"link {link.get('role')}:{link.get('practice_id')}"
        try:
            ts_in = int(link["ts_in"])
            ts_out = int(link["ts_out"])
            seq_in = int(link["seq_in"])
            seq_out = int(link["seq_out"])
        except (KeyError, TypeError, ValueError) as exc:
            errs.append(f"{where}: malformed timestamps/seqs ({exc})")
            continue
        if ts_out < ts_in:
            errs.append(f"{where}: ts_out before ts_in")
        if seq_out < seq_in:
            errs.append(f"{where}: seq_out before seq_in")
        swaps, _ = _events_from_actions(
            link.get("evidence", {}).get("actions", []), seq_in, seq_out,
            str(link.get("tractor")),
        )
        if bool(swaps) != bool(link.get("swapped")):
            errs.append(f"{where}: swapped flag not reproducible from actions")
        claim = link.get("instructor_claim")
        shifts = [
            s for s in link.get("evidence", {}).get("shifts", [])
            if s.get("instructor") == claim
        ]
        klass, coverage = _classify(claim, shifts, ts_in, ts_out)
        if klass != link.get("klass"):
            errs.append(f"{where}: klass {link.get('klass')} != recomputed {klass}")
        elif link.get("coverage") is not None and coverage is not None and abs(
            float(link["coverage"]) - coverage
        ) > 1e-9:
            errs.append(f"{where}: coverage not reproducible")
        if link.get("day_rule") == "epoch_day" and link.get("day") != epoch_day(ts_out):
            errs.append(f"{where}: day not reproducible from ts_out")
        if not link.get("swapped") and _product_identity(
            link.get("product_in", {})
        ) != _product_identity(link.get("product_out", {})):
            errs.append(f"{where}: no swap but product identities differ")
        pair = (link.get("ticket_in"), link.get("ticket_out"))
        if pair in pairs:
            errs.append(f"{where}: duplicate ticket pair in chain")
        pairs.add(pair)
        if link.get("trainee") != cert.get("trainee"):
            errs.append(f"{where}: trainee mismatch with certificate")

    by_role = {link.get("role"): link for link in chain}
    g = by_role.get(ROLE_GUIDED)
    first = by_role.get(ROLE_FIRST)
    second = by_role.get(ROLE_SECOND)
    if g is None or first is None or second is None:
        errs.append("chain lacks guided/first_independent/second_independent links")
        return errs
    if g.get("klass") != GUIDED or not g.get("swapped"):
        errs.append("guided link is not a verified guided swap")
    if g.get("instructor_claim") not in instructors:
        errs.append("guided instructor is not designated by the cohort rule")
    window = _next_school_days(calendar, str(g.get("day")), width)
    if first.get("klass") != INDEPENDENT or first.get("day") not in window:
        errs.append("first independent practice is not inside the window")
    if (
        second.get("klass") != INDEPENDENT
        or not second.get("swapped")
        or second.get("day") not in window
        or str(second.get("day")) <= str(first.get("day"))
    ):
        errs.append("second independent swap is not a later school day in the window")
    elif cert.get("qualified_day") != second.get("day"):
        errs.append("qualified_day does not match the second independent day")
    return errs
