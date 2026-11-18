(() => {
'use strict';
const root=document.getElementById('watch-history-panel');
const $=selector=>root.querySelector(selector);
const day=86400000, size=20;
let controller=null, version=0, currentPage=1, totalPages=1, deleting=false, deleteVersion=0;
function date(ms){return new Date(ms+8*3600000).toISOString().slice(0,10);}
function seconds(value){const s=Math.max(0,Math.floor(Number(value)||0));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;}
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function error(message=''){$('#watch-error').textContent=message;$('#watch-error').hidden=!message;}
function preset(days){const now=Date.now();$('#watch-from').value=date(now-(days-1)*day);$('#watch-to').value=date(now);root.querySelectorAll('[data-watch-days]').forEach(b=>b.classList.toggle('selected',Number(b.dataset.watchDays)===days));}
function clear(){for(const id of ['watch-count','watch-videos'])$('#'+id).textContent='0';for(const id of ['watch-total','watch-average'])$('#'+id).textContent='0:00';$('#watch-daily tbody').replaceChildren();$('#watch-ranking').replaceChildren();$('#watch-records').replaceChildren(node('p','这段时间还没有观看记录。','watch-empty'));$('#watch-record-count').textContent='0 条记录';$('#watch-pagination').hidden=true;}
function range(){const from=Date.parse($('#watch-from').value+'T00:00:00+08:00'),to=Date.parse($('#watch-to').value+'T00:00:00+08:00')+day;if(!Number.isFinite(from)||!Number.isFinite(to)||from<0||from>=to||to-from>31*day)throw Error('请选择有效日期范围，最多查询 31 天。');return new URLSearchParams({from_ms:from,to_ms:to});}
async function request(origin,path,signal,method='GET'){
 const response=await fetch(`/api/spaces/${origin}${path}`,{signal,method});
 const data=response.status===204?null:await response.json().catch(()=>null);
 if(!response.ok)throw Error(typeof data?.detail==='string'?data.detail:'读取失败，请稍后重试。');
 return data;
}
function showStats(stats,daily){
 $('#watch-count').textContent=stats.event_count;$('#watch-videos').textContent=stats.video_count;$('#watch-total').textContent=seconds(stats.watched_seconds);$('#watch-average').textContent=seconds(stats.average_seconds);
 $('#watch-daily tbody').replaceChildren(...daily.items.map(d=>{const row=node('tr');for(const v of [d.date.slice(5).replace('-',' / '),d.event_count,d.video_count,seconds(d.watched_seconds)])row.append(node('td',v));return row;}));
 $('#watch-ranking').replaceChildren(...stats.top_videos.map((v,index)=>{const item=node('li');const name=node('div',undefined,'watch-rank-name');name.append(node('strong',v.title),node('small',v.event_count+' 次观看'));item.append(node('span',String(index+1).padStart(2,'0'),'watch-rank-number'),name,node('span',seconds(v.watched_seconds),'watch-rank-time'));return item;}));
 if(!stats.top_videos.length)$('#watch-ranking').append(node('li','多看一点，慢慢发现你的兴趣。','watch-empty'));
}
function showRecords(data,origin){
 totalPages=Math.max(1,Math.ceil(data.total/size));$('#watch-record-count').textContent=data.total+' 条记录';
 $('#watch-records').replaceChildren(...data.items.map(item=>{
  const row=node('article',undefined,'watch-record');
  const content=node('div',undefined,'watch-record-main');content.append(node('h4',item.title));
  const when=new Date(item.created).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  content.append(node('p',when+' · 已观看 '+seconds(item.counted_seconds)+' / '+seconds(item.duration_seconds)));
  const progress=node('div',undefined,'watch-progress');progress.setAttribute('role','progressbar');progress.setAttribute('aria-label','观看进度');progress.setAttribute('aria-valuemin','0');progress.setAttribute('aria-valuemax','100');progress.setAttribute('aria-valuenow',String(Math.round(item.progress*100)));const fill=node('span');fill.style.width=(Math.max(0,Math.min(1,item.progress))*100)+'%';progress.append(fill);content.append(progress);
  const percent=node('span',Math.round(item.progress*100)+'%','watch-progress-label');const button=node('button','删除','watch-delete');button.type='button';button.disabled=deleting;button.onclick=()=>remove(item,origin,button);row.append(content,percent,button);return row;
 }));
 if(!data.items.length)$('#watch-records').append(node('p',$('#watch-query').value.trim()?'没有找到符合条件的观看记录。':'这段时间还没有观看记录，去发现一条想看的视频吧。','watch-empty'));
 $('#watch-page').textContent=`${currentPage} / ${totalPages}`;$('#watch-pagination').hidden=totalPages<2;$('#watch-prev').disabled=currentPage<=1;$('#watch-next').disabled=currentPage>=totalPages;
}
async function refresh(){
 controller?.abort();const active=new AbortController();controller=active;const ticket=++version,origin=spaceId;
 error();let params;try{params=range();}catch(e){root.setAttribute('aria-busy','false');error(e.message);return;}
 root.setAttribute('aria-busy','true');
 const historyParams=new URLSearchParams(params);historyParams.set('limit',size);historyParams.set('offset',(currentPage-1)*size);historyParams.set('q',$('#watch-query').value.trim());
 try{
  const [stats,daily,history]=await Promise.all([request(origin,'/watch-stats?'+params,active.signal),request(origin,'/watch-stats/daily?'+params,active.signal),request(origin,'/watch-history?'+historyParams,active.signal)]);
  if(ticket!==version||origin!==spaceId||active.signal.aborted)return;
  if(currentPage>1&&!history.items.length){currentPage=Math.max(1,Math.ceil(history.total/size));return refresh();}
  showStats(stats,daily);showRecords(history,origin);
 }catch(e){if(e.name!=='AbortError'&&ticket===version&&origin===spaceId)error(e.message);}
 finally{if(ticket===version)root.setAttribute('aria-busy','false');}
}
async function remove(item,origin,button){
 if(origin!==spaceId||deleting||!confirm('删除这条观看记录？对应统计也会更新。'))return;
 deleting=true;button.disabled=true;const ticket=version, deletion=++deleteVersion;
 try{await request(origin,'/watch-history/'+encodeURIComponent(item.event_id),undefined,'DELETE');if(origin===spaceId)await refresh();}
 catch(e){if(origin===spaceId&&ticket===version)error(e.message);}
 finally{if(deletion===deleteVersion)deleting=false;if(button.isConnected)button.disabled=false;}
}
$('#watch-range-form').onsubmit=event=>{event.preventDefault();currentPage=1;root.querySelectorAll('[data-watch-days]').forEach(b=>b.classList.remove('selected'));refresh();};
$('#watch-search-form').onsubmit=event=>{event.preventDefault();currentPage=1;refresh();};
root.querySelectorAll('[data-watch-days]').forEach(b=>b.onclick=()=>{preset(Number(b.dataset.watchDays));currentPage=1;refresh();});
$('#watch-prev').onclick=()=>{if(currentPage>1){currentPage--;refresh();}};$('#watch-next').onclick=()=>{if(currentPage<totalPages){currentPage++;refresh();}};
window.watchHistory={refresh,setSpace(){++version;controller?.abort();controller=null;++deleteVersion;deleting=false;root.setAttribute('aria-busy','false');currentPage=1;totalPages=1;preset(7);$('#watch-query').value='';error();clear();if(page==='profile')refresh();}};
preset(7);clear();
})();
