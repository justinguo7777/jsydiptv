// 第二轮验收：UI 与交互（在浏览器 evaluate 里执行）
// 用法：把本文件内容粘贴到 browser evaluate 的 script 参数
(async () => {
  const q=s=>document.querySelector(s);
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const R={}; const ck=(n,c,d)=>{ R[n]=(c?'✅ ':'❌ ')+(d===undefined?'':d); };

  // ---- 1.1 左侧列表频道名 → 「直播」
  document.querySelector('[data-p="live"]').click(); await wait(600);
  ck('1.1 左侧列表项为「直播」',
     document.querySelector('[data-p="live"]').textContent.trim()==='直播',
     document.querySelector('[data-p="live"]').textContent.trim());

  // ---- 1.2 点 4K 台，检查 EPG 区
  const it=Array.from(document.querySelectorAll('#liveList .item')).find(e=>e.textContent.includes('江苏卫视4K'));
  it.click(); await wait(1700);
  const pl=q('.plist'), ph=q('.plist .p');
  const rows=Math.floor(pl.getBoundingClientRect().height/ph.getBoundingClientRect().height);
  ck('1.2 EPG 可视行数 ≥5', rows>=5, rows+' 行');

  const head0=q('.epghead').getBoundingClientRect();
  pl.scrollTop=pl.scrollHeight; await wait(300);
  const head1=q('.epghead').getBoundingClientRect();
  ck('1.2 台标区滚动时固定', head0.y===head1.y);
  ck('1.2 台标区背景不透明',
     getComputedStyle(q('.epghead')).backgroundColor!=='rgba(0, 0, 0, 0)',
     getComputedStyle(q('.epghead')).backgroundColor);
  const hb=q('.epghead').getBoundingClientRect();
  const hit=document.elementFromPoint(hb.x+60, hb.y+hb.height/2);
  ck('1.2 内容不透出(遮盖测试)', q('.epghead').contains(hit)||hit===q('.epghead'));
  ck('1.2 头部两行不重叠',
     q('.epghead .top').getBoundingClientRect().bottom<=q('.epghead .days').getBoundingClientRect().top+1);

  // 回看：按天完整展示
  const days=Array.from(document.querySelectorAll('.dbtn'));
  ck('1.2 回看按天完整展示(≥7天)', days.length>=7, days.length+' 天');
  const per={};
  for(const b of days){ b.click(); await wait(110); per[b.textContent.trim()]=document.querySelectorAll('.plist .p').length; }
  const minRows=Math.min(...Object.values(per));
  ck('1.2 每天节目条数充足(≥20)', minRows>=20, '最少 '+minRows+' 条');
  // 回看可点
  days.find(b=>b.textContent.startsWith('今天')).click(); await wait(200);
  const past=document.querySelectorAll('.plist .p.past').length;
  const clk=document.querySelectorAll('.plist .p.past.clickable').length;
  ck('1.2 已播节目全部可点回看', past>0 && past===clk, clk+'/'+past);

  // ---- 1.3 右侧列表 EPG 实时
  const api=await (await fetch('/api/channels?group=&q=')).json();
  const map={}; api.channels.forEach(c=>map[c.uuid]=c);
  let mm=0,tot=0;
  document.querySelectorAll('#liveList .item').forEach(el=>{
    const c=map[el.dataset.uuid]; if(!c) return; tot++;
    const shown=(el.querySelector('.nowtxt')||{}).textContent||'';
    if(shown.trim()!==(c.now?c.now.t:'暂无节目单').trim()) mm++;
  });
  ck('1.3 列表节目名与实时接口一致', mm===0, tot+' 个比对，不一致 '+mm);
  const nowS=Date.now()/1000;
  ck('1.3 now 覆盖当前时刻', api.channels.filter(c=>c.now).every(c=>c.now.s<=nowS&&nowS<c.now.e));

  // ---- 1.4 4K 黑屏：编码判据 + 降档出口
  ck('1.4 4K 组均带实测编码',
     (api.channels.filter(c=>c.group==='4K').every(c=>c.codec)), 
     api.channels.filter(c=>c.group==='4K'&&!c.codec).map(c=>c.name).join(','));
  it.click(); await wait(1600);
  ck('1.4 H.265 台给出黑屏原因说明', /H\.265\/HEVC/.test(q('#ph').innerText));
  ck('1.4 提供降档按钮', !!q('#btnDowngrade'), (q('#btnDowngrade')||{}).textContent);
  ck('1.4 提供复制/外部打开', !!q('#btnCopy2') && !!q('#btnOpen2'));

  // ---- 1.5 分组
  ck('1.5 4K 分组存在', '4K' in api.groups, api.groups['4K']+' 个');
  ck('1.5 分组顺序 11 分组新方案',
     JSON.stringify(api.order)===JSON.stringify(['4K','央视','卫视','江苏','地方','影视剧场','少儿动画','体育竞技','生活纪录','音乐广播','购物']),
     JSON.stringify(api.order));
  ck('1.5 无「其他」垃圾组', !('其他' in api.groups), Object.keys(api.groups).join(','));
  ck('1.5 二级组影视剧场有台', (api.groups['影视剧场']||0)>0, api.groups['影视剧场']);
  ck('1.5 界面首分组是 4K', Array.from(document.querySelectorAll('.gh b'))[0].textContent==='4K');

  // ---- 2.1 定时更新页
  document.querySelector('[data-p="sched"]').click(); await wait(900);
  ck('2.1 第二栏位名为「立即更新」',
     Array.from(document.querySelectorAll('#pg-sched .panel h3')).map(e=>e.textContent).includes('立即更新'));
  const btn=q('#btnUpd2').getBoundingClientRect(), lu=q('.lastupd').getBoundingClientRect();
  ck('2.1 立即更新按钮位于左侧信息右边', btn.x>lu.x+lu.width);
  ck('2.1 左侧显示上次更新时间', /\d{4}-\d{2}-\d{2}/.test(q('#lastUpd').textContent), q('#lastUpd').textContent);
  ck('2.2 「支持回看频道」', Array.from(document.querySelectorAll('#pg-sched .st .k')).map(e=>e.textContent).includes('支持回看频道'));

  // ---- 3 播放器接入
  document.querySelector('[data-p="player"]').click(); await wait(600);
  const tabs=Array.from(document.querySelectorAll('#pTabs .tab')).map(e=>e.textContent.trim());
  ck('3 已移除 TiviMate', !tabs.includes('TiviMate'));
  ck('3 新增酷9/电视直播', tabs.some(t=>t.includes('酷9')));
  ck('3 新增天光云影', tabs.some(t=>t.includes('天光云影')));
  const u=id=>{const e=document.getElementById(id);return e?e.value.replace(/^https?:\/\/[^/]+/,''):''};
  ck('3 酷9 回看源用 fongmi.m3u', u('u_ku9')==='/fongmi.m3u', u('u_ku9'));
  ck('3 酷9 EPG 指向 epg.xml', u('u_epg_ku9')==='/epg.xml', u('u_epg_ku9'));
  ck('3 所有地址已填', ['u_fongmi','u_ku9','u_epg_ku9','u_tvbox','u_diyp','u_kodi','u_tg'].every(id=>u(id)!==''));

  // ---- 1.6 专项：列表跳位 / 一键展开折叠 / 4K CCTV 顺序 / 央视 CCTV-1
  document.querySelector('[data-p="live"]').click(); await wait(500);
  const box=q('#liveList');
  const items0=Array.from(document.querySelectorAll('#liveList .item'));
  const tg=items0[Math.floor(items0.length*0.55)];
  tg.scrollIntoView({block:'center'}); await wait(150);
  const top0=box.scrollTop;
  const tgName=tg.querySelector('.nm b').textContent;
  tg.click(); await wait(700);
  ck('1.6 点频道后列表不跳回顶部', Math.abs(q('#liveList').scrollTop-top0)<=40,
     'before='+top0+' after='+q('#liveList').scrollTop);
  ck('1.6 点击项保持高亮', (q('#liveList .item.on')||{}).querySelector && q('#liveList .item.on').querySelector('.nm b').textContent===tgName);

  const ab=q('#allToggle');
  ab.click(); await wait(250);
  ck('1.6 一键折叠全部生效', Array.from(document.querySelectorAll('#liveList .gsec')).every(s=>s.classList.contains('folded')), ab.textContent);
  ab.click(); await wait(250);
  ck('1.6 一键展开全部生效', Array.from(document.querySelectorAll('#liveList .gsec')).every(s=>!s.classList.contains('folded')), ab.textContent);

  const k4names=Array.from(document.querySelectorAll('#liveList .gsec')).find(s=>s.dataset.group==='4K');
  const n4=k4names?Array.from(k4names.querySelectorAll('.item .nm b')).map(b=>b.textContent):[];
  ck('1.6 4K 组首项为 CCTV-1 4K', n4[0]==='CCTV-1 4K', n4.slice(0,7).join(','));
  const cctvGrp=api.groups['央视'];
  ck('1.6 央视组含 CCTV-1', api.channels.some(c=>c.group==='央视'&&c.name==='CCTV-1'));
  ck('1.6 央视组频道数 ≥21', cctvGrp>=21, cctvGrp);

  // ---- 4 软件介绍
  document.querySelector('[data-p="about"]').click(); await wait(600);
  const about=document.querySelector('#pg-about').innerText;
  ck('4 含 4K 分组数', /4K\s*\d+/.test(about.replace(/\n/g,' ')));
  ck('4 已去掉过时的「节目源」页描述', !about.includes('节目源 —'));

  const pass=Object.values(R).filter(v=>v.startsWith('✅')).length;
  const fail=Object.values(R).filter(v=>v.startsWith('❌')).length;
  R['__总结__']=pass+' 通过 / '+fail+' 失败';
  return R;
})()
