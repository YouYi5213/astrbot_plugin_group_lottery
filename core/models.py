"""群抽奖系统 —— 数据模型与常量。"""

from __future__ import annotations

# ---------------------------------------------------------------- 抽奖状态
STATUS_OPEN = "open"
"""进行中：可继续报名、可开奖。"""

STATUS_DRAWN = "drawn"
"""已开奖。"""

STATUS_CANCELLED = "cancelled"
"""已取消。"""

STATUS_LABELS = {
    STATUS_OPEN: "进行中",
    STATUS_DRAWN: "已开奖",
    STATUS_CANCELLED: "已取消",
}

# ---------------------------------------------------------------- 奖品类型
KIND_CONTACT = "contact"
"""普通奖品：只公布名单，中奖者自行联系群主领取。"""

KIND_KEY = "key"
"""密钥奖品：机器人私聊把专属密钥发给中奖者。"""

# ---------------------------------------------------------------- 开奖触发来源
TRIGGER_MANUAL = "manual"
"""管理员手动开奖。"""

TRIGGER_SCHEDULED = "scheduled"
"""定时到点自动开奖。"""

TRIGGER_FULL = "full"
"""报名人数达到设定值提前开奖。"""

TRIGGER_LABELS = {
    TRIGGER_MANUAL: "手动开奖",
    TRIGGER_SCHEDULED: "定时自动开奖",
    TRIGGER_FULL: "满员自动开奖",
}

# 单次抽奖允许的最大中奖名额，防止误操作把整群抽空
MAX_WINNERS = 100

# 单个抽奖允许设置的密钥数量上限
MAX_KEYS = 5000
