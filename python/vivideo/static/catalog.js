(() => {
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
let cache = {assets:[], jobs:[]};
let lastData;
let lastMarkup = '';
const catalogVideos = new Map();
let tagsReady = false;
let tagRequest;
let videoRequest;
let writing = false;
let writeVersion=0;
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
  const origin = spaceId;
  const url = '/api/spaces/' + origin + path;
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
  if (origin !== spaceId) throw new DOMException('Space changed', 'AbortError');
  if (response.status === 204) return;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : '请求失败（' + response.status + '）。');
  }
  if (origin !== spaceId) throw new DOMException('Space changed', 'AbortError');
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
  if (!choices.length) container.append(element('span', container.id==='filter-tags'?'添加标签，慢慢建立你的内容地图。':'还没有标签，可以先创建一个。', 'catalog-tags-empty'));
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
  for (const id of ['create-button', 'confirm-tag-action', 'cancel-tag-action',
                    'save-video-tags', 'cancel-video-action', 'clear-tags', 'rename-tag', 'new-tag',
                    'edit-tag-fields']) {
    $(id).disabled = writing;
  }
  $('managed-tags').querySelectorAll('button').forEach(node => { node.disabled = writing; });
  $('library-grid').querySelectorAll('button').forEach(node => { node.disabled = writing || !tagsReady; });
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
  if(!tagsReady)$('tag-status').textContent = '正在加载标签…';
  try {
    const data = await request('/video-tags', { signal: controller.signal });
    if (controller.signal.aborted) return;
    const unchanged=tagsReady&&JSON.stringify(tags)===JSON.stringify(data.items);
    tags = data.items;
    tagsReady = true;
    if(unchanged)return;
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
  lastData=data;
  catalogVideos.clear();
  const markup=data.items.map(item => {
    const asset={pinned:item.pinned,bytes:item.bytes};
    const job=cache.jobs.find(j=>j.kind==='download'&&j.target===item.video_id&&['queued','running'].includes(j.status));
    const latest=cache.jobs.find(j=>j.kind==='download'&&j.target===item.video_id);
    const video={id:item.video_id,title:item.title,author:item.author,url:item.url,summary:'',local:item.cache_state!=='missing',cache_state:item.cache_state,color:spaceId==='learning'?'mint':'blue',duration_seconds:item.duration_seconds};
    catalogVideos.set(video.id,video);
    const id=escapeHTML(video.id);
    const cover=item.cache_state==='ready'?coverMarkup(video):`<div class="cover catalog-placeholder ${video.color}"><span>▷</span><small>${escapeHTML(cacheLabels[item.cache_state]||item.cache_state)}</small></div>`;
    const labels=item.tags.map(t=>`<span>${escapeHTML(t.name)}</span>`).join('');
    const failure=item.cache_state==='failed'&&latest?.error?`<p class="catalog-failure">${escapeHTML(latest.error)}</p>`:'';
    return `<article class="card catalog-card" data-catalog-id="${id}">${cover}<div class="card-body"><h3>${escapeHTML(item.title)}</h3><div class="author">${escapeHTML(item.author||'未知作者')}</div><div class="card-tags"><span>${escapeHTML(cacheLabels[item.cache_state]||item.cache_state)}</span>${asset?.bytes?`<span>${formatBytes(asset.bytes)}</span>`:''}${labels}</div>${failure}<div class="card-actions"><button type="button" data-action="save" data-id="${id}" class="${saved[video.id]?'selected':''}">${saved[video.id]?'★ 已收藏':'☆ 收藏'}</button><button type="button" data-video-note="${id}">笔记</button><button type="button" data-edit-tags="${id}">编辑标签</button>${item.cache_state==='ready'?`<button type="button" data-catalog-action="pin" data-id="${id}">${asset?.pinned?'取消保留':'保留缓存'}</button>`:''}${job?`<button type="button" data-catalog-action="cancel" data-id="${id}">取消下载</button>`:['missing','failed'].includes(item.cache_state)?`<button type="button" data-catalog-action="retry" data-id="${id}">重新下载</button>`:''}<button type="button" class="catalog-delete" data-catalog-action="delete" data-id="${id}">删除</button></div></div></article>`;
  }).join('')||'<div class="empty">还没有符合条件的视频。<br>调整筛选条件，或在下方粘贴视频链接开始下载。</div>';
  if(markup!==lastMarkup){$('library-grid').innerHTML=markup;lastMarkup=markup;hydrateCovers($('library-grid'));}
  const pages=Math.ceil(data.total/pageSize), current=Math.floor(offset/pageSize)+1;
  $('video-status').textContent=data.total?'共 '+data.total+' 个视频':'暂无视频';
  document.getElementById('saved-count').textContent=data.total;
  $('page-status').textContent='第 '+current+' / '+Math.max(1,pages)+' 页';
  $('pagination').hidden=pages<2;
  $('previous').disabled=current<=1;$('next').disabled=current>=pages;
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
  if(!lastData)$('video-status').textContent = '正在加载视频…';
  $('load-error').hidden = true;
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

async function refresh({force=false}={}) {
  if(!force&&(writing||$('tag-dialog').open||$('video-dialog').open||$('catalog-manager').open||document.getElementById('video-notes-dialog').open))return;
  const space = spaceId;
  const pending = loadTags();
  const controller = tagRequest;
  await pending;
  if (spaceId === space && !controller.signal.aborted) await loadVideos();
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
  const generation=++writeVersion;
  writing = true;
  syncButtons();
  try {
    await operation();
    await refresh({force:true});
  } catch (error) {
    if(error.name!=='AbortError')showError(errorId, error.message);
  } finally {
    if(generation===writeVersion){writing = false;syncButtons();}
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

function resetSpace() {
  ++writeVersion;writing=false;
  videoRequest?.abort();tagRequest?.abort();
  $('tag-dialog').close();$('video-dialog').close();$('catalog-manager').close();
  catalogVideos.clear();lastData=null;lastMarkup='';cache={assets:[],jobs:[]};
  $('library-grid').replaceChildren();
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
  $('pagination').hidden = true;
  $('load-error').hidden = true;
  if(!lastData)$('video-status').textContent = '正在加载视频…';
  showError('create-error');
  showError('filter-error');
  renderManagedTags();
  renderFilterTags();
  refresh();
 }

$('library-grid').addEventListener('click', async event => {
  const editor=event.target.closest('[data-edit-tags]');
  if(editor){const item=lastData?.items.find(v=>v.video_id===editor.dataset.editTags);if(item)openVideoEditor(item);return;}
  const button=event.target.closest('[data-catalog-action]');if(!button||writing)return;
  const id=button.dataset.id,action=button.dataset.catalogAction,origin=spaceId;
  if(action==='delete'&&!confirm('删除此视频并清理对应缓存？笔记和观看历史会保留。'))return;
  const generation=++writeVersion;writing=true;syncButtons();
  try{
    if(action==='delete'){
      await request('/video-catalog/'+encodeURIComponent(id),{method:'DELETE'});
      delete saved[id];persist('saved',saved);localCacheVideos.delete(id);coverCache.delete(origin+'/'+id);
      for(const turn of recommendationTurns)turn.videos=(turn.videos||[]).filter(v=>v.id!==id);
      videos=videos.filter(v=>v.id!==id);restoreConversation();
    }else if(action==='pin'){
      const asset=lastData.items.find(v=>v.video_id===id);
      await request('/local-videos/'+encodeURIComponent(id),{method:'PATCH',body:{pinned:!asset?.pinned}});
    }else if(action==='retry')await request('/local-videos/'+encodeURIComponent(id)+'/download',{method:'POST'});
    else {
      const job=cache.jobs.find(j=>j.target===id&&j.kind==='download'&&['queued','running'].includes(j.status));
      if(job)await request('/download-jobs/'+job.id+'/cancel',{method:'POST'});
    }
    if(origin===spaceId){await refresh({force:true});toast(action==='delete'?'视频与缓存已移除':'已更新');}
  }catch(error){if(origin===spaceId&&error.name!=='AbortError')toast(error.message);}
  finally{if(generation===writeVersion){writing=false;syncButtons();}if(origin===spaceId)refreshLocalCache();}
});
$('manage-catalog-tags').onclick=()=>{$('catalog-manager').showModal();$('new-tag').focus();};
$('close-catalog-manager').onclick=()=>$('catalog-manager').close();
$('catalog-manager').addEventListener('cancel',event=>{if(writing)event.preventDefault();});
$('open-library-download').onclick=()=>{$('local-library').open=true;$('import-video-url').focus();};
window.videoCatalog={refresh,setSpace:resetSpace,setCache(value){cache=value;if(lastData)renderVideos(lastData);},rerender(){if(lastData)renderVideos(lastData);},find(id){return catalogVideos.get(id);}};
renderFilterTags();
})();
