const bridge = window.AstrBotPluginPage;
const selector = document.getElementById('story');
const search = document.getElementById('filter');
const content = document.getElementById('content');
const refreshButton = document.getElementById('refresh');
const count = document.getElementById('story-count');
let activeTimeZone = 'Asia/Shanghai';

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

function renderMaintenance(hasStory) {
  section('故事维护');
  const maintenance = element('section', 'panel maintenance-panel');
  const explanation = element('div');
  explanation.append(
    element('h3', '', hasStory ? '管理当前故事' : '从备份恢复故事'),
    element('p', '', hasStory ? '格式化会清空剧情、记忆、关系、日程与待发送任务，保留角色设定、世界设定和参与者。' : '选择 JSON 备份，可重建已删除的故事。'),
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
  const exportButton = element('button', '', '导出备份');
  exportButton.type = 'button';
  exportButton.disabled = !hasStory;
  buttons.append(exportButton);
  const restoreButton = element('button', '', '恢复备份');
  restoreButton.type = 'button';
  buttons.append(restoreButton);
  const fileInput = element('input');
  fileInput.type = 'file';
  fileInput.accept = '.json,application/json';
  fileInput.hidden = true;
  fileInput.setAttribute('aria-label', '选择故事备份文件');
  const feedback = element('div', 'maintenance-feedback');
  feedback.setAttribute('aria-live', 'polite');
  const showFeedback = (text, error = false) => feedback.replaceChildren(element('p', error ? 'error-text' : '', text));
  exportButton.addEventListener('click', async () => {
    const storyId = selector.value;
    if (!storyId) return;
    exportButton.disabled = true;
    try {
      const backup = await bridge.apiGet('backup/export', {story_id: storyId});
      if (backup.error) throw new Error(backup.error);
      const url = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 2)], {type: 'application/json'}));
      const link = document.createElement('a');
      link.href = url;
      link.download = `interlude-${storyId.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80)}-${Date.now()}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      showFeedback(`已导出故事「${storyId}」，文件包含聊天内容，请妥善保管。`);
    } catch (error) { showFeedback(`导出失败：${error.message}`, true); }
    finally { exportButton.disabled = false; }
  });
  restoreButton.addEventListener('click', () => { fileInput.value = ''; fileInput.click(); });
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { showFeedback('备份文件不能超过 20 MiB。', true); return; }
    restoreButton.disabled = true;
    showFeedback('正在检查备份…');
    try {
      const preview = await bridge.upload('backup/preview', file);
      if (preview.error) throw new Error(preview.error);
      const panel = element('div', 'confirmation backup-confirmation');
      panel.append(
        element('p', '', `备份故事：${preview.story_id} · 导出时间：${dateLabel(preview.exported_at)}`),
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
          const result = await bridge.apiPost('backup/restore', {
            token: preview.token, story_id: preview.story_id, confirmation: 'CONFIRM',
          });
          if (result.error) throw new Error(result.error);
          await refresh(preview.story_id);
        } catch (error) { panel.append(element('p', 'error-text', `恢复失败：${error.message}。请重新选择备份文件。`)); }
      });
      feedback.replaceChildren(panel);
    } catch (error) { showFeedback(`备份检查失败：${error.message}`, true); }
    finally { restoreButton.disabled = false; }
  });
  maintenance.append(explanation, buttons, fileInput, feedback);
  content.append(maintenance);
}

function applyFilter() {
  const term = search.value.trim().toLowerCase();
  const rows = [...content.querySelectorAll('.timeline-item')];
  let shown = 0;
  for (const row of rows) {
    row.hidden = Boolean(term && !row.dataset.search.includes(term));
    if (!row.hidden) shown += 1;
  }
  const existing = content.querySelector('.filter-empty');
  if (existing) existing.remove();
  if (term && rows.length && !shown) {
    const empty = element('p', 'inline-empty filter-empty', '没有匹配的最近事件。');
    content.querySelector('.timeline').append(empty);
  }
}

async function refresh(preferredStory = '') {
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
    renderDashboard(data);
  } catch (error) {
    message('暂时无法载入故事', `请检查插件状态后重试：${error.message}`, '!');
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener('click', () => refresh());
selector.addEventListener('change', () => refresh());
search.addEventListener('input', applyFilter);
setInterval(updateLiveClock, 1000);
await refresh();
