"""Run the unpacked FPK on loopback with fixture inputs. For UI add --browser (Playwright + Chromium)."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def unpack(package, target):
    with tarfile.open(package) as outer, tarfile.open(fileobj=outer.extractfile('app.tgz')) as inner:
        for entry in inner:
            rel = Path(entry.name)
            if rel.is_absolute() or '..' in rel.parts or not (entry.isfile() or entry.isdir()):
                raise ValueError('Unsafe app archive entry: ' + entry.name)
            path = target / rel
            if entry.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('wb') as sink:
                    shutil.copyfileobj(inner.extractfile(entry), sink)


def browser_checks(base, app, fixture, out, version):
    from playwright.sync_api import sync_playwright, expect

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':1440,'height':960}, timezone_id='America/Los_Angeles', color_scheme='light')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("HTMLMediaElement.prototype.canPlayType=()=>''; if(window.MediaSource)MediaSource.isTypeSupported=()=>false;")
        page.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
        page.route('**/stream?*', lambda r: r.fulfill(status=503, body='synthetic: no media'))
        page.goto(base)
        result = page.evaluate((ROOT/'verify/round2_ui.js').read_text())
        assert result['ok'], result
        print(f"Browser read-only: {result['passed']} passed, 0 failed", flush=True)
        at = page.locator
        def click(s): at(s).click()
        def text(s, value): expect(at(s)).to_have_text(value)
        def check(name): print('  [UI PASS]', name, flush=True)
        def nav(p):
            click(f'.nav[data-p="{p}"]');expect(at('#pg-'+p)).to_be_visible()
        def channel(uid): click(f'#liveList .item[data-uuid="{uid}"] .channel-btn')
        def capture(name): page.screenshot(path=str(out/(name+'.png')), full_page=True, animations='disabled')

        nav('live');capture('watch-desktop')
        expect(at('#introPlay')).to_be_visible();expect(at('#introProgress')).to_be_visible();click('#introSearch');expect(at('#liveQ')).to_be_focused();expect(at('#playControls')).to_be_hidden()
        click('#introPlay');expect(at('#liveNow')).to_contain_text('CCTV-1 4K');expect(at('#watchIntro')).to_be_hidden()
        click('#btnStop');expect(at('#watchIntro')).to_be_visible()
        check('待播节目直达直播，搜索直达输入框，停止还原待播页')
        assert at('.brand-icon').evaluate('img=>img.complete && img.naturalWidth>0')
        page.locator('#main').focus();page.keyboard.press('/');expect(at('#liveQ')).to_be_focused()
        at('#liveQ').fill('江苏测试台');page.keyboard.press('Enter');expect(at('#liveNow')).to_contain_text('江苏测试台')
        at('#liveQ').focus();page.keyboard.press('Escape');expect(at('#liveQ')).to_have_value('')
        click('#btnStop');check('正式 LOGO 已加载；键盘搜索、播放与清除')
        at('#liveList .item[data-uuid="b"] .fav').focus();page.keyboard.press('Enter')
        click('#favOnly');expect(at('#liveList .item')).to_have_count(1);channel('b');click('#btnNext')
        expect(at('#liveNow')).to_contain_text('江苏测试台');check('键盘收藏与按筛选换台')
        click('[data-filter="recent"]');expect(at('#liveList .item').first).to_have_attribute('data-uuid','b')
        click('#allChannels');channel('a');expect(at('#ph')).to_contain_text('H.265')
        cross = at('#liveEpg .p').filter(has_text='跨午夜节目')
        for stamp in fixture['cross']:
            day = datetime.fromtimestamp(stamp, app.TZ8).date().isoformat()
            click(f'.dbtn[data-day="{day}"]');expect(cross).to_contain_text('23:30–00:30')
        click('#guideNow');expect(at('.dbtn.on')).to_have_attribute('data-day',fixture['today'])
        expect(at('.epg-summary')).to_contain_text('接下来')
        check('节目单回到现在、当前/下一节目与跨午夜北京时间')
        click('.dbtn[data-day="'+fixture['today']+'"]');click('#replayOnly')
        expect(at('.plist .p').filter(has_text='未来节目')).to_have_count(0)
        old=at('.plist .p').filter(has_text='历史回看');expect(old).to_be_visible()
        text('#playMode','直播')
        # Make the response ignore cancellation so the stale intent guard itself is exercised.
        page.route('**/api/catchup?*', lambda r: r.fulfill(content_type='application/json',body='{"ok":true,"url":"http://fixture.invalid/late"}'))
        page.evaluate('''() => {
          const fetch0=window.fetch.bind(window);
          window.fetch=(url,opts)=>String(url).includes('/api/catchup?')&&window.holdReplay ?
            new Promise(resolve=>window.releaseReplay=()=>fetch0(url,{...opts,signal:undefined}).then(resolve)) : fetch0(url,opts);
          window.holdReplay=true;
        }''')
        old.click();page.wait_for_function('!!window.releaseReplay');click('#btnStop');page.evaluate('releaseReplay()');
        page.wait_for_timeout(150);text('#playMode','待播放');check('停止后迟到回看不抢占')
        channel('a');expect(old).to_be_visible();page.evaluate('window.releaseReplay=null');old.click();page.wait_for_function('!!window.releaseReplay');
        channel('b');page.evaluate('releaseReplay()');page.wait_for_timeout(150);text('#playMode','直播');expect(at('#liveNow')).to_contain_text('江苏测试台');check('换台后迟到回看不抢占')
        page.unroute('**/api/catchup?*');page.evaluate('window.holdReplay=false');channel('a');expect(old).to_be_visible();old.click();
        text('#playMode','回看');expect(at('#liveNow')).to_contain_text('历史回看');expect(at('#btnReturnLive')).to_be_visible();
        expect(at('.phacts button').filter(has_text='试播低清晰度')).to_have_count(0);expect(at('.p.selected')).to_contain_text('历史回看')
        capture('catchup-desktop');node=at('#vid').element_handle();nav('player');click('#pTabs [data-t="guide"]')
        at('#playerChoice').select_option('kodi');expect(at('#rec_kodi')).to_have_value(base+'/kodi.m3u');capture('subscriptions-desktop')
        page.go_back();expect(at('#pg-live')).to_be_visible();text('#playMode','回看');assert node.evaluate('(n) => n === document.querySelector("#vid")')
        click('#btnReturnLive');text('#playMode','直播');expect(at('#liveNow')).to_contain_text('CCTV-1 4K');check('回看选中态、导航不重建播放器、返回直播')
        nav('sched');capture('settings-desktop')
        assert at('#tip').evaluate("e=>!e.classList.contains('on')")
        y1=at('#btnSaveSch').bounding_box()['y'];y2=at('#btnSaveOutput').bounding_box()['y'];assert abs(y1-y2)<2,(y1,y2)
        check('设置按钮桌面基线对齐且跨页反馈不残留')
        at('#outputMode').select_option('full');at('#epgEnabled').uncheck();text('#outputMsg','尚未保存')
        page.evaluate('refreshStatus()');expect(at('#outputMode')).to_have_value('full')
        click('#btnSaveOutput');expect(at('#updHint')).to_contain_text('设置待应用')
        cfg=app.load_config();assert cfg['output_mode']=='full' and not cfg['epg_enable']
        click('#applyOutput');expect(at('#updHint')).to_have_text('清单更新完成。',timeout=15000)
        nav('player');click('#pTabs [data-t="all"]');expect(at('#u_live_txt')).to_have_value(base+'/live.txt');expect(at('#subscriptionNotice')).to_contain_text('未生成节目单')
        check('轮询保留草稿，配置与生成分开，订阅按活动产物显示')
        nav('sched');at('#swOn').uncheck();click('#btnSaveSch');text('#scheduleMsg','定时设置已保存');text('#schNext','定时已关闭');assert not app.load_config()['schedule_enabled']
        with patch.object(app,'run_once',side_effect=RuntimeError('合成更新失败')):
            click('#btnUpd2');expect(at('#updHint')).to_contain_text('上一套清单仍可使用');text('#st','上次更新失败')
        click('#logDetails summary');expect(at('#logs')).to_contain_text('合成更新失败');check('定时保存、更新失败与日志')
        nav('live');channel('a');expect(at('#liveEpg')).to_contain_text('当前清单未生成节目单');click('#btnStop')
        page.reload();text('#version','v'+version);click('#favOnly');expect(at('#liveList .item[data-uuid="b"]')).to_be_visible();
        click('[data-filter="recent"]');expect(at('#liveList .item[data-uuid="a"]')).to_be_visible();check('收藏、最近频道跨刷新持久化，EPG 关闭独立提示')
        # Stress the actual responsive layout with a catalog the size of the upstream set,
        # including CJK names, very long programme titles, and multiple groups.
        page.route('**/api/channels', lambda r: r.fulfill(json={
            'ok':True,'generation':json.loads((Path(app._output_dir())/'meta.json').read_text())['generation'],
            'channels':[dict(uuid=f'visual{i}',name=f'频道 {i:03d} · '+('超长频道名用于检验窄屏布局'*4 if i%5==0 else '高清'),
                num=str(i),group=['央视','卫视','江苏','地方-苏州','体育竞技'][i//56],back=i%2,
                now={'t':('山河之间' if i==0 else '城市纪实 · '+('超长节目名称用于检验换行'*4 if i%5==0 else '今晚特别节目')),'s':int(datetime.now().timestamp())-900,'e':int(datetime.now().timestamp())+1800}) for i in range(280)]}))
        nav('live');click('#allChannels');page.evaluate('refreshCatalog()');expect(at('#liveList .item')).to_have_count(280)
        assert at('#liveList').evaluate('(el)=>el.scrollHeight > el.clientHeight')
        expect(at('#nowGuideList button')).to_have_count(3)
        capture('watch-desktop-dense')
        session=page.context.new_cdp_session(page)
        session.send('Emulation.setEmulatedMedia',{'features':[{'name':'prefers-reduced-transparency','value':'reduce'}]})
        assert at('.catalog').evaluate("e=>matchMedia('(prefers-reduced-transparency: reduce)').matches && getComputedStyle(e).backdropFilter==='none' && getComputedStyle(e).backgroundColor.startsWith('rgb(')")
        capture('watch-opaque-desktop')
        session.send('Emulation.setEmulatedMedia',{'features':[]})
        check('减少透明度偏好切换到实色材质')
        check('280 台长文本目录：独立滚动，不撑破布局')
        for width in (320,390,768,1440):
            page.set_viewport_size({'width':width,'height':850})
            for route in ('live','player','sched'):
                nav(route)
                assert page.evaluate('document.documentElement.scrollWidth === innerWidth'), (width,route,page.evaluate('document.documentElement.scrollWidth'))
        check('320/390/768/1440px 三页无横向溢出')
        # Host chrome and width are local stand-ins for an embedded fnOS window.
        embed=browser.new_page(viewport={'width':1440,'height':960})
        embed.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else r.abort())
        embed.set_content(f'<body style="margin:0;padding:34px;background:#e0e7f4"><iframe title="IPTV" src="{base}/" style="width:820px;height:700px;border:0;border-radius:16px" allow="fullscreen"></iframe>')
        frame=embed.frame_locator('iframe');expect(frame.locator('#introPlay')).to_be_visible()
        for width in (820,640,420):
            embed.locator('iframe').evaluate('(el,w)=>el.style.width=w+"px"',width)
            for route in ('player','sched','live'):
                frame.locator(f'.nav[data-p="{route}"]').click()
                expect(frame.locator('#pg-'+route)).to_be_visible()
                assert frame.locator('html').evaluate('(el)=>el.scrollWidth === innerWidth'), (width,route)
        embed.locator('iframe').evaluate('(el)=>el.style.width="820px"')
        assert frame.locator('.catalog').bounding_box()['y'] >= frame.locator('.player-card').bounding_box()['y']+frame.locator('.player-card').bounding_box()['height']
        frame.locator('#introTitle').scroll_into_view_if_needed()
        embed.locator('iframe').screenshot(path=str(out/'fnos-window.png'),animations='disabled')
        embed.close()
        check('模拟 fnOS iframe 动态宽度 820/640/420，三页可切换且无横向溢出')
        page.set_viewport_size({'width':390,'height':844});page.emulate_media(color_scheme='dark');nav('live');click('#allChannels');capture('watch-mobile-dark');nav('player');capture('subscriptions-mobile-dark');nav('sched');capture('settings-mobile-dark')
        assert not errors, errors
        browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fpk',type=Path,required=True)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--out',type=Path,default=ROOT/'verify/out/1.8.0')
    args=parser.parse_args()
    print('FPK SHA-256:',hashlib.sha256(args.fpk.read_bytes()).hexdigest(),flush=True)
    with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
        area=Path(d);payload=area/'app';unpack(args.fpk,payload)
        stack.enter_context(patch.dict(os.environ,TRIM_APPDEST=str(payload),TRIM_PKGETC=str(area/'etc'),TRIM_PKGVAR=str(area/'var')))
        sys.path.insert(0,str(payload/'server'))
        spec=importlib.util.spec_from_file_location('fixture_app',payload/'server/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
        def fixture(*_,epg_enable):
            rows=[('a','CCTV-1 4K','1','4K'),('b','江苏测试台','77','江苏'),('c','高清测试候选','86','4K')]
            ch=[dict(uuid=uid,name=name,num=num,group=group,district='320500',code='FIXTURE'+uid,cp='gitv',logo='',back=0 if uid=='b' else 1,mc='',otypes=['央视' if uid!='b' else '本地']) for uid,name,num,group in rows]
            ch[0].update(is_4k=True,codec='H.265',w=3840,h=2160)
            ch[2].update(uhd_unverified=True)
            return ch, programs if epg_enable else []
        stamp=datetime.now(app.TZ8);mid=stamp.replace(hour=0,minute=0,second=0,microsecond=0)
        future_midnight=int((mid+timedelta(days=1)).timestamp());cur=int(stamp.timestamp())
        times={'cross':(future_midnight-1800,future_midnight+1800),'today':mid.date().isoformat()}
        programs=[dict(uuid='a',title=title,start=st,end=en) for title,st,en in [('跨午夜节目',*times['cross']),('历史回看',cur-3600,cur-120),('山河之间',cur-120,cur+600),('未来节目',cur+600,cur+1200)]]
        programs.sort(key=lambda x:x['start'])
        stack.enter_context(patch.multiple(app,ordered_hosts=lambda _:['fixture.invalid:6610'],validate_live=lambda *a:(True,{'live':'1/1','sample':1}),collect=fixture,probe_uhd=lambda *a:None,_kick_scheduler=lambda *a:None))
        server=stack.enter_context(socketserver.ThreadingTCPServer(('127.0.0.1',0),app.Handler))
        base='http://127.0.0.1:%d'%server.server_address[1];app.Handler.base_url=base
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        stack.callback(thread.join);stack.callback(server.shutdown)
        version=(payload/'www/VERSION').read_text().strip();cfg=dict(app.DEFAULTS,district='320500',localize_logo=False)
        for mode,epg in [('minimal',True),('full',True),('full',False),('minimal',False)]:
            cfg.update(output_mode=mode,epg_enable=epg);app.save_config(cfg);app.run_once(cfg,base)
            subprocess.run([sys.executable,'-B',str(ROOT/'verify/round1_api.py'),'--base-url',base,'--version',version,'--mode',mode,'--epg','on' if epg else 'off'],check=True)
        if args.browser:
            cfg.update(output_mode='minimal',epg_enable=True);app.save_config(cfg);app.run_once(cfg,base)
            browser_checks(base,app,times,args.out,version)


if __name__=='__main__':
    main()
