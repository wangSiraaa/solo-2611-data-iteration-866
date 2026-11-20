# 挂车换接下的双向过磅与净装载事实仓

一台牵引车第一次带空挂车进厂、第二次却换了另一挂车出厂时，若按车牌配对两次
毛重，会把两套不同空车质量混在一起，制造不存在的净货重。本仓库把问题拆成
两个互相独立的模块：

* **车辆组合目录** (`weighbridge/catalog.py`)：只管车辆稳定身份、空车质量整数
  区间 `[lo, hi]`（kg）以及按严格递增序号发生的组合动作。它不知道磅单。
* **过磅事实仓** (`weighbridge/warehouse.py`)：消费目录在票面序号处的 *公开组合
  产品*，把毛重换算成货重区间、组织运输、计算净装卸量的精确区间、处理重评。

全部输入为本地合成整数记录，不连接地磅或车牌识别设备。Python 3.11，仅用标准
库（SQLite + `fractions.Fraction`）。

第三个模块建立在前两者之上：

* **学校资格复核** (`weighbridge/school.py`)：运输学校从磅单认定实习经历。
  把已确认运输、目录动作流水、教练排班与考评簿抄本还原成**练习**，按届
  （校历 + 指定教练）判定实习司机最早具备结业资格的那一天，结论可签证、
  可离线复核。

## 快速开始

```bash
python3 -m unittest discover -s tests -v     # 78 个测试
python3 -m demo.run_demo                     # 磅单合成案例
python3 -m demo.run_school_demo              # 学校资格复核合成案例
python3 -m demo.run_demo --json              # 另写 demo/out/*.json
python3 -m demo.run_school_demo --json       # 另写 demo/out/school_*.json
```

```python
from weighbridge import Catalog, Warehouse, LOAD, IN, OUT

c = Catalog()
c.define_mass("tractor", "T1", 7000, 7200)   # 整数 kg，lo <= hi
c.define_mass("trailer", "R1", 1500, 1600)
c.define_mass("trailer", "R2", 2000, 2200)

c.apply_attach("T1", "R1")        # seq 1
c.apply_detach("T1", "R1")        # seq 2
c.apply_attach("T1", "R2")        # seq 3
print(c.product_at(1, "T1").stable_identity())   # ('T1', 'R1')，历史不被换挂覆盖

w = Warehouse(c)
w.add_ticket(dict(ticket_id="IN",  tractor="T1", catalog_seq=1, ts=100,
                  direction=IN,  gross=9000))
w.add_ticket(dict(ticket_id="OUT", tractor="T1", catalog_seq=3, ts=200,
                  direction=OUT, gross=12000))

ev = w.preview(LOAD, "IN", "OUT")  # DRAFT：只算，不占票
print(ev.net_lo, ev.net_hi)        # 2300 2600（整数空重区间上的精确极值）
w.confirm(ev.trip_id)              # CONFIRMED：此刻才占票
print(w.reconcile())               # [] 表示核对通过
```

## 目录语义

* **稳定身份**：实体 ID 为 `tractor:<id>` / `trailer:<id>`，永不复用。
* **动作序号**：`attach` / `detach` / `set_mass` 的序号严格递增；失败不消耗
  序号。**批动作** `apply_batch` 在单事务内执行，任一步失败整批回滚（序号、
  绑定、产品、批次记录全部不存在）。
* **组合约束**：一个挂车同时至多一台牵引车；一台牵引车同时至多一个挂车。
  由两枚 `WHERE detach_seq IS NULL` 的部分唯一索引强制。
* **组合产品**：每次 `attach`/`detach` 产生一个 `EMPTY` 产品；`set_mass` 产生
  `RELABEL` 产品。产品带 `relation_src`（`initial`/`attach`/`detach`/
  `mass_correction`）、`attach_seq`、`predecessor`。
* **历史不被覆盖**：产品的质量是落盘时的快照。查询任意序号：

  ```python
  c.product_at(seq, tractor)   # 该序号处已生效的 live 产品
  c.history("trailer", "R1")   # 该实体参与过的全部产品（含更正版）
  c.snapshot_at(seq, tractor)  # 可跨系统交换的 JSON 快照
  c.corrected_product(p)       # 同一挂接 + attach_seq 的最新更正版
  ```

  换挂之后，旧序号处的旧产品原样保留；`corrected_product` 只沿同一
  `(identity, attach_seq)` 的 `predecessor` 链走，绝不从 "当前挂接" 猜历史。
* **展示号**：`apply_set_display(tractor, display)` 只改组合对外展示的号牌，
  稳定身份不变（`display_change` 来源的 RELABEL 产品，展示号随产品沿
  attach/detach 继承）。`c.actions_between(tractor, lo, hi)` 按序号区间返回
  该牵引车的动作流水，换挂与改号在此严格区分——学校据此认定练习期间的
  实际换挂。

## 仓库语义

**单票货重**：`tare` 为组合产品各实体空车质量之和，
`goods = gross - tare`，可行区间
`[max(0, gross - tare_hi), gross - tare_lo]`；`gross < tare_lo`（上界为负）时该票
不合物理约束。`explain_ticket(tid)` 给出毛重、`tare`、`goods`、每个实体区间与
宽度——即 "这张票为什么是这个货重、哪些实体贡献了不确定性"。

**运输**：`preview(kind, ticket_in, ticket_out)` 显式选定一 IN 一 OUT 票，绝不按
时间就近猜配。IN 时间不得晚于 OUT；两票必须同车；一票不能同时占两个角色。
结果状态：

| 状态 | 含义 |
| --- | --- |
| `DRAFT` | 预览，未占票；失败时为不可确认的草稿 |
| `CONFIRMED` | 已确认；两票写进 `confirmed_ticket`，此后不可被别的运输占用 |
| `REVIEW` | 对已确认运输的重开/重评 |
| `SUPERSEDED` | 未确认的旧草稿/复核被同族新结果取代 |

**净装卸量**：

* 组合完全相同（同一牵引车 + 同一挂车）：空车质量是同一未知量，在毛重差中
  **精确抵消**，`net == |gross_out - gross_in|`，与区间宽度无关。
* 换挂：共享实体（如牵引车）系数为 0，精确抵消；不同实体以目录区间参与。
  在 "两张票货重非负 + 运输方向" 的联合可行域上，对净装卸量做**精确整数分支
  定界**（实数 LP 松弛用有理数顶点枚举），输出最小/最大净装卸量与货流区间。
  可行域为空则拒绝，区分 `DIRECTION_IMPOSSIBLE` 与 `TICKET_INFEASIBLE`。
* 绝不使用区间中点或实数顶点作为结算值。

`contributions` 逐个实体给出 `role`（`cancelled`/`fixed`/`uncertain`）、线性系数与
区间宽度。

**重评与重开**：

```python
c.apply_set_mass("trailer", "R1", 1500, 1600)  # 目录迟到更正
re = w.reopen(trip_id)        # 新增 REVIEW 运输，带 predecessor_trip
w.evaluation_history(re.trip_id)  # 旧（CONFIRMED）与新（REVIEW）都在，链式可追溯
```

* 已确认运输及其评估是历史事实，不会被覆盖或标为 `superseded`。
* 被确认运输占用的票不会被重新记成另一运输（重评沿用同一票与角色）。
* 重评默认对原评估中的 *历史产品* 取 `corrected_product`，而不是套用当前挂接。

**外部组合快照**：`Warehouse(None)` 可完全不依赖目录运行；用
`register_snapshot(...)` 导入与目录相同 JSON 格式的快照，按票面序号取产品。
若某张票的序号没有对应快照，直接报错——绝不回退到当前挂接去猜。

**核对**：`w.reconcile()`（`w.verify()` 同义，含目录自检）返回不一致列表，覆盖：
确认运输的两票方向/同车/时间序与可行性、确认占票唯一、每条评估在存下的快照
上可重算、同身份精确抵消、重评前序存在。

## 学校语义（运输学校资格复核）

学校不修改运输库，只在其上建自己的簿记（`School`，独立 SQLite）：

* **练习还原**：一份练习 = 一趟**已确认运输**（IN/OUT 票对）+ 当时组合版
  （入票/出票序号处的目录快照）+ 一份考评簿抄本（学员、声称教练）。
  同一票对的多份抄本只计一次；内容冲突的抄本使该票对进入冲突态，纠正
  （撤销抄本）前不计入任何资格。无确认运输/无事实的抄本记为孤儿。
* **换挂认定**：入票序号至出票序号之间 `(seq_in, seq_out]`，该牵引车发生的
  任何 `attach`/`detach`（稳定挂车身份变化）都算练习期间换过挂，包括
  A→B→A；`set_display`（展示号变化）与 `set_mass`（质量更正）不算。
  因此"同一挂车两次改号"即便票面和净重与真换挂完全相同，也不算换挂。
* **指导核实**：考评簿声称的教练经**排班**核实覆盖 `[ts_in, ts_out]` 全程 →
  `GUIDED`；声称无教练 → `INDEPENDENT`；覆盖不足 → `PARTIAL`；声称有教练
  但排班查无在场 → `UNVERIFIED`。后两者另记，均不能充抵指导或独立。
* **届规则**：先完成**指定教练的一次指导换挂**（第一阶段）；每个这样的指导
  日各自开启窗口 = 校历上紧随其后 `window_days` 个校历日（默认 3）。窗口内
  按顺序完成两次独立练习、**不同日**、**第二次须换挂**，即结业于第二次的
  日期。各窗口分别考察，新指导不抹掉尚有效的旧窗口；最早达标日取所有窗口
  的最小值。`evaluate()` 同时给出"当前尚可能完成的阶段"（每个窗口缺什么、
  还剩哪些校历日、是否还来得及）。
* **补录与纠正**：排班按 `(shift_id, version)` 版本化，可补报多天前的班次、
  可撤销；受影响的练习自动重分类、相关学员自动重评，结论历史保留在
  `ruling` 表——允许改变过去可证明的最早日期（推迟、消除，或补出更早的）。
* **签证与离线复核**：达标时 `issue_certificate()` 签发内嵌全部证据（票对、
  组合快照、动作流水、班次、届规则）的 HMAC 签名证书；运输库离线时用模块级
  `verify_certificate(cert, key=...)` 重算换挂事件、班次覆盖、窗口与顺序，
  复核原判。已签证的证书不受后来更正影响，永远可按自身证据重开。
* **规模**：`nightly_review()` 每晚全量重分类 + 重评（实测 12,000 份练习、
  400 学员 × 3 届约 1 秒）；日间单个教练补报只触发相交练习的增量重评。
  运输库离线或分片时，用 `register_practice_facts()` 直接登记练习事实。

```python
from weighbridge import Catalog, School, Warehouse, verify_certificate

s = School(signing_key=b"...")
s.record_shift("SH-1", "U", ts_in_day1, ts_out_day1)      # 排班（可补报/撤销）
s.record_report("C1", "IN-1", "OUT-1", "甲", "U")          # 考评簿抄本
s.set_cohort("2026届", calendar=[...校历日...], instructors=["U"])
s.ingest_warehouse(w, c)                                   # 还原练习
r = s.evaluate("2026届", "甲")                             # 最早达标日 + 链 + 阶段
cert = s.issue_certificate("2026届", "甲")                 # 签证
verify_certificate(cert, key=b"...")                       # 离线复核原判 → []
```

## 容量

目录 100 实体（`define_mass` 时强制）；仓库 1000 张票、200 次运输。同一车同一秒
可有多张不同 `ticket_id` 的票。学校簿记不设硬上限（夜间全量复核上万份练习）。

## 文件

```
weighbridge/catalog.py      独立车辆组合目录（含展示号）
weighbridge/warehouse.py    过磅事实仓 + 精确整数求解
weighbridge/school.py       运输学校资格复核（练习/排班/届规则/签证）
weighbridge/__init__.py     公共 API
tests/test_weighbridge.py   49 个测试（含小整数区间暴力核对极值）
tests/test_school.py        29 个测试（题面案例/窗口/纠正/签证/规模）
demo/run_demo.py            磅单合成案例
demo/run_school_demo.py     学校资格复核合成案例
demo/out/                   演示输出（--json 时生成）
```

## 覆盖的案例（学校）

* 题面案例：D1 指导 A→B→A、D2 独立直行、D3 独立换挂 → D3 结业
* 同一挂车两次改号：票面/净重与真换挂相同 → 不具备资格
* 另一届只认别的指定教练 → 同一批练习仍在等待第一阶段
* 两次指导窗口重叠：新指导不抹掉旧窗口，最早日出自旧窗口
* 晚到更正把最早结业日推迟（D3→D6）与消除（回到等待第一阶段）
* 教练补报多天前的班次 → 过去可证明的最早日期被改写
* 部分陪同/查无班次另记，不能充抵指导或独立；同票对抄本去重与冲突
* 窗口恰为三个校历日、两次独立须不同日、第二次须换挂、校历空档跳过
* 签证离线复核：篡改/错钥可检出；更正后旧证书仍按自身证据复核原判
* 每晚全量复核 12,000 份练习（约 1 秒），日间补报走增量重评

## 覆盖的案例

* 同车同挂精确抵消（含极宽空车区间）
* 仅换挂：共享牵引车抵消、两挂车参与整数区间极值（与暴力枚举逐个核对）
* 目录迟到更正：已确认运输不变，新增带前序身份的重评
* 方向不可能、单票物理不可行、两票区间不相交
* 同票竞争：已确认运输占票后另一运输确认失败，且失败不改变状态/不占票
* 重开：多轮更正形成可追溯评估链
* 外部快照独立运行、快照冲突拒绝、缺失历史产品拒绝
* 批动作原子回滚、序号严格递增、单挂车/单牵引车约束、容量上限
