/*
 * 到期处置台前端。
 * 分层：api 为唯一请求入口（加头、统一错误）；state 保存页面状态；
 * render/dom 负责展示；actions 负责交互编排。规则文本集中在 BLOCKER_TEXT。
 */
'use strict';

// ---- 规则展示文本（与后端 rules.py 的阻塞项代码对应） -----------------
const BLOCKER_TEXT = {
  retention_not_due: '保留期限未到',
  retention_changed: '期限已变化，需重新确认',
  frozen: '审计保全冻结中',
  already_disposed: '档案已处置',
};
const GROUP_LABEL = { pending: '待确认', ready: '可处置', frozen: '已冻结', disposed: '已处置' };

// ---- 请求入口 -----------------------------------------------------------
const api = (() => {
  const uid = () => document.querySelector('#user').value;
  async function request(method, path, body) {
    const opts = { method, headers: { 'X-User-Id': uid() } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data?.error?.message || `请求失败 (${res.status})`);
      err.code = data?.error?.code;
      err.details = data?.error?.details;
      err.status = res.status;
      throw err;
    }
    return data;
  }
  return {
    console: () => request('GET', '/api/disposition/console'),
    candidates: () => request('GET', '/api/disposition/candidates'),
    createBatch: (name, archive_ids) => request('POST', '/api/disposition/batches', { name, archive_ids }),
    batch: (id) => request('GET', `/api/disposition/batches/${id}`),
    addArchives: (id, archive_ids) => request('POST', `/api/disposition/batches/${id}/archives`, { archive_ids }),
    check: (id) => request('POST', `/api/disposition/batches/${id}/check`),
    confirm: (id, item_ids) => request('POST', `/api/disposition/batches/${id}/confirm`, { item_ids }),
    execute: (id) => request('POST', `/api/disposition/batches/${id}/execute`),
    freeze: (batchId, itemId, reason) =>
      request('POST', `/api/disposition/batches/${batchId}/items/${itemId}/freeze`, { reason }),
    unfreeze: (batchId, itemId) =>
      request('POST', `/api/disposition/batches/${batchId}/items/${itemId}/unfreeze`),
    remove: (batchId, itemId) =>
      request('DELETE', `/api/disposition/batches/${batchId}/items/${itemId}`),
    status: (archiveId) => request('GET', `/api/archives/${archiveId}/status`),
  };
})();

// ---- 页面状态 -----------------------------------------------------------
const state = {
  consoleData: null,
  candidates: [],
  selectedCandidates: new Set(),
  openBatchId: null,
  batchDetail: null,
};

// ---- 小工具 -------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTime = (t) => t ? t.replace('T', ' ').replace('+00:00', ' UTC') : '—';
const dayText = (it) => {
  if (it.state === 'disposed') return `处置于 ${fmtTime(it.disposed_at)}`;
  if (it.days_remaining > 0) return `剩余 ${it.days_remaining} 天`;
  if (it.days_remaining === 0) return '今日到期';
  return `已过期 ${-it.days_remaining} 天`;
};

function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = `toast${isError ? ' error' : ''}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 3200);
}
function showError(err) {
  let msg = err.message || String(err);
  if (err.code === 'execution_blocked' && Array.isArray(err.details)) {
    const names = err.details.map((d) =>
      `#${d.archive_id}（${d.state === 'frozen' ? '已冻结' : '待确认'}：${d.blockers.map((b) => BLOCKER_TEXT[b] || b).join('、')}）`);
    msg = `执行被拒，以下条目阻塞：${names.join('；')}`;
  }
  toast(msg, true);
}

// ---- 模态框 -------------------------------------------------------------
const modal = (() => {
  let onOk = null;
  function open({ title, body, okText = '确定', onConfirm }) {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = body;
    $('#modal-ok').textContent = okText;
    onOk = onConfirm;
    $('#modal').classList.remove('hidden');
    const input = $('#modal').querySelector('input,textarea');
    if (input) input.focus();
  }
  function close() { $('#modal').classList.add('hidden'); onOk = null; }
  $('#modal-cancel').onclick = close;
  $('#modal-ok').onclick = () => { if (onOk) onOk(); };
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') close(); });
  return { open, close };
})();

// ---- 渲染：处置台总览 ---------------------------------------------------
function blockerChips(blockers) {
  if (!blockers || !blockers.length) return '';
  return `<div class="blockers">${blockers
    .map((b) => `<span class="blocker-chip">${esc(BLOCKER_TEXT[b] || b)}</span>`).join('')}</div>`;
}

function itemCard(it, inDrawer = false) {
  const batchLine = inDrawer ? '' :
    `<div class="sub">批次 #${it.batch_id} ${esc(it.batch_name || '')}</div>`;
  const freeze = it.frozen ? `<div class="freeze-reason">❄ 冻结理由：${esc(it.freeze_reason || '—')}</div>` : '';
  const checkedLine = it.state !== 'disposed'
    ? `<div class="sub">最近核对：${fmtTime(it.checked_at)}</div>` : '';
  return `
    <div class="card ${it.state}">
      <div class="t">#${it.archive_id} ${esc(it.archive_name)}</div>
      ${batchLine}
      <div class="sub">${dayText(it)} · 期限 ${esc(it.retention_until)}${it.retention_changed ? ' · <b style="color:var(--pending)">快照不一致</b>' : ''}</div>
      ${freeze}
      ${blockerChips(it.blockers)}
      ${checkedLine}
      <div class="card-actions" data-archive="${it.archive_id}" data-item="${it.item_id}" data-batch="${it.batch_id || state.openBatchId}"></div>
    </div>`;
}

function role() { return $('#user').value; }
function isManager() { return role() === 'archivist' || role() === 'owner'; }
function isAuditor() { return role() === 'auditor'; }

function cardActions(it) {
  const acts = [];
  if (it.state === 'disposed') {
    acts.push(`<button class="btn" data-act="status">查看版本/副本/审计</button>`);
    return acts.join('');
  }
  if (it.state !== 'frozen' && isAuditor()) {
    acts.push(`<button class="btn" data-act="freeze">保全冻结</button>`);
  }
  if (it.state === 'frozen' && isAuditor()) {
    acts.push(`<button class="btn" data-act="unfreeze">解除冻结</button>`);
  }
  if (isManager()) {
    if (it.state === 'pending') acts.push(`<button class="btn" data-act="confirm">确认可处置</button>`);
    acts.push(`<button class="btn" data-act="status">查看档案</button>`);
    if (state.openBatchId && it.batch_id === state.openBatchId) {
      acts.push(`<button class="btn btn-ghost" data-act="remove">移出批次</button>`);
    }
  }
  return acts.join('');
}

function bindCardActions(root) {
  root.querySelectorAll('.card-actions').forEach((box) => {
    const itemId = Number(box.dataset.item);
    const batchId = Number(box.dataset.batch);
    const archiveId = Number(box.dataset.archive);
    box.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => cardAction(btn.dataset.act, { itemId, batchId, archiveId });
    });
  });
}

async function cardAction(act, { itemId, batchId, archiveId }) {
  try {
    if (act === 'freeze') {
      modal.open({
        title: '保全冻结',
        body: '<p>请说明争议/保全理由（将写入审计记录）：</p><textarea id="freeze-reason" rows="3"></textarea>',
        okText: '冻结',
        onConfirm: async () => {
          const reason = $('#freeze-reason').value.trim();
          modal.close();
          await api.freeze(batchId, itemId, reason);
          toast('已冻结，执行前核对将拦截该批次');
          await refreshAll(batchId);
        },
      });
      return;
    }
    if (act === 'unfreeze') {
      await api.unfreeze(batchId, itemId);
      toast('已解除冻结');
    } else if (act === 'confirm') {
      await api.confirm(batchId, [itemId]);
      toast('已确认，进入可处置');
    } else if (act === 'remove') {
      await api.remove(batchId, itemId);
      toast('已移出批次');
    } else if (act === 'status') {
      const s = await api.status(archiveId);
      showStatusModal(s);
      return;
    }
    await refreshAll(state.openBatchId);
  } catch (err) { showError(err); }
}

function showStatusModal(s) {
  const rows = s.versions.map((v) =>
    `<li>v${v.version} · ${v.state} · 文件 ${v.file_count} · 副本 ${v.copy_count} · ${fmtTime(v.created_at)}</li>`).join('') || '<li>无版本</li>';
  const audits = s.audit.slice(-12).map((a) =>
    `<li>${fmtTime(a.created_at)} <b>${esc(a.actor_id)}</b> ${esc(a.action)}</li>`).join('');
  modal.open({
    title: `#${s.archive.id} ${esc(s.archive.name)}（处置后仍可查阅）`,
    body: `
      <div class="muted">期限 ${esc(s.archive.retention_until)} · 剩余 ${s.days_remaining} 天
      ${s.archive.disposed_at ? ` · <b style="color:var(--disposed)">已于 ${fmtTime(s.archive.disposed_at)} 处置（只读）</b>` : ''}
      ${s.frozen ? ' · <b style="color:var(--frozen)">冻结中</b>' : ''}</div>
      <p><b>版本与副本</b></p><ul>${rows}</ul>
      <p><b>最近审计</b></p><ul>${audits || '<li>无</li>'}</ul>`,
    okText: '关闭',
    onConfirm: () => modal.close(),
  });
}

function renderConsole(data) {
  state.consoleData = data;
  for (const g of ['pending', 'ready', 'frozen', 'disposed']) {
    $(`[data-count="${g}"]`).textContent = data.counts[g];
    const body = $(`[data-body="${g}"]`);
    const items = data.groups[g] || [];
    body.innerHTML = items.length
      ? items.map((it) => itemCard(it)).join('')
      : '<div class="empty">—</div>';
    bindCardActions(body);
  }
  $('#blocker-count').textContent = `${data.blocking_items.length} 项`;
  $('#blockers').innerHTML = data.blocking_items.length
    ? data.blocking_items.map((b) => `
        <div class="detail-row">
          <div class="info">
            <b>#${b.archive_id} ${esc(b.archive_name)}</b>
            <span class="tag tag-${b.state}">${GROUP_LABEL[b.state]}</span>
            <div class="sub">批次 #${b.batch_id} · 最近核对 ${fmtTime(b.last_checked_at)}</div>
            ${b.freeze_reason ? `<div class="freeze-reason">❄ ${esc(b.freeze_reason)}</div>` : ''}
          </div>
          <div>${blockerChips(b.blockers)}</div>
        </div>`).join('')
    : '<div class="empty">当前没有阻塞项</div>';

  // 批次列表
  $('#batches').innerHTML = data.batches.length ? data.batches.map((b) => {
    const counts = ['pending', 'ready', 'frozen', 'disposed'].map((g) =>
      b.counts[g] ? `<span class="tag tag-${g}">${GROUP_LABEL[g]} ${b.counts[g]}</span>` : '').join('');
    return `
      <div class="batch-row" data-batch="${b.id}">
        <span class="name">#${b.id} ${esc(b.name)}</span>
        <span class="counts">${counts}</span>
        <span class="state-pill state-${b.state}">${b.state === 'open' ? '进行中' : '已执行'}</span>
        <span class="muted">最近核对 ${fmtTime(b.last_checked_at)}</span>
      </div>`;
  }).join('') : '<div class="empty">还没有处置批次</div>';
  $('#batches').querySelectorAll('.batch-row').forEach((row) => {
    row.onclick = () => openBatch(Number(row.dataset.batch));
  });
}

function renderCandidates(candidates) {
  state.candidates = candidates;
  state.selectedCandidates = new Set();
  const box = $('#candidates');
  if (!candidates.length) {
    box.className = 'candidate-list muted';
    box.textContent = '没有可纳入的到期档案。';
    return;
  }
  box.className = 'candidate-list';
  box.innerHTML = candidates.map((c) => `
    <label class="candidate">
      <input type="checkbox" data-id="${c.archive_id}" ${c.frozen ? 'disabled' : ''}>
      <span class="meta">
        <span class="name">#${c.archive_id} ${esc(c.name)}</span>
        <span class="sub"> · 期限 ${esc(c.retention_until)} · 已过期 ${c.days_overdue} 天${c.frozen ? ' · <b style="color:var(--frozen)">冻结中（纳入后即已冻结）</b>' : ''}</span>
      </span>
    </label>`).join('');
  box.querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.onchange = () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) state.selectedCandidates.add(id); else state.selectedCandidates.delete(id);
    };
  });
}

// ---- 批次详情抽屉 -------------------------------------------------------
function renderBatchDetail(detail) {
  state.batchDetail = detail;
  const b = detail.batch;
  $('#detail-title').textContent = `#${b.id} ${b.name}`;
  $('#detail-meta').innerHTML =
    `${b.state === 'open' ? '进行中' : '已执行'} · 创建人 ${esc(b.created_by)} · 创建于 ${fmtTime(b.created_at)}
     · <b>最近核对 ${fmtTime(b.last_checked_at)}</b>${b.executed_at ? ` · 执行于 ${fmtTime(b.executed_at)}` : ''}
     · 共 ${b.item_count} 条（待确认 ${detail.counts.pending} / 可处置 ${detail.counts.ready} / 已冻结 ${detail.counts.frozen}）`;
  const open = b.state === 'open';
  $('#btn-check').disabled = !open;
  $('#btn-confirm').disabled = !open || !isManager();
  $('#btn-execute').disabled = !open || !isManager() || detail.counts.ready === 0;
  $('#btn-add').disabled = !open || !isManager();
  const all = [...detail.groups.pending, ...detail.groups.ready, ...detail.groups.frozen, ...detail.groups.disposed];
  $('#detail-body').innerHTML = all.map((it) => {
    const pick = it.state === 'disposed' ? '' :
      `<label class="pick-line"><input type="checkbox" class="pick" data-item="${it.item_id}"> 选择</label>`;
    return pick + itemCard(it, true);
  }).join('') || '<div class="empty">空批次</div>';
  bindCardActions($('#detail-body'));
}

async function openBatch(id) {
  state.openBatchId = id;
  $('#detail').classList.remove('hidden');
  const detail = await api.batch(id);
  renderBatchDetail(detail);
}

async function refreshAll(keepBatchId) {
  try {
    const data = await api.console();
    renderConsole(data);
    if (keepBatchId) {
      const detail = await api.batch(keepBatchId);
      renderBatchDetail(detail);
    }
  } catch (err) { showError(err); }
}

// ---- 交互编排 -----------------------------------------------------------
$('#refresh').onclick = () => refreshAll(state.openBatchId);
$('#user').onchange = () => refreshAll(state.openBatchId);

$('#load-candidates').onclick = async () => {
  try {
    const data = await api.candidates();
    renderCandidates(data.candidates);
  } catch (err) { showError(err); }
};

$('#create-batch').onclick = async () => {
  const name = $('#batch-name').value.trim();
  const ids = [...state.selectedCandidates];
  if (!name) return toast('请填写批次名称', true);
  if (!ids.length) return toast('请勾选至少一个到期档案', true);
  try {
    const detail = await api.createBatch(name, ids);
    toast(`批次 #${detail.batch.id} 已建立`);
    $('#batch-name').value = '';
    await refreshAll(detail.batch.id);
    openBatch(detail.batch.id);
    $('#load-candidates').click();
  } catch (err) { showError(err); }
};

$('#detail-close').onclick = () => {
  $('#detail').classList.add('hidden');
  state.openBatchId = null;
};

$('#btn-check').onclick = async () => {
  try {
    const r = await api.check(state.openBatchId);
    toast(r.changed.length
      ? `核对完成：${r.changed.length} 条状态变化，已退回待确认/冻结`
      : '核对完成：期限与冻结状态均无变化');
    await refreshAll(state.openBatchId);
  } catch (err) { showError(err); }
};

$('#btn-confirm').onclick = async () => {
  const ids = [...$('#detail-body').querySelectorAll('.pick:checked')].map((cb) => Number(cb.dataset.item));
  if (!ids.length) return toast('请勾选要确认的条目', true);
  try {
    const r = await api.confirm(ids);
    toast(r.rejected && r.rejected.length
      ? `${r.rejected.length} 条核对有变化，仍为待确认；其余已确认`
      : '勾选条目已确认，可执行');
    await refreshAll(state.openBatchId);
  } catch (err) { showError(err); }
};

$('#btn-add').onclick = async () => {
  try {
    const data = await api.candidates();
    if (!data.candidates.length) return toast('没有可纳入的候选档案', true);
    const opts = data.candidates.map((c) =>
      `<label class="candidate"><input type="checkbox" data-id="${c.archive_id}">
       <span class="meta"><span class="name">#${c.archive_id} ${esc(c.name)}</span>
       <span class="sub"> · 已过期 ${c.days_overdue} 天</span></span></label>`).join('');
    modal.open({
      title: '纳入到期档案',
      body: `<div class="candidate-list">${opts}</div>`,
      okText: '纳入',
      onConfirm: async () => {
        const ids = [...$('#modal').querySelectorAll('input:checked')].map((cb) => Number(cb.dataset.id));
        modal.close();
        if (!ids.length) return toast('未勾选档案', true);
        await api.addArchives(state.openBatchId, ids);
        toast('已纳入批次');
        await refreshAll(state.openBatchId);
      },
    });
  } catch (err) { showError(err); }
};

$('#btn-execute').onclick = async () => {
  const detail = state.batchDetail;
  modal.open({
    title: '执行到期处置',
    body: `<p>将对批次 <b>#${detail.batch.id} ${esc(detail.batch.name)}</b> 中
           <b>${detail.counts.ready}</b> 条可处置档案执行处置。</p>
           <p class="muted">系统会先重新核对期限与冻结状态；有变化将退回待确认并拒绝执行。
           处置后档案只读，版本、副本与审计记录保留可查。此操作不可撤销。</p>`,
    okText: '确认执行',
    onConfirm: async () => {
      modal.close();
      try {
        await api.execute(state.openBatchId);
        toast('批次已执行，档案进入只读留存');
        await refreshAll(state.openBatchId);
      } catch (err) {
        showError(err);
        await refreshAll(state.openBatchId);
      }
    },
  });
};

// ---- 启动 ---------------------------------------------------------------
refreshAll();
