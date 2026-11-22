import { get, post } from '../api/client.js';
import { layout, bindLayout } from '../components/common/layout.js';
import { statusPill } from '../components/common/status.js';
import { toast } from '../hooks/useToast.js';
import { escapeHtml, tableShell } from '../components/common/table.js';

const MONTH = '2026-11';
const DEPARTMENT = '信息工程学院';

function bookingText(b) {
  if (b.kind === 'base') {
    return `原月报 ${escapeHtml(b.sheet_month)} 入账 ${Number(b.hours).toFixed(2)} h`;
  }
  const delta = Number(b.delta_hours);
  const sign = delta >= 0 ? '补扣' : '退回';
  const status = b.status === 'applied'
    ? `已入账 ${escapeHtml(b.applied_month || b.target_month)}`
    : b.status === 'pending' ? `待入账 ${escapeHtml(b.target_month)}` : '已作废';
  return `第 ${b.adjustment_id} 次更正 ${sign} ${delta.toFixed(2)} h · ${status}`;
}

export function adjustmentDetailHtml(detail, recordMap) {
  const items = detail.items || [];
  if (!items.length) {
    return `<div class="empty-state"><div class="empty-mark">∅</div><h3>暂无补扣工时</h3><p>已交月报之后的工时更正会出现在这里。</p></div>`;
  }
  const blocks = items.map((item) => {
    const record = recordMap[item.work_record_id];
    const bookings = item.bookings.map((b) => `<li>${bookingText(b)}${b.note ? `<small class="cell-sub">${escapeHtml(b.note)}</small>` : ''}</li>`).join('');
    const pending = item.pending ? `待入账 ${Number(item.pending.delta_hours).toFixed(2)} h · ${escapeHtml(item.pending.target_month)}` : (item.bookings.some((b) => b.kind === 'adjustment' && b.status === 'applied') ? '已全部入账' : '');
    return `<div class="adjust-block"><div class="adjust-head"><b>${escapeHtml(item.class_code)}</b><span>${escapeHtml(item.work_date)} · ${escapeHtml(item.activity_type)} · ${escapeHtml(item.ta_name)}</span></div><div class="adjust-meta">自报 ${Number(item.reported_hours).toFixed(2)} h · 当前有效 ${Number(item.current_validated_hours).toFixed(2)} h · 财务已入账 <b>${Number(item.booked_hours).toFixed(2)} h</b>${pending ? `<span class="pill pill-manual_review">${pending}</span>` : ''}</div><ul class="adjust-bookings">${bookings}</ul></div>`;
  });
  return `<div class="adjust-summary">待入账合计 <b>${Number(detail.pending_total_hours).toFixed(2)} h</b> · 已入账差额 ${Number(detail.applied_total_hours).toFixed(2)} h</div><div class="adjust-list">${blocks.join('')}</div>`;
}

export async function renderReview() {
  const [records, report, detail] = await Promise.all([
    get('/work-records'),
    get(`/reports/monthly?month=${MONTH}&department=${encodeURIComponent(DEPARTMENT)}`),
    get(`/reports/monthly/adjustments?department=${encodeURIComponent(DEPARTMENT)}`),
  ]);
  const recordMap = Object.fromEntries(records.items.map((r) => [r.id, r]));
  const rows = records.items.map((r) => `<tr><td><b>${escapeHtml(r.class_code)}</b><small class="cell-sub">${escapeHtml(r.ta_name)} · ${escapeHtml(r.work_date)}</small></td><td>${escapeHtml(r.activity_type)}<small class="cell-sub">有效 ${r.validated_hours} h</small></td><td>${statusPill(r.status)}${r.conflict_type ? `<small class="cell-sub conflict">${escapeHtml(r.conflict_type)}</small>` : ''}</td><td>${r.status === 'manual_review' ? `<button class="outline-btn resolve" data-id="${r.id}">学院复核</button>` : r.status === 'submitted' ? `<button class="primary-mini approve" data-id="${r.id}">教师审批</button>` : r.status === 'approved' ? `<button class="outline-btn correct" data-id="${r.id}" data-hours="${r.validated_hours}">更正工时</button>` : '<span class="muted">已处理</span>'}</td></tr>`);
  const pendingAdjust = Number(detail.pending_total_hours || 0);
  const reportRows = report.rows.slice(0, 8).map((r) => `<tr><td>${escapeHtml(r.class_code)}<small class="cell-sub">${escapeHtml(r.course_name)}</small></td><td>${escapeHtml(r.activity_type)}</td><td>${escapeHtml(r.ta_name)}</td><td><b>${Number(r.approved_hours).toFixed(2)} h</b>${Number(r.adjustment_hours) ? `<small class="cell-sub conflict">补扣 ${Number(r.adjustment_hours).toFixed(2)} h</small>` : ''}</td></tr>`);
  document.getElementById('app').innerHTML = layout('review', `<div class="section-bar"><div><p class="kicker">REVIEW / MONTHLY PIVOT</p><h2>教师审批与学院月报</h2><p>审批队列与月报透视共用同一条工时状态链路。已交月报留原样，课后更正只补扣进后续未交月份。</p></div><div class="report-total"><span>${MONTH} 已入账</span><b>${Number(report.total_approved_hours).toFixed(2)} h</b><small>${report.status === 'published' ? '已交快照' : '未交草稿'}</small></div></div><div class="review-grid"><section class="panel table-panel"><div class="panel-head"><div><span class="eyebrow">REVIEW QUEUE</span><h3>待处理记录</h3></div><span class="queue-count">${records.items.filter((r) => ['submitted','manual_review'].includes(r.status)).length} 条</span></div>${tableShell(['课程班 / 助教','活动 / 有效工时','状态','动作'], rows, '审批队列为空')}</section><section class="panel table-panel report-panel"><div class="panel-head"><div><span class="eyebrow">COLLEGE PIVOT / ${MONTH}</span><h3>信息工程学院月报</h3></div><div class="head-actions"><button class="outline-btn" id="open-adjustments">补扣工时${pendingAdjust ? ` · ${pendingAdjust.toFixed(2)} h` : ''}</button><button class="primary-mini" id="publish-report">发布快照</button></div></div>${tableShell(['课程班','活动类型','助教','已审批'], reportRows, '暂无已审批工时')}<div class="report-foot">透视维度：学院 × 课程班 × 活动类型 × 助教</div></section></div><section class="panel adjust-panel" id="adjust-panel" hidden><div class="panel-head"><div><span class="eyebrow">SUPPLEMENTARY DEDUCTION</span><h3>补扣工时明细</h3></div><button class="ghost-btn" id="close-adjustments">收起</button></div><div id="adjust-body" class="adjust-body"></div></section><section class="audit-strip"><span class="eyebrow">CONTROL NOTE</span><b>未审核工时不会出现在月报合计。</b><span>已交月报保持原样；更正差额按财务已入账工时重算，并在后续首个未交月份补扣。</span></section>`);
  bindLayout();

  const adjustPanel = document.getElementById('adjust-panel');
  const adjustBody = document.getElementById('adjust-body');
  document.getElementById('open-adjustments').addEventListener('click', () => {
    adjustPanel.hidden = false;
    adjustBody.innerHTML = adjustmentDetailHtml(detail, recordMap);
    adjustPanel.scrollIntoView({ behavior: 'smooth' });
  });
  document.getElementById('close-adjustments').addEventListener('click', () => { adjustPanel.hidden = true; });

  document.querySelectorAll('.approve').forEach((b) => b.addEventListener('click', async () => {
    try { await post(`/work-records/${b.dataset.id}/review`, { note: '任课教师核对签到与工作说明' }); toast('已审批，记录进入月报', 'success'); renderReview(); } catch (e) { toast(e.message, 'error'); }
  }));
  document.querySelectorAll('.resolve').forEach((b) => b.addEventListener('click', async () => {
    try { await post(`/work-records/${b.dataset.id}/resolve-conflict`, { note: '学院管理员核对排班后转教师审批' }); toast('冲突已解除，等待教师审批', 'success'); renderReview(); } catch (e) { toast(e.message, 'error'); }
  }));
  document.querySelectorAll('.correct').forEach((b) => b.addEventListener('click', async () => {
    const value = window.prompt(`更正有效工时（当前 ${b.dataset.hours} h，按 0.25 取整）`, b.dataset.hours);
    if (value === null) return;
    const hours = Number(value);
    if (!Number.isFinite(hours) || hours < 0 || hours > 12) { toast('请输入 0 到 12 之间的小时数', 'error'); return; }
    try {
      const result = await post(`/work-records/${b.dataset.id}/correction`, { validated_hours: hours, note: '教师更正已批工时' });
      const msg = result.mode === 'adjustment'
        ? `原月报留原样，${Number(result.delta_hours).toFixed(2)} h 将补扣进 ${result.target_month}`
        : result.mode === 'no_change' ? '更正后与已入账工时一致，无需补扣'
        : '原月份尚未交账，已直接更新有效工时';
      toast(msg, 'success');
      renderReview();
    } catch (e) { toast(e.message, 'error'); }
  }));
  document.getElementById('publish-report').addEventListener('click', async () => {
    try {
      const result = await post('/reports/monthly/publish', { month: MONTH, department: DEPARTMENT });
      toast(result.already_published ? `月报此前已交，保持原样（${Number(result.total_approved_hours).toFixed(2)} 小时）` : `月报已发布，共 ${Number(result.total_approved_hours).toFixed(2)} 小时`, 'success');
      renderReview();
    } catch (e) { toast(e.message, 'error'); }
  });
}
