-- 已交月报的不可变明细：发布时把每条 approved 记录和当期补扣差额固化下来。
CREATE TABLE IF NOT EXISTS approval_sheet_items (
  id SERIAL PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES approval_sheets(id) ON DELETE CASCADE,
  work_record_id INTEGER NOT NULL REFERENCES work_records(id),
  kind TEXT NOT NULL CHECK (kind IN ('base', 'adjustment')),
  adjustment_id INTEGER,
  class_code TEXT,
  course_name TEXT,
  department TEXT NOT NULL,
  ta_name TEXT,
  activity_type TEXT NOT NULL,
  hours NUMERIC(8,2) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approval_sheet_items_sheet_idx ON approval_sheet_items(sheet_id);
CREATE INDEX IF NOT EXISTS approval_sheet_items_record_idx ON approval_sheet_items(work_record_id);

-- 已交月报之后的工时更正差额。创建时即锁定要入账的目标月份：
-- 该学院原记录月份之后的首个未交月份。pending 等待入账，
-- applied 已随月报入账，superseded 表示同一笔记录再次更正后作废。
CREATE TABLE IF NOT EXISTS work_record_adjustments (
  id SERIAL PRIMARY KEY,
  work_record_id INTEGER NOT NULL REFERENCES work_records(id),
  department TEXT NOT NULL,
  origin_month TEXT NOT NULL,
  target_month TEXT NOT NULL,
  delta_hours NUMERIC(8,2) NOT NULL,
  booked_hours_before NUMERIC(8,2) NOT NULL,
  effective_hours_after NUMERIC(8,2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'superseded')),
  applied_month TEXT,
  applied_sheet_id INTEGER REFERENCES approval_sheets(id),
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS work_record_adjustments_one_pending
  ON work_record_adjustments(work_record_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS work_record_adjustments_target_idx
  ON work_record_adjustments(department, target_month, status);
CREATE INDEX IF NOT EXISTS work_record_adjustments_record_idx ON work_record_adjustments(work_record_id);
