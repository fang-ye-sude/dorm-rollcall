"""在线表结构单一真源：日块头、层行偏移、星期→块序。

原来这几组常量散在 rollcall_suite / pipeline_ops / webapp 各写一份，
改表就得四处同步，漏一处就是错行。现在只留这里。
"""

# 每个日块的表头行（周日~周四，块长 24 行）
HEADS = [1, 25, 49, 73, 97]

# 各层数据行相对块头的偏移：A2 A3 A4 A5 B1 B2 B3
ROW_OFF = {("A", 2): 4, ("A", 3): 6, ("A", 4): 8, ("A", 5): 10,
           ("B", 1): 14, ("B", 2): 16, ("B", 3): 18}

# 偏移集合（校验用）
OFFS = (4, 6, 8, 10, 14, 16, 18)

# isoweekday → 块序（周日=0 … 周四=4；周五/周六不在点名周）
DAY_SEQ = {7: 0, 1: 1, 2: 2, 3: 3, 4: 4}

# 楼层中文名，报错与结构校验共用
FLOOR_CN = "一二三四五六"


def floor_label(building, floor):
    """B1 → 1座一层？不，返回「B座一层」这种。"""
    try:
        return "%s座%s层" % (building, FLOOR_CN[int(floor) - 1])
    except Exception:
        return "%s座%s层" % (building, floor)


def row_of(seq, building, floor):
    """seq=块序(0..4)，返回该层当天数据行；未知楼层返回 None（绝不默认 B1）。"""
    off = ROW_OFF.get((building, int(floor)))
    if off is None or not (0 <= int(seq) < len(HEADS)):
        return None
    return HEADS[int(seq)] + off
