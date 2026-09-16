'use strict';
(() => {
  let lastSignature = null, refreshing = false, refreshPending = false;
  const status = $('#retentionStatus'), notice = $('#retentionNotice');

  function displayTime(value) {
    if (!value) return '—';
    // Keep the server's clock and offset, not the visiting browser's timezone.
    const text = String(value);
    const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/);
    return match ? `${match[1]} ${match[2]}${match[3] ? ' ' + (match[3] === 'Z' ? 'UTC' : 'UTC' + match[3]) : ''}` : text;
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
    if (refreshing) return;
    refreshing = true;
    try {
      await refreshDatasets();
      renderArchives();
      refreshPending = false;
      $('#retainedRefreshStatus').textContent = '';
    } catch {
      // An offline tab or maintenance does not generate recurring toast errors.
      $('#retainedRefreshStatus').textContent = '暂时无法刷新列表；服务可能正在维护，请稍后重试。';
    } finally { refreshing = false; }
  }

  function applyStatus(result) {
    const wasBusy = Boolean(state.retentionBusy);
    state.retentionBusy = result.phase === 'running';
    const schedule = result.enabled ? `下次检查 ${displayTime(result.next_run)}` : '自动清理已关闭';
    const reason = result.message || result.last_result?.reason || '暂时无法完成清理';
    status.textContent = state.retentionBusy ? '正在清理并释放磁盘空间…' : result.phase === 'deferred' ? '清理已延期，15 分钟后重试' : result.phase === 'error' ? '上次清理未完成，将稍后重试' : schedule;
    status.title = `服务电脑时区：${result.timezone || '本地时区'}。${result.message || ''}`;
    notice.hidden = !['running', 'deferred', 'error'].includes(result.phase);
    notice.classList.toggle('is-busy', state.retentionBusy);
    if (state.retentionBusy) notice.textContent = '正在清理过期日志索引并压缩数据库，暂时暂停日志查询，请稍后再搜。原始 ZIP、AI 会话和配置不会删除。';
    else if (result.phase === 'deferred') notice.textContent = `过期索引清理已延期：${reason}。15 分钟后再检查，原始 ZIP 保留。`;
    else if (result.phase === 'error') notice.textContent = `自动清理暂未完成：${reason}。稍后自动重试，原始 ZIP 保留。`;

    const signature = JSON.stringify([result.last_run, result.last_result]);
    const changed = lastSignature !== null && lastSignature !== signature;
    lastSignature = signature;
    if (wasBusy || changed) refreshPending = true;
    if (!state.retentionBusy && refreshPending) void refreshArchiveData();
  }

  async function poll() {
    try { applyStatus(await api('/api/retention')); }
    catch { status.textContent = '清理计划暂不可用，稍后自动重试'; }
    finally { setTimeout(poll, 30000); }
  }

  $('#retainedArchives').addEventListener('click', () => {
    renderArchives();
    $('#retainedDialog').showModal();
    $('#retainedRefreshStatus').textContent = '正在刷新归档列表…';
    void refreshArchiveData();
  });
  $('#retainedUpload').addEventListener('click', () => { $('#retainedDialog').close(); openUpload(); });
  document.addEventListener('logscope:datasets', renderArchives);
  renderArchives();
  void poll();
})();
