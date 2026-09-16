'use strict';
(() => {
  let lastSignature = null, refreshing = false, refreshPending = true;
  let statusGeneration = 0, pollTimer, cancelPending = false, lastStatus = null;
  const status = $('#retentionStatus'), notice = $('#retentionNotice');
  const activePhases = new Set(['queued', 'running', 'cancelling']);
  const stages = {checking:'检查过期日志', queued:'等待清理', deleting:'删除过期索引', checkpoint:'写回并释放 WAL', fts:'合并全文索引', vacuum:'压缩日志数据库', finishing:'完成维护', rollback:'回滚事务'};

  function displayTime(value) {
    if (!value) return '—';
    // Keep the server's clock and offset, not the visiting browser's timezone.
    const text = String(value);
    const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/);
    return match ? `${match[1]} ${match[2]}${match[3] ? ' ' + (match[3] === 'Z' ? 'UTC' : 'UTC' + match[3]) : ''}` : text;
  }

  function duration(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
  }

  function renderArchives() {
    const archives = state.datasets.filter(dataset => dataset.state === 'expired');
    $('#retainedCount').textContent = number(archives.length);
    $('#retainedArchiveList').innerHTML = archives.length ? archives.map(dataset => {
      const path = dataset.archive_relative_path || `archives/${dataset.id}.zip`;
      const hasArchive = Number(dataset.archive_bytes) > 0;
      const archive = hasArchive ? `<code>${escapeHTML(path)}</code><a class="retained-download" href="/api/archives/download?dataset=${encodeURIComponent(dataset.id)}" download>⇩ 下载原始 ZIP</a>` : '<p class="retained-missing">原 ZIP 未留存，请使用原上传文件</p>';
      return `<article class="retained-card"><div class="retained-card-heading"><strong>${escapeHTML(dataset.name)}</strong><span class="retained-badge">索引已清理</span></div><p>${hasArchive ? formatBytes(dataset.archive_bytes) + ' · ' : ''}清理时间 ${escapeHTML(displayTime(dataset.expired_at))}</p>${archive}</article>`;
    }).join('') : '<div class="retained-empty"><strong>暂时没有过期归档</strong><p>索引满 72 小时后，在每日清理时移到这里；正在使用的日志包仍在左侧选择。</p></div>';
  }

  async function refreshArchiveData() {
    if (refreshing || state.datasetsLoading) { refreshPending = true; return; }
    refreshing = true;
    try {
      const result = await refreshDatasets();
      if (!result) return; // A newer user-driven refresh owns the selection.
      renderArchives();
      refreshPending = false;
      $('#retainedRefreshStatus').textContent = '';
    } catch {
      refreshPending = true;
      $('#retainedRefreshStatus').textContent = '暂时无法刷新列表，输入内容已保留；稍后自动重试。';
    } finally { refreshing = false; }
  }

  function applyStatus(result) {
    const wasBusy = Boolean(state.retentionBusy);
    lastStatus = result;
    // Only the server's actual maintenance lock can block searches.
    state.retentionBusy = result.maintenance === true;
    const active = activePhases.has(result.phase), progress = result.progress || {};
    if (!active && !state.retentionBusy) cancelPending = false;
    const stopping = (active || state.retentionBusy) && (cancelPending || result.phase === 'cancelling' || Boolean(progress.cancel_requested));
    const reason = active || state.retentionBusy ? progress.message || result.message || '' : result.message || result.last_result?.reason || '';
    const stage = stages[progress.stage] || progress.stage || '准备清理';
    const nextRun = result.enabled ? `下次检查 ${displayTime(result.next_run)}` : '自动清理已关闭';
    status.textContent = stopping && active ? '正在安全停止本次清理…' : active ? `${stage} · ${duration(progress.elapsed_seconds)}` : nextRun;
    status.title = `服务电脑时区：${result.timezone || '本地时区'}。${result.message || ''}`;
    notice.hidden = !active && !state.retentionBusy && !['deferred', 'error', 'cancelled'].includes(result.phase);
    notice.classList.toggle('is-busy', state.retentionBusy);
    $('#retentionTitle').textContent = stopping && (active || state.retentionBusy) ? '正在安全停止' : active ? '过期索引清理进度' : result.phase === 'cancelled' ? '本次清理已停止' : result.phase === 'error' ? '本次清理未完成' : '过期索引清理已延期';
    $('#retentionMessage').textContent = active || state.retentionBusy
      ? `${reason || '正在准备清理'}。${state.retentionBusy ? '数据库暂被维护占用，请稍后查询。' : '当前未占用日志查询，可继续搜索。'}${stopping ? '正在等待 SQL 中断或事务回滚，停止完成以实时状态为准。' : ''}原 ZIP、AI 会话及配置保留。`
      : `${reason || '本轮任务已结束'}。${nextRun}。原 ZIP 保留。`;
    $('#retentionProgress').hidden = !active && !state.retentionBusy;
    if (active || state.retentionBusy) {
      const counts = Number.isFinite(Number(progress.total)) && Number(progress.total) > 0
        ? `已处理 ${number(progress.completed)} / ${number(progress.total)} 个日志包` : `已处理 ${number(progress.completed)} 个日志包`;
      const current = progress.current ? ` · ${progress.current}` : '';
      // DELETE / VACUUM have no honest within-statement percentage.
      $('#retentionProgress').innerHTML = progressHTML(stage, `已用时 ${duration(progress.elapsed_seconds)} · ${counts}${current}`, null);
    }
    $('#retentionCancel').hidden = !result.can_cancel && !stopping;
    $('#retentionCancel').disabled = stopping || !result.can_cancel;
    $('#retentionCancel').textContent = stopping ? '正在安全停止…' : '停止本次清理';
    const signature = JSON.stringify([result.last_run, result.last_result]);
    if (wasBusy || (lastSignature !== null && lastSignature !== signature) || state.datasetRefreshPending) refreshPending = true;
    lastSignature = signature;
    if (!state.retentionBusy && refreshPending) void refreshArchiveData();
  }

  function unknownStatus(message = '暂时无法确认维护状态，可以刷新状态或重试搜索；后端仍会保护实际正在维护的数据库。') {
    state.retentionBusy = false; // A stale client flag must never permanently block searches.
    status.textContent = '暂时无法确认清理状态';
    notice.hidden = false; notice.classList.remove('is-busy');
    $('#retentionTitle').textContent = '维护状态待确认';
    $('#retentionMessage').textContent = message;
    $('#retentionProgress').hidden = true;
    $('#retentionCancel').hidden = !lastStatus?.can_cancel && !cancelPending;
    $('#retentionCancel').disabled = cancelPending;
    $('#retentionCancel').textContent = cancelPending ? '停止进度待确认…' : '重试停止清理';
  }

  function schedulePoll(delay) { clearTimeout(pollTimer); pollTimer = setTimeout(() => { void poll(); }, delay); }
  async function poll() {
    clearTimeout(pollTimer);
    const generation = ++statusGeneration;
    try {
      const result = await api('/api/retention');
      if (generation !== statusGeneration) return;
      applyStatus(result);
      schedulePoll(activePhases.has(result.phase) || result.maintenance ? 2000 : 30000);
    } catch {
      if (generation !== statusGeneration) return;
      unknownStatus(); schedulePoll(5000);
      if (state.datasetRefreshPending) void refreshArchiveData();
    }
  }

  $('#retentionCancel').addEventListener('click', async () => {
    if (cancelPending) return;
    clearTimeout(pollTimer);
    const generation = ++statusGeneration;
    cancelPending = true;
    $('#retentionCancel').disabled = true;
    $('#retentionCancel').textContent = '正在安全停止…';
    $('#retentionTitle').textContent = '正在安全停止';
    $('#retentionMessage').textContent = '已请求停止，等待后端确认和事务安全收尾；数据库是否恢复以实时状态为准。';
    try {
      const result = await api('/api/retention/cancel', {});
      if (generation !== statusGeneration) return;
      applyStatus(result); schedulePoll(1000);
    } catch {
      cancelPending = false;
      if (generation !== statusGeneration) { if (lastStatus) applyStatus(lastStatus); return; }
      unknownStatus('取消请求暂未确认。可以重试停止，或刷新状态确认；不要手动删除数据库和伴随文件。');
      schedulePoll(2000);
    }
  });
  $('#retentionRetry').addEventListener('click', () => { void poll(); });
  document.addEventListener('logscope:maintenance', () => {
    state.retentionBusy = true;
    notice.hidden = false; notice.classList.add('is-busy');
    $('#retentionTitle').textContent = '日志索引正在维护';
    $('#retentionMessage').textContent = '后端确认数据库暂被占用，正在读取具体阶段；你的输入已保留。';
    schedulePoll(100);
  });
  $('#retainedArchives').addEventListener('click', () => {
    renderArchives();
    $('#retainedDialog').showModal();
    $('#retainedRefreshStatus').textContent = '正在刷新归档列表…';
    void refreshArchiveData();
  });
  $('#retainedUpload').addEventListener('click', () => { $('#retainedDialog').close(); openUpload(); });
  document.addEventListener('logscope:datasets', () => { renderArchives(); refreshPending = false; $('#retainedRefreshStatus').textContent = ''; });
  renderArchives();
  void poll();
})();
