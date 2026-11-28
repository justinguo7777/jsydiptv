"""Offline regressions for generation publication and EPG contracts."""
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SPEC = importlib.util.spec_from_file_location('iptv_server', Path(__file__).resolve().parents[1] / 'fpk/app/server/server.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class Generations(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enter(tempfile.TemporaryDirectory())
        self.data = Path(self.tmp) / 'data'
        self.data.mkdir()
        self.cfg = dict(app.DEFAULTS, district='320500', localize_logo=False)
        self.enter(patch.multiple(app, DATA=str(self.data), PKGVAR=self.tmp))
        self.enter(patch.object(app, 'log'))
        self.enter(patch.object(app, 'ordered_hosts', return_value=['fixture.invalid:6610']))
        self.enter(patch.object(app, 'validate_live', return_value=(True, {'live':'1/1', 'sample':1})))
        self.enter(patch.object(app, 'probe_uhd'))
        self.collect = self.enter(patch.object(app, 'collect', side_effect=self.fixtures))
        self.day = datetime.now(app.TZ8).replace(hour=0, minute=0, second=0, microsecond=0)

    def enter(self, mgr):
        obj = mgr.__enter__()
        self.addCleanup(mgr.__exit__, None, None, None)
        return obj

    def fixtures(self, *args, epg_enable):
        channels = [dict(uuid='sample', name='示例频道', num='15', group='央视', district='320500',
                         code='FIXTURE', cp='gitv', logo='', back=1, mc='', otypes=['央视'])]
        midnight = int((self.day + timedelta(days=1)).timestamp())
        progs = [dict(uuid='sample', title='跨午夜节目', start=midnight-1800, end=midnight+1800)]
        return channels, progs if epg_enable else []

    def run_update(self, **changes):
        return app.run_once(dict(self.cfg, **changes), 'http://fixture.invalid:18123')

    def current(self):
        return Path(app._output_dir())

    def test_full_minimal_and_epg_switch(self):
        for mode, enabled in [('full', True), ('minimal', True), ('full', False), ('minimal', False), ('minimal', True)]:
            with self.subTest(mode=mode, enabled=enabled):
                previous = self.current()
                meta = self.run_update(output_mode=mode, epg_enable=enabled)
                out = self.current()
                self.assertEqual(meta['generation'], out.name)
                self.assertNotEqual(previous, out)
                self.assertTrue(previous.is_dir())
                self.assertEqual(meta['epg_enabled'], enabled)
                self.assertEqual(meta['output_mode'], mode)
                self.assertEqual(self.collect.call_args.kwargs['epg_enable'], enabled)
                self.assertEqual((out/'live.txt').exists(), mode == 'full')
                self.assertEqual('epg' in json.loads((out/'fongmi.json').read_text()), enabled)
                self.assertEqual('epg' in json.loads((out/'tvbox.json').read_text()), enabled)
                self.assertNotIn('${start}', (out/'kodi.m3u').read_text())
                self.assertIn('${start:YmdHMS}', (out/'kodi.m3u').read_text())
                self.assertEqual((out/'iptv.m3u8').read_bytes(), (out/'fongmi.m3u').read_bytes())
                self.assertEqual('url-tvg' in (out/'iptv.m3u8').read_text(), enabled)
                self.assertEqual(bool(ET.parse(out/'epg.xml').findall('programme')), enabled)
                self.assertEqual(bool(ET.parse(out/'orig_epg.xml').findall('programme')), enabled)
                for delta in (0, 1):
                    day = (self.day + timedelta(days=delta)).date().isoformat()
                    file = out/'epg'/'sample'/(day+'.json')
                    self.assertEqual(file.exists(), enabled)
                    if enabled:
                        self.assertEqual(json.loads(file.read_text())['epg_data'][0]['title'], '跨午夜节目')

    def test_legacy_data_and_fault_before_publish(self):
        legacy = self.data/'channels.json'
        legacy.write_text('[]')
        self.assertEqual(self.current(), self.data)
        self.run_update()
        out = self.current()
        files = {f.relative_to(out): f.read_bytes() for f in out.rglob('*') if f.is_file()}
        for step in ('write_orig_outputs', 'validate_generation', 'publish'):
            with self.subTest(step=step), patch.object(app, step, side_effect=OSError('fault injected')):
                with self.assertRaises(OSError):
                    self.run_update(output_mode='full')
                self.assertEqual(self.current(), out)
                self.assertEqual({f.relative_to(out): f.read_bytes() for f in out.rglob('*') if f.is_file()}, files)
                self.assertEqual(list((self.data/'.generations').iterdir()), [out])
        self.assertEqual(legacy.read_text(), '[]')

    def test_now_and_next_in_same_generation(self):
        out = self.data
        (out/'programs.json').write_text(json.dumps({'sample': [
            {'start': 10, 'end': 20, 'title': 'now'}, {'start': 20, 'end': 30, 'title': 'next'}]}))
        ch = [{'uuid':'sample'}]
        app._refresh_now_next(ch, str(out), nowts=15)
        self.assertEqual(ch[0]['now']['t'], 'now')
        self.assertEqual(ch[0]['next']['t'], 'next')

    def test_missing_probe_does_not_claim_4k(self):
        ch = [{'name':'示例4K', 'code':'fixture', 'is_4k':True}]
        with patch.object(app, '_mp', None):
            # 直接调用未被 setUp stub 的原实现。
            ORIGINAL_PROBE(ch, 'http://fixture.invalid')
        self.assertFalse(ch[0]['is_4k'])
        self.assertTrue(ch[0]['uhd_unverified'])

    def test_epg_bad_rows_are_isolated(self):
        payload = {'resultCode': '000', 'content': [None, {'programs': [None,
            {'programName': '坏记录', 'startTime': 'bad', 'endTime': 5},
            *[{'programName': ' 正常节目 ', 'startTime': '1', 'endTime': '5'}]*2]}]}
        with patch.object(app, 'http_get', return_value=json.dumps(payload)) as fetch:
            rows = app.build_programs([{'uuid': 'a&b'}], 'fixture', 1, 1)
        self.assertEqual(rows, [dict(uuid='a&b', title='正常节目', start=1, end=5)])
        self.assertIn('uuid=a%26b', fetch.call_args.args[0])

    def test_epg_batch_failures_abort(self):
        with patch.object(app, 'http_get', return_value='{"resultCode":"500","content":[]}'):
            with self.assertRaisesRegex(RuntimeError, '40%'):
                app.build_programs([{'uuid': str(i)} for i in range(10)], 'fixture', 1, 1)

    def test_epg_guard_uses_common_channels(self):
        baseline = Path(self.tmp)/'previous'; baseline.mkdir()
        (baseline/'programs.json').write_text(json.dumps({str(i): [{}] for i in range(15)}))
        channels = [{'uuid': str(i)} for i in range(10)]
        with self.assertRaisesRegex(RuntimeError, '覆盖下降'):
            app._guard_epg_coverage(str(baseline), channels, channels[:5])
        app._guard_epg_coverage(str(baseline), channels, channels[:6])

    def test_cdn_sample_is_unique(self):
        channels = [dict(uuid=str(i), group='a', cp='gitv', code=str(i)) for i in range(7)]
        with patch.object(app, 'probe_live', return_value=True) as probe:
            ok, details = ORIGINAL_VALIDATE(channels, 'http://fixture.invalid')
        self.assertTrue(ok)
        self.assertEqual(details['sample'], 7)
        self.assertEqual(len(set(c.args[0] for c in probe.call_args_list)), 7)

    def test_uhd_dedup_preserves_group_and_guide_contracts(self):
        channels, programs = self.fixtures(epg_enable=True)
        for uuid, name, height, zhx, groups in [
            ('hd', 'CCTV-1 4K', 1080, True, ['超高清', '央视']),
            ('4k', 'CCTV1高清臻享', 2160, True, ['超高清']),
            ('sd', 'CCTV-1', None, False, ['央视']),
            ('zx', '高清臻享睛彩竞技', 1080, True, ['超高清']),
            ('unknown', '睛彩竞技4K', None, False, ['超高清']),
            ('plus', 'CCTV-1+4K', 2160, True, ['超高清']),
        ]:
            channels.append(dict(channels[0], uuid=uuid, name=name, h=height, zhx=zhx, otypes=groups,
                                 code=uuid, is_4k=height==2160, uhd_unverified=uuid=='unknown'))
            programs.append(dict(programs[0], uuid=uuid))
        self.collect.side_effect = lambda *args, **kwargs: (channels, programs)
        self.run_update()
        out = self.current()
        smart = json.loads((out/'channels.json').read_text())
        by_id = {c['uuid']:c for c in smart}
        self.assertEqual({c['uuid'] for c in smart if c['group']=='4K'}, {'4k','zx','plus'})
        self.assertEqual([(c['uuid'], c['group']) for c in smart if c['name']=='CCTV-1'], [('hd','央视')])
        self.assertIn('1080P 臻享', [a['name'] for a in by_id['4k']['alts']])
        self.assertIn('/lookback/4k/', by_id['4k']['catchup'])
        self.assertEqual(set(json.loads((out/'programs.json').read_text())), set(by_id))
        orig = json.loads((out/'orig.json').read_text())
        groups = {g['name']:{c['uuid'] for c in g['channels']} for g in orig['groups']}
        self.assertEqual(groups['超高清'], {'4k','zx','plus'})
        self.assertTrue({'hd','sd'} <= groups['央视'])
        self.assertIn('sd', {p.attrib['channel'] for p in ET.parse(out/'orig_epg.xml').findall('programme')})


class MineLayout(unittest.TestCase):
    """第三套（自定义分组）必须只影响自己，不能动①②两套。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.etc = Path(self.tmp.name)/'etc'
        self.etc.mkdir()
        self.data = Path(self.tmp.name)/'data'
        self.data.mkdir()
        self.enter(patch.multiple(app, DATA=str(self.data), PKGVAR=self.tmp.name, PKGETC=str(self.etc),
                                  log=lambda *a, **k: None))
        self.enter(patch.object(app, 'ordered_hosts', return_value=['fixture.invalid:6610']))
        self.enter(patch.object(app, 'validate_live', return_value=(True, {'live':'1/1', 'sample':1})))
        self.enter(patch.object(app, 'probe_uhd'))
        day = datetime.now(app.TZ8).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight = int((day + timedelta(days=1)).timestamp())
        self.channels = [
            dict(uuid='cctv1', name='CCTV-1', num='1', group='央视', district='320500',
                 code='C1', cp='gitv', logo='', back=1, mc='', otypes=['央视']),
            dict(uuid='cctv5', name='CCTV-5', num='5', group='央视', district='320500',
                 code='C5', cp='gitv', logo='', back=1, mc='', otypes=['央视', '体育']),
            dict(uuid='sz', name='苏州新闻', num='101', group='地方', district='320500',
                 code='SZ', cp='gitv', logo='', back=1, mc='', otypes=['本地']),
            dict(uuid='tv', name='某卫视', num='30', group='卫视', district='320500',
                 code='TV', cp='gitv', logo='', back=0, mc='', otypes=['卫视'])]
        self.programs = [dict(uuid='cctv1', title='跨午夜节目', start=midnight-1800, end=midnight+1800)]
        self.enter(patch.object(app, 'collect',
                                side_effect=lambda *a, **k: (self.channels, self.programs)))
        self.cfg = dict(app.DEFAULTS, district='320500', localize_logo=False)

    def enter(self, mgr):
        obj = mgr.__enter__()
        self.addCleanup(mgr.__exit__, None, None, None)
        return obj

    def write_layout(self, **changes):
        base = app.seed_layout(self.channels)
        base['groups'].insert(0, {'id': 'fav', 'title': '我的常看', 'multi': False,
                                  'rules': [{'type': 'channel', 'uuid': 'sz'}]})
        base.update(changes)
        (self.etc/'layout.json').write_text(json.dumps(base, ensure_ascii=False), encoding='utf-8')

    def test_seed_matches_upstream_groups(self):
        app.run_once(dict(self.cfg), 'http://fixture.invalid:18123')
        out = Path(app._output_dir())
        self.assertTrue((out/'mine.m3u8').is_file())
        mine = json.loads((out/'mine.json').read_text())
        self.assertEqual({g['name'] for g in mine['groups']}, {'央视', '本地', '卫视'})
        self.assertEqual(sum(len(g['channels']) for g in mine['groups']), 4)
        # 首次种子应写回配置，供用户接着改
        saved = json.loads((self.etc/'layout.json').read_text())
        self.assertEqual(saved['seeded_from'], 'orig')
        self.assertEqual(len(saved['groups']), len(app.ORIG_GROUPS))

    def test_custom_layout_keeps_other_two_untouched(self):
        app.run_once(dict(self.cfg), 'http://fixture.invalid:18123')
        out = Path(app._output_dir())
        before = {n: (out/n).read_bytes() for n in
                  ('iptv.m3u8', 'kodi.m3u', 'fongmi.m3u', 'epg.xml', 'orig.m3u8', 'orig.json')}
        self.write_layout(hide=[{'type': 'match', 'field': 'num', 'op': 'equals', 'value': '30'}])
        app.run_once(dict(self.cfg), 'http://fixture.invalid:18123')
        out = Path(app._output_dir())
        for name, data in before.items():
            self.assertEqual((out/name).read_bytes(), data, name)
        mine = (out/'mine.m3u8').read_text()
        self.assertNotIn('某卫视', mine)
        self.assertIn('group-title="我的常看"', mine)
        self.assertLess(mine.index('我的常看'), mine.index('央视'))

    def test_multi_group_and_renumber(self):
        self.write_layout(renumber=True)
        layout = app.load_layout() or json.loads((self.etc/'layout.json').read_text())
        layout['groups'].append({'id': 'sport', 'title': '体育', 'multi': True,
                                 'rules': [{'type': 'orig_group', 'value': '体育'}]})
        entries, stats = app.apply_mine_layout(self.channels, layout,
                                               {c['uuid']: c['otypes'] for c in self.channels})
        self.assertEqual([(c['uuid'], g) for c, g in entries],
                         [('sz', '我的常看'), ('cctv1', '央视'), ('cctv5', '央视'),
                          ('tv', '卫视'), ('cctv5', '体育')])
        self.assertEqual(stats['multi'], 1)
        d = Path(self.tmp.name)/'mine'
        app.write_mine_outputs(d, 'http://cdn.x/gitv', entries, 7, 'http://nas:18123', True, layout)
        kodi = (d/'mine_kodi.m3u').read_text()
        self.assertIn('${start:YmdHMS}', kodi)
        self.assertIn('tvg-chno="1"', kodi)
        ap = (d/'mine.m3u8').read_text()
        self.assertIn('${(b)yyyyMMddHHmmss}', ap)
        self.assertEqual((d/'mine_fongmi.m3u').read_text(), ap)

    def test_empty_layout_falls_back_without_failing_update(self):
        self.write_layout(hide=[{'type': 'flag', 'flag': 'back'},
                                {'type': 'match', 'field': 'name', 'op': 'contains', 'value': '卫视'}])
        meta = app.run_once(dict(self.cfg), 'http://fixture.invalid:18123')
        self.assertEqual(meta['mine']['state'], 'fallback:empty')
        mine = (Path(app._output_dir())/'mine.m3u8').read_text()
        self.assertIn('#EXTINF', mine)

    def test_validation_rejects_bad_layout(self):
        cases = [
            ({'groups': [{'id': 'x', 'title': 'A', 'rules': [{'type': 'match', 'field': 'name',
                                                             'op': 'regex', 'value': '['}]}]}, '无效规则'),
            ({'groups': [{'id': 'x', 'title': 'A'*25, 'rules': []}]}, '24 个字符'),
            ({'groups': [{'id': 'x', 'title': 'A'}, {'id': 'x', 'title': 'B'}]}, '重复'),
            ({'groups': [{'id': '有中文', 'title': 'A'}]}, '分组 id'),
            ({'groups': [{'id': 'x', 'title': 'A'}]*51}, '不能超过'),
            ({'groups': [{'id': 'x', 'title': 'A', 'rules': [{'type': 'nope'}]}]}, '无效规则'),
        ]
        for body, expect in cases:
            with self.subTest(expect=expect):
                _, err = app.validate_layout(body)
                self.assertIn(expect, err)

    def test_fongmi_exposes_three_sets(self):
        app.run_once(dict(self.cfg), 'http://fixture.invalid:18123')
        cfg = json.loads((Path(app._output_dir())/'fongmi.json').read_text())
        self.assertEqual([l['name'] for l in cfg['lives']], ['智能分组', '原版分组', '自定义分组'])
        self.assertEqual(cfg['lives'][0]['url'], 'http://fixture.invalid:18123/fongmi.m3u')
        self.assertEqual(cfg['lives'][2]['url'], 'http://fixture.invalid:18123/mine_fongmi.m3u')


ORIGINAL_PROBE = app.probe_uhd
ORIGINAL_VALIDATE = app.validate_live
