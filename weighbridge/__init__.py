"""Public API for the standalone vehicle-composition catalogue and weighbridge
fact store.

    from weighbridge import Catalog, Warehouse

两个模块互相独立：``Catalog`` 不知道磅单；``Warehouse`` 只通过
``Product``/``Snapshot`` 公开结构读取目录，也可导入外部同格式快照。

运输学校资格复核（``School``）建立在这两者之上：从已确认运输与目录
动作流水还原练习，按届（校历 + 指定教练）判定最早结业日，结论可
签证并离线复核。
"""

from .catalog import (
    Action,
    Catalog,
    CatalogError,
    LimitError,
    MassRange,
    MAX_ENTITIES,
    MODE_EMPTY,
    MODE_INITIAL,
    MODE_RELABEL,
    Product,
    ProductMass,
    Snapshot,
    SRC_ATTACH,
    SRC_DETACH,
    SRC_DISPLAY,
    SRC_INITIAL,
    SRC_MASS_CORRECTION,
    TRAILER,
    TRACTOR,
)
from .school import (
    DEFAULT_WINDOW_DAYS,
    GUIDED,
    INDEPENDENT,
    IN_PROGRESS,
    PARTIAL,
    QUALIFIED,
    UNVERIFIED,
    WAITING_GUIDED,
    Practice,
    Ruling,
    School,
    SchoolError,
    day_to_ts,
    epoch_day,
    verify_certificate,
)
from .warehouse import (
    EntityContribution,
    EntityInterval,
    IN,
    LOAD,
    MAX_TICKETS,
    MAX_TRIPS,
    OUT,
    PairEvaluation,
    REASON_DIRECTION_IMPOSSIBLE,
    REASON_MASS_INTERVAL_CONFLICT,
    REASON_NO_PRODUCT,
    REASON_OK,
    REASON_ROLE_MISMATCH,
    REASON_TICKET_INFEASIBLE,
    REASON_TIME_ORDER,
    REASON_TRACTOR_MISMATCH,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_REVIEW,
    STATUS_SUPERSEDED,
    Ticket,
    TicketBusy,
    TicketDerivation,
    TicketRejected,
    UNLOAD,
    Warehouse,
    WarehouseError,
)

__all__ = [
    "Action", "Catalog", "CatalogError", "LimitError", "MassRange", "MAX_ENTITIES",
    "MODE_EMPTY", "MODE_INITIAL", "MODE_RELABEL", "Product", "ProductMass",
    "Snapshot", "SRC_ATTACH", "SRC_DETACH", "SRC_DISPLAY", "SRC_INITIAL",
    "SRC_MASS_CORRECTION", "TRAILER", "TRACTOR",
    "EntityContribution", "EntityInterval", "IN", "OUT", "MAX_TICKETS", "MAX_TRIPS",
    "LOAD", "UNLOAD", "PairEvaluation", "Ticket", "TicketDerivation", "TicketBusy",
    "TicketRejected", "Warehouse", "WarehouseError",
    "REASON_OK", "REASON_TICKET_INFEASIBLE", "REASON_DIRECTION_IMPOSSIBLE",
    "REASON_TIME_ORDER", "REASON_ROLE_MISMATCH", "REASON_TRACTOR_MISMATCH",
    "REASON_NO_PRODUCT",
    "STATUS_DRAFT", "STATUS_CONFIRMED", "STATUS_REVIEW", "STATUS_SUPERSEDED",
    "School", "SchoolError", "Practice", "Ruling", "verify_certificate",
    "epoch_day", "day_to_ts", "DEFAULT_WINDOW_DAYS",
    "GUIDED", "INDEPENDENT", "PARTIAL", "UNVERIFIED",
    "QUALIFIED", "IN_PROGRESS", "WAITING_GUIDED",
]
