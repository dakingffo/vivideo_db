'use strict';

const $ = id => document.getElementById(id);
const pageSize = 20;
const maxTags = 20;
const cacheLabels = {
  missing: '未缓存',
  queued: '排队中',
  downloading: '下载中',
  processing: '处理中',
  ready: '已就绪',
  failed: '失败',
};

let tags = [];
let tagsReady = false;
let tagRequest;
let videoRequest;
let writing = false;
let offset = 0;
let query = '';
let tagAction;
let videoEdit;
const selectedTags = new Set();

function element(type, text, className) {
  const node = document.createElement(type);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action) {
  const node = element('button', text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function showError(id, message = '') {
  $(id).textContent = message;
  $(id).hidden = !message;
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const url = '/api/spaces/' + $('space').value + path;
  let response;
  try {
    response = await fetch(url, {
      method,
      signal,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('连接失败，请检查网络后重试。');
  }
  if (response.status === 204) return;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : '请求失败（' + response.status + '）。');
  }
  return data;
}

function renderChoices(container, available, selected, onChange) {
  const choices = available.map(tag => {
    const label = element('label', undefined, 'choice');
    const input = element('input');
    input.type = 'checkbox';
    input.checked = selected.has(tag.id);
    input.addEventListener('change', () => onChange(tag.id, input));
    label.append(input, element('span', tag.name));
    return label;
  });
  container.replaceChildren(...choices);
  if (!choices.length) container.append(element('p', '暂无标签。', 'status'));
}

function updateSelection(selection, id, input, errorId) {
  if (input.checked && selection.size >= maxTags) {
    input.checked = false;
    showError(errorId, '最多可选择 20 个标签。');
    return false;
  }
  showError(errorId);
  if (input.checked) selection.add(id);
  else selection.delete(id);
  return true;
}

function renderFilterTags() {
  renderChoices($('filter-tags'), tags, selectedTags, (id, input) => {
    if (!updateSelection(selectedTags, id, input, 'filter-error')) return;
    if (input.checked) $('untagged').checked = false;
    filterChanged();
  });
  $('tag-mode').disabled = selectedTags.size === 0;
}

function syncButtons() {
  for (const id of ['space', 'create-button', 'confirm-tag-action', 'cancel-tag-action',
                    'save-video-tags', 'cancel-video-action', 'clear-tags', 'rename-tag', 'new-tag',
                    'edit-tag-fields']) {
    $(id).disabled = writing;
  }
  $('managed-tags').querySelectorAll('button').forEach(node => { node.disabled = writing; });
  $('videos').querySelectorAll('button').forEach(node => { node.disabled = writing || !tagsReady; });
}

function renderManagedTags() {
  $('managed-tags').replaceChildren(...tags.map(tag => {
    const row = element('li');
    const description = element('div', undefined, 'tag-description');
    description.append(element('strong', tag.name), element('small', tag.video_count + ' 个视频'));
    const actions = element('div', undefined, 'tag-actions');
    actions.append(
      button('改名', () => openTagAction('rename', tag)),
      button('删除', () => openTagAction('delete', tag)),
    );
    row.append(description, actions);
    return row;
  }));
}

async function loadTags() {
  tagRequest?.abort();
  const controller = new AbortController();
  tagRequest = controller;
  $('tag-status').textContent = '正在加载标签…';
  try {
    const data = await request('/video-tags', { signal: controller.signal });
    if (controller.signal.aborted) return;
    tags = data.items;
    tagsReady = true;
    const available = new Set(tags.map(tag => tag.id));
    for (const id of selectedTags) {
      if (!available.has(id)) selectedTags.delete(id);
    }
    $('tag-status').textContent = tags.length ? '' : '还没有标签。';
    renderManagedTags();
    renderFilterTags();
    syncButtons();
  } catch (error) {
    if (controller.signal.aborted) return;
    $('tag-status').replaceChildren(
      document.createTextNode('标签加载失败。'),
      button('重试', refresh),
    );
  }
}

function renderVideos(data) {
  $('videos').replaceChildren(...data.items.map(video => {
    const row = element('li', undefined, 'video-row');
    const info = element('div', undefined, 'video-information');
    info.append(element('h3', video.title, 'video-title'));
    if (video.author) info.append(element('p', video.author, 'video-meta'));
    const badges = element('div', undefined, 'badges');
    badges.append(element('span', cacheLabels[video.cache_state] ?? video.cache_state,
      'badge cache ' + video.cache_state));
    for (const tag of video.tags) badges.append(element('span', tag.name, 'badge'));
    info.append(badges);
    row.append(info, button('编辑标签', () => openVideoEditor(video)));
    return row;
  }));
  const pages = Math.ceil(data.total / pageSize);
  const page = Math.floor(offset / pageSize) + 1;
  $('video-status').textContent = data.total ? '共 ' + data.total + ' 个视频' : '没有符合条件的视频。';
  $('page-status').textContent = '第 ' + page + ' / ' + pages + ' 页';
  $('pagination').hidden = pages < 2;
  $('previous').disabled = page <= 1;
  $('next').disabled = page >= pages;
  syncButtons();
}

async function loadVideos() {
  videoRequest?.abort();
  const controller = new AbortController();
  videoRequest = controller;
  const params = new URLSearchParams({
    q: query, limit: pageSize, offset, tag_mode: $('tag-mode').value,
  });
  if ($('cache-state').value) params.set('cache_state', $('cache-state').value);
  if ($('untagged').checked) params.set('untagged', 'true');
  for (const id of selectedTags) params.append('tag_ids', id);
  $('video-status').textContent = '正在加载视频…';
  $('load-error').hidden = true;
  $('pagination').hidden = true;
  $('videos').replaceChildren();
  try {
    const data = await request('/video-catalog?' + params, { signal: controller.signal });
    if (controller.signal.aborted) return;
    if (offset > 0 && offset >= data.total) {
      offset = Math.floor(Math.max(0, data.total - 1) / pageSize) * pageSize;
      return loadVideos();
    }
    renderVideos(data);
  } catch (error) {
    if (controller.signal.aborted) return;
    $('video-status').textContent = '';
    $('load-error-message').textContent = error.message;
    $('load-error').hidden = false;
  }
}

async function refresh() {
  const space = $('space').value;
  const pending = loadTags();
  const controller = tagRequest;
  await pending;
  if ($('space').value === space && !controller.signal.aborted) await loadVideos();
}

function filterChanged() {
  offset = 0;
  $('tag-mode').disabled = selectedTags.size === 0;
  loadVideos();
}

function openTagAction(kind, tag) {
  tagAction = { kind, tag };
  const renaming = kind === 'rename';
  $('tag-dialog-title').textContent = renaming ? '重命名标签' : '删除标签';
  $('rename-field').hidden = !renaming;
  $('rename-tag').required = renaming;
  $('rename-tag').value = tag.name;
  $('delete-message').hidden = renaming;
  $('delete-message').textContent = '确认删除「' + tag.name + '」？已有视频将移除此标签。';
  $('confirm-tag-action').textContent = renaming ? '保存名称' : '确认删除';
  $('confirm-tag-action').className = renaming ? 'primary' : 'danger';
  showError('tag-action-error');
  $('tag-dialog').showModal();
  $(renaming ? 'rename-tag' : 'cancel-tag-action').focus();
}

function renderVideoChoices() {
  renderChoices($('video-tag-choices'), tags, videoEdit.selected, (id, input) => {
    updateSelection(videoEdit.selected, id, input, 'video-action-error');
  });
}

function openVideoEditor(video) {
  videoEdit = { id: video.video_id, selected: new Set(video.tags.map(tag => tag.id)) };
  $('video-dialog-name').textContent = video.title;
  showError('video-action-error');
  renderVideoChoices();
  $('video-dialog').showModal();
}

function readName(input) {
  const name = input.value.trim();
  const length = [...name].length;
  if (length < 1 || length > 30) throw new Error('标签名称需要 1–30 个字符。');
  return name;
}

async function write(errorId, operation) {
  if (writing) return;
  showError(errorId);
  writing = true;
  syncButtons();
  try {
    await operation();
    await refresh();
  } catch (error) {
    showError(errorId, error.message);
  } finally {
    writing = false;
    syncButtons();
  }
}

$('create-tag').addEventListener('submit', event => {
  event.preventDefault();
  write('create-error', async () => {
    await request('/video-tags', { method: 'POST', body: { name: readName($('new-tag')) } });
    $('new-tag').value = '';
  });
});

$('tag-action-form').addEventListener('submit', event => {
  event.preventDefault();
  write('tag-action-error', async () => {
    const path = '/video-tags/' + tagAction.tag.id;
    if (tagAction.kind === 'rename') {
      await request(path, { method: 'PATCH', body: { name: readName($('rename-tag')) } });
    } else {
      await request(path, { method: 'DELETE' });
      if (selectedTags.delete(tagAction.tag.id)) offset = 0;
      renderFilterTags();
    }
    $('tag-dialog').close();
  });
});

$('edit-video').addEventListener('submit', event => {
  event.preventDefault();
  write('video-action-error', async () => {
    await request('/video-catalog/' + encodeURIComponent(videoEdit.id) + '/tags', {
      method: 'PUT', body: { tag_ids: [...videoEdit.selected] },
    });
    $('video-dialog').close();
  });
});

$('clear-tags').addEventListener('click', () => {
  videoEdit.selected.clear();
  showError('video-action-error');
  renderVideoChoices();
});
for (const [dialog, cancel] of [['tag-dialog', 'cancel-tag-action'], ['video-dialog', 'cancel-video-action']]) {
  $(cancel).addEventListener('click', () => $(dialog).close());
  $(dialog).addEventListener('cancel', event => { if (writing) event.preventDefault(); });
}

$('search-form').addEventListener('submit', event => {
  event.preventDefault();
  query = $('search').value.trim();
  filterChanged();
});
$('untagged').addEventListener('change', () => {
  if ($('untagged').checked) selectedTags.clear();
  showError('filter-error');
  renderFilterTags();
  filterChanged();
});
$('tag-mode').addEventListener('change', filterChanged);
$('cache-state').addEventListener('change', filterChanged);
$('retry').addEventListener('click', refresh);
$('previous').addEventListener('click', () => { offset -= pageSize; loadVideos(); });
$('next').addEventListener('click', () => { offset += pageSize; loadVideos(); });

$('space').addEventListener('change', () => {
  videoRequest?.abort();
  tags = [];
  tagsReady = false;
  selectedTags.clear();
  query = '';
  offset = 0;
  $('search').value = '';
  $('cache-state').value = '';
  $('tag-mode').value = 'all';
  $('untagged').checked = false;
  $('new-tag').value = '';
  $('videos').replaceChildren();
  $('pagination').hidden = true;
  $('load-error').hidden = true;
  $('video-status').textContent = '正在加载视频…';
  showError('create-error');
  showError('filter-error');
  renderManagedTags();
  renderFilterTags();
  refresh();
});

renderFilterTags();
refresh();
