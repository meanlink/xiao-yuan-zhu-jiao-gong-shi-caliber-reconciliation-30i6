"""已交月报之后的工时更正与补扣入账。

核心口径：
- 已发布（已交）的月报是不可变快照，发布时把每条 approved 记录固化到
  approval_sheet_items，之后不再改动。
- 教师更正原月份已交的已批工时，只生成一条差额 adjustment，锁定进入该学院
  在原记录月份之后的“首个未交月份”。
- 同一笔反复更正时，差额按财务已经入账的工时（快照基数 + 已入账调整）计算，
  新的更正会把尚未入账的旧 adjustment 作废，避免重复入账。
- 更正与交账并发时，以学院为粒度加事务级 advisory lock，按实际获得锁的先后
  决定更正进入哪一期。
"""
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0.00")


def round_quarter(value: Decimal) -> Decimal:
    return (Decimal(value) * 4).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 4


def next_month(month: str, step: int = 1) -> str:
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + step
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def lock_department(conn, department: str) -> None:
    """按学院串行化更正与交账，保证“实际生效先后”可线性化。"""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"department:{department}",))


def is_month_published(conn, department: str, month: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM approval_sheets WHERE department=%s AND month=%s AND status='published'",
        (department, month),
    ).fetchone()
    return row is not None


def first_unsubmitted_month(conn, department: str, origin_month: str) -> str:
    """原记录月份之后首个尚未交账的月份。"""
    candidate = next_month(origin_month)
    for _ in range(240):
        if not is_month_published(conn, department, candidate):
            return candidate
        candidate = next_month(candidate)
    raise RuntimeError("无法定位后续未交月份")


def booked_hours_for_record(conn, record_id: int, origin_month: str, fallback_hours) -> Decimal:
    """财务已经入账的工时：已发布快照基数 + 已入账调整差额。

    - 记录在快照中：取快照基数，再叠加后续已入账差额。
    - 原月份已交账但快照里没有该记录（月报发布后才审批）：从未入账，基数为 0。
    - 原月份快照为空（历史数据未固化明细）：回退到已审批工时。
    """
    base = conn.execute(
        """SELECT count(*) AS cnt, coalesce(sum(hours),0) AS hours
           FROM approval_sheet_items WHERE work_record_id=%s AND kind='base'""",
        (record_id,),
    ).fetchone()
    if not base["cnt"]:
        legacy = conn.execute(
            """SELECT count(*) AS cnt FROM approval_sheet_items i
               JOIN approval_sheets s ON s.id=i.sheet_id
               WHERE i.department=(SELECT c.department FROM work_records w
                     JOIN course_classes c ON c.id=w.class_id WHERE w.id=%s)
                 AND s.month=%s""",
            (record_id, origin_month),
        ).fetchone()
        if legacy["cnt"]:
            return ZERO
        return Decimal(str(fallback_hours or 0))
    applied = conn.execute(
        """SELECT coalesce(sum(delta_hours),0) AS hours FROM work_record_adjustments
           WHERE work_record_id=%s AND status='applied'""",
        (record_id,),
    ).fetchone()
    return Decimal(str(base["hours"])) + Decimal(str(applied["hours"]))


def pending_adjustments(conn, department: str, month: str):
    """该学院待补扣、且目标月份不晚于本次交账月份的全部差额。

    目标月份在更正生效时即锁定为“原记录之后首个未交月份”。若管理员跳过目标
    月份先交更晚的月份，也用 ``target_month <= month`` 一并吸收，避免差额搁浅。
    """
    return conn.execute(
        """SELECT a.id, a.work_record_id, a.department, a.delta_hours, a.target_month,
                  c.code AS class_code, c.course_name, c.department AS dept,
                  w.activity_type, u.name AS ta_name
           FROM work_record_adjustments a
           JOIN work_records w ON w.id=a.work_record_id
           JOIN course_classes c ON c.id=w.class_id
           JOIN users u ON u.id=w.ta_id
           WHERE a.department=%s AND a.target_month<=%s AND a.status='pending'
           ORDER BY a.id""",
        (department, month),
    ).fetchall()


def apply_adjustments(result: dict, adjustment_rows: list[dict]) -> dict:
    """把补扣差额并入透视结果；差额可为负。"""
    index = {
        (r["department"], r["class_code"], r["course_name"], r["activity_type"], r["ta_name"]): r
        for r in result["rows"]
    }
    net = ZERO
    count = 0
    for row in adjustment_rows:
        delta = Decimal(str(row["delta_hours"]))
        key = (row["dept"], row["class_code"], row["course_name"], row["activity_type"], row["ta_name"])
        item = index.get(key)
        if item is None:
            item = {
                "department": key[0],
                "class_code": key[1],
                "course_name": key[2],
                "activity_type": key[3],
                "ta_name": key[4],
                "approved_hours": 0.0,
                "approved_record_count": 0,
                "adjustment_hours": 0.0,
            }
            index[key] = item
            result["rows"].append(item)
        item["adjustment_hours"] = float(Decimal(str(item.get("adjustment_hours", 0.0))) + delta)
        net += delta
        count += 1

    base_total = Decimal(str(result.get("total_approved_hours", 0.0)))
    result["rows"].sort(key=lambda x: (x["department"], x["class_code"], x["activity_type"], x["ta_name"]))
    result["base_approved_hours"] = float(base_total)
    result["adjustment_total_hours"] = float(net)
    result["adjustment_count"] = count
    result["total_approved_hours"] = float(base_total + net)
    result["row_count"] = len(result["rows"])
    return result


def correct_record(conn, record: dict, new_hours: Decimal, note: str, actor_id: int) -> dict:
    """对一条已审批记录提交更正。调用方需已持有该学院 advisory lock。

    若原月份尚未交账，直接改写基数；否则生成/更新待入账差额。
    """
    department = record["department"]
    origin_month = record["work_date"].strftime("%Y-%m")
    new_hours = round_quarter(new_hours)
    old_hours = Decimal(str(record["validated_hours"]))
    published = is_month_published(conn, department, origin_month)

    if not published:
        conn.execute(
            """UPDATE work_records SET validated_hours=%s, review_note=%s, updated_at=now() WHERE id=%s""",
            (new_hours, note or record.get("review_note"), record["id"]),
        )
        conn.execute(
            """UPDATE work_record_adjustments SET status='superseded'
               WHERE work_record_id=%s AND status='pending'""",
            (record["id"],),
        )
        return {
            "record_id": record["id"],
            "mode": "in_place",
            "department": department,
            "origin_month": origin_month,
            "booked_hours_before": float(old_hours),
            "effective_hours_after": float(new_hours),
            "delta_hours": float(new_hours - old_hours),
            "target_month": origin_month,
            "adjustment_id": None,
        }

    booked = booked_hours_for_record(conn, record["id"], origin_month, old_hours)
    # 同一笔反复更正：作废尚未入账的旧差额，按已入账口径重新计算。
    conn.execute(
        """UPDATE work_record_adjustments SET status='superseded'
           WHERE work_record_id=%s AND status='pending'""",
        (record["id"],),
    )
    # 记录本身更新为更正后的有效工时；已发布月报的快照不动，差额进后续未交月份。
    conn.execute(
        """UPDATE work_records SET validated_hours=%s, review_note=%s, updated_at=now() WHERE id=%s""",
        (new_hours, note or record.get("review_note"), record["id"]),
    )
    delta = new_hours - booked
    if delta == ZERO:
        return {
            "record_id": record["id"],
            "mode": "no_change",
            "department": department,
            "origin_month": origin_month,
            "booked_hours_before": float(booked),
            "effective_hours_after": float(new_hours),
            "delta_hours": 0.0,
            "target_month": None,
            "adjustment_id": None,
        }

    target_month = first_unsubmitted_month(conn, department, origin_month)
    row = conn.execute(
        """INSERT INTO work_record_adjustments(
             work_record_id, department, origin_month, target_month, delta_hours,
             booked_hours_before, effective_hours_after, status, note, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) RETURNING id""",
        (record["id"], department, origin_month, target_month, delta,
         booked, new_hours, note, actor_id),
    ).fetchone()
    return {
        "record_id": record["id"],
        "mode": "adjustment",
        "department": department,
        "origin_month": origin_month,
        "booked_hours_before": float(booked),
        "effective_hours_after": float(new_hours),
        "delta_hours": float(delta),
        "target_month": target_month,
        "adjustment_id": row["id"],
    }


def adjustment_detail(conn, department: str, month: str | None) -> dict:
    """补扣工时明细：原记录 + 各次入账（发布快照与每次差额）。

    给定月份时，只挑选该月有补扣入账（待入账或已入账）的原记录，
    但仍返回该记录的全部入账历史，便于追溯。
    """
    params: list = [department]
    month_filter = ""
    if month:
        month_filter = """AND a.work_record_id IN (
            SELECT work_record_id FROM work_record_adjustments
            WHERE department=%s AND (target_month=%s OR applied_month=%s))"""
        params.extend([department, month, month])

    adjustments = conn.execute(
        f"""SELECT a.*, w.work_date, w.reported_hours, w.validated_hours AS current_validated_hours,
                   w.activity_type, c.code AS class_code, c.course_name, u.name AS ta_name
            FROM work_record_adjustments a
            JOIN work_records w ON w.id=a.work_record_id
            JOIN course_classes c ON c.id=w.class_id
            JOIN users u ON u.id=w.ta_id
            WHERE a.department=%s {month_filter}
            ORDER BY a.work_record_id, a.created_at, a.id""",
        params,
    ).fetchall()
    if not adjustments:
        return {"department": department, "month": month, "items": [],
                "pending_total_hours": 0.0, "applied_total_hours": 0.0}

    record_ids = sorted({row["work_record_id"] for row in adjustments})
    snapshots = conn.execute(
        """SELECT i.work_record_id, i.hours, i.kind, s.month AS sheet_month, s.published_at
           FROM approval_sheet_items i JOIN approval_sheets s ON s.id=i.sheet_id
           WHERE i.work_record_id = ANY(%s) AND i.kind='base'
           ORDER BY s.published_at, i.id""",
        (record_ids,),
    ).fetchall()

    by_record: dict[int, dict] = {}
    for row in adjustments:
        item = by_record.setdefault(row["work_record_id"], {
            "work_record_id": row["work_record_id"],
            "class_code": row["class_code"],
            "course_name": row["course_name"],
            "activity_type": row["activity_type"],
            "ta_name": row["ta_name"],
            "work_date": str(row["work_date"]),
            "reported_hours": float(row["reported_hours"]),
            "current_validated_hours": float(row["current_validated_hours"]),
            "bookings": [],
        })

    for snap in snapshots:
        item = by_record.get(snap["work_record_id"])
        if item is None:
            continue
        item["bookings"].append({
            "kind": "base",
            "sheet_month": snap["sheet_month"],
            "hours": float(snap["hours"]),
            "published_at": snap["published_at"].isoformat() if snap["published_at"] else None,
        })

    pending_total = ZERO
    applied_total = ZERO
    for row in adjustments:
        item = by_record[row["work_record_id"]]
        delta = Decimal(str(row["delta_hours"]))
        if row["status"] == "pending":
            pending_total += delta
        elif row["status"] == "applied":
            applied_total += delta
        item["bookings"].append({
            "kind": "adjustment",
            "adjustment_id": row["id"],
            "status": row["status"],
            "origin_month": row["origin_month"],
            "target_month": row["target_month"],
            "applied_month": row["applied_month"],
            "delta_hours": float(delta),
            "booked_hours_before": float(row["booked_hours_before"]),
            "effective_hours_after": float(row["effective_hours_after"]),
            "note": row["note"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "applied_at": row["applied_at"].isoformat() if row["applied_at"] else None,
        })

    pending_by_record = {
        row["work_record_id"]: row for row in adjustments if row["status"] == "pending"
    }
    for record_id, item in by_record.items():
        bases = [Decimal(str(b["hours"])) for b in item["bookings"] if b["kind"] == "base"]
        applied_delta = sum(
            Decimal(str(b["delta_hours"])) for b in item["bookings"]
            if b["kind"] == "adjustment" and b["status"] == "applied"
        )
        # 财务已入账 = 已发布快照基数 + 已入账差额；无快照时以当前有效工时为参照。
        booked = (sum(bases) if bases else Decimal(str(item["current_validated_hours"]))) + applied_delta
        item["booked_hours"] = float(booked)
        pending = pending_by_record.get(record_id)
        item["pending"] = None
        if pending:
            item["pending"] = {
                "target_month": pending["target_month"],
                "delta_hours": float(pending["delta_hours"]),
                "effective_hours_after": float(pending["effective_hours_after"]),
            }

    items = sorted(by_record.values(), key=lambda x: (x["work_date"], x["class_code"]))
    return {
        "department": department,
        "month": month,
        "items": items,
        "pending_total_hours": float(pending_total),
        "applied_total_hours": float(applied_total),
    }
