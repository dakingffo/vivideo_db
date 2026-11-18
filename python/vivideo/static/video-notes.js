(() => {
'use strict';
const dialog=document.getElementById('video-notes-dialog'),$=id=>document.getElementById(id);
let video=null,origin='',current=null,baseline='',generation=0,reading=null,writing=false,loading=false,operation=0;
function draft(){return JSON.stringify([$('video-notes-name').value,$('video-notes-content').value]);}
function dirty(){return dialog.open&&draft()!==baseline;}
function canLeave(){return !writing&&(!dirty()||confirm('笔记还未保存，确定放弃本次修改吗？'));}
function status(text='',failure=false){$('video-notes-status').textContent=text;$('video-notes-status').classList.toggle('failure',failure);}
function sync(){for(const id of ['video-notes-name','video-notes-content','video-notes-select','new-video-note','save-video-note','delete-video-note','reload-video-notes'])$(id).disabled=writing||loading;$('close-video-notes').disabled=writing;}
async function api(path,options={}){
 let response;
 try{response=await fetch('/api/spaces/'+origin+path,{...options,headers:{'Content-Type':'application/json'},body:options.body===undefined?undefined:JSON.stringify(options.body)});}
 catch(e){if(e.name==='AbortError')throw e;throw Error('连接失败，当前输入已保留，请稍后重试。');}
 const data=response.status===204?null:await response.json().catch(()=>null);
 if(!response.ok){const e=Error(typeof data?.detail==='string'?data.detail:'保存失败，请检查输入或稍后重试。');e.status=response.status;throw e;}
 return data;
}
function editable(note=null){
 current=note;loading=false;
 $('video-notes-name').value=note?.title||'';$('video-notes-content').value=note?.content||'';baseline=draft();
 $('delete-video-note').hidden=!note;$('video-note-versions').hidden=!note;$('video-note-versions').open=false;$('video-note-revisions').replaceChildren();$('reload-video-notes').hidden=true;
 status(note?'已保存 · 版本 '+note.version:'只记录属于这条视频的想法。');sync();
}
async function selectNote(id){
 const previous=current?.id||'';
 if(!canLeave()){$('video-notes-select').value=previous;return;}
 if(!id){editable();return;}
 const token=++generation;reading?.abort();reading=new AbortController();loading=true;sync();status('正在读取笔记…');
 try{const note=await api('/notes/'+encodeURIComponent(id),{signal:reading.signal});if(token!==generation||origin!==spaceId||!dialog.open)return;editable(note);}
 catch(e){if(token===generation&&e.name!=='AbortError'){status(e.message,true);$('reload-video-notes').hidden=false;}}
 finally{if(token===generation){loading=false;sync();}}
}
async function loadList(preferred){
 const token=++generation;reading?.abort();reading=new AbortController();loading=true;sync();status('正在读取笔记…');
 try{
  const items=[];let total=1;
  while(items.length<total){const data=await api('/notes?'+new URLSearchParams({video_id:video.id,limit:100,offset:items.length}),{signal:reading.signal});if(token!==generation||origin!==spaceId||!dialog.open)return;items.push(...data.items);total=data.total;if(!data.items.length)break;}
  const choices=[new Option('新建一条笔记',''),...items.map(note=>new Option(note.title,note.id))];$('video-notes-select').replaceChildren(...choices);
  const selected=preferred!==undefined?preferred:(items[0]?.id||'');$('video-notes-select').value=selected;
  // The previous draft was already confirmed before initiating this load.
  baseline=draft();loading=false;
  if(selected&&items.some(n=>n.id===selected)){await selectNote(selected);}
  else{editable();const legacy=notes[video.id];if(legacy){$('video-notes-name').value='我的视频笔记';$('video-notes-content').value=legacy;status('已带入此浏览器的旧笔记，点击保存后同步到空间。');}}
 }catch(e){if(token===generation&&e.name!=='AbortError'){status(e.message,true);$('reload-video-notes').hidden=false;}}
 finally{if(token===generation){loading=false;sync();}}
}
function close(){if(!canLeave())return;++generation;reading?.abort();dialog.close();loading=false;video=null;sync();}
async function open(item){
 if(!item||!canLeave())return;
 origin=spaceId;video=item;editable();$('video-notes-video').textContent=item.title;dialog.showModal();await loadList();
}
$('library-grid').addEventListener('click',event=>{const button=event.target.closest('[data-video-note]');if(button)open(window.videoCatalog?.find(button.dataset.videoNote));});
$('close-video-notes').onclick=close;
dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
$('video-notes-select').onchange=()=>selectNote($('video-notes-select').value);
$('new-video-note').onclick=()=>{if(!canLeave())return;++generation;reading?.abort();$('video-notes-select').value='';editable();$('video-notes-name').focus();};
$('reload-video-notes').onclick=()=>{if(canLeave()){baseline=draft();loadList(current?.id);}};
$('video-notes-form').onsubmit=async event=>{
 event.preventDefault();if(writing||loading)return;
 const title=$('video-notes-name').value.trim();if(!title){status('请填写笔记标题。',true);return;}
 const token=generation,scope=origin,op=++operation;writing=true;sync();status('正在保存…');
 try{
  const payload={title,content:$('video-notes-content').value};if(current)payload.expected_version=current.version;else payload.video_id=video.id;
  const note=await api(current?'/notes/'+current.id:'/notes',{method:current?'PUT':'POST',body:payload});
  if(token!==generation||scope!==spaceId||!dialog.open)return;
  writing=false;baseline=draft();editable(note);await loadList(note.id);
 }catch(e){if(token===generation&&scope===spaceId){status(e.status===409?'笔记已在其他页面更新。请先复制当前文字，再重新加载；本次输入已保留。':e.message,true);if(e.status===409)$('reload-video-notes').hidden=false;}}
 finally{if(op===operation){writing=false;sync();}}
};
$('delete-video-note').onclick=async()=>{
 if(!current||writing||!confirm('删除这条笔记及其修改记录？'))return;
 const token=generation,scope=origin,op=++operation;writing=true;sync();
 try{await api('/notes/'+current.id,{method:'DELETE'});if(token!==generation||scope!==spaceId)return;writing=false;baseline=draft();editable();await loadList();}
 catch(e){if(token===generation&&scope===spaceId)status(e.message,true);}
 finally{if(op===operation){writing=false;sync();}}
};
$('video-note-versions').addEventListener('toggle',async()=>{
 if(!$('video-note-versions').open||!current)return;
 const token=generation,id=current.id;
 try{const data=await api('/notes/'+id+'/revisions?limit=20');if(token!==generation||current?.id!==id||origin!==spaceId)return;$('video-note-revisions').replaceChildren(...data.items.map(revision=>{const row=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('pre');title.textContent='版本 '+revision.version+' · '+new Date(revision.saved).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'});body.textContent=revision.title+'\n\n'+revision.content;row.append(title,body);return row;}));}
 catch(e){if(token===generation)status(e.message,true);}
});
window.videoNotes={canLeave,setSpace(){++generation;++operation;reading?.abort();dialog.close();writing=false;loading=false;video=null;current=null;baseline='';sync();}};
})();
