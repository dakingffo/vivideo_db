const $ = s => document.querySelector(s);
const escapeHTML = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function read(key, fallback){try{return JSON.parse(localStorage.getItem(key))??fallback}catch{return fallback}}
const SPACES=[
 {id:'learning',name:'学习空间',icon:'▤',hue:148,description:'把好奇变成知识',tags:['AI Agent','编程实践','科技与新知']},
 {id:'roaming',name:'漫游空间',icon:'☷',hue:211,description:'没有目的，也有发现',tags:['旅行','自然','人文探索']},
];
let spaceId=read('vivideo.currentSpace','learning');if(!SPACES.some(s=>s.id===spaceId))spaceId='learning';
const spaceKey=key=>`vivideo.space.${spaceId}.${key}`;
function readSpace(key,fallback){return read(spaceKey(key),fallback)}
// Preserve the original single-space data once, in the learning space only.
if(!read('vivideo.spacesMigrated',false)){
 try{
  for(const key of ['saved','tags','feedback','notes','sessions','activeSession','personalized']){
   const value=localStorage.getItem('vivideo.'+key);
   if(value!==null&&localStorage.getItem('vivideo.space.learning.'+key)===null)localStorage.setItem('vivideo.space.learning.'+key,value);
  }
  localStorage.setItem('vivideo.spacesMigrated','true');
 }catch{}
}
let saved={},tags=[],feedback={},notes={},sessions=[],activeSession=null;
let videos=[],initialVideos={learning:[],roaming:[]},history=[],busy=false,mode='demo',page='discover',resultDemo=true;
let spaceTransition=false,ready=false,requestController=null,pendingMessage='';
let chatStarted=false,welcomeAnimations=[],stageTransitionVersion=0,returningToWelcome=false,recommendationTurns=[];
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
function setChatStarted(started,animate=false){
 const panel=$('#welcome-panel');const conversation=$('#conversation');const version=++stageTransitionVersion;
 welcomeAnimations.forEach(animation=>animation.cancel());welcomeAnimations=[];
 if(returningToWelcome){conversation.replaceChildren();returningToWelcome=false}
 chatStarted=started;$('.discover-content').classList.toggle('chat-active',started);
 panel.inert=started;conversation.inert=!started;panel.setAttribute('aria-hidden',String(started));conversation.setAttribute('aria-hidden',String(!started));
 if(animate&&!reducedMotion.matches){
  stopExplore();returningToWelcome=!started;panel.classList.remove('hidden');conversation.classList.remove('hidden');
  // Both views occupy the same fixed slot; only opacity changes.
  const options={duration:320,easing:'ease-in-out',fill:'forwards'};
  const animations=[panel.animate([{opacity:started?1:0},{opacity:started?0:1}],options),conversation.animate([{opacity:started?0:1},{opacity:started?1:0}],options)];
  welcomeAnimations=animations;
  Promise.all(animations.map(animation=>animation.finished)).then(()=>{
   if(version!==stageTransitionVersion)return;
   panel.classList.toggle('hidden',started);conversation.classList.toggle('hidden',!started);
   if(!started)conversation.replaceChildren();returningToWelcome=false;
   animations.forEach(animation=>animation.cancel());welcomeAnimations=[];
  }).catch(()=>{});
 }else{
  panel.classList.toggle('hidden',started);conversation.classList.toggle('hidden',!started);
  if(!started)conversation.replaceChildren();
 }
}
function uniqueRecommendations(items){const seen=new Set();return items.filter(item=>{const key=item.url||item.id;if(seen.has(key))return false;seen.add(key);return true})}
function attachRecommendations(el,items,demo){
 items=uniqueRecommendations(items);
 if(!items.length)return;el._videos=[...items];
 const heading=document.createElement('div');heading.className='bubble-results-heading';
 heading.innerHTML=`<span class="bubble-results-label">${demo?'示例推荐':'推荐结果'} · ${items.length} 条</span><div class="shelf-tools"><button data-shelf-scroll="-1" aria-label="向左滚动推荐">←</button><button data-shelf-scroll="1" aria-label="向右滚动推荐">→</button></div>`;
 const results=document.createElement('div');results.className='bubble-results recommendation-track';results.tabIndex=0;results.setAttribute('aria-label','视频推荐，左右方向键滚动');
 results.onkeydown=e=>{if(e.target===results&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();scrollShelf(results,e.key==='ArrowLeft'?-1:1)}};
 el.append(heading,results);renderShelf(results,items);requestAnimationFrame(()=>{if(el.isConnected)$('#conversation').scrollTop=$('#conversation').scrollHeight});
}
let spaceTraits=[],pendingTurn=null,failedTurn=null;
async function stateAPI(space,path,options={}){
 const response=await fetch(`/api/spaces/${encodeURIComponent(space)}${path}`,{...options,headers:{'Content-Type':'application/json'}});
 if(!response.ok){let detail='数据保存失败，请重试';try{const data=await response.json();if(typeof data.detail==='string')detail=data.detail}catch{}throw Error(detail)}
 return response.status===204?null:response.json();
}
async function fetchRemoteSpace(space){
 const [conversationData,traitData]=await Promise.all([stateAPI(space,'/sessions'),stateAPI(space,'/traits')]);
 return {sessions:conversationData.sessions,traits:traitData.traits};
}
function cacheRemoteSpace(space,data){
 const key=name=>`vivideo.space.${space}.${name}`;
 const view=read(key('view'),{});const id=view.activeSession??read(key('activeSession'),null);
 const session=data.sessions.find(s=>s.id===id);
 const nextView={...view,history:session?.history||[],recommendationTurns:session?.recommendationTurns||[],activeSession:session?.id||null,resultDemo:false,videos:session?.videos||[],chatStarted:Boolean(session?.history.length)};
 try{localStorage.setItem(key('sessions'),JSON.stringify(data.sessions));localStorage.setItem(key('traits'),JSON.stringify(data.traits));localStorage.setItem(key('tags'),JSON.stringify(data.traits.map(t=>t.text)));localStorage.setItem(key('activeSession'),JSON.stringify(session?.id||null));localStorage.setItem(key('view'),JSON.stringify(nextView))}catch{throw Error('浏览器无法缓存服务端数据，请检查存储设置')}
}
function cancelPendingTurn(){
 if(pendingTurn){const turn=pendingTurn;pendingTurn=null;fetch(`/api/spaces/${turn.space}/requests/${turn.id}/cancel`,{method:'POST',keepalive:true}).catch(()=>{})}
}
function renderTags(){
 $('#tags').innerHTML=mode==='zmq'?(spaceTraits.map(t=>`<button data-trait="${escapeHTML(t.id)}" aria-label="删除特征 ${escapeHTML(t.text)}">${escapeHTML(t.text)} ×</button>`).join('')||'<p>还没有已确认的长期偏好。</p>'):(tags.map((t,i)=>`<button data-tag="${i}" aria-label="删除兴趣 ${escapeHTML(t)}">${escapeHTML(t)} ×</button>`).join('')||'<p>还没有长期兴趣，可以在下方添加。</p>');
}
function attachTraitProposals(host,proposals){
 host.querySelector('.trait-proposals')?.remove();
 const pending=proposals.filter(p=>p.status==='pending');if(!pending.length)return;
 const box=document.createElement('div');box.className='trait-proposals';
 box.innerHTML='<p>这些偏好要记在当前空间吗？你可以修改后保存。</p>'+pending.map(p=>`<div class="trait-proposal" data-proposal="${escapeHTML(p.id)}"><input maxlength="200" value="${escapeHTML(p.text)}" aria-label="待确认的用户特征"><small>依据：${escapeHTML(p.evidence)}</small><div><button class="text-button" type="button" data-trait-decision="accept">记住</button><button class="text-button" type="button" data-trait-decision="reject">忽略</button></div></div>`).join('');
 host.append(box);
}
document.addEventListener('click',async event=>{
 const button=event.target.closest('[data-trait-decision]');if(!button)return;
 const row=button.closest('[data-proposal]');const text=row.querySelector('input').value.trim();const accept=button.dataset.traitDecision==='accept';if(accept&&!text){toast('特征不能为空');return}
 const origin=spaceId,sid=activeSession;row.querySelectorAll('button,input').forEach(el=>el.disabled=true);
 try{
  const data=await stateAPI(origin,`/trait-proposals/${row.dataset.proposal}/decision`,{method:'POST',body:JSON.stringify({accept,text:accept?text:null})});
  if(origin!==spaceId)return;spaceTraits=data.traits;tags=spaceTraits.map(t=>t.text);persist('traits',spaceTraits);persist('tags',tags);
  const session=sessions.find(s=>s.id===sid);const proposal=session?.trait_proposals?.find(p=>p.id===row.dataset.proposal);if(proposal)proposal.status=accept?'accepted':'rejected';persist('sessions',sessions);row.remove();renderTags();toast(accept?'已记入当前空间':'已忽略这条提议');
 }catch(error){if(origin===spaceId){row.querySelectorAll('button,input').forEach(el=>el.disabled=false);toast(error.message)}}
});

function restoreConversation(){
 $('#conversation').replaceChildren();let lastAssistant=null;
 history.forEach((m,index)=>{const el=bubble(m.role,m.content);el.dataset.messageIndex=index;if(m.role==='assistant'){lastAssistant=el;const turn=recommendationTurns.find(t=>t.messageIndex===index);if(turn)attachRecommendations(el,restoreVideoSelection(turn.videos,turn.demo),turn.demo)}});
 if(lastAssistant&&!recommendationTurns.length)attachRecommendations(lastAssistant,videos,resultDemo);
 if(lastAssistant&&mode==='zmq')attachTraitProposals(lastAssistant,sessions.find(s=>s.id===activeSession)?.trait_proposals||[]);
}
function freshVideos(){return [...initialVideos[spaceId]]}
function restoreVideoSelection(items,demo){
 // Update old roaming demo shelves without changing conversations or saved items.
 const oldLearningIds=new Set(['agent','workflow','rag','tools','design']);
 if(spaceId==='roaming'&&demo&&items.some(v=>oldLearningIds.has(v.id)))return freshVideos();
 return [...items];
}
function renderSessions(){
 syncPreparationEvents();
 $('#history-list').innerHTML=sessions.map(s=>`<div class="history-row" data-history-id="${escapeHTML(s.id)}"><button class="history-item ${s.id===activeSession?'active':''}" data-session="${escapeHTML(s.id)}"><span class="history-icon">◌</span><span><strong>${escapeHTML(s.title)}</strong><small>${escapeHTML(new Date(s.updated).toLocaleDateString('zh-CN',{month:'short',day:'numeric'}))} · ${Math.floor(s.history.length/2)} 轮对话</small></span></button><button class="history-delete" data-delete-session="${escapeHTML(s.id)}" aria-label="删除对话：${escapeHTML(s.title)}" title="删除对话"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg></button></div>`).join('')||'<div class="history-empty">还没有历史对话<br><span>发送第一条需求，<br>让探索从这里开始。</span></div>';
}
async function deleteSession(id,row){
 if(spaceTransition||!sessions.some(s=>s.id===id))return;
 const origin=spaceId;
 if(mode==='zmq'){row.inert=true;try{await stateAPI(origin,'/sessions/'+id,{method:'DELETE'})}catch(error){row.inert=false;toast(error.message);return}if(origin!==spaceId)return}
 const keyboardFocus=row.contains(document.activeElement);
 const positions=new Map([...$('#history-list').children].map(el=>[el.dataset.historyId,el.getBoundingClientRect().top]));
 if(!reducedMotion.matches){
  const rect=row.getBoundingClientRect();const ghost=row.cloneNode(true);ghost.classList.add('history-falling');ghost.setAttribute('aria-hidden','true');ghost.inert=true;
  Object.assign(ghost.style,{left:`${rect.left}px`,top:`${rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});document.body.append(ghost);
  const frames=Array.from({length:13},(_,i)=>{const t=i/12;return {offset:t,transform:`translate(${8*t}px,${180*t*t}px) rotate(${8*t*t}deg)`,opacity:1-t*t}});
  ghost.animate(frames,{duration:560,easing:'linear',fill:'forwards'}).finished.catch(()=>{}).finally(()=>ghost.remove());
 }
 sessions=sessions.filter(s=>s.id!==id);persist('sessions',sessions);
 if(activeSession===id){
  closeWatch({immediate:true,restoreFocus:false});
  const draft=pendingMessage||$('#message').value;cancelPendingTurn();failedTurn=null;requestController?.abort();requestController=null;pendingMessage='';busy=false;$('#send').disabled=false;
  activeSession=null;history=[];recommendationTurns=[];videos=freshVideos();resultDemo=true;$('#message').value=draft;
  persist('activeSession',null);setChatStarted(false,true);updateResultHeading();render();
 }
 renderSessions();stashSpace();
 if(!reducedMotion.matches){
  for(const el of $('#history-list').children){const before=positions.get(el.dataset.historyId);if(before===undefined)continue;const distance=before-el.getBoundingClientRect().top;if(distance)el.animate([{transform:`translateY(${distance}px)`},{transform:'translateY(0)'}],{duration:300,easing:'cubic-bezier(.2,.7,.2,1)'})}
 }
 if(keyboardFocus){const next=$('#history-list [data-delete-session]')||$('#history-new');next.focus({preventScroll:true})}
 toast('历史对话已删除');
}
function updateResultHeading(){
 $('#result-title').textContent='为你的好奇心准备';
 $('#result-subtitle').textContent='看看推荐结果会如何呈现，或开始一段新的对话';
 $('.pill').textContent=`示例内容 · ${freshVideos().length} 条`;
}
function saveSession(){
 const existing=sessions.find(s=>s.id===activeSession);
 activeSession=activeSession||crypto.randomUUID();
 const session={id:activeSession,title:existing?.title&&existing.title!=='新对话'?existing.title:history[0].content.slice(0,40),updated:Date.now(),history:[...history],videos:[...videos],recommendationTurns:[...recommendationTurns],demo:resultDemo,trait_proposals:existing?.trait_proposals||[]};
 sessions=[session,...sessions.filter(s=>s.id!==activeSession)];
 if(mode!=='zmq')sessions=sessions.slice(0,30);
 persist('sessions',sessions);persist('activeSession',activeSession);renderSessions();
}
function restoreSession(id){
 if(busy){toast('请等待当前推荐完成');return}
 const session=sessions.find(s=>s.id===id);if(!session)return;
 closeWatch({immediate:true,restoreFocus:false});
 activeSession=id;history=[...session.history];recommendationTurns=session.recommendationTurns||[];resultDemo=session.demo;videos=restoreVideoSelection(session.videos,resultDemo);
 setChatStarted(true);restoreConversation();$('#message').value='';
 persist('activeSession',id);updateResultHeading();render();renderSessions();navigate('discover');
}
$('#history-list').onclick=e=>{const remove=e.target.closest('[data-delete-session]');if(remove){deleteSession(remove.dataset.deleteSession,remove.closest('.history-row'));return}const item=e.target.closest('[data-session]');if(item)restoreSession(item.dataset.session)};


function persist(key,value){try{localStorage.setItem(spaceKey(key),JSON.stringify(value))}catch{toast('浏览器存储不可用，本次修改仅在当前页面保留')}}
let toastTimer;function toast(text){$('#toast').textContent=text;$('#toast').classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('visible'),2400)}
function coverMarkup(v){
 const id=escapeHTML(v.id),color=['mint','purple','peach','blue','yellow'].includes(v.color)?v.color:'mint';
 if(v.url)return `<button type="button" class="cover video-cover ${color}" data-action="watch" data-id="${id}" data-video-cover-id="${id}" aria-label="播放 ${escapeHTML(v.title)}"><img class="video-poster" alt="" hidden loading="lazy" referrerpolicy="no-referrer"><span class="cover-loading">正在获取封面</span><span class="cover-play" aria-hidden="true">▶</span><small class="duration" data-cover-duration hidden></small></button>`;
 return `<div class="cover ${color}"><b>${escapeHTML(v.mark||'VIDEO')}</b>${v.minutes?`<small class="duration">${escapeHTML(v.minutes)} 分钟</small>`:''}</div>`;
}
function card(v){const id=escapeHTML(v.id);return `<article class="card ${v.url?'video-card':''}">${coverMarkup(v)}<div class="card-body"><h3>${escapeHTML(v.title)}</h3><div class="author">${escapeHTML(v.author)}${v.url?'':' · 示例视频元数据'}</div><div class="card-tags">${[v.level,v.style].filter(Boolean).map(t=>`<span>${escapeHTML(t)}</span>`).join('')}</div><div class="reason"><strong>✦ 推荐理由</strong><br>${escapeHTML(v.reason||'根据当前需求选择的视频。')}</div><div class="card-actions"><button data-action="detail" data-id="${id}">查看详情 ↗</button><button data-action="save" data-id="${id}" class="${saved[v.id]?'selected':''}">${saved[v.id]?'★ 已收藏':'☆ 收藏'}</button><button data-action="like" data-id="${id}" class="${feedback[v.id]==='like'?'selected':''}" aria-label="喜欢这条推荐">♡</button><button data-action="dislike" data-id="${id}" class="${feedback[v.id]==='dislike'?'selected':''}" aria-label="不感兴趣">−</button></div></div></article>`}
function compactCard(v){
 const id=escapeHTML(v.id);
 return `<article class="card compact-card ${v.url?'video-card':''}">${coverMarkup(v)}<div class="card-body"><h3 title="${escapeHTML(v.title)}">${escapeHTML(v.title)}</h3><div class="card-actions"><button data-action="detail" data-id="${id}">查看详情 ↗</button><button data-action="save" data-id="${id}" class="${saved[v.id]?'selected':''}" aria-label="${saved[v.id]?'取消收藏':'收藏'} ${escapeHTML(v.title)}">${saved[v.id]?'★ 已收藏':'☆ 收藏'}</button></div></div></article>`;
}
function render(){ document.querySelectorAll('.bubble-results').forEach(el=>{renderShelf(el,el.parentElement._videos)}); $('#detail [data-action=like]')?.classList.toggle('selected',feedback[$('#detail [data-action=like]').dataset.id]==='like');$('#detail [data-action=dislike]')?.classList.toggle('selected',feedback[$('#detail [data-action=dislike]').dataset.id]==='dislike'); renderShelf($('#video-grid'),freshVideos());$('#library-grid').innerHTML=Object.values(saved).map(card).join('')||'<div class="empty">你的资料库还空着。<br>在对话发现中收藏一条视频，从这里开始积累。</div>';$('#saved-count').textContent=Object.keys(saved).length;renderTags();hydrateCovers($('#library-grid'));syncStageSize()}
let navigationVersion=0;
async function navigate(target){
 if(spaceTransition||target===page)return;
 closeWatch({immediate:true,restoreFocus:false});
 const version=++navigationVersion;
 const order=['discover','library','profile'];const direction=order.indexOf(target)>order.indexOf(page)?1:-1;
 const outgoing=$('#'+page);const incoming=$('#'+target);page=target;
 document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===target));
 $('#page-title').textContent={discover:'对话发现',library:'我的资料库',profile:'偏好'}[target];
 document.querySelectorAll('.page').forEach(el=>el.getAnimations().forEach(a=>a.cancel()));
 $('.history-sidebar').getAnimations().forEach(a=>a.cancel());
 document.querySelectorAll('.page').forEach(el=>el.classList.toggle('hidden',el!==outgoing));
 if(!reducedMotion.matches){
  if(outgoing.id==='discover')$('.history-sidebar').animate([{opacity:1,transform:'translateX(0)'},{opacity:0,transform:'translateX(20px)'}],{duration:160,easing:'ease-out'});
  try{await outgoing.animate([{opacity:1,transform:'translateX(0)'},{opacity:0,transform:`translateX(${-direction*35}px)`}],{duration:170,easing:'ease-in'}).finished}catch{}
 }
 if(version!==navigationVersion)return;
 outgoing.classList.add('hidden');incoming.classList.remove('hidden');
 if(!reducedMotion.matches){
  incoming.animate([{opacity:0,transform:`translateX(${direction*40}px)`},{opacity:1,transform:'translateX(0)'}],{duration:300,easing:'cubic-bezier(.2,.7,.2,1)'});
  if(target==='discover')$('.history-sidebar').animate([{opacity:0,transform:'translateX(25px)'},{opacity:1,transform:'translateX(0)'}],{duration:420,easing:'ease-out'});
 }
}
document.querySelectorAll('.nav').forEach(el=>el.onclick=()=>navigate(el.dataset.page));
function bubble(role,text){const el=document.createElement('div');el.className='bubble '+role;if(spaceTransition)el.style.animation='none';el.textContent=text;$('#conversation').append(el);requestAnimationFrame(()=>{if(el.isConnected)$('#conversation').scrollTop=$('#conversation').scrollHeight});return el}
function animateReply(el,previousHeight){
 if(reducedMotion.matches||!el.isConnected)return;
 const height=el.getBoundingClientRect().height;
 const animation=el.animate([
  {opacity:0,transform:'translateY(6px)',height:previousHeight+'px',overflow:'hidden'},
  {opacity:1,transform:'translateY(0)',height:height+'px',overflow:'hidden'}
 ],{duration:360,easing:'cubic-bezier(.2,.7,.2,1)'});
 animation.finished.then(()=>{if(el.isConnected)$('#conversation').scrollTo({top:$('#conversation').scrollHeight,behavior:'smooth'})}).catch(()=>{});
}
$('#chat-form').onsubmit=async e=>{
 e.preventDefault();const message=$('#message').value.trim();if(!message||busy||!ready||spaceTransition||isWatching())return;
 const origin=spaceId;const controller=new AbortController();requestController=controller;pendingMessage=message;
 const personalized=$('#personalized').checked;
 busy=true;$('#send').disabled=true;if(!chatStarted)setChatStarted(true,true);restoreConversation();bubble('user',message);const loading=bubble('assistant','正在思考…');loading.classList.add('thinking');$('#message').value='';
 try{
  if(mode==='zmq'&&!activeSession){
   const session=await stateAPI(origin,'/sessions',{method:'POST',body:'{}',signal:controller.signal});
   if(origin!==spaceId||controller.signal.aborted)return;
   activeSession=session.id;sessions.unshift(session);persist('activeSession',activeSession);persist('sessions',sessions);renderSessions();
  }
  const requestId=failedTurn?.message===message&&failedTurn?.session===activeSession&&failedTurn?.personalized===personalized?failedTurn.id:crypto.randomUUID();
  pendingTurn={space:origin,id:requestId};failedTurn={id:requestId,message,session:activeSession,personalized};
  const response=await fetch('/api/chat',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({space_id:origin,session_id:activeSession,request_id:requestId,message,history:mode==='zmq'?[]:history.slice(-50),preferences:mode==='zmq'?[]:personalized?tags:[],personalized})});
  const data=await response.json();if(origin!==spaceId||controller.signal.aborted)return;
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'请求失败，请检查输入后重试');
  const previousHeight=loading.getBoundingClientRect().height;loading.classList.remove('thinking');loading.textContent=data.message;history.push({role:'user',content:message},{role:'assistant',content:data.message});videos=data.videos;resultDemo=Boolean(data.demo);recommendationTurns.push({messageIndex:history.length-1,videos:[...videos],demo:resultDemo,requestId:data.request_id});loading.dataset.messageIndex=history.length-1;attachRecommendations(loading,videos,resultDemo);
  const session=sessions.find(s=>s.id===activeSession);if(session)session.trait_proposals=[...(session.trait_proposals||[]),...(data.trait_proposals||[])];
  attachTraitProposals(loading,session?.trait_proposals||data.trait_proposals||[]);failedTurn=null;updateResultHeading();saveSession();render();stashSpace();animateReply(loading,previousHeight);
 }catch(err){if(origin===spaceId&&!controller.signal.aborted){const previousHeight=loading.getBoundingClientRect().height;loading.classList.remove('thinking');loading.classList.add('error');loading.textContent=err.message;animateReply(loading,previousHeight);$('#message').value=message}}
 finally{if(requestController===controller){busy=false;pendingMessage='';requestController=null;pendingTurn=null;$('#send').disabled=false;syncPreparationEvents(true)}}
};
$('#message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#chat-form').requestSubmit()}};
document.querySelectorAll('[data-prompt]').forEach(el=>el.onclick=()=>{$('#message').value=el.dataset.prompt;$('#message').focus()});
$('#personalized').onchange=()=>persist('personalized',$('#personalized').checked);
function newConversation(){if(busy){toast('请等待当前推荐完成');return}closeWatch({immediate:true,restoreFocus:false});history=[];recommendationTurns=[];activeSession=null;failedTurn=null;persist('activeSession',null);videos=freshVideos();resultDemo=true;setChatStarted(false,chatStarted);$('#message').value='';updateResultHeading();render();renderSessions();navigate('discover');toast('已开始新对话')}
$('#history-new').onclick=newConversation;
$('#tag-form').onsubmit=async e=>{
 e.preventDefault();const text=$('#tag-input').value.trim();if(!text)return;
 if(mode!=='zmq'){if(tags.length>=30){toast('最多保存 30 个兴趣标签');return}if(tags.includes(text)){toast('这个兴趣已经存在');return}tags.push(text);persist('tags',tags);$('#tag-input').value='';render();return}
 const origin=spaceId;const button=$('#tag-form button');button.disabled=true;
 try{const data=await stateAPI(origin,'/traits',{method:'POST',body:JSON.stringify({text})});if(origin!==spaceId)return;spaceTraits=data.traits;tags=spaceTraits.map(t=>t.text);persist('traits',spaceTraits);persist('tags',tags);$('#tag-input').value='';render()}catch(error){toast(error.message)}finally{button.disabled=false}
};
$('#tags').onclick=async e=>{
 const local=e.target.closest('[data-tag]');if(local){tags.splice(Number(local.dataset.tag),1);persist('tags',tags);render();return}
 const button=e.target.closest('[data-trait]');if(!button)return;const origin=spaceId;button.disabled=true;
 try{await stateAPI(origin,'/traits/'+button.dataset.trait,{method:'DELETE'});if(origin!==spaceId)return;spaceTraits=spaceTraits.filter(t=>t.id!==button.dataset.trait);tags=spaceTraits.map(t=>t.text);persist('traits',spaceTraits);persist('tags',tags);render()}catch(error){button.disabled=false;toast(error.message)}
};
document.addEventListener('click',e=>{const b=e.target.closest('[data-action]');if(!b)return;const v=b.closest('.bubble')?._videos?.find(v=>v.id===b.dataset.id)||b.closest('#detail')?._video||videos.find(v=>v.id===b.dataset.id)||saved[b.dataset.id]||localCacheVideos.get(b.dataset.id)||freshVideos().find(v=>v.id===b.dataset.id);if(!v)return;const action=b.dataset.action;if(action==='save'){if(saved[v.id]){delete saved[v.id];toast('已取消收藏')}else{saved[v.id]=v;toast('已加入我的资料库')}persist('saved',saved);if(v.local)stateAPI(spaceId,'/local-videos/'+encodeURIComponent(v.id),{method:'PATCH',body:JSON.stringify({pinned:Boolean(saved[v.id])})}).catch(error=>toast(error.message));render()}else if(action==='like'||action==='dislike'){feedback[v.id]=feedback[v.id]===action?null:action;persist('feedback',feedback);render();toast('反馈已保存在本地，后端接入后可用于画像更新')}else if(action==='detail'){showDetail(v)}else if(action==='watch'){startWatch(v,b)}});
function videoDestination(v){try{const url=new URL(v.url);if(['https:','http:'].includes(url.protocol))return url.href}catch{}return 'https://search.bilibili.com/all?keyword='+encodeURIComponent(v.title)}
function showDetail(v){$('#detail')._video=v;$('#detail-content').innerHTML=`<span class="eyebrow">VIDEO NOTES</span><h2 style="margin-top:20px">${escapeHTML(v.title)}</h2>${v.url?`<button type="button" class="primary" data-action="watch" data-id="${escapeHTML(v.id)}">在这里播放 ▶</button>`:''}<p>${escapeHTML(v.summary)}</p><p>${escapeHTML(v.author)}${v.minutes?' · '+escapeHTML(v.minutes)+' 分钟':''}${v.level?' · '+escapeHTML(v.level):''}</p><div class="card-tags"><span>${escapeHTML(v.category)}</span><span>${escapeHTML(v.style)}</span></div><p class="detail-reason">✦ 推荐理由<br>${escapeHTML(v.reason||'示例推荐：探索 AI 与工程实践，找到下一步学习方向。')}</p><div class="card-actions detail-feedback"><button data-action="like" data-id="${escapeHTML(v.id)}" class="${feedback[v.id]==='like'?'selected':''}">♡ 喜欢</button><button data-action="dislike" data-id="${escapeHTML(v.id)}" class="${feedback[v.id]==='dislike'?'selected':''}">− 不感兴趣</button></div><label for="note">我的学习笔记</label><textarea id="note" placeholder="记下你想学的内容或观看心得…"></textarea><button id="save-note" class="primary">保存笔记</button><br><a class="external" target="_blank" rel="noopener noreferrer" href="${escapeHTML(videoDestination(v))}">${v.url?'打开原视频 ↗':'在 Bilibili 搜索此主题 ↗'}</a><p style="font-size:10px">${v.url?'来自网络搜索，内容与可用性以来源平台为准。':'当前为虚构示例元数据，搜索链接不代表已核验的视频来源。'}</p>`;$('#note').value=notes[v.id]||'';$('#save-note').onclick=()=>{notes[v.id]=$('#note').value;persist('notes',notes);toast('笔记已保存')};$('#detail').showModal()}
$('#close-detail').onclick=()=>$('#detail').close();$('#detail').onclick=e=>{if(e.target===$('#detail')){const r=$('#detail').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)$('#detail').close()}};
function renderSpaceMenu(){
 $('#space-options').innerHTML=SPACES.map(s=>`<button class="space-option ${s.id===spaceId?'selected':''}" data-space="${s.id}" style="--option-hue:${s.hue}" aria-pressed="${s.id===spaceId}"><span class="space-option-icon">${s.icon}</span><span><strong>${s.name}</strong><small>${s.description}</small></span><span class="space-check">${s.id===spaceId?'✓':''}</span></button>`).join('');
}
function setSpaceMenu(open){$('#space-menu').classList.toggle('hidden',!open);$('#space-toggle').setAttribute('aria-expanded',String(open))}
$('#space-toggle').onclick=()=>{if(!spaceTransition)setSpaceMenu($('#space-menu').classList.contains('hidden'))};
$('#space-options').onclick=e=>{const option=e.target.closest('[data-space]');if(option){setSpaceMenu(false);switchSpace(option.dataset.space)}};
document.addEventListener('click',e=>{if(!e.target.closest('.space-picker'))setSpaceMenu(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#space-menu').classList.contains('hidden')){setSpaceMenu(false);$('#space-toggle').focus()}});
function updateSpaceIdentity(){
 const space=SPACES.find(s=>s.id===spaceId);document.documentElement.style.setProperty('--tone',space.hue);
 document.documentElement.dataset.space=spaceId;$('#space-name').textContent=space.name;$('#sidebar-space-name').textContent=space.name;$('.breadcrumb').textContent=space.name;
 const prompts={
  learning:[['✧ AI Agent 入门','想了解 AI Agent，适合入门，偏实战，20 分钟以内'],['↗ 拓宽科技视野','推荐一些能拓宽视野的科技视频'],['◷ 十分钟学点新知','只有十分钟，想学一个有趣的新知识']],
  roaming:[['☷ 威尼斯漫步','想看看威尼斯的水巷和街头生活，节奏轻松一点'],['↗ 城市漫步','推荐一些有当地文化和生活细节的城市漫步视频'],['◷ 自然一刻','想看十分钟的自然风景，放松一下']],
 }[spaceId];
 document.querySelectorAll('[data-prompt]').forEach((el,i)=>{el.textContent=prompts[i][0];el.dataset.prompt=prompts[i][1]});
 $('#message').placeholder='想看点什么？试试「'+prompts[0][1]+'」';
 renderSpaceMenu();renderSpaceArt();
}
function stashSpace(){
 persist('view',{history,videos,recommendationTurns,activeSession,resultDemo,chatStarted,page,draft:pendingMessage||$('#message').value,tagDraft:$('#tag-input').value,scrollY:window.scrollY,trackScroll:$('#video-grid').scrollLeft});
}
function loadSpace(){
 const space=SPACES.find(s=>s.id===spaceId);
 spaceTraits=readSpace('traits',[]);saved=readSpace('saved',{});tags=mode==='zmq'?spaceTraits.map(t=>t.text):readSpace('tags',[...space.tags]);feedback=readSpace('feedback',{});notes=readSpace('notes',{});
 sessions=readSpace('sessions',[]);activeSession=readSpace('activeSession',null);history=[];recommendationTurns=[];videos=freshVideos();resultDemo=true;page='discover';
 const view=readSpace('view',null);const session=sessions.find(s=>s.id===activeSession);
 if(view){history=view.history||[];recommendationTurns=view.recommendationTurns||[];activeSession=view.activeSession;resultDemo=view.resultDemo;videos=restoreVideoSelection(view.videos||freshVideos(),resultDemo);page=['discover','library','profile'].includes(view.page)?view.page:'discover'}
 else if(session){history=[...session.history];recommendationTurns=session.recommendationTurns||[];resultDemo=session.demo;videos=restoreVideoSelection(session.videos,resultDemo)}
 $('#personalized').checked=readSpace('personalized',true);$('#message').value=view?.draft||'';$('#tag-input').value=view?.tagDraft||'';
 setChatStarted(view?.chatStarted??history.length>0);restoreConversation();
 document.querySelectorAll('.page').forEach(el=>el.classList.toggle('hidden',el.id!==page));
 document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
 $('#page-title').textContent={discover:'对话发现',library:'我的资料库',profile:'偏好'}[page];
 updateSpaceIdentity();updateResultHeading();render();renderSessions();refreshServiceStatus();
 setShelfPosition($('#video-grid'),view?.trackScroll||0);
 return view?.scrollY||0;
}
async function switchSpace(target){
 if(target===spaceId||spaceTransition||!ready)return;
 closeWatch({immediate:true,restoreFocus:false});spaceTransition=true;setSpaceMenu(false);stopExplore();stashSpace();
 const remote=mode==='zmq'?fetchRemoteSpace(target).then(data=>({data}),error=>({error})):null;cancelPendingTurn();failedTurn=null;
 requestController?.abort();requestController=null;pendingMessage='';busy=false;$('#send').disabled=false;
 $('#detail').close();++navigationVersion;
 document.querySelectorAll('.page').forEach(el=>el.getAnimations().forEach(a=>a.cancel()));$('.history-sidebar').getAnimations().forEach(a=>a.cancel());
 const direction=SPACES.findIndex(s=>s.id===target)>SPACES.findIndex(s=>s.id===spaceId)?1:-1;
 const main=$('main');main.inert=true;main.classList.add('space-switching');let exit=null,enter=null;
 try{
  document.documentElement.style.setProperty('--tone',SPACES.find(s=>s.id===target).hue);
  if(!reducedMotion.matches){
   exit=main.animate([{opacity:1,transform:'translateY(0)'},{opacity:0,transform:`translateY(${-direction*46}px)`}],{duration:180,easing:'ease-in',fill:'forwards'});
   try{await exit.finished}catch{}
  }
  // Save the outgoing strip's final phase; the loop continues throughout the transition.
  const outgoingView=readSpace('view',{});outgoingView.trackScroll=loopShelves.get($('#video-grid'))?.position??$('#video-grid').scrollLeft;persist('view',outgoingView);
  if(remote){const result=await remote;if(result.error)throw result.error;cacheRemoteSpace(target,result.data)}
  spaceId=target;try{localStorage.setItem('vivideo.currentSpace',JSON.stringify(spaceId))}catch{}
  const scrollY=loadSpace();window.scrollTo({top:scrollY,behavior:'instant'});
  if(!reducedMotion.matches){
   enter=main.animate([{opacity:0,transform:`translateY(${direction*56}px)`},{opacity:1,transform:'translateY(0)'}],{duration:360,easing:'cubic-bezier(.2,.7,.2,1)',fill:'forwards'});
   try{await enter.finished}catch{}
  }
 }catch(error){document.documentElement.style.setProperty('--tone',SPACES.find(s=>s.id===spaceId).hue);toast(error.message)}finally{
  exit?.cancel();enter?.cancel();main.classList.remove('space-switching');main.inert=false;spaceTransition=false;
 }
}
window.addEventListener('pagehide',()=>{if(ready)stashSpace();cancelPendingTurn();closeWatch({immediate:true,restoreFocus:false})});
let serviceStatusVersion=0;
function showServiceStatus(id,label,state){const el=$(id);el.textContent=label;el.dataset.state=state}
async function refreshServiceStatus({quiet=false}={}){
 const origin=spaceId,version=++serviceStatusVersion;
 if(!quiet){showServiceStatus('#database-status','数据库服务检测中','checking');showServiceStatus('#model-api-status','模型 API 检测中','checking')}
 try{
  const response=await fetch('/api/service-status?space_id='+encodeURIComponent(origin),{signal:AbortSignal.timeout(8000),cache:'no-store'});
  if(!response.ok)throw Error('status');const data=await response.json();if(origin!==spaceId||version!==serviceStatusVersion)return;
  const db=data.database.state;showServiceStatus('#database-status',db==='connected'?'数据库服务已连接':'数据库服务未连接',db);
  const state=data.model_api.state;
  const labels={connected:'模型 API 已连接',unconfigured:'模型 API 未配置',unauthorized:'模型 API 认证失败',limited:'模型 API 请求受限',unverified:'模型 API 未验证',model_unlisted:'模型 API：模型未验证',disconnected:'模型 API 未连接'};
  showServiceStatus('#model-api-status',labels[state]||'模型 API 未验证',state);
  $('#model-api-status').title=data.model_api.verified_by==='models'?'当前 API 可访问，模型列表包含所选模型；不代表生成或工具调用已经验证。':state==='unverified'?'该服务未提供兼容的模型列表接口，无法验证连接。':state==='model_unlisted'?'API 可访问，但所选模型未出现在模型列表中，暂无法验证。':'';
 }catch{
  if(origin!==spaceId||version!==serviceStatusVersion)return;
  showServiceStatus('#database-status','数据库服务未连接','disconnected');showServiceStatus('#model-api-status','模型 API 状态未知','unverified');
 }
}
setInterval(()=>{if(ready&&!document.hidden&&!spaceTransition)refreshServiceStatus({quiet:true})},30000);
document.addEventListener('visibilitychange',()=>{if(ready&&!document.hidden)refreshServiceStatus()});
async function init(){
 updateSpaceIdentity();
 try{
  const [health,learning,roaming]=await Promise.all([fetch('/api/health'),fetch('/api/videos?space_id=learning'),fetch('/api/videos?space_id=roaming')]);if(!health.ok||!learning.ok||!roaming.ok)throw Error('加载失败');
  const h=await health.json();mode=h.mode;$('#local-library').hidden=mode!=='zmq';refreshServiceStatus();
  if(mode==='zmq'){const states=await Promise.all(SPACES.map(async space=>({space:space.id,data:await fetchRemoteSpace(space.id)})));states.forEach(state=>cacheRemoteSpace(state.space,state.data));$('.history-local').textContent='保存在当前空间';$('.profile-panel>p').textContent='来源：你的确认和手动编辑 · 保存在当前空间';$('.profile-info').textContent='已确认偏好仅在开启「结合我的长期偏好」时供模型参考。你可以随时删除。';$('.settings-mode').textContent='选择的模型用于当前空间的新请求。'}
  initialVideos={learning:(await learning.json()).videos,roaming:(await roaming.json()).videos};const scrollY=loadSpace();ready=true;window.scrollTo({top:scrollY,behavior:'instant'});
 }catch{toast('服务加载失败，请刷新重试')}
}

// Pointer proximity highlights cards while hovering pauses the loop.
const loopShelves=new Map();let loopFrame=0,lastLoopTime=0;
function renderShelf(shelf,items){
 items=uniqueRecommendations(items);
 const stationary=items.length<=3;
 shelf.classList.toggle('static-shelf',stationary);shelf.style.setProperty('--shelf-count',items.length);
 shelf.tabIndex=stationary?-1:0;shelf.setAttribute('aria-label',stationary?'视频推荐列表':'视频推荐，左右方向键滚动');
 const controls=shelf.id==='video-grid'?[$('#carousel-prev'),$('#carousel-next')]:[...shelf.previousElementSibling?.querySelectorAll('[data-shelf-scroll]')||[]];
 controls.forEach(button=>button.hidden=stationary);
 const previous=loopShelves.get(shelf);const signature=items.map(item=>item.id).join('|');const reusable=previous?.spaceId===spaceId&&previous?.signature===signature;const phase=reusable&&previous.period?previous.position/previous.period:0;
 shelf._shelfItems=[...items];shelf.innerHTML=items.map(compactCard).join('');
 if(stationary){
  loopShelves.delete(shelf);shelf.scrollLeft=0;hydrateCovers(shelf);
  if(!loopShelves.size){cancelAnimationFrame(loopFrame);loopFrame=0;lastLoopTime=0}
  return;
 }
 const first=shelf.firstElementChild;const width=first.getBoundingClientRect().width||parseFloat(getComputedStyle(first).flexBasis)||255;
 const gap=parseFloat(getComputedStyle(shelf).gap)||18;const period=(width+gap)*items.length;
 const copies=reducedMotion.matches?1:Math.max(2,Math.ceil((shelf.clientWidth||width)/period)+2);
 const originals=[...shelf.children];
 for(let copy=1;copy<copies;copy++)for(const original of originals){
  const clone=original.cloneNode(true);clone.dataset.loopCopy='true';clone.setAttribute('aria-hidden','true');clone.querySelectorAll('button').forEach(button=>button.tabIndex=-1);shelf.append(clone);
 }
 const state={spaceId,signature,period:reducedMotion.matches?0:period,position:((phase%1)+1)%1*period,manualUntil:reusable?previous.manualUntil:0};
 loopShelves.set(shelf,state);shelf.scrollLeft=state.position;
 if(!shelf.dataset.loopReady){
  shelf.dataset.loopReady='true';
  const pause=()=>{const current=loopShelves.get(shelf);if(current)current.manualUntil=performance.now()+2000};
  shelf.addEventListener('wheel',pause,{passive:true});shelf.addEventListener('pointerdown',pause,{passive:true});
  shelf.addEventListener('scroll',()=>{const current=loopShelves.get(shelf);if(current&&!spaceTransition&&(shelf.matches(':hover')||shelf.contains(document.activeElement)||performance.now()<current.manualUntil))current.position=shelf.scrollLeft},{passive:true});
  shelf.addEventListener('pointerdown',e=>{if(e.target.closest('[data-loop-copy]')&&e.pointerType==='mouse')e.preventDefault()});
 }
 hydrateCovers(shelf);startShelfLoop();
}
function setShelfPosition(shelf,position){
 if(shelf.classList.contains('static-shelf')){shelf.scrollLeft=0;return}
 const state=loopShelves.get(shelf);const offset=Number.isFinite(position)?position:0;
 if(state){state.position=state.period?((offset%state.period)+state.period)%state.period:offset;shelf.scrollLeft=state.position}
 else shelf.scrollLeft=offset;
}
function startShelfLoop(){if(!loopFrame&&!document.hidden&&!reducedMotion.matches&&loopShelves.size)loopFrame=requestAnimationFrame(shelfLoopTick)}
function shelfLoopTick(time){
 loopFrame=0;const delta=lastLoopTime?Math.min(time-lastLoopTime,50):0;lastLoopTime=time;
 for(const [shelf,state] of loopShelves){
  if(!shelf.isConnected){loopShelves.delete(shelf);continue}
  if(!state.period||page!=='discover'||isWatching()||document.hidden||reducedMotion.matches||document.querySelector('dialog[open]')||(!spaceTransition&&(shelf.matches(':hover')||shelf.contains(document.activeElement)||time<state.manualUntil))||!shelf.getClientRects().length)continue;
  const box=shelf.getBoundingClientRect();if(box.bottom<=0||box.top>=innerHeight)continue;
  state.position=((state.position+delta*.018)%state.period+state.period)%state.period;shelf.scrollLeft=state.position;
 }
 startShelfLoop();
}
function refreshShelfLoops(){for(const shelf of [...loopShelves.keys()]){if(shelf.isConnected)renderShelf(shelf,shelf._shelfItems);else loopShelves.delete(shelf)}}
window.addEventListener('resize',refreshShelfLoops);
reducedMotion.addEventListener('change',()=>{cancelAnimationFrame(loopFrame);loopFrame=0;lastLoopTime=0;refreshShelfLoops()});
document.addEventListener('visibilitychange',()=>{cancelAnimationFrame(loopFrame);loopFrame=0;lastLoopTime=0;startShelfLoop()});
const track=$('#video-grid');let pointerX=null, scrollFrame=0, previousFrame=0;
function clearCardFocus(){track.querySelectorAll('.card').forEach(c=>{c.style.removeProperty('--focus');c.style.removeProperty('--lift')})}
function stopExplore(){pointerX=null;cancelAnimationFrame(scrollFrame);scrollFrame=0;previousFrame=0;clearCardFocus()}
function exploreFrame(time){
 if(pointerX===null||page!=='discover'||document.hidden){stopExplore();return}
 // Hover keeps the shelf still while the pointer controls card focus.
 track.querySelectorAll('.card').forEach(c=>{const b=c.getBoundingClientRect();const focus=Math.max(0,1-Math.abs(pointerX-(b.left+b.width/2))/(b.width*.85));c.style.setProperty('--focus',focus.toFixed(3));c.style.setProperty('--lift',`${(-focus*7).toFixed(2)}px`)});
 scrollFrame=requestAnimationFrame(exploreFrame);
}
track.addEventListener('pointermove',e=>{if(e.pointerType!=='mouse'||reducedMotion.matches)return;pointerX=e.clientX;if(!scrollFrame)scrollFrame=requestAnimationFrame(exploreFrame)});
track.addEventListener('pointerleave',stopExplore);track.addEventListener('pointerdown',stopExplore);
window.addEventListener('blur',stopExplore);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopExplore()});
function scrollShelf(shelf,direction){
 if(shelf.classList.contains('static-shelf'))return;
 const state=loopShelves.get(shelf);if(state)state.manualUntil=performance.now()+2000;
 const step=shelf.querySelector('.card')?.getBoundingClientRect().width+18||300;
 if(state?.period&&direction<0&&shelf.scrollLeft<step)shelf.scrollLeft+=state.period;
 shelf.scrollBy({left:direction*step,behavior:reducedMotion.matches?'instant':'smooth'});
}
function scrollCards(direction){stopExplore();scrollShelf(track,direction)}
document.addEventListener('click',e=>{const button=e.target.closest('[data-shelf-scroll]');if(button){const shelf=button.closest('.bubble').querySelector('.bubble-results');scrollShelf(shelf,Number(button.dataset.shelfScroll))}});
$('#carousel-prev').onclick=()=>scrollCards(-1);$('#carousel-next').onclick=()=>scrollCards(1);
track.addEventListener('keydown',e=>{if(e.target===track&&['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();scrollCards(e.key==='ArrowLeft'?-1:1)}});

const ART_ICONS={
 book:'<path d="M12 5v15M3 4c3-1 6-1 9 1 3-2 6-2 9-1v15c-3-1-6-1-9 1-3-2-6-2-9-1Z"/>',
 code:'<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
 bulb:'<path d="M9 18h6m-6 3h6M8 14a7 7 0 1 1 8 0l-1 2H9Z"/>',
 cap:'<path d="m2 9 10-5 10 5-10 5Zm4 3v6c4 3 8 3 12 0v-6m4-3v8"/>',
 flask:'<path d="M9 3h6m-5 0v7l-6 9c-1 2 0 3 2 3h12c2 0 3-1 2-3l-6-9V3M7 16h10"/>',
 pencil:'<path d="m4 16-1 5 5-1L21 7l-4-4Zm10-10 4 4"/>',
 compass:'<circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5Z"/>',
 mountain:'<path d="m2 20 8-15 5 9 3-5 5 11Zm5-9 3 2 2-1"/>',
 planet:'<circle cx="12" cy="12" r="7"/><ellipse cx="12" cy="12" rx="12" ry="4" transform="rotate(-30 12 12)"/>',
 map:'<path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2Zm6-2v16m6-14v16"/>',
 wave:'<path d="M2 7c3-5 5 5 10 0s7 5 10 0M2 12c3-5 5 5 10 0s7 5 10 0M2 17c3-5 5 5 10 0s7 5 10 0"/>',
 camera:'<rect x="2" y="6" width="20" height="15" rx="3"/><path d="m7 6 2-3h6l2 3"/><circle cx="12" cy="13" r="4"/>'
};
function renderSpaceArt(){
 const icons=spaceId==='learning'?['book','code','bulb','cap','flask','pencil']:['compass','mountain','planet','map','wave','camera'];
 const group=offset=>`<div class="icon-group">${icons.map((_,i)=>{const name=icons[(i+offset)%icons.length];return `<span class="art-tile"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">${ART_ICONS[name]}</svg></span>`}).join('')}</div>`;
 $('#space-art').innerHTML=[0,3].map(offset=>`<div class="icon-lane"><div class="icon-belt">${group(offset)}${group(offset)}</div></div>`).join('');
 $('.side-note .tiny-label').textContent=spaceId==='learning'?'让每一次观看都有方向':'让每一次漫游都有惊喜';
 $('.side-note p').innerHTML=spaceId==='learning'?'从你的好奇心出发，<br>找到值得花时间的内容。':'跟着好奇心走走，<br>偶然遇见，也值得停留。';
 if(!reducedMotion.matches)$('#space-art').animate([{opacity:0,transform:`translateX(${spaceId==='learning'?-12:12}px)`},{opacity:1,transform:'translateX(0)'}],{duration:450,easing:'ease-out'});
}
let modelConfigs=[],selectedModelId=null,editingModelId=null,settingsSpaceId=null,settingsBusy=false;
function resetKeyVisibility(){ $('#api-key').type='password';$('#toggle-api-key').textContent='显示';$('#toggle-api-key').setAttribute('aria-pressed','false');$('#toggle-api-key').setAttribute('aria-label','显示 API Key') }
function modelStatus(message){$('#model-settings-status').textContent=message}
async function modelAPI(path='',options={}){
 const response=await fetch('/api/model-configs'+path,{...options,headers:{'Content-Type':'application/json'}});
 if(!response.ok){let message='模型配置操作失败，请重试';try{const data=await response.json();if(typeof data.detail==='string')message=data.detail;else if(Array.isArray(data.detail))message='请检查配置名称、模型名称和 API 地址'}catch{}throw new Error(message)}
 return response.status===204?null:response.json();
}
function renderModelConfigs(){
 $('#model-config-list').innerHTML=modelConfigs.length?modelConfigs.map(config=>`<div class="model-config-row ${config.id===selectedModelId?'selected':''}"><label class="model-choice"><input type="radio" name="model-config" value="${escapeHTML(config.id)}" ${config.id===selectedModelId?'checked':''}><span><strong>${escapeHTML(config.name)}</strong><small>${escapeHTML(config.model)} · ${config.has_api_key?'已保存密钥':'无密钥'}</small><small>${escapeHTML(config.base_url)}</small></span></label><div class="model-row-actions"><button type="button" class="text-button" data-edit-model="${escapeHTML(config.id)}">编辑</button><button type="button" class="text-button" data-delete-model="${escapeHTML(config.id)}" aria-label="删除 ${escapeHTML(config.name)}">删除</button></div></div>`).join(''):'<p class="model-list-empty">还没有配置，添加一个模型开始。</p>';
}
function editModelConfig(config=null){
 editingModelId=config?.id||null;$('#settings-form').hidden=false;$('#settings-form').reset();resetKeyVisibility();
 $('#model-editor-title').textContent=config?'编辑配置':'新增配置';$('#api-name').value=config?.name||'';$('#api-base').value=config?.base_url||'';$('#api-model').value=config?.model||'';
 $('#api-key').placeholder=config?.has_api_key?'已保存，留空保留原密钥':'填写 API Key';$('.remove-key-label').hidden=!config?.has_api_key;
 $('#api-base').setCustomValidity('');modelStatus('');
}
async function modelOperation(action){
 if(settingsBusy)return;settingsBusy=true;modelStatus('');
 const controls=[...$('#settings-dialog').querySelectorAll('input,button:not(#close-settings)')];controls.forEach(control=>control.disabled=true);
 try{await action()}catch(error){modelStatus(error.message)}finally{settingsBusy=false;$('#settings-dialog').querySelectorAll('input,button').forEach(control=>control.disabled=false)}
}
async function refreshModelConfigs(){
 const data=await modelAPI('?space_id='+encodeURIComponent(settingsSpaceId));refreshServiceStatus();modelConfigs=data.configs;selectedModelId=data.selected_id;renderModelConfigs();
 // New controls inserted by rendering must also stay disabled during a mutation.
 if(settingsBusy)$('#model-config-list').querySelectorAll('input,button').forEach(control=>control.disabled=true);
}
$('#open-settings').onclick=async()=>{
 if(spaceTransition||settingsBusy)return;setSpaceMenu(false);settingsSpaceId=spaceId;
 $('#settings-space-name').textContent=SPACES.find(s=>s.id===settingsSpaceId).name;$('#settings-form').hidden=true;$('#model-config-list').textContent='正在加载…';modelStatus('');$('#settings-dialog').showModal();
 await modelOperation(async()=>{
  await refreshModelConfigs();
  if(!modelConfigs.length&&$('#settings-dialog').open){
   editModelConfig();
   const legacy=readSpace('modelAPI',{});$('#api-base').value=legacy.baseURL||'';$('#api-model').value=legacy.model||'';
   if(legacy.model)$('#api-name').value=legacy.model;
   try{$('#api-key').value=sessionStorage.getItem(spaceKey('apiKey'))||''}catch{}
  }
 });
};
let settingsCloseTimer;
function closeSettings(){
 const dialog=$('#settings-dialog');if(!dialog.open||dialog.classList.contains('closing'))return;
 if(reducedMotion.matches){dialog.close();return}
 dialog.classList.add('closing');settingsCloseTimer=setTimeout(()=>dialog.close(),280);
}
$('#close-settings').onclick=closeSettings;
$('#settings-dialog').addEventListener('cancel',event=>{event.preventDefault();closeSettings()});
$('#settings-dialog').addEventListener('close',()=>{clearTimeout(settingsCloseTimer);$('#settings-dialog').classList.remove('closing');$('#api-key').value='';resetKeyVisibility()});
$('#settings-dialog').onclick=e=>{if(e.target===$('#settings-dialog')){const r=$('#settings-dialog').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeSettings()}};
$('#toggle-api-key').onclick=()=>{const visible=$('#api-key').type==='password';$('#api-key').type=visible?'text':'password';$('#toggle-api-key').textContent=visible?'隐藏':'显示';$('#toggle-api-key').setAttribute('aria-pressed',String(visible));$('#toggle-api-key').setAttribute('aria-label',visible?'隐藏 API Key':'显示 API Key')};
$('#new-model-config').onclick=()=>{if(!settingsBusy){editModelConfig();$('#api-name').focus()}};
$('#cancel-model-edit').onclick=()=>{$('#settings-form').hidden=true;$('#api-key').value='';modelStatus('')};
$('#model-config-list').onchange=e=>{
 if(!e.target.matches('input[name="model-config"]'))return;
 const id=e.target.value;
 modelOperation(async()=>{await modelAPI('/selection/'+settingsSpaceId,{method:'PUT',body:JSON.stringify({config_id:id})});selectedModelId=id;renderModelConfigs();toast('当前空间已切换模型')}).then(()=>{renderModelConfigs();refreshServiceStatus()});
};
$('#model-config-list').onclick=e=>{
 if(settingsBusy)return;
 const edit=e.target.closest('[data-edit-model]');if(edit){editModelConfig(modelConfigs.find(config=>config.id===edit.dataset.editModel));return}
 const remove=e.target.closest('[data-delete-model]');if(remove)modelOperation(async()=>{
  const id=remove.dataset.deleteModel;await modelAPI('/'+id,{method:'DELETE'});
  if(editingModelId===id){$('#settings-form').hidden=true;$('#api-key').value='';editingModelId=null}
  await refreshModelConfigs();toast('模型配置已删除');
 });
};
$('#settings-form').onsubmit=e=>{
 e.preventDefault();
 const payload={name:$('#api-name').value.trim(),base_url:$('#api-base').value.trim(),model:$('#api-model').value.trim()};
 const secret=$('#api-key').value.trim();if($('#remove-api-key').checked)payload.api_key='';else if(secret)payload.api_key=secret;
 modelOperation(async()=>{
  const created=!editingModelId;
  const config=await modelAPI(editingModelId?'/'+editingModelId:'',{method:editingModelId?'PUT':'POST',body:JSON.stringify(payload)});
  // Clear the secret immediately after persistence, including if subsequent selection fails.
  $('#api-key').value='';editingModelId=config.id;
  if(created)await modelAPI('/selection/'+settingsSpaceId,{method:'PUT',body:JSON.stringify({config_id:config.id})});
  await refreshModelConfigs();$('#settings-form').hidden=true;
  try{localStorage.removeItem(`vivideo.space.${settingsSpaceId}.modelAPI`);sessionStorage.removeItem(`vivideo.space.${settingsSpaceId}.apiKey`)}catch{}
  toast('模型配置已保存');
 });
};

const coverCache=new Map(),sdkLoads=new Map();let activeWatch=null,watchGeneration=0,watchCloseTimer=null;
function isWatching(){return Boolean(activeWatch)}
function formatVideoTime(seconds){const total=Math.floor(seconds);return `${Math.floor(total/60)}:${String(total%60).padStart(2,'0')}`}
function hydrateCovers(root){
 const origin=spaceId;
 for(const cover of root.querySelectorAll('[data-video-cover-id]')){
  const id=cover.dataset.videoCoverId,key=origin+'/'+id;let cached=coverCache.get(key);
  if(!cached||cached.expires<Date.now()){
   const promise=stateAPI(origin,`/videos/${encodeURIComponent(id)}/metadata`);
   cached={promise,expires:Date.now()+60000};coverCache.set(key,cached);
   if(coverCache.size>512)coverCache.delete(coverCache.keys().next().value);
  }
  cached.promise.then(info=>{
   if(!cover.isConnected||origin!==spaceId)return;
   const image=cover.querySelector('img'),label=cover.querySelector('.cover-loading');
   const duration=cover.querySelector('[data-cover-duration]');if(info.duration_seconds){duration.textContent=formatVideoTime(info.duration_seconds);duration.hidden=false}
   if(!info.thumbnail_url){label.textContent='封面暂不可用';cover.dataset.coverState='unavailable';return}
   image.onload=()=>{if(!cover.isConnected)return;image.hidden=false;label.hidden=true;cover.dataset.coverState='ready'};
   image.onerror=()=>{image.hidden=true;label.hidden=false;label.textContent='封面暂不可用';cover.dataset.coverState='unavailable'};
   image.hidden=false;image.src=info.thumbnail_url;
  }).catch(()=>{if(cover.isConnected&&origin===spaceId){cover.querySelector('.cover-loading').textContent='封面暂不可用';cover.dataset.coverState='unavailable'}});
 }
}
function playerNote(watch,text){if(activeWatch===watch)$('#watch-note').textContent=text}
function playerLoaded(watch){if(activeWatch===watch)$('#watch-loading').hidden=true}
function playerVisible(watch){return activeWatch===watch&&watch.inView&&!document.hidden&&!document.querySelector('dialog[open]')}
function sampleWatch(watch,state){
 if(activeWatch!==watch||!watch.tracking)return;
 watch.counter.sample({...state,visible:playerVisible(watch)},performance.now());
 reportWatch(watch,false);
}
function reportWatch(watch,force=false){
 if(mode!=='zmq'||!watch.counter.qualified)return;
 const watched=watch.counter.watched;
 if(!force&&(watch.reporting||Date.now()<(watch.retryAfter||0)||watched-watch.lastReported<10&&watch.lastReported>0))return;
 if(!force)watch.reporting=true;
 const payload={event_id:watch.id,video_id:watch.video.id,session_id:watch.session,
  watched_seconds:watched,duration_seconds:watch.counter.duration};
 fetch(`/api/spaces/${watch.space}/watch-events`,{method:'POST',keepalive:force,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
 .then(async response=>{if(!response.ok)throw Error('watch event');const result=await response.json();if(result.recorded)watch.lastReported=Math.max(watch.lastReported,watched)})
 .catch(()=>{watch.retryAfter=Date.now()+5000}).finally(()=>{watch.reporting=false});
}
function destroyPlayback(watch){
 clearInterval(watch.timer);clearInterval(watch.leaseTimer);watch.timer=null;watch.abort.abort();watch.observer?.disconnect();
 if(watch.native){watch.native.pause();watch.native.removeAttribute('src');watch.native.load()}
 if(watch.frame)watch.frame.src='about:blank';
 if(watch.sdk){try{const result=watch.sdk.destroy();if(result?.catch)result.catch(()=>{})}catch{}}
 watch.sdk=null;watch.native=null;
}
function closeWatch({immediate=false,restoreFocus=true}={}){
 const watch=activeWatch;if(!watch)return;
 // Read actual media progress before closing; never infer it from wall time alone.
 watch.sample?.();
 reportWatch(watch,true);activeWatch=null;++watchGeneration;destroyPlayback(watch);
 const region=$('#watch-region'),panel=$('#watch-player');region.classList.remove('watching');panel.inert=true;
 $('.conversation-stage').inert=false;$('.composer-wrap').inert=false;
 clearTimeout(watchCloseTimer);
 const finish=()=>{if(activeWatch)return;panel.hidden=true;$('#watch-mount').replaceChildren();panel.inert=false};
 if(immediate||reducedMotion.matches)finish();else watchCloseTimer=setTimeout(finish,360);
 if(restoreFocus){const target=watch.trigger?.isConnected?watch.trigger:$('#message');target.focus({preventScroll:true})}
}
function loadPlayerSDK(name){
 if(name==='youtube'&&window.YT?.Player)return Promise.resolve();
 if(name==='vimeo'&&window.Vimeo?.Player)return Promise.resolve();
 if(sdkLoads.has(name))return sdkLoads.get(name);
 const promise=new Promise((resolve,reject)=>{
  const script=document.createElement('script');let timer;
  const done=()=>{clearTimeout(timer);resolve()};
  const fail=()=>{clearTimeout(timer);script.remove();sdkLoads.delete(name);reject(Error('平台播放器加载失败，请稍后重试或打开原网页。'))};
  if(name==='youtube'){
   const previous=window.onYouTubeIframeAPIReady;
   window.onYouTubeIframeAPIReady=()=>{try{previous?.()}catch{}done()};script.src='https://www.youtube.com/iframe_api';
  }else{script.src='https://player.vimeo.com/api/player.js';script.onload=()=>window.Vimeo?.Player?done():fail()}
  script.onerror=fail;timer=setTimeout(fail,15000);document.head.append(script);
 });
 sdkLoads.set(name,promise);return promise;
}
function embedFallback(watch,info){
 if(activeWatch!==watch)return;
 clearInterval(watch.timer);watch.timer=null;watch.tracking=false;watch.counter.reset();
 if(watch.native){watch.native.pause();watch.native.removeAttribute('src');watch.native.load();watch.native=null}
 $('#watch-mount').replaceChildren();const frame=document.createElement('iframe');frame.src=info.embed_url;
 frame.title=watch.video.title;frame.allow='autoplay; fullscreen; picture-in-picture';frame.allowFullscreen=true;
 watch.frame=frame;frame.referrerPolicy='strict-origin-when-cross-origin';frame.onload=()=>playerLoaded(watch);$('#watch-mount').append(frame);
 playerNote(watch,'当前平台未提供可读取的播放进度，本次观看暂不计入记录。');
}
async function mountPlayback(watch,info){
 if(activeWatch!==watch)return;
 watch.info=info;watch.tracking=Boolean(info.tracking_supported);
 if(info.provider==='local')watch.leaseTimer=setInterval(()=>{if(activeWatch===watch)stateAPI(watch.space,'/local-videos/'+encodeURIComponent(watch.video.id)+'/lease',{method:'POST'}).catch(()=>{})},30000);
 if(info.kind==='native'){
  const video=document.createElement('video');watch.native=video;video.controls=true;video.playsInline=true;video.preload='metadata';video.src=info.stream_url;
  if(info.thumbnail_url)video.poster=info.thumbnail_url;
  if(info.subtitles_url){const track=document.createElement('track');track.kind='subtitles';track.label='字幕';track.srclang='zh';track.src=info.subtitles_url;video.append(track)}
  const sample=override=>sampleWatch(watch,{position:video.currentTime,duration:video.duration,
   playing:typeof override==='boolean'?override:!video.paused&&!video.ended&&video.readyState>=3&&!watch.buffering,seeking:video.seeking,rate:video.playbackRate});
  watch.sample=()=>sample();
  const reset=()=>watch.counter.reset();
  video.addEventListener('loadedmetadata',()=>playerLoaded(watch));
  video.addEventListener('playing',()=>{watch.buffering=false;reset()});
  video.addEventListener('waiting',()=>{watch.buffering=true;reset()});
  video.addEventListener('stalled',reset);
  for(const event of ['pause','ended'])video.addEventListener(event,()=>{sample(true);reset();reportWatch(watch,true)});
  for(const event of ['seeking','seeked','ratechange'])video.addEventListener(event,()=>{reset();reportWatch(watch,true)});
  video.addEventListener('error',()=>{if(activeWatch===watch){if(info.embed_url)embedFallback(watch,info);else{watch.tracking=false;playerNote(watch,'本地视频暂时无法播放，请检查缓存或重新下载。');playerLoaded(watch)}}});
  $('#watch-mount').replaceChildren(video);watch.timer=setInterval(sample,500);
  video.play().catch(()=>playerNote(watch,'点击播放，开始观看。'));return;
 }
 if(info.kind==='youtube'){
  await loadPlayerSDK('youtube');if(activeWatch!==watch)return;
  const node=document.createElement('div');node.id='youtube-watch-'+watch.id;$('#watch-mount').replaceChildren(node);
  watch.sdk=new YT.Player(node.id,{videoId:info.platform_id,playerVars:{playsinline:1,origin:location.origin,autoplay:1},events:{
   onReady:event=>{if(activeWatch!==watch)return;playerLoaded(watch);event.target.playVideo()},
   onStateChange:event=>{if(activeWatch!==watch)return;if((event.data===0||event.data===2)&&watch.playing)watch.sample?.(true);watch.playing=event.data===1;watch.counter.reset();if(!watch.playing)reportWatch(watch,true)},
   onPlaybackRateChange:()=>watch.counter.reset(),
   onError:()=>{watch.tracking=false;watch.counter.reset();playerLoaded(watch);playerNote(watch,'视频暂时无法在此播放，可尝试打开原网页。')},
   onAutoplayBlocked:()=>playerNote(watch,'点击播放，开始观看。')
  }});
  watch.sample=override=>{
   if(activeWatch!==watch||!watch.sdk?.getCurrentTime)return;
   sampleWatch(watch,{position:watch.sdk.getCurrentTime(),duration:watch.sdk.getDuration(),playing:override===true||watch.sdk.getPlayerState()===1,rate:watch.sdk.getPlaybackRate()});
  };
  watch.timer=setInterval(()=>watch.sample(),500);return;
 }
 if(info.kind==='vimeo'){
  await loadPlayerSDK('vimeo');if(activeWatch!==watch)return;
  watch.sdk=new Vimeo.Player($('#watch-mount'),{id:Number(info.platform_id),autoplay:true,playsinline:true});
  watch.rate=1;watch.playing=false;watch.seeking=false;watch.buffering=false;
  watch.sdk.on('timeupdate',data=>sampleWatch(watch,{position:data.seconds,duration:data.duration,
    playing:watch.playing&&!watch.buffering,seeking:watch.seeking,rate:watch.rate}));
  watch.sdk.on('playing',()=>{watch.playing=true;watch.buffering=false;watch.counter.reset()});
  for(const event of ['pause','ended'])watch.sdk.on(event,data=>{if(watch.playing)sampleWatch(watch,{position:data.seconds,duration:data.duration,playing:true,seeking:watch.seeking,rate:watch.rate});watch.playing=false;watch.counter.reset();reportWatch(watch,true)});
  watch.sdk.on('seeking',()=>{watch.seeking=true;watch.counter.reset()});watch.sdk.on('seeked',()=>{watch.seeking=false;watch.counter.reset()});
  watch.sdk.on('playbackratechange',data=>{watch.rate=data.playbackRate;watch.counter.reset()});
  watch.sdk.on('bufferstart',()=>{watch.buffering=true;watch.counter.reset()});watch.sdk.on('bufferend',()=>{watch.buffering=false;watch.counter.reset()});
  watch.sdk.on('error',()=>{watch.tracking=false;watch.counter.reset();playerLoaded(watch);playerNote(watch,'视频暂时无法在此播放，可尝试打开原网页。')});
  await watch.sdk.ready();playerLoaded(watch);return;
 }
 embedFallback(watch,info);
}
async function startWatch(video,trigger){
 if(!video.url){toast('示例内容没有真实视频，请先发送需求获取推荐。');return}
 if(spaceTransition)return;
 const origin=spaceId;const generation=++watchGeneration;
 if(page!=='discover')await navigate('discover');
 if(origin!==spaceId||spaceTransition||generation!==watchGeneration)return;
 closeWatch({immediate:true,restoreFocus:false});clearTimeout(watchCloseTimer);
 const watch={id:crypto.randomUUID(),video,space:origin,session:activeSession,trigger,counter:new VivideoWatchAccumulator(),
  tracking:false,inView:false,lastReported:0,reporting:false,abort:new AbortController()};activeWatch=watch;
 const region=$('#watch-region'),panel=$('#watch-player');
 const composerHeight=$('.composer-wrap').getBoundingClientRect().height;
 $('#detail').close();$('#watch-title').textContent=video.title;$('#watch-provider').textContent='';$('#watch-note').textContent='';
 $('#watch-source').href=videoDestination(video);$('#watch-mount').replaceChildren();$('#watch-loading').textContent='正在加载播放器…';$('#watch-loading').hidden=false;
 panel.hidden=false;panel.inert=false;$('.conversation-stage').inert=true;$('.composer-wrap').inert=true;
 // Keep the whole conversation/composer rectangle fixed while only its inner layout changes.
 panel.getBoundingClientRect();region.classList.add('watching');
 if(!reducedMotion.matches)$('#watch-player .watch-input').animate([{height:composerHeight+'px'},{height:'46px'}],{duration:360,easing:'cubic-bezier(.2,.7,.2,1)'});
 $('#close-watch').focus({preventScroll:true});
 watch.observer=new IntersectionObserver(entries=>{const visible=entries[0].intersectionRatio>=.5;if(watch.inView!==visible)watch.counter.reset();watch.inView=visible;if(!visible)reportWatch(watch,true)},{threshold:[0,.5,1]});watch.observer.observe($('.watch-screen'));
 try{
  const info=await stateAPI(origin,`/videos/${encodeURIComponent(video.id)}/playback`,{signal:watch.abort.signal});
  if(activeWatch!==watch)return;$('#watch-provider').textContent={bilibili:'哔哩哔哩',youtube:'YouTube',vimeo:'Vimeo',local:'本地视频'}[info.provider]||'视频';
  await mountPlayback(watch,info);
 }catch(error){if(activeWatch===watch&&!watch.abort.signal.aborted){$('#watch-loading').textContent='播放器暂时无法加载';playerNote(watch,error.message)}}
}
$('#close-watch').onclick=()=>closeWatch();
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&activeWatch&&!document.querySelector('dialog[open]')){event.preventDefault();closeWatch()}});
document.addEventListener('visibilitychange',()=>{if(activeWatch){activeWatch.counter.reset();if(document.hidden)reportWatch(activeWatch,true)}});
const watchDialogObserver=new MutationObserver(()=>{if(activeWatch){activeWatch.counter.reset();if(document.querySelector('dialog[open]'))reportWatch(activeWatch,true)}});
for(const dialog of document.querySelectorAll('dialog'))watchDialogObserver.observe(dialog,{attributes:true,attributeFilter:['open']});


// Durable background preparation; subscriptions are scoped to the visible session.
let preparationEvents=null,preparationEventKey='',lastPreparationData=null;
const localCacheVideos=new Map();
function stopPreparationEvents(){preparationEvents?.close();preparationEvents=null;preparationEventKey=''}
function syncPreparationEvents(force=false){
 const key=mode==='zmq'&&activeSession&&!document.hidden?spaceId+'/'+activeSession:'';
 if(!force&&key===preparationEventKey)return;
 stopPreparationEvents();if(!key)return;
 const origin=spaceId,sid=activeSession;preparationEventKey=key;
 preparationEvents=new EventSource(`/api/spaces/${origin}/sessions/${sid}/events`);
 preparationEvents.addEventListener('preparation',event=>{
  if(origin!==spaceId||sid!==activeSession||busy)return;
  try{applyPreparation(JSON.parse(event.data))}catch{}
 });
 preparationEvents.addEventListener('deleted',()=>{stopPreparationEvents()});
}
function applyPreparation(data){
 lastPreparationData=data;
 const session=sessions.find(s=>s.id===activeSession);
 for(const remote of data.turns||[]){
  const turn=recommendationTurns.find(t=>t.requestId===remote.request_id);if(!turn)continue;
  if(JSON.stringify(turn.videos)===JSON.stringify(remote.videos))continue;
  turn.videos=remote.videos;
  const el=$('#conversation').querySelector(`[data-message-index="${turn.messageIndex}"]`);
  if(el){const previous=el.getBoundingClientRect().height;el.querySelectorAll('.bubble-results-heading,.bubble-results').forEach(node=>node.remove());attachRecommendations(el,turn.videos,false);animateReply(el,previous)}
 }
 if(session){session.recommendationTurns=recommendationTurns;const last=recommendationTurns.at(-1);if(last){session.videos=last.videos;videos=last.videos}persist('sessions',sessions)}
 $('#conversation .preparation-status')?.remove();
 const plan=data.preparation,host=$('#conversation .bubble.assistant:last-child');
 if(plan&&host){
  const ready=plan.candidates.filter(c=>c.state==='ready').length;
  const waiting=plan.candidates.filter(c=>['missing','queued','downloading','processing'].includes(c.state)).length;
  const note=document.createElement('p');note.className='preparation-status';note.setAttribute('role','status');
  note.textContent=plan.status==='failed'?'视频准备暂时失败，可稍后重试或更换方向。':plan.error?plan.error:waiting?`正在准备「${plan.topic}」的视频 · ${ready} 条已就绪，${waiting} 条候选待准备`:ready?`「${plan.topic}」已有 ${ready} 条本地缓存`:plan.status==='prepared'?'暂未找到可用视频，可以调整具体方向。':`正在为「${plan.topic}」寻找并准备视频…`;
  host.append(note);
 }
 persist('view',{...readSpace('view',{}),recommendationTurns,videos});
}
document.addEventListener('visibilitychange',()=>syncPreparationEvents());
window.addEventListener('pagehide',stopPreparationEvents);
function formatBytes(bytes){return bytes>=1024**3?(bytes/1024**3).toFixed(1)+' GB':(bytes/1024**2).toFixed(1)+' MB'}
let localCacheLoading=false,cacheSpace='',cacheLastTime=0;
async function refreshLocalCache(){
 if(localCacheLoading||mode!=='zmq')return;localCacheLoading=true;const origin=spaceId;
 try{
  const [cache,result]=await Promise.all([stateAPI(origin,'/cache'),stateAPI(origin,'/local-videos')]);
  if(origin!==spaceId)return;cacheSpace=origin;cacheLastTime=Date.now();
  localCacheVideos.clear();result.videos.forEach(v=>localCacheVideos.set(v.id,v));
  $('#cache-summary').textContent=`已使用 ${formatBytes(cache.used_bytes)} / ${formatBytes(cache.quota_bytes)} · 下载预留 ${formatBytes(cache.reserved_bytes)}`;
  if(document.activeElement!==$('#cache-quota'))$('#cache-quota').value=Math.round(cache.quota_bytes/1024**3);
  const labels={missing:'待准备',queued:'等待下载',downloading:'下载中',processing:'处理中',ready:'可播放',failed:'下载失败'};
  $('#local-cache-list').innerHTML=cache.assets.filter(a=>a.state!=='evicted').map(a=>{
   const job=cache.jobs.find(j=>j.kind==='download'&&j.target===a.video_id&&['queued','running'].includes(j.status));
   const latestJob=cache.jobs.find(j=>j.kind==='download'&&j.target===a.video_id);
   const failure=job?.error||(a.state==='failed'?latestJob?.error:'');
   return `<article class="cache-row"><div><strong>${escapeHTML(a.title||'正在获取视频信息')}</strong><small>${labels[a.state]||a.state} · ${formatBytes(a.bytes)}${failure?' · '+escapeHTML(failure):''}</small></div><div class="cache-actions">${a.state==='ready'?`<button class="text-button" data-action="watch" data-id="${escapeHTML(a.video_id)}">播放</button>`:''}<button class="text-button" data-cache-pin="${escapeHTML(a.video_id)}" data-pinned="${a.pinned?'1':'0'}">${a.pinned?'取消保留':'保留'}</button>${job?`<button class="text-button" data-cache-cancel="${job.id}">取消下载</button>`:['failed','missing'].includes(a.state)?`<button class="text-button" data-cache-retry="${escapeHTML(a.video_id)}">重新下载</button>`:''}<button class="text-button" data-cache-delete="${escapeHTML(a.video_id)}">清理</button></div></article>`;
  }).join('')||'<p class="cache-empty">当前空间还没有本地视频。提出一个感兴趣的领域，或粘贴视频链接开始准备。</p>';
 }catch(error){if(origin===spaceId)$('#cache-message').textContent=error.message}
 finally{localCacheLoading=false}
}
$('#refresh-cache').onclick=refreshLocalCache;
$('#import-video-form').onsubmit=async event=>{
 event.preventDefault();const origin=spaceId,field=$('#import-video-url'),url=field.value.trim();if(!url)return;
 const button=event.submitter;button.disabled=true;
 try{await stateAPI(origin,'/local-videos',{method:'POST',body:JSON.stringify({url})});if(origin===spaceId){field.value='';$('#cache-message').textContent='已加入下载队列';await refreshLocalCache()}}catch(error){if(origin===spaceId)$('#cache-message').textContent=error.message}finally{button.disabled=false}
};
$('#cache-quota-form').onsubmit=async event=>{
 event.preventDefault();const origin=spaceId;
 try{await stateAPI(origin,'/cache',{method:'PUT',body:JSON.stringify({quota_bytes:Number($('#cache-quota').value)*1024**3})});if(origin===spaceId){$('#cache-message').textContent='缓存上限已更新';refreshLocalCache()}}catch(error){if(origin===spaceId)$('#cache-message').textContent=error.message}
};
$('#local-cache-list').onclick=async event=>{
 const button=event.target.closest('[data-cache-pin],[data-cache-delete],[data-cache-retry],[data-cache-cancel]');if(!button)return;
 const origin=spaceId;button.disabled=true;
 try{
  if(button.dataset.cachePin)await stateAPI(origin,'/local-videos/'+encodeURIComponent(button.dataset.cachePin),{method:'PATCH',body:JSON.stringify({pinned:button.dataset.pinned!=='1'})});
  else if(button.dataset.cacheDelete)await stateAPI(origin,'/local-videos/'+encodeURIComponent(button.dataset.cacheDelete),{method:'DELETE'});
  else if(button.dataset.cacheRetry)await stateAPI(origin,'/local-videos/'+encodeURIComponent(button.dataset.cacheRetry)+'/download',{method:'POST'});
  else await stateAPI(origin,'/download-jobs/'+button.dataset.cacheCancel+'/cancel',{method:'POST'});
  if(origin===spaceId)await refreshLocalCache();
 }catch(error){if(origin===spaceId)$('#cache-message').textContent=error.message}finally{button.disabled=false}
};
setInterval(()=>{if(ready&&page==='library'&&!document.hidden&&(cacheSpace!==spaceId||Date.now()-cacheLastTime>2500))refreshLocalCache()},500);

function syncStageSize(){
 if(!initialVideos.learning.length)return;
 const panel=$('#welcome-panel');const hidden=panel.classList.contains('hidden');
 if(hidden)panel.classList.remove('hidden');
 const height=$('.welcome-content').getBoundingClientRect().height;
 if(hidden)panel.classList.add('hidden');
 if(height>0)$('.conversation-stage').style.setProperty('--stage-height',`${Math.max(340,Math.ceil(height))}px`);
}
window.addEventListener('resize',syncStageSize);
const stage=$('.conversation-stage');
const welcomeSizeObserver=new ResizeObserver(entries=>{
 if(!ready)return;
 const height=entries[0].contentRect.height;
 // Hidden welcome content has no measurable height; retain its last visible size.
 if(height>0)stage.style.setProperty('--stage-height',`${Math.max(340,Math.ceil(height))}px`);
});
welcomeSizeObserver.observe($('.welcome-content'));
init();
