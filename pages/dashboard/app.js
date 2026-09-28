const bridge = window.AstrBotPluginPage;
const selector = document.getElementById('story');
const search = document.getElementById('filter');
const content = document.getElementById('content');
const refreshButton = document.getElementById('refresh');
const count = document.getElementById('story-count');
let activeTimeZone = 'Asia/Shanghai';
const views = new Set(['dashboard', 'story', 'memory', 'schedule']);

function currentView() {
  const name = window.location.hash.replace(/^#\/?/, '');
  return views.has(name) ? name : 'dashboard';
}

function syncNavigation() {
  const view = currentView();
  for (const link of document.querySelectorAll('.page-nav a')) {
    const active = link.dataset.view === view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  search.placeholder = view === 'dashboard' || view === 'story' ? '搜索最近事件' : '搜索当前页面';
}

function element(tag, className = '', value = '') {
  const item = document.createElement(tag);
  item.className = className;
  item.textContent = value;
  return item;
}

function dateLabel(value, withSeconds = false) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return date.toLocaleString('zh-CN', {
      timeZone: activeTimeZone, dateStyle: 'medium', timeStyle: withSeconds ? 'medium' : 'short',
    });
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
    return date.toLocaleString('zh-CN', {timeZone: 'UTC', dateStyle: 'medium', timeStyle: withSeconds ? 'medium' : 'short'});
  }
}

function updateLiveClock() {
  const clock = content.querySelector('[data-live-clock]');
  if (clock) clock.textContent = dateLabel(new Date(), true);
}

function section(title, note = '') {
  const heading = element('div', 'section-heading');
  heading.append(element('h2', '', title), element('span', '', note));
  content.append(heading);
}

function message(title, detail, icon = '○') {
  const panel = element('section', 'panel message-panel');
  panel.append(element('div', 'message-icon', icon), element('h2', '', title), element('p', '', detail));
  content.replaceChildren(panel);
  return panel;
}

function metric(label, value, note = '') {
  const box = element('section', 'panel metric');
  box.append(element('p', 'metric-label', label), element('p', 'metric-value', String(value)), element('small', 'metric-note', note));
  return box;
}

function detail(label, value, note = '') {
  const box = element('section', 'panel detail');
  box.append(element('h3', '', label), element('p', '', value), element('small', '', note));
  return box;
}

function renderEmpty() {
  if (currentView() !== 'dashboard') {
    const titles = {story: '还没有故事', memory: '还没有记忆', schedule: '还没有日程'};
    message(titles[currentView()], '与角色交流后，这里会显示对应的记录。', '✦');
    return;
  }
  const panel = message('还没有故事', '与角色的第一段私聊会创建故事，此后这里会显示场景、状态与最近事件。', '✦');
  const steps = element('div', 'steps');
  for (const [number, description] of [
    ['01 · 配置角色', '在插件配置中设置角色资料与叙事模型。'],
    ['02 · 发起私聊', '向接入 AstrBot 的角色发送一条消息。'],
    ['03 · 回到这里', '刷新页面，查看故事如何继续。'],
  ]) {
    const step = element('div', 'step');
    step.append(element('b', '', number), document.createTextNode(description));
    steps.append(step);
  }
  const actions = element('div', 'message-actions');
  const settings = element('a', 'primary', '前往插件管理');
  settings.href = '/#/extension';
  settings.target = '_top';
  const retry = element('button', '', '重新检查');
  retry.type = 'button';
  retry.addEventListener('click', () => refresh());
  actions.append(settings, retry);
  panel.append(steps, actions);
  renderEmotion({});
  renderProactive({});
  renderMaintenance(false);
}

function renderEmotion(state) {
  section('角色情绪', '当前故事');
  const panel = element('section', 'panel emotion-panel');
  const emotion = state.emotion || '尚未记录';
  const intensity = Math.max(0, Math.min(5, Number(state.emotion_intensity) || 0));
  panel.append(
    element('p', 'emotion-name', emotion),
    element('p', 'emotion-reason', state.emotion
      ? `缘由 · ${state.emotion_reason || '暂无明确缘由'}`
      : '与角色交流后，情绪会根据实际剧情更新。'),
  );
  const strength = element('div', 'emotion-strength');
  const progress = element('progress', 'emotion-progress');
  progress.max = 5;
  progress.value = intensity;
  progress.setAttribute('aria-label', '情绪强度');
  strength.append(element('span', '', `强度 ${state.emotion ? `${intensity}/5` : '未记录'}`), progress);
  panel.append(strength);
  content.append(panel);
}

function renderProactive(proactive) {
  section('主动聊天', '触发缘由与发送状态');
  const panel = element('section', 'panel proactive-panel');
  const enabled = Boolean(proactive.enabled);
  const idle = Boolean(proactive.idle_enabled);
  panel.append(element('p', 'proactive-state', !enabled ? '主动消息未开启'
    : idle ? `静默触发已开启 · 至少 ${proactive.idle_minutes || 1200} 分钟` : '主动消息已开启 · 静默触发未开启'));
  panel.append(element('p', 'proactive-hint', '在插件配置中开启主动消息、静默触发并填写平台用户 ID 白名单后，角色才会评估是否主动联系。已有的剧情主动意图也会记录在这里。'));
  const records = proactive.records || [];
  if (!records.length) panel.append(element('p', 'proactive-empty', '暂无主动聊天记录。'));
  for (const item of records) {
    const record = element('div', 'proactive-record');
    const status = item.sent_at ? '已发送' : ({pending: '等待发送', processing: '发送中', completed: '已处理', cancelled: '已取消'})[item.status] || item.status;
    record.append(
      element('p', 'proactive-record-title', `${item.display_name || item.platform_user_id || '参与者'} · ${status}`),
      element('p', 'proactive-record-reason', `主动缘由：${item.reason || '历史记录未注明缘由'}`),
      element('p', 'proactive-record-content', `消息：${item.content}`),
      element('small', '', item.sent_at ? `发送于 ${dateLabel(item.sent_at)}` : `计划时间 ${dateLabel(item.due_at)}`),
    );
    panel.append(record);
  }
  content.append(panel);
}

function renderDashboard(data) {
  const story = data.story || {};
  let state = {};
  try { state = JSON.parse(story.state_json || '{}') || {}; } catch { /* Malformed stored state should not hide the dashboard. */ }
  const entries = (data.entries || []).slice(-8).reverse();
  const pending = (data.intents || []).filter(item => item.status === 'pending').length;
  const schedulerRunning = Boolean(data.scheduler?.running);

  const scene = element('section', 'panel scene-panel');
  scene.append(element('p', 'panel-kicker', '当前场景'), element('p', 'scene-text', data.scene?.summary || '故事已创建，等待下一段场景展开。'));
  const sceneMeta = element('div', 'scene-meta');
  sceneMeta.append(element('span', `status-pill${story.paused ? ' paused' : ''}`, story.paused ? '故事已暂停' : '故事进行中'));
  if (data.scene?.last_activity_at) sceneMeta.append(element('span', '', `最近活动 · ${dateLabel(data.scene.last_activity_at)}`));
  scene.append(sceneMeta);
  content.append(scene);
  renderEmotion(state);
  renderProactive(data.proactive || {});

  section('运行概览', '故事与调度器状态');
  const metrics = element('div', 'metrics');
  metrics.append(
    metric('故事进度', dateLabel(story.cursor) || '—', `上次叙事推进 · 修订版本 ${story.revision ?? '—'}`),
    metric('待处理意图', pending, '等待后续推进'),
    metric('调度器', schedulerRunning ? '运行中' : '已停止', data.scheduler?.last_error || '无最近错误'),
    metric('最近事件', (data.entries || []).length, '最多显示最近 50 条'),
  );
  content.append(metrics);

  section('角色与时间');
  const details = element('div', 'details');
  const clockDetail = detail('现实时间', dateLabel(new Date(), true), `时区 · ${activeTimeZone}`);
  clockDetail.querySelector('p').dataset.liveClock = '';
  details.append(
    detail('角色此刻', state.activity || '暂无活动记录', state.location ? `地点 · ${state.location}` : '地点暂未记录'),
    clockDetail,
    detail('世界状态', state.world_state || '世界状态暂未记录'),
  );
  content.append(details);

  section('最近事件', `显示 ${entries.length} 条`);
  const timeline = element('div', 'panel timeline');
  if (!entries.length) timeline.append(element('p', 'inline-empty', '还没有事件。与角色交流后，这里会记录故事变化。'));
  for (const item of entries) {
    const row = element('article', 'timeline-item');
    row.dataset.search = `${item.event_type || ''} ${item.content || ''} ${item.occurred_at || ''}`.toLowerCase();
    const type = element('div', 'event-type', item.event_type || '事件');
    const body = element('div');
    body.append(element('p', 'event-content', item.content || '无文字内容'), element('time', 'event-time', dateLabel(item.occurred_at)));
    row.append(type, body);
    timeline.append(row);
  }
  content.append(timeline);

  renderMaintenance(true);
  applyFilter();
}

function recordCard(title, body, note = '') {
  const card = element('section', 'panel record-card');
  card.dataset.search = `${title} ${body} ${note}`.toLowerCase();
  card.append(element('h3', '', title), element('p', '', body));
  if (note) card.append(element('small', '', note));
  content.append(card);
  return card;
}

function addManagedAction(card, label, action, itemId = '') {
  const button = element('button', 'secondary-button', label);
  button.type = 'button';
  button.addEventListener('click', () => {
    content.querySelector('.inline-confirmation')?.remove();
    const storyId = selector.value;
    const confirmation = element('div', 'inline-confirmation');
    confirmation.append(element('p', '', `确认对故事「${storyId}」执行「${label}」？输入 CONFIRM。`));
    if (action === 'story_purge') confirmation.append(element('p', '', '若当前配置仍指向此故事，新消息会重新创建它。'));
    const input = element('input');
    input.setAttribute('aria-label', '输入 CONFIRM 确认操作');
    input.autocomplete = 'off';
    const submit = element('button', 'danger-button', '确认执行');
    submit.type = 'button';
    submit.disabled = true;
    input.addEventListener('input', () => { submit.disabled = input.value !== 'CONFIRM'; });
    const cancel = element('button', '', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', () => confirmation.remove());
    submit.addEventListener('click', async () => {
      if (input.value !== 'CONFIRM') return;
      submit.disabled = true;
      try {
        const challenge = await bridge.apiPost('challenge', {action, story_id: storyId, item_id: itemId});
        if (challenge.error) throw new Error(challenge.error);
        const result = await bridge.apiPost('action', {token: challenge.token, confirmation: 'CONFIRM'});
        if (result.error) throw new Error(result.error);
        await refresh();
        if (action === 'story_purge') content.prepend(element('p', 'inline-empty', `故事「${storyId}」已删除。`));
      } catch (error) {
        confirmation.append(element('p', 'action-error', `操作失败：${error.message}`));
        submit.disabled = false;
      }
    });
    confirmation.append(input, submit, cancel);
    card.append(confirmation);
    input.focus();
  });
  card.append(button);
}

function renderStory(data) {
  section('故事', '最近 50 条事件');
  recordCard('当前场景', data.scene?.summary || '尚无摘要', dateLabel(data.scene?.last_activity_at));
  const maintenance = recordCard('故事维护', '重置会清空剧情但保留角色和参与者；永久删除会移除整个故事。');
  addManagedAction(maintenance, '重置故事', 'story_reset');
  addManagedAction(maintenance, '永久删除故事', 'story_purge');
  const timeline = element('div', 'panel timeline');
  const entries = (data.entries || []).slice().reverse();
  if (!entries.length) timeline.append(element('p', 'inline-empty', '还没有故事事件。'));
  for (const item of entries) {
    const row = element('article', 'timeline-item');
    row.dataset.search = `${item.event_type || ''} ${item.content || ''}`.toLowerCase();
    const body = element('div');
    body.append(element('p', 'event-content', item.content || '无文字内容'), element('time', 'event-time', dateLabel(item.occurred_at)));
    row.append(element('div', 'event-type', item.event_type || '事件'), body);
    timeline.append(row);
  }
  content.append(timeline);
}

function renderMemory(data) {
  section('记忆', '事实、关系与演化层');
  for (const item of data.facts || []) {
    const card = recordCard(item.scope, item.content, item.status);
    if (item.status === 'active') addManagedAction(card, '归档记忆', 'memory_delete', item.id);
  }
  for (const item of data.overlays || []) recordCard(`Overlay · ${item.scope}`, item.content, item.status);
  for (const item of data.perspectives || []) recordCard('Perspective', item.content, item.status);
  for (const item of data.relationships || []) recordCard(`Relationship · ${item.participant_id}`, item.summary || '暂无文字摘要');
  if ((data.overlays || []).some(item => item.status === 'active')) {
    addManagedAction(recordCard('演化层维护', '清除所有 Overlay 与 Perspective。'), '清除演化层', 'overlay_clear');
  }
  if (!content.querySelector('.record-card')) content.append(element('p', 'inline-empty', '暂无记忆。'));
}

function renderSchedule(data) {
  section('日程', '计划、意图与任务');
  for (const item of data.schedules || []) recordCard(item.kind, item.content, `${dateLabel(item.start_at)} → ${dateLabel(item.end_at)}`);
  for (const item of data.intents || []) recordCard(item.type, item.content, `${dateLabel(item.due_at)} · ${item.status}`);
  for (const item of data.jobs || []) recordCard(`任务 · ${item.kind}`, item.id, `${dateLabel(item.due_at)} · ${item.status}`);
  if (!content.querySelector('.record-card')) content.append(element('p', 'inline-empty', '暂无日程或任务。'));
}

function renderMaintenance(hasStory) {
  section('故事维护');
  const maintenance = element('section', 'panel maintenance-panel');
  const explanation = element('div');
  explanation.append(
    element('h3', '', hasStory ? '管理当前故事' : '从备份恢复故事'),
    element('p', '', hasStory ? '格式化会清空剧情；本地备份保存在 AstrBot 服务器，恢复时选择备份时间。' : '选择服务器本地的备份时间，可重建已删除的故事。'),
  );
  const resetButton = element('button', 'danger-button', '格式化当前故事');
  resetButton.type = 'button';
  resetButton.addEventListener('click', () => {
    const storyId = selector.value;
    resetButton.disabled = true;
    const confirmation = element('div', 'confirmation');
    confirmation.append(element('p', '', `即将恢复故事「${storyId}」的初始状态。请输入 CONFIRM 确认。`));
    const input = element('input');
    input.setAttribute('aria-label', '输入 CONFIRM 确认格式化');
    input.autocomplete = 'off';
    const submit = element('button', 'danger-button', '确认格式化');
    submit.type = 'button';
    submit.disabled = true;
    input.addEventListener('input', () => { submit.disabled = input.value !== 'CONFIRM'; });
    const cancel = element('button', '', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', () => { confirmation.remove(); resetButton.disabled = false; });
    submit.addEventListener('click', async () => {
      if (input.value !== 'CONFIRM') return;
      submit.disabled = true;
      cancel.disabled = true;
      try {
        const challenge = await bridge.apiPost('challenge', {action: 'story_reset', story_id: storyId});
        if (challenge.error) throw new Error(challenge.error);
        const result = await bridge.apiPost('action', {token: challenge.token, confirmation: 'CONFIRM'});
        if (result.error) throw new Error(result.error);
        await refresh();
      } catch (error) {
        confirmation.append(element('p', 'error-text', `格式化失败：${error.message}`));
        cancel.disabled = false;
        submit.disabled = false;
      }
    });
    confirmation.append(input, submit, cancel);
    maintenance.append(confirmation);
    input.focus();
  });
  const buttons = element('div', 'maintenance-actions');
  if (hasStory) buttons.append(resetButton);
  const exportButton = element('button', '', '创建本地备份');
  exportButton.type = 'button';
  exportButton.disabled = !hasStory;
  buttons.append(exportButton);
  const restoreButton = element('button', '', '按时间恢复');
  restoreButton.type = 'button';
  buttons.append(restoreButton);
  const feedback = element('div', 'maintenance-feedback');
  feedback.setAttribute('aria-live', 'polite');
  const showFeedback = (text, error = false) => feedback.replaceChildren(element('p', error ? 'error-text' : '', text));
  exportButton.addEventListener('click', async () => {
    const storyId = selector.value;
    if (!storyId) return;
    exportButton.disabled = true;
    try {
      const result = await bridge.apiPost('backup/save', {story_id: storyId});
      if (result.error) throw new Error(result.error);
      showFeedback(`故事「${storyId}」已备份到 AstrBot 服务器 · ${dateLabel(result.backup.exported_at, true)}。`);
    } catch (error) { showFeedback(`备份失败：${error.message}`, true); }
    finally { exportButton.disabled = false; }
  });
  restoreButton.addEventListener('click', async () => {
    restoreButton.disabled = true;
    showFeedback('正在读取本地备份…');
    try {
      const result = await bridge.apiGet('backup/list');
      if (result.error) throw new Error(result.error);
      if (!result.backups?.length) { showFeedback('还没有本地备份。先选择故事并点击“创建本地备份”。'); return; }
      const panel = element('div', 'confirmation backup-confirmation');
      const label = element('label', '', '选择恢复时间');
      const selected = element('select');
      selected.setAttribute('aria-label', '选择恢复时间和故事');
      for (const backup of result.backups) {
        const option = element('option', '', `${dateLabel(backup.exported_at, true)} · ${backup.story_id} · ${backup.events} 条事件 · #${backup.id.slice(0, 6)}`);
        option.value = backup.id;
        selected.append(option);
      }
      label.append(selected);
      const previewButton = element('button', '', '预览此时间点');
      previewButton.type = 'button';
      panel.append(label, previewButton);
      previewButton.addEventListener('click', async () => {
        previewButton.disabled = true;
        try {
          const preview = await bridge.apiPost('backup/preview-local', {backup_id: selected.value});
          if (preview.error) throw new Error(preview.error);
          const confirmation = element('div', 'confirmation backup-confirmation');
          confirmation.append(
            element('p', '', `备份故事：${preview.story_id} · 备份时间：${dateLabel(preview.exported_at, true)}`),
            element('p', '', `事件 ${preview.counts.story_entries}、记忆 ${preview.counts.facts}、参与者 ${preview.counts.participants}、日程 ${preview.counts.schedules}`),
            element('p', '', preview.exists ? '将覆盖此故事现有数据。' : '将重新创建已删除的故事。'),
            element('p', '', '当前角色与世界设定优先；旧的未完成任务不会补发。输入 CONFIRM 确认，预览 5 分钟后失效。'),
          );
          const input = element('input');
          input.setAttribute('aria-label', '输入 CONFIRM 确认恢复');
          input.autocomplete = 'off';
          const submit = element('button', 'danger-button', '确认恢复');
          submit.type = 'button';
          submit.disabled = true;
          input.addEventListener('input', () => { submit.disabled = input.value !== 'CONFIRM'; });
          submit.addEventListener('click', async () => {
            if (input.value !== 'CONFIRM') return;
            submit.disabled = true;
            try {
              const restored = await bridge.apiPost('backup/restore', {
                token: preview.token, story_id: preview.story_id, confirmation: 'CONFIRM',
              });
              if (restored.error) throw new Error(restored.error);
              await refresh(preview.story_id);
            } catch (error) { confirmation.append(element('p', 'error-text', `恢复失败：${error.message}。请重新选择时间点。`)); }
          });
          confirmation.append(input, submit);
          feedback.replaceChildren(confirmation);
        } catch (error) { panel.append(element('p', 'error-text', `预览失败：${error.message}`)); previewButton.disabled = false; }
      });
      feedback.replaceChildren(panel);
    } catch (error) { showFeedback(`读取备份失败：${error.message}`, true); }
    finally { restoreButton.disabled = false; }
  });
  maintenance.append(explanation, buttons, feedback);
  content.append(maintenance);
}

function applyFilter() {
  const term = search.value.trim().toLowerCase();
  const rows = [...content.querySelectorAll('[data-search]')];
  let shown = 0;
  for (const row of rows) {
    row.hidden = Boolean(term && !row.dataset.search.includes(term));
    if (!row.hidden) shown += 1;
  }
  const existing = content.querySelector('.filter-empty');
  if (existing) existing.remove();
  if (term && rows.length && !shown) {
    const empty = element('p', 'inline-empty filter-empty', '没有匹配的内容。');
    (content.querySelector('.timeline') || content).append(empty);
  }
}

async function refresh(preferredStory = '') {
  syncNavigation();
  refreshButton.disabled = true;
  content.replaceChildren(element('p', 'inline-empty', '正在加载故事…'));
  try {
    if (!bridge) throw new Error('AstrBot 页面桥接不可用');
    await bridge.ready();
    const stories = await bridge.apiGet('stories');
    if (stories.error) throw new Error(stories.error);
    const previous = preferredStory || selector.value;
    const list = stories.stories || [];
    selector.replaceChildren(...list.map(story => {
      const option = document.createElement('option');
      option.value = story.id;
      option.textContent = story.id;
      return option;
    }));
    if (!list.length) selector.append(element('option', '', '尚无故事'));
    if (list.some(story => story.id === previous)) selector.value = previous;
    selector.disabled = !list.length;
    search.disabled = !list.length;
    count.textContent = list.length ? `共 ${list.length} 个故事` : '尚未创建故事';
    if (!list.length) { renderEmpty(); return; }
    const data = await bridge.apiGet('snapshot', {story_id: selector.value});
    if (data.error) throw new Error(data.error);
    activeTimeZone = data.timezone || 'Asia/Shanghai';
    content.replaceChildren();
    const view = currentView();
    if (view === 'dashboard') renderDashboard(data);
    else {
      ({story: renderStory, memory: renderMemory, schedule: renderSchedule})[view](data);
      applyFilter();
    }
  } catch (error) {
    message('暂时无法载入故事', `请检查插件状态后重试：${error.message}`, '!');
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener('click', () => refresh());
selector.addEventListener('change', () => refresh());
search.addEventListener('input', applyFilter);
window.addEventListener('hashchange', () => refresh());
setInterval(updateLiveClock, 1000);
await refresh();
