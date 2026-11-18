'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const limit = 10;
  const state = {space: 'learning', videos: [], note: null, editing: false,
    busy: false, loading: false, conflicted: false, offset: 0, total: 0,
    historyOffset: 0, historyTotal: 0, listSeq: 0, videoSeq: 0, editSeq: 0,
    historySeq: 0, listLoading: false, historyLoading: false, failedNote: null};
  let confirmResolve = null;
  const url = (space, path) => `/api/spaces/${space}${path}`;
  const text = (tag, value, className) => {
    const el = document.createElement(tag);
    el.textContent = value;
    if (className) el.className = className;
    return el;
  };
  const date = value => new Date(value).toLocaleString('zh-CN', {hour12: false});
  const message = (id, value = '', error = false) => {
    $(id).textContent = value;
    $(id).classList.toggle('error', error);
  };
  async function request(space, path, method = 'GET', body) {
    const response = await fetch(url(space, path), {
      method, headers: body === undefined ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}），请检查输入后重试`);
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }
  function dirty() {
    if (!state.editing) return false;
    return $('title').value !== (state.note?.title ?? '') ||
      $('content').value !== (state.note?.content ?? '');
  }
  function confirmAction(prompt) {
    if (confirmResolve) return Promise.resolve(false);
    $('confirm-message').textContent = prompt;
    $('confirm-dialog').showModal();
    $('confirm-no').focus();
    return new Promise(resolve => { confirmResolve = resolve; });
  }
  function finishConfirm(accepted) {
    $('confirm-dialog').close();
    const resolve = confirmResolve;
    confirmResolve = null;
    resolve?.(accepted);
  }
  async function canLeave() {
    if (state.busy) return false;
    return !dirty() || await confirmAction('有尚未保存的内容，确认放弃并继续吗？');
  }
  function controls() {
    $('save').disabled = state.busy || state.loading || state.conflicted;
    $('delete').disabled = state.busy || !state.note;
    $('delete').hidden = !state.note;
    $('cancel').disabled = state.busy;
    $('reload').disabled = state.busy;
    $('space').disabled = state.busy;
    $('new').disabled = state.busy || !state.videos.length;
    $('title').disabled = $('content').disabled = state.busy || state.loading;
    $('video').disabled = state.busy || !!state.note;
    $('prev').disabled = state.listLoading || state.offset === 0;
    $('next').disabled = state.listLoading || state.offset + limit >= state.total;
    $('history-prev').disabled = state.historyLoading || state.historyOffset === 0;
    $('history-next').disabled = state.historyLoading || state.historyOffset + limit >= state.historyTotal;
  }
  function resetEditor() {
    state.editSeq++;
    state.historySeq++;
    state.note = null;
    state.editing = state.loading = state.conflicted = false;
    state.failedNote = null;
    $('editor').hidden = $('history').hidden = $('conflict').hidden = true;
    $('retry-note').hidden = true;
    $('editor-empty').hidden = false;
    $('title').value = $('content').value = '';
    message('editor-status');
    markSelected();
    controls();
  }
  function markSelected() {
    for (const card of $('notes').children) card.setAttribute('aria-current', String(card.dataset.id === state.note?.id));
  }
  function videoOption(video) {
    const option = text('option', `${video.title || video.id} · ${video.url}`);
    option.value = video.id;
    return option;
  }
  async function loadVideos() {
    const space = state.space, seq = ++state.videoSeq;
    $('retry-videos').hidden = true;
    message('videos-status', '正在加载视频…');
    try {
      const data = await request(space, '/videos');
      if (space !== state.space || seq !== state.videoSeq) return;
      state.videos = data.videos;
      $('filter').replaceChildren(text('option', '全部视频'));
      $('filter').firstChild.value = '';
      $('video').replaceChildren();
      for (const video of state.videos) {
        $('filter').append(videoOption(video));
        $('video').append(videoOption(video));
      }
      if (state.note) setEditorVideo(state.note);
      message('videos-status', state.videos.length ? '' : '当前空间没有视频，暂时无法新建笔记。');
    } catch (error) {
      if (space !== state.space || seq !== state.videoSeq) return;
      message('videos-status', `视频加载失败：${error.message}`, true);
      $('retry-videos').hidden = false;
    }
    controls();
  }
  async function loadList() {
    const space = state.space, seq = ++state.listSeq;
    const params = new URLSearchParams({q: $('search').value.trim(), limit, offset: state.offset});
    if ($('filter').value) params.set('video_id', $('filter').value);
    state.listLoading = true;
    controls();
    $('retry-list').hidden = true;
    message('list-status', '正在加载笔记…');
    try {
      const data = await request(space, `/notes?${params}`);
      if (space !== state.space || seq !== state.listSeq) return;
      state.total = data.total;
      if (state.offset > 0 && state.offset >= data.total) {
        state.offset = Math.max(0, Math.ceil(data.total / limit) - 1) * limit;
        return loadList();
      }
      $('notes').replaceChildren();
      for (const note of data.items) {
        const card = text('button', '', 'note-card');
        card.dataset.id = note.id;
        card.append(text('strong', note.title), text('p', note.excerpt || '（空正文）'),
          text('small', note.video_title), text('small', `版本 ${note.version} · ${date(note.updated)}`));
        card.addEventListener('click', async () => { if (await canLeave()) openNote(note.id); });
        $('notes').append(card);
      }
      message('list-status', data.total ? `共 ${data.total} 条笔记` : '没有找到笔记。');
      $('page').textContent = data.total ? `${Math.floor(state.offset / limit) + 1} / ${Math.ceil(data.total / limit)}` : '0 / 0';
      markSelected();
    } catch (error) {
      if (space !== state.space || seq !== state.listSeq) return;
      message('list-status', `笔记加载失败：${error.message}`, true);
      $('retry-list').hidden = false;
    } finally {
      if (space === state.space && seq === state.listSeq) { state.listLoading = false; controls(); }
    }
  }
  function setEditorVideo(note) {
    // Existing notes can refer to a video beyond the existing API's 100-item list.
    if (![...$('video').options].some(option => option.value === note.video_id)) {
      const option = text('option', note.video_title);
      option.value = note.video_id;
      $('video').append(option);
    }
    $('video').value = note.video_id;
  }
  function showNote(note) {
    state.note = note;
    state.editing = true;
    state.conflicted = false;
    state.failedNote = null;
    $('retry-note').hidden = true;
    $('editor-empty').hidden = true;
    $('conflict').hidden = true;
    $('editor').hidden = false;
    $('title').value = note.title;
    $('content').value = note.content;
    setEditorVideo(note);
    $('meta').textContent = `${note.video_title} · 版本 ${note.version} · 更新于 ${date(note.updated)}`;
    markSelected();
    controls();
  }
  async function openNote(id) {
    resetEditor();
    const space = state.space, seq = ++state.editSeq;
    state.loading = true;
    message('editor-status', '正在打开笔记…');
    controls();
    try {
      const note = await request(space, `/notes/${id}`);
      if (space !== state.space || seq !== state.editSeq) return;
      showNote(note);
      message('editor-status');
      state.historyOffset = 0;
      loadHistory();
    } catch (error) {
      if (space !== state.space || seq !== state.editSeq) return;
      message('editor-status', error.message, true);
      state.failedNote = id;
      $('retry-note').hidden = false;
    } finally {
      if (space === state.space && seq === state.editSeq) { state.loading = false; controls(); }
    }
  }
  async function loadHistory() {
    if (!state.note) return;
    const space = state.space, id = state.note.id, seq = ++state.historySeq;
    state.historyLoading = true;
    $('history').hidden = false;
    $('revisions').replaceChildren();
    message('history-status', '正在加载历史…');
    controls();
    try {
      const data = await request(space, `/notes/${id}/revisions?limit=${limit}&offset=${state.historyOffset}`);
      if (space !== state.space || id !== state.note?.id || seq !== state.historySeq) return;
      state.historyTotal = data.total;
      for (const revision of data.items) {
        const details = text('details', '', 'revision');
        details.append(text('summary', `版本 ${revision.version} · ${date(revision.saved)}`),
          text('h3', revision.title), text('pre', revision.content || '（空正文）'));
        $('revisions').append(details);
      }
      message('history-status', `共 ${data.total} 个版本，点击展开纯文本快照。`);
      $('history-page').textContent = `${Math.floor(state.historyOffset / limit) + 1} / ${Math.max(1, Math.ceil(data.total / limit))}`;
    } catch (error) {
      if (space !== state.space || id !== state.note?.id || seq !== state.historySeq) return;
      message('history-status', `历史加载失败：${error.message}，可点击刷新历史重试。`, true);
    } finally {
      if (space === state.space && id === state.note?.id && seq === state.historySeq) { state.historyLoading = false; controls(); }
    }
  }
  $('confirm-yes').onclick = () => finishConfirm(true);
  $('confirm-no').onclick = () => finishConfirm(false);
  $('confirm-dialog').addEventListener('cancel', event => { event.preventDefault(); finishConfirm(false); });
  $('space').addEventListener('change', async () => {
    const target = $('space').value;
    $('space').value = state.space;
    if (!await canLeave()) return;
    state.space = target;
    $('space').value = target;
    resetEditor();
    state.videos = [];
    state.offset = state.total = 0;
    $('search').value = '';
    $('filter').replaceChildren(text('option', '全部视频'));
    $('filter').firstChild.value = '';
    $('video').replaceChildren();
    $('notes').replaceChildren();
    controls();
    loadVideos();
    loadList();
  });
  $('new').onclick = async () => {
    if (!await canLeave()) return;
    resetEditor();
    state.editing = true;
    $('editor').hidden = false;
    $('editor-empty').hidden = true;
    $('meta').textContent = '新笔记 · 保存后将创建版本 1';
    $('video').replaceChildren(...state.videos.map(videoOption));
    controls();
    $('title').focus();
  };
  $('editor').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy || state.loading || state.conflicted) return;
    if (!$('title').value.trim()) { message('editor-status', '标题不能为空或只有空白。', true); return; }
    const space = state.space, seq = state.editSeq, note = state.note;
    const body = {title: $('title').value, content: $('content').value};
    if (note) body.expected_version = note.version;
    else body.video_id = $('video').value;
    state.busy = true;
    controls();
    message('editor-status', '正在保存…');
    try {
      const saved = await request(space, note ? `/notes/${note.id}` : '/notes', note ? 'PUT' : 'POST', body);
      if (space !== state.space || seq !== state.editSeq) return;
      showNote(saved);
      message('editor-status', `已保存版本 ${saved.version}`);
      state.historyOffset = 0;
      loadHistory();
      loadList();
    } catch (error) {
      if (space !== state.space || seq !== state.editSeq) return;
      if (error.status === 409) { state.conflicted = true; $('conflict').hidden = false; }
      message('editor-status', error.message, true);
    } finally { state.busy = false; controls(); }
  });
  $('cancel').onclick = async () => {
    if (!await canLeave()) return;
    if (state.note) { showNote(state.note); message('editor-status', '已取消未保存的修改。'); }
    else resetEditor();
  };
  $('reload').onclick = async () => { if (await canLeave()) openNote(state.note.id); };
  $('delete').onclick = async () => {
    if (state.busy || !state.note || !await confirmAction('确认删除此笔记及全部修改历史？未保存的内容也会丢弃。')) return;
    const space = state.space, id = state.note.id;
    state.busy = true;
    controls();
    try {
      await request(space, `/notes/${id}`, 'DELETE');
      if (space !== state.space || id !== state.note?.id) return;
      resetEditor();
      message('editor-status', '笔记及其修改历史已删除。');
      loadList();
    } catch (error) { message('editor-status', error.message, true); }
    finally { state.busy = false; controls(); }
  };
  $('search-form').onsubmit = event => { event.preventDefault(); state.offset = 0; loadList(); };
  $('filter').onchange = () => { state.offset = 0; loadList(); };
  $('prev').onclick = () => { state.offset = Math.max(0, state.offset - limit); loadList(); };
  $('next').onclick = () => { state.offset += limit; loadList(); };
  $('history-prev').onclick = () => { state.historyOffset = Math.max(0, state.historyOffset - limit); loadHistory(); };
  $('history-next').onclick = () => { state.historyOffset += limit; loadHistory(); };
  $('refresh-history').onclick = () => loadHistory();
  $('retry-videos').onclick = () => loadVideos();
  $('retry-list').onclick = () => loadList();
  $('retry-note').onclick = () => { if (state.failedNote) openNote(state.failedNote); };
  window.addEventListener('beforeunload', event => { if (dirty() || state.busy) { event.preventDefault(); event.returnValue = ''; } });
  loadVideos();
  loadList();
})();
