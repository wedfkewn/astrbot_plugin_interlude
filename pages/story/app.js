const bridge = window.AstrBotPluginPage;
await bridge.ready();
const selector = document.getElementById('story');
const content = document.getElementById('content');
const view = content.dataset.view;
let timeZone = 'Asia/Shanghai';
function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try { return date.toLocaleString('zh-CN', {timeZone, dateStyle: 'medium', timeStyle: 'short'}); }
  catch (error) {
    if (!(error instanceof RangeError)) throw error;
    return date.toLocaleString('zh-CN', {timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short'});
  }
}
function node(tag, className, value) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}
function card(title, detail, meta = '') {
  const box = node('section', 'card', '');
  box.append(node('strong', '', title), node('p', '', detail), node('small', 'meta', meta));
  content.append(box);
  return box;
}
function addAction(box, label, action, itemId = '') {
  const button = node('button', '', label);
  button.addEventListener('click', () => {
    const selectedStory = selector.value;
    const existing = document.getElementById('confirm-panel');
    if (existing) existing.remove();
    const panel = node('section', 'card', '');
    panel.id = 'confirm-panel';
    panel.append(node('p', '', `确认对故事「${selectedStory}」执行「${label}」？输入 CONFIRM 后提交。`));
    if (action === 'story_purge') panel.append(node('p', 'empty', '若当前插件配置仍指向此故事，新消息会重新创建它。'));
    const input = document.createElement('input');
    input.setAttribute('aria-label', '确认文字');
    const submit = node('button', '', '执行');
    const cancel = node('button', '', '取消');
    cancel.addEventListener('click', () => panel.remove());
    submit.addEventListener('click', async () => {
      if (input.value !== 'CONFIRM') { panel.append(node('p', 'empty', '请输入 CONFIRM')); return; }
      submit.disabled = true;
      try {
        const body = {action, story_id: selectedStory, item_id: itemId};
        const challenge = await bridge.apiPost('challenge', body);
        if (challenge.error) throw new Error(challenge.error);
        const result = await bridge.apiPost('action', {token: challenge.token, confirmation: 'CONFIRM'});
        if (result.error) throw new Error(result.error);
        await refresh();
        if (action === 'story_purge') content.prepend(node('p', 'empty', `故事「${selectedStory}」已删除。`));
      } catch (error) {
        panel.append(node('p', 'empty', `操作失败：${error.message}`));
        submit.disabled = false;
      }
    });
    panel.append(input, submit, cancel);
    content.prepend(panel);
    input.focus();
  });
  box.append(button);
}
async function refresh(preferredStory = '') {
  content.replaceChildren(node('p', 'empty', '加载中…'));
  try {
    const stories = await bridge.apiGet('stories');
    const selected = preferredStory || selector.value;
    selector.replaceChildren();
    for (const story of stories.stories || []) {
      const option = document.createElement('option');
      option.value = story.id;
      const configured = stories.configured_story_id || 'default';
      if (story.id === configured) option.textContent = `${story.id} · 共享故事${stories.shared_story ? '（当前模式）' : '（历史）'}`;
      else if (story.id.startsWith(`${configured}:`)) option.textContent = `${story.id} · 独立会话故事${stories.shared_story ? '（历史）' : '（当前模式）'}`;
      else option.textContent = `${story.id} · 旧 Story ID`;
      selector.append(option);
    }
    if (selected && [...selector.options].some(option => option.value === selected)) selector.value = selected;
    if (!selector.value) { content.replaceChildren(node('p', 'empty', '暂无故事，可在故事总览页恢复备份。')); return; }
    const data = await bridge.apiGet('snapshot', {story_id: selector.value});
    if (data.error) throw new Error(data.error);
    timeZone = data.timezone || 'Asia/Shanghai';
    content.replaceChildren();
    if (view === 'dashboard') {
      card('当前故事', `上次推进：${formatTime(data.story.cursor)}`, `Revision ${data.story.revision} · ${data.story.paused ? '已暂停' : '运行中'}`);
      card('当前时间', formatTime(data.current_time) || '未知');
      card('调度器', data.scheduler?.running ? '运行中' : '已停止', data.scheduler?.last_error || '无最近错误');
      const state = JSON.parse(data.story.state_json || '{}');
      card('角色状态', `地点：${state.location || '未知'} · 活动：${state.activity || '未知'}`, state.world_state || '');
      card('当前场景', data.scene?.summary || '尚无摘要', formatTime(data.scene?.last_activity_at));
      card('待处理意图', String((data.intents || []).filter(x => x.status === 'pending').length));
      for (const item of (data.entries || []).slice(-8).reverse()) card(item.event_type, item.content, formatTime(item.occurred_at));
    } else if (view === 'story') {
      card('当前场景', data.scene?.summary || '尚无摘要', formatTime(data.scene?.last_activity_at));
      const actions = card('故事维护', '重置会清空当前剧情但保留角色与参与者；永久删除会移除整个故事。');
      addAction(actions, '重置故事', 'story_reset');
      addAction(actions, '永久删除故事', 'story_purge');
      for (const item of (data.entries || []).slice().reverse()) card(item.event_type, item.content, formatTime(item.occurred_at));
    } else if (view === 'memory') {
      for (const item of data.facts || []) {
        const box = card(item.scope, item.content, item.status);
        if (item.status === 'active') addAction(box, '归档记忆', 'memory_delete', item.id);
      }
      for (const item of data.overlays || []) card(`Overlay · ${item.scope}`, item.content, item.status);
      for (const item of data.perspectives || []) card('Perspective', item.content, item.status);
      for (const item of data.relationships || []) card(`Relationship · ${item.participant_id}`, item.summary || '暂无文字摘要');
      if ((data.overlays || []).some(x => x.status === 'active')) {
        addAction(card('演化层维护', '清除所有 Overlay 与 Perspective。'), '清除演化层', 'overlay_clear');
      }
    } else {
      for (const item of data.schedules || []) card(item.kind, item.content, `${formatTime(item.start_at)} → ${formatTime(item.end_at)}`);
      for (const item of data.intents || []) card(item.type, item.content, `${item.due_at} · ${item.status}`);
      for (const item of data.jobs || []) card(`任务 · ${item.kind}`, item.id, `${item.due_at} · ${item.status}`);
    }
    if (!content.children.length) content.append(node('p', 'empty', '暂无记录'));
    applyFilter();
  } catch (error) { content.replaceChildren(node('p', 'empty', `加载失败：${error.message}`)); }
}
function applyFilter() {
  const term = document.getElementById('filter').value.trim().toLowerCase();
  for (const box of content.querySelectorAll('.card')) box.hidden = !!term && !box.textContent.toLowerCase().includes(term);
}
document.getElementById('refresh').addEventListener('click', () => refresh());
selector.addEventListener('change', () => refresh());
document.getElementById('filter').addEventListener('input', applyFilter);
await refresh();
