let controller = null;
let totalPages = 1;
let currentPage = 1;
const pageSize = 20;
let currentSpace = 'learning';
let currentFromMs = null;
let currentToMs = null;

document.addEventListener('DOMContentLoaded', () => {
    initDates();
    bindEvents();
    fetchData();
});

function initDates() {
    const today = new Date();
    const sixDaysAgo = new Date(today);
    sixDaysAgo.setDate(today.getDate() - 6);
    
    document.getElementById('start-date').value = formatDate(sixDaysAgo);
    document.getElementById('end-date').value = formatDate(today);
}

function bindEvents() {
    document.getElementById('btn-query').addEventListener('click', () => { currentPage = 1; fetchData(); });
    document.getElementById('btn-search').addEventListener('click', () => { currentPage = 1; fetchData(); });
    document.getElementById('space-select').addEventListener('change', (e) => { currentSpace = e.target.value; currentPage = 1; fetchData(); });
    document.getElementById('btn-prev').addEventListener('click', () => { if(currentPage > 1) { currentPage--; fetchData(); } });
    document.getElementById('btn-next').addEventListener('click', () => { currentPage++; fetchData(); });
}

function formatDate(date) {
    return new Date(date.getTime() + 8 * 3600000).toISOString().split('T')[0];
}

function formatSeconds(sec) {
    if (sec == null || isNaN(sec)) return '00:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function formatDateTime(ms) {
    const d = new Date(ms);
    return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
}

function getMsFromDate(dateStr, isEnd) {
    const d = new Date(dateStr + 'T00:00:00+08:00');
    return d.getTime() + (isEnd ? 86400000 : 0);
}

function toggleButtons(disabled) {
    document.querySelectorAll('button, input, select').forEach(el => el.disabled = disabled);
}

async function fetchData() {
    if (controller) controller.abort();
    controller = new AbortController();
    const request = controller;

    const startDate = document.getElementById('start-date').value;
    const endDate = document.getElementById('end-date').value;
    const keyword = document.getElementById('keyword-input').value.trim();

    currentFromMs = getMsFromDate(startDate, false);
    currentToMs = getMsFromDate(endDate, true);

    toggleButtons(true);

    try {
        await Promise.all([
            fetchStats(),
            fetchDaily(),
            fetchHistory(keyword)
        ]);
    } catch (err) {
        if (err.name !== 'AbortError') alert(err.message);
    } finally {
        if (request === controller) {
            toggleButtons(false);
            document.getElementById('btn-prev').disabled = currentPage <= 1;
            document.getElementById('btn-next').disabled = currentPage >= totalPages;
        }
    }
}

async function fetchJson(url) {
    const request = controller;
    const res = await fetch(url, { signal: request.signal });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(err.detail);
    }
    const data = await res.json();
    if (request !== controller || request.signal.aborted) throw new DOMException('Stale request', 'AbortError');
    return data;
}

async function fetchStats() {
    const data = await fetchJson(`/api/spaces/${currentSpace}/watch-stats?from_ms=${currentFromMs}&to_ms=${currentToMs}`);
    document.getElementById('stat-event-count').textContent = data.event_count;
    document.getElementById('stat-video-count').textContent = data.video_count;
    document.getElementById('stat-watched-seconds').textContent = formatSeconds(data.watched_seconds);
    document.getElementById('stat-average-seconds').textContent = formatSeconds(data.average_seconds);

    const tbody = document.querySelector('#ranking-table tbody');
    tbody.innerHTML = '';
    data.top_videos.forEach(v => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td></td><td></td><td></td>`;
        tr.children[0].textContent = v.title;
        tr.children[1].textContent = v.event_count;
        tr.children[2].textContent = formatSeconds(v.watched_seconds);
        tbody.appendChild(tr);
    });
}

async function fetchDaily() {
    const data = await fetchJson(`/api/spaces/${currentSpace}/watch-stats/daily?from_ms=${currentFromMs}&to_ms=${currentToMs}`);
    const tbody = document.querySelector('#daily-table tbody');
    tbody.innerHTML = '';
    data.items.forEach(d => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td></td><td></td><td></td><td></td>`;
        tr.children[0].textContent = d.date;
        tr.children[1].textContent = d.event_count;
        tr.children[2].textContent = d.video_count;
        tr.children[3].textContent = formatSeconds(d.watched_seconds);
        tbody.appendChild(tr);
    });
}

async function fetchHistory(keyword) {
    const offset = (currentPage - 1) * pageSize;
    let url = `/api/spaces/${currentSpace}/watch-history?from_ms=${currentFromMs}&to_ms=${currentToMs}&limit=${pageSize}&offset=${offset}`;
    if (keyword) url += `&q=${encodeURIComponent(keyword)}`;

    const data = await fetchJson(url);
    const tbody = document.querySelector('#history-table tbody');
    tbody.innerHTML = '';

    if (data.items.length === 0 && currentPage > 1) {
        currentPage--;
        return fetchHistory(keyword);
    }

    data.items.forEach(item => {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td><button class="btn-del">删除</button></td>`;
        tr.children[0].textContent = item.title;
        tr.children[1].textContent = formatSeconds(item.duration_seconds);
        tr.children[2].textContent = formatSeconds(item.counted_seconds);
        tr.children[3].textContent = `${Math.round(item.progress * 100)}%`;
        tr.children[4].textContent = formatDateTime(item.created);
        
        tr.querySelector('.btn-del').addEventListener('click', () => deleteEvent(item.event_id, keyword));
        tbody.appendChild(tr);
    });

    totalPages = Math.max(1, Math.ceil(data.total / pageSize));
    document.getElementById('page-info').textContent = `第 ${currentPage} / ${totalPages} 页 (共 ${data.total} 条)`;
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;
}

async function deleteEvent(eventId, keyword) {
    if (!confirm('确认删除该条观看记录？')) return;
    try {
        const response = await fetch(`/api/spaces/${currentSpace}/watch-history/${eventId}`, { method: 'DELETE' });
        if (!response.ok) throw new Error((await response.json()).detail || '删除失败');
        await fetchData(); // 刷新所有数据
    } catch (err) {
        alert('删除失败: ' + err.message);
    }
}