import json
from fastapi import APIRouter, Depends, Query
from ..db import get_conn
from ..schemas.reports import ReportRequest
from ..services.hour_policy import pivot_monthly
from ..services.audit import record_audit
from ..services import adjustments as adj
from .deps import require_roles

router = APIRouter(prefix="/reports", tags=["reports"])

APPROVED_SQL = """SELECT w.id AS work_record_id,w.work_date,w.status,w.validated_hours,w.activity_type,
        c.department,c.code AS class_code,c.course_name,u.name AS ta_name
    FROM work_records w JOIN course_classes c ON c.id=w.class_id JOIN users u ON u.id=w.ta_id
    WHERE c.department=%s AND w.work_date >= (%s || '-01')::date AND w.work_date < ((%s || '-01')::date + interval '1 month')"""


def _fetch_rows(conn, department: str, month: str):
    return [dict(r) for r in conn.execute(APPROVED_SQL, (department, month, month)).fetchall()]


def _published_sheet(conn, department: str, month: str):
    return conn.execute(
        "SELECT * FROM approval_sheets WHERE department=%s AND month=%s AND status='published'",
        (department, month),
    ).fetchone()


@router.get("/monthly")
def monthly_report(month: str = "2026-11", department: str = "信息工程学院", user=Depends(require_roles("teacher", "college_admin", "ta"))):
    with get_conn() as conn:
        sheet = _published_sheet(conn, department, month)
        if sheet:
            # 已交月报是不可变快照，返回发布时固化的口径。
            result = dict(sheet["totals_json"])
            result["department"] = department
            result["month"] = month
            result["status"] = "published"
            result["published_at"] = sheet["published_at"].isoformat() if sheet["published_at"] else None
            return result
        rows = _fetch_rows(conn, department, month)
        result = pivot_monthly(rows, month)
        pending = adj.pending_adjustments(conn, department, month)
        adj.apply_adjustments(result, pending)
    result["department"] = department
    result["status"] = "draft"
    return result


@router.get("/monthly/adjustments")
def adjustment_details(month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"), department: str = "信息工程学院", user=Depends(require_roles("teacher", "college_admin"))):
    """学院月报页的补扣工时明细：原记录、发布快照基数与各次差额入账。

    不传 month 时返回该学院全部更正；传 month 时只挑选该月有补扣入账的原记录，
    但仍返回这些记录的全部入账历史。
    """
    with get_conn() as conn:
        return adj.adjustment_detail(conn, department, month)


@router.post("/monthly/publish")
def publish_report(payload: ReportRequest, user=Depends(require_roles("college_admin"))):
    with get_conn() as conn:
        # 与工时更正共用学院级锁：并发时按实际获得锁的先后决定更正进入哪一期。
        adj.lock_department(conn, payload.department)
        existing = _published_sheet(conn, payload.department, payload.month)
        if existing:
            # 交账重试幂等：已交月报留原样，不重复计入待补扣工时。
            result = dict(existing["totals_json"])
            result["status"] = "published"
            result["already_published"] = True
            sheet_id = existing["id"]
        else:
            rows = _fetch_rows(conn, payload.department, payload.month)
            result = pivot_monthly(rows, payload.month)
            pending = adj.pending_adjustments(conn, payload.department, payload.month)
            adj.apply_adjustments(result, pending)

            sheet = conn.execute(
                """INSERT INTO approval_sheets(department,month,status,totals_json,approved_by,published_at)
                   VALUES (%s,%s,'published',%s,%s,now())
                   ON CONFLICT(department,month) DO UPDATE SET status='published', totals_json=EXCLUDED.totals_json,
                     approved_by=EXCLUDED.approved_by, published_at=now()
                   RETURNING id""",
                (payload.department, payload.month, json.dumps(result, ensure_ascii=False), user["id"]),
            ).fetchone()
            sheet_id = sheet["id"]

            # 固化已审批工时基数。
            for item in rows:
                if item["status"] != "approved":
                    continue
                conn.execute(
                    """INSERT INTO approval_sheet_items(sheet_id,work_record_id,kind,class_code,course_name,department,ta_name,activity_type,hours)
                       VALUES (%s,%s,'base',%s,%s,%s,%s,%s,%s)""",
                    (sheet_id, item["work_record_id"], item["class_code"], item["course_name"],
                     item["department"], item["ta_name"], item["activity_type"], item["validated_hours"]),
                )
            # 固化本期入账的补扣差额，并标记为已入账。
            for adjustment in pending:
                conn.execute(
                    """INSERT INTO approval_sheet_items(sheet_id,work_record_id,kind,adjustment_id,class_code,course_name,department,ta_name,activity_type,hours)
                       VALUES (%s,%s,'adjustment',%s,%s,%s,%s,%s,%s,%s)""",
                    (sheet_id, adjustment["work_record_id"], adjustment["id"], adjustment["class_code"],
                     adjustment["course_name"], adjustment["department"], adjustment["ta_name"],
                     adjustment["activity_type"], adjustment["delta_hours"]),
                )
                conn.execute(
                    """UPDATE work_record_adjustments
                       SET status='applied', applied_month=%s, applied_sheet_id=%s, applied_at=now()
                       WHERE id=%s AND status='pending'""",
                    (payload.month, sheet_id, adjustment["id"]),
                )
            result["status"] = "published"

    record_audit(user["id"], "monthly_report_published", "approval_sheet", sheet_id, {
        "department": payload.department,
        "month": payload.month,
        "total_approved_hours": result["total_approved_hours"],
        "adjustment_total_hours": result.get("adjustment_total_hours", 0.0),
        "retry": existing is not None,
    })
    return result
