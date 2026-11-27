// Read-only browser acceptance on an isolated instance. No config writes or media playback.
(async()=>{
  const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)], checks={};
  const ck=(name,result)=>checks[name]=Boolean(result);
  const wait=async f=>{for(let i=0;i<50;i++){if(f())return true;await new Promise(r=>setTimeout(r,80));}return false;};
  const get=async url=>{const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error(`${url}: HTTP ${r.status}`);return r.json();};
  const nav=async p=>{$(`.nav[data-p="${p}"]`).click();return wait(()=>!$(`#pg-${p}`).hidden);};
  const tab=t=>$(`#pTabs [data-t="${t}"]`).click();
  const checkPath=(id,path)=>{try{return new URL($('#'+id).value).pathname===path;}catch{return false;}};
  const ids=()=>$$('#liveList .item').map(el=>el.dataset.uuid);
  const originalHash=location.hash;
  try {
    const st=await get('/api/status'), api=await get('/api/channels');
    ck('运行版本、界面版本和产物代号一致', st.version && st.meta.generation===api.generation && await wait(()=>$('#version').textContent==='v'+st.version));
    ck('观看页面路由',await nav('live'));
    $('#allChannels').click();$('#liveQ').value='';$('#liveQ').dispatchEvent(new Event('input'));
    ck('频道列表已加载',await wait(()=>ids().length>0));
    const map=new Map(api.channels.map(c=>[c.uuid,c]));
    ck('频道列表与 API 一致',ids().length===map.size&&ids().every(id=>map.has(id)));
    ck('未实测候选标为待探测', $$('#liveList .item').filter(el=>map.get(el.dataset.uuid)?.uhd_unverified).every(el=>el.querySelector('.u4k')?.textContent==='待探测'));
    if(api.groups['4K'])ck('4K 分组置顶',$('#liveList .gh b')?.textContent==='4K');
    const folds=()=>$$('#liveList .gsec').map(s=>s.classList.contains('folded'));
    if(folds().every(Boolean))$('#allToggle').click();$('#allToggle').click();
    ck('一键折叠',folds().length&&folds().every(Boolean));$('#allToggle').click();
    ck('一键展开',folds().length&&folds().every(x=>!x));
    if($('#filterMenu'))$('#filterMenu').open=true;$('[data-filter="catchup"]').click();ck('可回看筛选',ids().length===api.channels.filter(c=>c.back>0).length&&ids().every(id=>map.get(id)?.back>0));
    if($('#filterMenu'))$('#filterMenu').open=true;$('[data-filter="uhd"]').click();ck('真 4K 排除待探测',ids().length===api.channels.filter(c=>c.is_4k).length&&ids().every(id=>map.get(id)?.is_4k));
    $('#allChannels').click();
    const q=$('#liveQ');q.value=String(api.channels[0].num);q.dispatchEvent(new Event('input'));
    ck('支持按台号搜索',ids().includes(api.channels[0].uuid));q.value='';q.dispatchEvent(new Event('input'));
    ck('设置页和当前产物状态',await nav('sched')&&$('#lastUpd').textContent===st.meta.updated&&$('#outputMode').value===st.config.output_mode);
    const label=st.updating.running?'正在更新':st.updating.error?'上次更新失败':'运行正常';
    ck('更新状态与 API 一致',await wait(()=>$('#st').textContent===label));
    ck('订阅页面路由',await nav('player'));
    tab('all');ck('通用、Kodi、FongMi、TVBox、EPG 入口',['diyp:/iptv.m3u8','kodi:/kodi.m3u','ku9:/fongmi.m3u','fongmi:/fongmi.json','tvbox:/tvbox.json','epg:/epg.xml'].every(s=>{const [id,path]=s.split(':');return checkPath('u_'+id,path);}));
    ck('完整模式额外订阅按当前产物判断',st.meta.output_mode==='full'?checkPath('u_live_txt','/live.txt'):$('[data-c="u_live_txt"]').disabled);
    tab('guide');ck('按播放器有推荐项',!!$('#recommended input')?.value&&!!$('#playerHint').textContent);
    tab('orig');ck('原版重建与三件套',!$('#porig').hidden&&$('#porig').textContent.includes('重建')&&['m3u','epg','json'].every((k,i)=>checkPath('u_orig_'+k,['/orig.m3u8','/orig_epg.xml','/orig.json'][i])));
    const end=await get('/api/status');ck('验收期间产物与版本未变',end.meta.generation===st.meta.generation&&end.version===st.version);
  }catch(e){checks.error=String(e);}
  finally {if(location.hash!==originalHash)location.hash=originalHash;}
  const values=Object.values(checks), passed=values.filter(v=>v===true).length;
  return {ok:passed>0&&passed===values.length,passed,failed:values.length-passed,checks};
})()
