'use strict';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[x]);
const dateFmt = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'});
const timeFmt = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
const dayOf = t => {const p=Object.fromEntries(dateFmt.formatToParts(t*1000).map(({type,value})=>[type,value])); return `${p.year}-${p.month}-${p.day}`;};
const timeOf = t => timeFmt.format(t*1000);
const now = () => Date.now()/1000;
const read = (key, fallback) => {try {return JSON.parse(localStorage.getItem('jsydiptv.'+key)) ?? fallback;} catch {return fallback;}};
const listPref = key => {const v=read(key,[]);return Array.isArray(v) ? v.filter(x=>typeof x==='string') : [];};
const save = (key, value) => {try {localStorage.setItem('jsydiptv.'+key, JSON.stringify(value));} catch { /* storage is optional */ }};
function tip(text) {
  const el=$('#tip'); el.textContent=text; el.classList.add('on');
  clearTimeout(el.timer); el.timer=setTimeout(()=>el.classList.remove('on'),2600);
}
async function request(path,{method='GET',body,timeout=12000,signal}={}) {
  const ac=new AbortController();
  const cancel=()=>ac.abort();
  if(signal?.aborted) cancel(); else signal?.addEventListener('abort',cancel,{once:true});
  const timer=setTimeout(cancel,timeout);
  try {
    const res=await fetch(path,{method,cache:'no-store',signal:ac.signal,
      ...(body===undefined?{}:{headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
    const data=await res.json().catch(()=>null);
    if(!res.ok || !data || data.ok===false) throw Error(data?.msg || data?.error || `HTTP ${res.status}`);
    return data;
  } catch (error) {
    if(ac.signal.aborted) throw Error('请求超时或已取消');
    throw error;
  } finally {clearTimeout(timer); signal?.removeEventListener('abort',cancel);}
}
async function copy(text,input) {
  try {
    if(navigator.clipboard && isSecureContext) {try {await navigator.clipboard.writeText(text); tip('已复制'); return;} catch { /* try legacy copy */ }}
    const el=input || document.createElement('textarea');
    const focus=document.activeElement;
    if(!input){el.value=text; el.setAttribute('readonly',''); el.style.cssText='position:fixed;opacity:0'; document.body.append(el);}
    let ok;
    try {el.select(); ok=document.execCommand('copy');}
    finally {if(!input){el.remove(); focus?.focus({preventScroll:true});}}
    if(!ok) throw Error();
    tip('已复制');
  } catch {if(input){input.focus();input.select();} else window.prompt('手动复制流地址',text);tip('未能自动复制');}
}
function notice(id,text,kind='') {const el=$(id); if(el.textContent!==text) el.textContent=text; el.hidden=!text; el.dataset.kind=kind;}
function empty(el,text,label,action) {
  const p=document.createElement('div'); p.className='empty'; const msg=document.createElement('p'); msg.textContent=text;p.append(msg);
  if(label){const b=document.createElement('button');b.className='btn';b.textContent=label;b.onclick=action;p.append(b);}
  el.replaceChildren(p);
}

// Navigation hides, but never remounts, the player. Browser history works inside fnOS too.
const routes={watch:'live',subscriptions:'player',settings:'sched'};
let page='live';
function route(focus=false) {
  const key=location.hash.slice(1);
  if(key==='main'||key==='guide-title') return;
  const previous=page;
  page=Object.hasOwn(routes,key)?routes[key]:'live';
  document.body.dataset.page=page;
  $$('.nav').forEach(a=>{const on=a.dataset.p===page; a.classList.toggle('on',on); if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  $$('.page').forEach(section=>section.hidden=section.id!==`pg-${page}`);
  if(previous!==page){const toast=$('#tip');clearTimeout(toast.timer);toast.classList.remove('on');}
  if(focus){window.scrollTo(0,0);$('#main').focus({preventScroll:true});}
  if(page==='sched') refreshStatus();
}
window.addEventListener('hashchange',()=>route(true));
// Channel filters are shared by rendering and previous/next; recent channels store only IDs.
let channels=[], byId=new Map(), catalogError='', filter='all', query='', generation=null;
let favorites=new Set(listPref('fav')), recent=listPref('recent').slice(0,12);
const folded=new Set();
const filters={all:()=>true,fav:c=>favorites.has(c.uuid),recent:c=>recent.includes(c.uuid),catchup:c=>c.back>0,uhd:c=>c.is_4k};
function visibleChannels(){
  const result=channels.filter(c=>filters[filter](c) && (!query || c.name.toLowerCase().includes(query) || String(c.num).includes(query)));
  if(filter==='recent')result.sort((a,b)=>recent.indexOf(a.uuid)-recent.indexOf(b.uuid));
  return result;
}
function logo(c){const tone=c.logo_tone==='dark'?' t-dark':'';return c.logo?`<span class="lg${tone}"><img src="${esc(c.logo)}" loading="lazy" alt=""></span>`:`<span class="lg t-none" aria-hidden="true">${esc(String(c.num??'').slice(0,4)||'TV')}</span>`;}
function changeFilter(next){
  filter=next; $('#liveList').scrollTop=0;
  const menu=$('#filterMenu'), extra=next==='catchup'||next==='uhd';
  const summary=menu.querySelector('summary');
  if(extra&&menu.contains(document.activeElement))summary.focus();
  menu.open=false;
  menu.classList.toggle('active',extra);
  summary.textContent=extra?({catchup:'可回看',uhd:'真 4K'})[next]:'筛选';
  $$('.chip').forEach(b=>{const on=b.dataset.filter===next;b.classList.toggle('on',on);b.setAttribute('aria-pressed',String(on));});
  renderChannels();
}
function updateFold(){
  const sections=$$('#liveList .gsec'); const b=$('#allToggle');
  b.disabled=!sections.length||!!query||filter==='recent';
  b.textContent=sections.some(e=>!e.classList.contains('folded'))?'全部折叠':'全部展开';
}
function renderChannels(){
  const box=$('#liveList'), list=visibleChannels(), scroll=box.scrollTop;
  const focus=document.activeElement.closest('.item');
  const focusSel=document.activeElement.classList.contains('fav')?'.fav':'.channel-btn';
  const focusGroup=document.activeElement.classList.contains('fold')?document.activeElement.closest('.gsec')?.dataset.group:null;
  $('#chSum').textContent=channels.length?`${list.length} / ${channels.length}`:'';
  if(!list.length){
    if(!channels.length) empty(box,catalogError||'暂无频道',catalogError&&!catalogError.includes('尚未生成')?'重试':'查看更新',()=>catalogError&&!catalogError.includes('尚未生成')?refreshCatalog():goSettings());
    else if(query) empty(box,'没有匹配的频道','清除搜索',()=>{$('#liveQ').value='';query='';renderChannels();$('#liveQ').focus();});
    else empty(box,({fav:'还没有收藏频道',recent:'还没有最近频道',catchup:'没有可回看的频道',uhd:'没有已验证的 4K 频道'})[filter]||'没有频道','查看全部',()=>{$('#allChannels').focus();changeFilter('all');});
    if(focus)box.querySelector('button')?.focus({preventScroll:true});
    updateFold();updateControls();return;
  }
  const groups=new Map();
  for(const c of list){const g=filter==='recent'?'最近选择':c.group;if(!groups.has(g))groups.set(g,[]);groups.get(g).push(c);}
  const frag=document.createDocumentFragment();
  for(const [group, rows] of groups){
    const section=document.createElement('div'); section.className='gsec'; section.dataset.group=group;
    const collapse=!query && filter!=='recent' && folded.has(group); section.classList.toggle('folded',collapse);
    const head=document.createElement('div');head.className='gh';
    head.innerHTML=`<b>${esc(group)}</b><em>${rows.length} 台</em>`;
    if(!query && filter!=='recent'){
      const b=document.createElement('button');b.className='fold';b.textContent=collapse?'展开':'折叠';b.setAttribute('aria-label',`${group}：${b.textContent}`);b.setAttribute('aria-expanded',String(!collapse));
      b.onclick=()=>{if(folded.has(group))folded.delete(group);else folded.add(group);renderChannels();};head.append(b);
    }
    const grid=document.createElement('div');grid.className='grid';
    for(const c of rows){
      const el=document.createElement('div'); el.className='item';el.dataset.uuid=c.uuid;
      const active=live?.uuid===c.uuid, fav=favorites.has(c.uuid);
      el.classList.toggle('on',active);
      const badge=c.is_4k?'<span class="u4k">4K</span>':c.uhd_unverified?'<span class="u4k zhx">待探测</span>':c.zhx?'<span class="u4k zhx">臻享</span>':'';
      el.innerHTML=`<button class="channel-btn"${active?' aria-current="true"':''}>${logo(c)}<span class="nm"><b title="${esc(c.name)}">${esc(c.name)}</b><span class="nowtxt" title="${esc(c.now?.t||'')}">${esc(c.now?.t||'暂无节目单')}</span></span>${badge}</button><button class="fav${fav?' on':''}" aria-label="${fav?'取消收藏':'收藏'} ${esc(c.name)}" aria-pressed="${fav}">${fav?'★':'☆'}</button>`;
      el.querySelector('.channel-btn').onclick=()=>playChannel(c);
      el.querySelector('.fav').onclick=()=>{if(fav)favorites.delete(c.uuid);else favorites.add(c.uuid);save('fav',[...favorites]);renderChannels();tip(fav?'已取消收藏':'已收藏');};
      grid.append(el);
    }
    section.append(head,grid);frag.append(section);
  }
  box.replaceChildren(frag);box.scrollTop=scroll;
  if(focus){([...box.querySelectorAll('.item')].find(e=>e.dataset.uuid===focus.dataset.uuid)?.querySelector(focusSel)||box.querySelector('.channel-btn'))?.focus({preventScroll:true});}
  else if(focusGroup){[...box.querySelectorAll('.gsec')].find(e=>e.dataset.group===focusGroup)?.querySelector('.fold')?.focus({preventScroll:true});}
  updateFold();updateControls();
}
function updateProgramLabels(){
  $$('#liveList .item').forEach(el=>{const c=byId.get(el.dataset.uuid);if(!c)return;const label=el.querySelector('.nowtxt');label.textContent=c.now?.t||'暂无节目单';label.title=c.now?.t||'';});
  renderPlaybackTitle();
}
function goSettings(){if(location.hash==='#settings')return;location.hash='settings';}
let catalogSeq=0, catalogTimer;
async function refreshCatalog(){
  const seq=++catalogSeq;
  try {
    const next=await request('/api/channels'); if(seq!==catalogSeq)return;
    const gen=next.generation??null, catalog=next.channels;
    if(!Array.isArray(catalog))throw Error('频道数据不可用');
    const changed=gen!==generation || catalog.length!==channels.length || catalog.some((c,i)=>['uuid','name','group','num','logo','live','back','is_4k','uhd_unverified','zhx'].some(k=>c[k]!==channels[i][k]));
    const guideChanged=gen!==generation;
    generation=gen;channels=catalog;byId=new Map(channels.map(c=>[c.uuid,c]));catalogError='';notice('#catalogNotice','');
    if(changed || !channels.length)renderChannels(); else updateProgramLabels();
    if(!live)renderIntro();
    if(guideChanged){guideCache.clear();if(live)loadGuide(live.uuid);}
  } catch (e) {if(seq===catalogSeq){catalogError=e.message;if(!channels.length)renderChannels();else notice('#catalogNotice','频道暂未刷新，正在显示上次可用结果');}}
  finally {
    if(seq===catalogSeq){clearTimeout(catalogTimer);const next=Math.min(...channels.map(c=>c.now?.e??Infinity).filter(t=>t>now()));
      catalogTimer=setTimeout(refreshCatalog, Math.min(60000,Math.max(5000,(next-now()+2)*1000)));}
  }
}
function step(delta){
  const list=visibleChannels();if(!list.length)return;
  const i=list.findIndex(c=>c.uuid===live?.uuid);
  playChannel(list[i<0?(delta>0?0:list.length-1):(i+delta+list.length)%list.length]);
}
$('#liveQ').addEventListener('input',e=>{query=e.target.value.trim().toLowerCase();$('#liveList').scrollTop=0;renderChannels();});
$('#liveQ').addEventListener('keydown',e=>{
  if(e.isComposing)return;
  if(e.key==='Enter'&&query&&visibleChannels()[0]){e.preventDefault();playChannel(visibleChannels()[0]);}
  else if(e.key==='Escape'&&query){e.preventDefault();e.target.value='';e.target.dispatchEvent(new Event('input'));}
});
document.addEventListener('keydown',e=>{
  if(page==='live'&&e.key==='/'&&!e.altKey&&!e.ctrlKey&&!e.metaKey&&!e.isComposing&&!e.target.closest('input, textarea, select, [contenteditable], video')){
    e.preventDefault();$('#introSearch').click();
  }
});
$$('.chip').forEach(b=>b.onclick=()=>changeFilter(b.dataset.filter));
$('#allToggle').onclick=()=>{
  const sections=$$('#liveList .gsec'), collapse=sections.some(e=>!e.classList.contains('folded'));
  for(const e of sections){if(collapse)folded.add(e.dataset.group);else folded.delete(e.dataset.group);}
  renderChannels();
};
document.addEventListener('keydown',e=>{
  if(page!=='live' || e.key!=='/' || e.altKey || e.ctrlKey || e.metaKey || e.target.closest('input,textarea,select,[contenteditable],video,button'))return;
  e.preventDefault();$('#liveQ').focus();
});

// Keep media/session identity separate from the live catalog and from pending catch-up requests.
let live=null, mediaSeq=0, intent=0, catchupRequest=null, hls=null, watchdog=null;
const video=$('#vid');
const hlsModule=new Promise(resolve=>{
  const s=document.createElement('script');s.src='/hls.min.js';s.onload=()=>resolve(window.Hls);s.onerror=()=>resolve(null);document.head.append(s);
});
function proxy(url){return '/stream?u='+encodeURIComponent(url);}
function openStream(url){window.open(proxy(url),'_blank','noopener');}
function updateControls(){
  const any=visibleChannels().length>0;
  $('#btnPrev').disabled=!any;$('#btnNext').disabled=!any;
  $('#btnStop').disabled=!live&&!catchupRequest;
  $('#btnCopyLive').disabled=!live;$('#btnOpenLive').disabled=!live;
  $('#playControls').hidden=!live&&!catchupRequest;
  const back=$('#btnReturnLive'); back.hidden=!live?.catchup&&!catchupRequest;
  back.disabled=!byId.get(live?.uuid);
}
function renderPlaybackTitle(){
  const c=live && (byId.get(live.uuid)||live.channel);
  const mode=$('#playMode'), title=$('#liveNow');
  mode.textContent=live?.catchup?'回看':live?'直播':'待播放'; mode.dataset.mode=live?.catchup?'catchup':'live';
  title.replaceChildren();
  if(!c){title.textContent='未选择频道';title.removeAttribute('title');return;}
  const name=document.createElement('b');name.textContent=c.name;title.append(name);
  const p=live.catchup;
  title.title=c.name;
  if(p) title.append(` · ${dayOf(p.start).slice(5)} ${timeOf(p.start)}–${timeOf(p.end)} · ${p.title}`);
  else if(c.now) title.append(` · ${c.now.t}`);
}
function cancelCatchup(){++intent;catchupRequest?.abort();catchupRequest=null;}
function teardown(){
  clearTimeout(watchdog);watchdog=null;
  video.onerror=video.onplaying=video.onwaiting=null;
  if(hls){const old=hls;hls=null;old.destroy();}
  video.pause();video.removeAttribute('src');video.load();
}
function renderIntro(){
  const currentTime=now();
  const onAir=c=>c?.now?.t && c.now.s<=currentTime && currentTime<c.now.e;
  const recentChannel=byId.get(recent[0]);
  const channel=recentChannel||channels.find(onAir)||channels[0];
  const program=onAir(channel)?channel.now:null;
  const progress=$('#introProgress');
  $('#introLabel').textContent=program?.t?'正在播出':recentChannel?'最近观看':'直播电视';
  $('#introTitle').textContent=program?.t||channel?.name||'直播与回看';
  $('#introMeta').textContent=program?.t&&program.s&&program.e
    ? `${channel.name}  ·  ${timeOf(program.s)}–${timeOf(program.e)}`
    : program?.t?channel.name:channel?'浏览频道，或直接开始观看':'从频道列表选择要看的电视节目';
  progress.hidden=!program;
  if(program)progress.value=(currentTime-program.s)/(program.e-program.s);
  const play=$('#introPlay');
  play.hidden=!channel;
  play.onclick=channel?()=>playChannel(channel):null;
  const others=channels.filter(c=>c.uuid!==channel?.uuid&&onAir(c)).slice(0,3);
  const key=JSON.stringify(others.map(c=>[c.uuid,c.name,c.now]));
  const list=$('#nowGuideList');
  $('#nowGuide').hidden=!others.length;
  if(list.dataset.key===key)return;
  list.dataset.key=key;
  list.replaceChildren(...others.map(c=>{
    const button=document.createElement('button');
    button.className='now-guide-item';
    button.innerHTML=`${logo(c)}<span><b>${esc(c.now.t)}</b><small>${esc(c.name)} · 至 ${timeOf(c.now.e)}</small></span>`;
    button.onclick=()=>{const latest=byId.get(c.uuid);if(latest)playChannel(latest);};
    return button;
  }));
}
$('#introSearch').onclick=()=>{
  $('#liveQ').focus({preventScroll:true});
  $('#catalog-title').scrollIntoView({block:'nearest'});
};
function idle(text,initial=false){
  const ph=$('#ph');
  ph.hidden=initial;
  $('#watchIntro').hidden=!initial;
  ph.replaceChildren();
  if(initial)renderIntro();
  else {
    const title=document.createElement('b');
    title.textContent=text;
    ph.append(title);
  }
  video.hidden=true;
  $('#pw').classList.add('idle');
}
function mediaState(text){notice('#mediaState',text);}
function fail(seq,text,codec=false){
  if(seq!==mediaSeq||!live)return;
  ++mediaSeq;teardown();mediaState('');idle(text);
  const current=live, actions=document.createElement('div');actions.className='phacts';
  const add=(label,fn)=>{const b=document.createElement('button');b.className='btn';b.textContent=label;b.onclick=fn;actions.append(b);};
  const alt=codec&&!current.catchup&&(current.channel.alts||[]).find(a=>a.live&&a.live!==current.url&&!/4K|UHD|高清臻享/i.test((a.name||'')+' '+a.live));
  if(alt)add('试播低清晰度',()=>playChannel(current.channel,{url:alt.live,alternate:true}));
  else if(!codec)add('重试',()=>playChannel(current.channel,current));
  add('复制流地址',()=>copy(current.url));
  if(!codec)add('新窗口打开',()=>openStream(current.url));
  actions.firstElementChild.classList.add('primary');
  if(codec&&!alt){const hint=document.createElement('p');hint.textContent='请将流地址粘贴到支持此编码的外部播放器。';hint.className='ph-hint';$('#ph').append(hint);}
  $('#ph').append(actions);
}
function supportsHEVC(){
  const types=['hvc1.1.6.L120.90','hvc1.1.6.L153.B0','hvc1.2.4.L153.B0','hev1.1.6.L120.90'];
  return types.some(t=>{const mime=`video/mp4;codecs="${t}"`;return !!video.canPlayType(mime)||!!window.MediaSource?.isTypeSupported(mime);});
}
function startWatchdog(seq){
  clearTimeout(watchdog);watchdog=setTimeout(()=>{if(seq===mediaSeq&&!(video.readyState>=3&&video.videoWidth>0))fail(seq,'播放等待超时，可重试或检查网络');},15000);
}
async function tryPlay(seq){
  try {await video.play();}
  catch (e) {
    if(seq!==mediaSeq||e.name==='AbortError')return;
    if(e.name==='NotAllowedError'){
      video.muted=true;
      try {await video.play();if(seq===mediaSeq)tip('已静音，点播放器音量开声');}
      catch {if(seq===mediaSeq){clearTimeout(watchdog);mediaState('请点播放按钮继续');}}
    } else fail(seq,'无法解码或播放此流',true);
  }
}
async function useHls(seq,url){
  const Player=await hlsModule;
  if(seq!==mediaSeq)return;
  if(!Player?.isSupported()){fail(seq,'当前浏览器不支持 HLS');return;}
  const instance=hls=new Player({enableWorker:true,lowLatencyMode:false,maxBufferLength:24,maxMaxBufferLength:50});
  let networkRetry=0, mediaRetry=0;
  const current=()=>seq===mediaSeq&&hls===instance;
  instance.on(Player.Events.ERROR,(_event,data)=>{
    if(!current()||!data?.fatal)return;
    // Hls may still dispatch from the same fragment/append stack after ERROR.
    // Defer destroy/recovery until it unwinds; a manual channel change wins.
    queueMicrotask(()=>{
      if(!current())return;
      if(data.type===Player.ErrorTypes.MEDIA_ERROR&&/codec|append|buffer/i.test(data.details||'')){fail(seq,'浏览器无法解码此流',true);return;}
      try {
        if(data.type===Player.ErrorTypes.NETWORK_ERROR&&!/manifest/i.test(data.details||'')&&networkRetry++===0){instance.startLoad();return;}
        if(data.type===Player.ErrorTypes.MEDIA_ERROR&&mediaRetry++===0){instance.recoverMediaError();return;}
      } catch { /* show failure below */ }
      fail(seq,data.type===Player.ErrorTypes.NETWORK_ERROR?'未能加载播放流，请检查江苏移动网络':'播放中断');
    });
  });
  instance.on(Player.Events.MANIFEST_PARSED,()=>{if(current())tryPlay(seq);});
  instance.loadSource(url);instance.attachMedia(video);
}
let specSeq=0;
async function loadSpec(){
  if(!live||!$('#tech').open)return;
  const seq=++specSeq,url=live.url, box=$('#specBar');box.hidden=false;box.textContent='读取规格…';
  try {
    const {spec}=await request('/api/spec?u='+encodeURIComponent(url),{timeout:18000});if(seq!==specSeq)return;
    const v=spec?.video||{},a=spec?.audio||{};
    box.textContent=[v.w&&v.h?`${v.w}×${v.h}`:null,v.codec,v.profile,v.fps?`${v.fps} fps`:null,a.codec,a.ch?`${a.ch} 声道`:null,a.sr?`${a.sr/1000} kHz`:null,spec?.bitrate?`${(spec.bitrate/1e6).toFixed(1)} Mbps`:null].filter(Boolean).join(' · ')||'暂无可识别规格';
  } catch(e){if(seq===specSeq)box.textContent='未读到规格：'+e.message;}
}
function playChannel(c,opts={}){
  cancelCatchup();const seq=++mediaSeq;teardown();++specSeq;
  const same=live?.uuid===c.uuid;
  live={channel:c,uuid:c.uuid,url:opts.url||c.live,catchup:opts.catchup||null,alternate:!!opts.alternate};
  $('#pg-live').dataset.idle='false';
  $('#nowGuide').hidden=true;
  recent=[c.uuid,...recent.filter(id=>id!==c.uuid)].slice(0,12);save('recent',recent);
  renderPlaybackTitle();renderChannels();
  const active=$('#liveList .item.on');
  if(active&&window.matchMedia('(min-width:761px)').matches){const box=$('#liveList'),r=active.getBoundingClientRect(),b=box.getBoundingClientRect();if(r.top<b.top||r.bottom>b.bottom)box.scrollTop+=r.top-b.top-5;}
  if(!same||!guidePrograms)loadGuide(c.uuid);else renderGuide({keepScroll:true});
  $('#tech').hidden=false;$('#specBar').hidden=true;loadSpec();
  if(!opts.alternate&&/265|hevc/i.test(c.codec||'')&&!supportsHEVC()){fail(seq,'当前浏览器不支持 H.265 / HEVC',true);return;}
  $('#watchIntro').hidden=true;$('#ph').hidden=true;$('#pw').classList.remove('idle');video.hidden=false;video.muted=false;
  mediaState('正在连接…');
  video.onplaying=()=>{if(seq===mediaSeq){if(video.videoWidth>0)clearTimeout(watchdog);mediaState('');}};
  video.onwaiting=()=>{if(seq===mediaSeq){mediaState('缓冲中…');startWatchdog(seq);}};
  const url=proxy(live.url);startWatchdog(seq);
  if(video.canPlayType('application/vnd.apple.mpegurl')){
    video.onerror=()=>{if(seq===mediaSeq){video.onerror=null;useHls(seq,url);}};
    video.src=url;
    video.play().catch(e=>{if(seq===mediaSeq && e.name==='NotAllowedError')tryPlay(seq);else if(seq===mediaSeq&&e.name!=='AbortError'&&video.error===null){video.onerror=null;useHls(seq,url);}});
  }else useHls(seq,url);
}
function stop(){
  cancelCatchup();++mediaSeq;++specSeq;++guideSeq;clearTimeout(guideClock);teardown();mediaState('');live=null;guidePrograms=null;
  $('#pg-live').dataset.idle='true';$('#streamActions').open=false;
  idle('选台，即可开始观看',true);$('#tech').hidden=true;empty($('#liveEpg'),'选择频道后查看节目和回看');renderPlaybackTitle();renderChannels();
}
async function playCatchup(c,p){
  cancelCatchup();const seq=intent;const ac=catchupRequest=new AbortController();updateControls();
  try {
    const r=await request(`/api/catchup?uuid=${encodeURIComponent(c.uuid)}&start=${p.start}&end=${p.end}`,{signal:ac.signal});
    if(!r.url)throw Error('未返回回看地址');
    if(seq===intent&&live?.uuid===c.uuid)playChannel(c,{url:r.url,catchup:p});
  }catch(e){if(seq===intent)tip('回看不可用：'+e.message);}
  finally {if(seq===intent){catchupRequest=null;updateControls();}}
}
$('#jumpGuide').onclick=e=>{e.preventDefault();$('#guide-title').focus({preventScroll:true});$('#guide-title').scrollIntoView({block:'start'});};
$('#btnPrev').onclick=()=>step(-1);$('#btnNext').onclick=()=>step(1);$('#btnStop').onclick=stop;
$('#btnReturnLive').onclick=()=>{const c=byId.get(live?.uuid);if(c)playChannel(c);};
$('#btnCopyLive').onclick=()=>{if(live)copy(live.url);};$('#btnOpenLive').onclick=()=>{if(live)openStream(live.url);};
$('#tech').addEventListener('toggle',()=>{if($('#tech').open)loadSpec();});

// A programme spanning midnight is listed on both Beijing dates, with its original times.
const guideCache=new Map(), guidePrefs=new Map();
let guideSeq=0, guidePrograms=null, guideClock=0;
const dayLabel=(key,t)=>key===dayOf(t)?'今天':key===dayOf(t-86400)?'昨天':key===dayOf(t+86400)?'明天':key.slice(5);
async function loadGuide(uuid){
  const seq=++guideSeq, box=$('#liveEpg');guidePrograms=null;clearTimeout(guideClock);
  if(statusData?.meta?.generation===generation&&statusData?.meta?.epg_enabled===false){empty(box,'当前清单未生成节目单');return;}
  const cached=guideCache.get(uuid);
  if(cached){guidePrograms=cached;renderGuide();return;}
  empty(box,'加载节目单…');
  try {
    const data=await request('/api/epg?uuid='+encodeURIComponent(uuid));
    if(seq!==guideSeq||live?.uuid!==uuid)return;
    if(data.generation&&data.generation!==generation){empty(box,'清单已更新，正在同步…');refreshCatalog();return;}
    if(!Array.isArray(data.programs))throw Error();
    if(guideCache.size>=32)guideCache.delete(guideCache.keys().next().value);
    guidePrograms=data.programs;guideCache.set(uuid,guidePrograms);renderGuide();
  }catch(e){if(seq===guideSeq&&live?.uuid===uuid)empty(box,'节目单加载失败','重试',()=>loadGuide(uuid));}
}
function renderGuide({keepScroll=false}={}){
  if(!live||!guidePrograms)return;
  const box=$('#liveEpg'), oldList=box.querySelector('.plist'), scroll=oldList?.scrollTop||0, t=now();
  clearTimeout(guideClock);
  if(!guidePrograms.length){empty(box,'该频道暂无节目单');return;}
  const c=byId.get(live.uuid)||live.channel, pstate=guidePrefs.get(c.uuid)||{day:dayOf(t),only:false};guidePrefs.set(c.uuid,pstate);
  const byDay=new Map();
  for(const p of guidePrograms){
    if(!Number.isFinite(p.start)||!Number.isFinite(p.end)||p.end<=p.start)continue;
    const bucket=(d)=>{if(!byDay.has(d))byDay.set(d,[]);byDay.get(d).push(p);};
    const first=dayOf(p.start), last=dayOf(p.end-1);bucket(first);if(last!==first)bucket(last);
  }
  const days=[...byDay.keys()].sort(), today=dayOf(t);
  if(!days.length){empty(box,'该频道暂无节目单');return;}
  if(!byDay.has(pstate.day))pstate.day=byDay.has(today)?today:days.find(d=>d>today)||days.at(-1);
  const keep=Number(statusData?.meta?.backup_days??7);
  const canReplay=p=>c.back>0&&p.end<=t&&t-p.start<keep*86400;
  const head=document.createElement('div');head.className='epghead';
  head.innerHTML=`<div class="epg-tools"><b>${esc(c.name)}</b><span class="muted">${c.back>0&&keep>0?`回看 ${keep} 天`:'不支持回看'}</span>${c.back>0&&keep>0?`<button class="chip${pstate.only?' on':''}" aria-pressed="${pstate.only}" id="replayOnly">只看可回看</button>`:''}</div><div class="days" aria-label="节目日期"></div>`;
  const current=guidePrograms.find(p=>p.start<=t&&t<p.end);
  const next=guidePrograms.reduce((best,p)=>p.start>t&&(!best||p.start<best.start)?p:best,null);
  const nowButton=document.createElement('button');nowButton.className='chip';nowButton.id='guideNow';nowButton.textContent='回到现在';
  nowButton.disabled=!byDay.has(today);
  nowButton.onclick=()=>{pstate.day=today;pstate.only=false;renderGuide();$('#guideNow')?.focus({preventScroll:true});};
  head.querySelector('.epg-tools').append(nowButton);
  if(current||next){const summary=document.createElement('div');summary.className='epg-summary muted';
    for(const text of [current&&`正在播出 · ${current.title}`,next&&`接下来 ${timeOf(next.start)} · ${next.title}`].filter(Boolean)){
      const span=document.createElement('span');span.textContent=text;summary.append(span);
    }
    head.insertBefore(summary,head.querySelector('.days'));
  }
  const replay=head.querySelector('#replayOnly');if(replay)replay.onclick=()=>{pstate.only=!pstate.only;renderGuide();$('#replayOnly')?.focus({preventScroll:true});};
  for(const day of days){
    const b=document.createElement('button');b.className='dbtn'+(day===pstate.day?' on':'');b.dataset.day=day;b.title=day;b.setAttribute('aria-label',dayLabel(day,t)+' '+day);b.setAttribute('aria-pressed',String(day===pstate.day));
    b.textContent=dayLabel(day,t);if(byDay.get(day).some(canReplay)){const dot=document.createElement('i');dot.className='rvd';dot.setAttribute('aria-hidden','true');b.append(dot);}
    b.onclick=()=>{pstate.day=day;renderGuide();box.querySelector('.dbtn.on')?.focus({preventScroll:true});};head.querySelector('.days').append(b);
  }
  const list=document.createElement('div');list.className='plist';
  const rows=byDay.get(pstate.day).filter(p=>!pstate.only||canReplay(p)).sort((a,b)=>a.start-b.start);
  for(const p of rows){
    const active=p.start<=t&&t<p.end, can=canReplay(p), selected=live.catchup?.start===p.start&&live.catchup?.end===p.end;
    const row=document.createElement(can?'button':'div');row.className='p'+(active?' now':'')+(can?' clickable':'')+(selected?' selected':'');
    row.dataset.start=p.start;row.dataset.end=p.end;
    if(can)row.onclick=()=>playCatchup(c,p);
    if(selected)row.setAttribute('aria-current','true');
    row.innerHTML=`<span class="a">${timeOf(p.start)}–${timeOf(p.end)}</span><span class="b">${esc(p.title)}</span><span class="rv">${selected?'已选':active?'直播中':can?'回看':''}</span>`;
    list.append(row);
  }
  if(!rows.length)empty(list,'这天没有可回看的节目');
  const focus=document.activeElement;
  box.replaceChildren(head,list);
  if(box.querySelector('.dbtn.on')){const d=head.querySelector('.days'), b=head.querySelector('.dbtn.on');d.scrollLeft=Math.max(0,b.getBoundingClientRect().left-d.getBoundingClientRect().left-12);}
  if(keepScroll)list.scrollTop=scroll;
  else if(pstate.day===today&&!pstate.only){const cur=list.querySelector('.p.now');if(cur)list.scrollTop=cur.getBoundingClientRect().top-list.getBoundingClientRect().top;}
  if(focus?.classList.contains('clickable'))[...list.querySelectorAll('button')].find(e=>e.dataset.start===focus.dataset.start&&e.dataset.end===focus.dataset.end)?.focus({preventScroll:true});
  else if(focus?.classList.contains('dbtn'))box.querySelector('.dbtn.on')?.focus({preventScroll:true});
  else if(['replayOnly','guideNow'].includes(focus?.id))box.querySelector('#'+focus.id)?.focus({preventScroll:true});
  const ends=guidePrograms.flatMap(p=>[p.start,p.end]).filter(x=>x>t);
  guideClock=setTimeout(()=>renderGuide({keepScroll:true}),Math.min(60000,Math.max(1000,(Math.min(...ends)-t)*1000+1000)));
}

// Subscription URLs keep the existing endpoint contracts; availability uses the active generation.
const links={
  fongmi:['FongMi 配置','/fongmi.json','填入「配置地址」。'],tvbox:['TVBox 配置','/tvbox.json','填入「配置地址」。'],
  diyp:['通用 M3U','/iptv.m3u8','适用于 APTV、酷9、mytv、DIYP 等。'],kodi:['Kodi M3U','/kodi.m3u','供 PVR IPTV Simple Client 使用。'],
  ku9:['FongMi 兼容 M3U','/fongmi.m3u','与通用 M3U 内容一致。'],epg:['节目单 XMLTV','/epg.xml','在播放器的 EPG 设置中添加。'],
  ch:['频道数据','/channels.json','JSON'], '4k':['真 4K 与候选数据','/4k.json','JSON'],
  live_txt:['纯直播 TXT','/live.txt','完整模式额外输出。','full'],multicast:['组播 M3U','/multicast.m3u','完整模式额外输出，需组播网络。','full'],
  orig_m3u:['原版 M3U','/orig.m3u8','运营商分组。'],orig_epg:['原版节目单','/orig_epg.xml','与原版清单配对。'],orig_json:['原版配置','/orig.json','原版分组 JSON。']
};
const profiles={
  fongmi:['FongMi TV','优先使用一条配置。已有 M3U 配置可继续用。',['fongmi','ku9']],
  tvbox:['TVBox / 影视仓','新增直播时可用配置地址或通用清单。',['tvbox','diyp']],
  kodi:['Kodi','请用 Kodi 专用地址，不要用通用 M3U 代替。',['kodi','epg']],
  common:['APTV / 酷9 / mytv / DIYP','导入通用清单；如需手动配置节目单，使用 XMLTV。',['diyp','epg']],
  skylight:['天光云影 / VLC','按纯直播接入，不依赖回看。',['diyp']]
};
const choice=$('#playerChoice');
for(const [key,[name]] of Object.entries(profiles)){const o=document.createElement('option');o.value=key;o.textContent=name;choice.append(o);}
const preferred=read('player','fongmi');choice.value=Object.hasOwn(profiles,preferred)?preferred:'fongmi';
function buildCards(target,keys,prefix){
  $(target).replaceChildren(...keys.map(key=>{
    const [title,,hint]=links[key];const id=prefix+key, card=document.createElement('article');card.className='surface address-card';
    card.dataset.link=key;
    card.innerHTML=`<h3>${esc(title)}</h3><p class="muted">${esc(hint)}</p><div class="addr"><input readonly id="${id}" aria-label="${esc(title)}地址" placeholder="等待清单…"><button class="btn" type="button" disabled data-c="${id}" aria-label="复制${esc(title)}地址">复制</button></div>`;
    return card;
  }));
  if(prefix==='rec_')$(target).querySelector('.btn')?.classList.add('primary');
}
function showProfile(){const [,hint,keys]=profiles[choice.value];$('#playerHint').textContent=hint;buildCards('#recommended',keys,'rec_');refreshLinks();}
function refreshLinks(){
  const m=statusData?.meta||{}, ready=Number(m.channels)>0;
  for(const card of $$('[data-link]')){
    const key=card.dataset.link, [,path,,mode]=links[key];const input=card.querySelector('input');
    const available=ready&&(!mode||m.output_mode===mode);
    input.placeholder=!ready?'等待生成清单':!available?'仅完整模式提供':'';
    input.value=available?location.origin+path:'';
    card.querySelector('button').disabled=!available;
  }
  const hint=!ready?'清单生成后可以复制地址。':m.epg_enabled===false?'当前未生成节目单；直播订阅仍可用。':'';
  notice('#subscriptionNotice',hint);
}
buildCards('#pall',Object.keys(links).filter(k=>!k.startsWith('orig_')),'u_');
buildCards('#originalLinks',['orig_m3u','orig_epg','orig_json'],'u_');
choice.onchange=()=>{save('player',choice.value);showProfile();};
$$('#pTabs .tab').forEach(t=>t.onclick=()=>{
  $$('#pTabs .tab').forEach(b=>{b.classList.toggle('on',b===t);b.setAttribute('aria-pressed',String(b===t));});
  for(const k of ['guide','all','orig'])$('#p'+k).hidden=k!==t.dataset.t;
});
$('#pg-player').addEventListener('click',e=>{const b=e.target.closest('[data-c]');if(b){const el=document.getElementById(b.dataset.c);if(el.value)copy(el.value,el);}});

let statusData=null, statusSeq=0, statusOnline=false, updateBusy=false;
let scheduleDirty=false, scheduleSaving=false, outputDirty=false, outputSaving=false, updateResult='';
function renderStatus(){
  const cfg=statusData?.config||{}, meta=statusData?.meta||{}, upd=statusData?.updating||{}, sch=statusData?.schedule||{};
  const failed=upd.error||meta.status==='fail';
  const label=!statusOnline?'无法连接':upd.running||updateBusy?'正在更新':failed?'上次更新失败':meta.status==='ok'?'运行正常':'待更新';
  $('.header-status').dataset.error=String(!statusOnline||!!failed&&!upd.running&&!updateBusy);
  $('#dot').className='dot '+(!statusOnline||failed&&!upd.running&&!updateBusy?'err':upd.running||updateBusy?'warn':meta.status==='ok'?'':'idle');
  if($('#st').textContent!==label)$('#st').textContent=label;
  $('#settingsStatus').textContent=$('#st').textContent;
  const v=statusData?.version;$('#version').textContent=typeof v==='string'&&/^\d+\.\d+\.\d+([+-][\w.-]+)?$/.test(v)?'v'+v:'版本 —';
  $('#lastUpd').textContent=meta.updated||'—';
  $('#stCh').textContent=meta.channels??'—';$('#stBack').textContent=meta.with_catchup??'—';$('#stUhd').textContent=meta.uhd_real??'—';$('#stProg').textContent=meta.epg_programs?.toLocaleString()??'—';
  $('#btnUpd2').disabled=!statusOnline||updateBusy||upd.running||scheduleSaving||outputSaving;
  $('#btnUpd2').textContent=upd.running||updateBusy?'更新中…':'立即更新';
  $('#schNext').textContent=sch.enabled===false?'定时已关闭':sch.next_run?'下次更新：'+sch.next_run:'';
  if(!scheduleDirty&&!scheduleSaving&&statusData){$('#swOn').checked=sch.enabled!==false;$('#tm').value=sch.at||'03:30';}
  if(!outputDirty&&!outputSaving&&statusData){$('#outputMode').value=cfg.output_mode||'minimal';$('#epgEnabled').checked=cfg.epg_enable!==false;}
  $('#scheduleFields').disabled=!statusOnline||scheduleSaving||outputSaving||updateBusy||!!upd.running;
  $('#tm').disabled=!$('#swOn').checked;
  $('#outputFields').disabled=!statusOnline||outputSaving||scheduleSaving||updateBusy||!!upd.running;
  let pending=[];
  if(meta.generation){if(cfg.output_mode&&cfg.output_mode!==meta.output_mode)pending.push('输出模式');if(typeof meta.epg_enabled==='boolean'&&cfg.epg_enable!==meta.epg_enabled)pending.push('节目单');}
  $('#applyOutput').hidden=!pending.length||outputDirty||!statusOnline||updateBusy||!!upd.running;
  const stale=meta.channels?'；上一套清单仍可使用':'';
  if(!statusOnline)notice('#updHint','无法连接服务。恢复连接后会重新检查状态。','error');
  else if(upd.running||updateBusy)notice('#updHint','正在生成并校验清单'+stale+'。');
  else if(failed)notice('#updHint','更新失败'+stale+'。'+(upd.error?` 原因：${upd.error}`:''),'error');
  else if(pending.length)notice('#updHint',`${pending.join('、')}设置待应用。下次成功更新后生效。`);
  else if(updateResult)notice('#updHint',updateResult,'ok');
  else notice('#updHint','');
  refreshLinks();
}
async function refreshStatus(){
  const seq=++statusSeq;
  try {
    const data=await request('/api/status');if(seq!==statusSeq)return;
    statusData=data;statusOnline=true;renderStatus();
    if(data.meta?.generation&&data.meta.generation!==generation)refreshCatalog();
  }catch(e){if(seq===statusSeq){statusOnline=false;renderStatus();}}
}
async function loadLogs(){
  const box=$('#logs');box.textContent='加载日志…';
  try {const {log}=await request('/api/log?lines=100');box.textContent=log?.trim()||'暂无日志';box.scrollTop=box.scrollHeight;}
  catch(e){box.textContent='日志读取失败，请刷新重试。';}
}
$('#refreshLogs').onclick=loadLogs;$('#logDetails').addEventListener('toggle',()=>{if($('#logDetails').open)loadLogs();});
$('#btnUpd2').onclick=async()=>{
  if(updateBusy||statusData?.updating?.running)return;
  updateBusy=true;updateResult='';renderStatus();
  let error='';
  try {await request('/api/update',{method:'POST',timeout:360000});updateResult='清单更新完成。';}
  catch(e){error=e.message;tip('更新请求未完成，请查看状态');}
  finally {
    updateBusy=false;await refreshStatus();
    if(error&&statusOnline&&!statusData?.updating?.running&&!statusData?.updating?.error)notice('#updHint','更新请求未完成：'+error,'error');
    if($('#logDetails').open)loadLogs();
  }
};
$('#applyOutput').onclick=()=>{const button=$('#btnUpd2');button.scrollIntoView({block:'center'});button.focus({preventScroll:true});button.click();};
$('#tm').addEventListener('input',e=>{const v=e.target.value;if(/^\d{4}$/.test(v))e.target.value=v.slice(0,2)+':'+v.slice(2);});
function formMessage(el,text,kind=''){const node=$(el);node.textContent=text;node.dataset.kind=kind;}
$('#scheduleForm').oninput=()=>{scheduleDirty=true;$('#tm').disabled=!$('#swOn').checked;formMessage('#scheduleMsg','尚未保存');};
$('#outputForm').oninput=()=>{outputDirty=true;$('#applyOutput').hidden=true;formMessage('#outputMsg','尚未保存');};
$('#scheduleForm').onsubmit=async e=>{
  e.preventDefault();if(scheduleSaving||outputSaving)return;
  const enabled=$('#swOn').checked, at=$('#tm').value||statusData?.schedule?.at||'03:30';
  scheduleSaving=true;formMessage('#scheduleMsg','保存中…');renderStatus();
  try {
    const data=await request('/api/schedule',{method:'POST',body:{enabled,at}});
    statusData.schedule=data;scheduleDirty=false;formMessage('#scheduleMsg','定时设置已保存');
  }catch(e){formMessage('#scheduleMsg','保存失败：'+e.message,'error');}
  finally {scheduleSaving=false;renderStatus();refreshStatus();}
};
$('#outputForm').onsubmit=async e=>{
  e.preventDefault();if(scheduleSaving||outputSaving)return;
  const body={output_mode:$('#outputMode').value,epg_enable:$('#epgEnabled').checked};
  outputSaving=true;formMessage('#outputMsg','保存中…');renderStatus();
  try {
    const data=await request('/api/config',{method:'POST',body});
    statusData.config=data.config;outputDirty=false;updateResult='';formMessage('#outputMsg','已保存，下次成功更新后生效');
  }catch(e){formMessage('#outputMsg','保存失败：'+e.message,'error');}
  finally {outputSaving=false;renderStatus();refreshStatus();}
};

idle('选台，即可开始观看',true);
showProfile();route();
if(page!=='sched')refreshStatus();
refreshCatalog();setInterval(refreshStatus,15000);
