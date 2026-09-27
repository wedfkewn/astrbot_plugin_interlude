const bridge = window.AstrBotPluginPage;
const selector = document.getElementById('story');
const search = document.getElementById('filter');
const content = document.getElementById('content');
const refreshButton = document.getElementById('refresh');
const count = document.getElementById('story-count');

function element(tag, className = '', value = '') {
  const item = document.createElement(tag);
  item.className = className;
  item.textContent = value;
  return item;
}

function dateLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {dateStyle: 'medium', timeStyle: 'short'});
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
  retry.addEventListener('click', refresh);
  actions.append(settings, retry);
  panel.append(steps, actions);
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

  section('运行概览', '故事与调度器状态');
  const metrics = element('div', 'metrics');
  metrics.append(
    metric('故事进度', story.cursor ?? '—', `修订版本 ${story.revision ?? '—'}`),
    metric('待处理意图', pending, '等待后续推进'),
    metric('调度器', schedulerRunning ? '运行中' : '已停止', data.scheduler?.last_error || '无最近错误'),
    metric('最近事件', (data.entries || []).length, '最多显示最近 50 条'),
  );
  content.append(metrics);

  section('角色与时间');
  const details = element('div', 'details');
  details.append(
    detail('角色此刻', state.activity || '暂无活动记录', state.location ? `地点 · ${state.location}` : '地点暂未记录'),
    detail('当前时间', dateLabel(data.current_time) || '未知', state.world_state || '世界状态暂未记录'),
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
  applyFilter();
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

async function refresh() {
  refreshButton.disabled = true;
  content.replaceChildren(element('p', 'inline-empty', '正在加载故事…'));
  try {
    if (!bridge) throw new Error('AstrBot 页面桥接不可用');
    await bridge.ready();
    const stories = await bridge.apiGet('stories');
    if (stories.error) throw new Error(stories.error);
    const previous = selector.value;
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
    content.replaceChildren();
    renderDashboard(data);
  } catch (error) {
    message('暂时无法载入故事', `请检查插件状态后重试：${error.message}`, '!');
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener('click', refresh);
selector.addEventListener('change', refresh);
search.addEventListener('input', applyFilter);
await refresh();
