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

    def test_cctv1_hd_does_not_reenter_4k_group(self):
        raw = [dict(uuid='hd', name='CCTV-1 4K', cp='jsyd', code='HD'),
               dict(uuid='4k', name='CCTV1高清臻享', cp='gitv', code='UHD', num='201'),
               dict(uuid='sd', name='CCTV-1', cp='gitv', code='SD', num='1')]
        raw[0].update(h=1080, grp_uhd=True)
        raw[1].update(h=2160, grp_uhd=True, is_4k=True)
        app._reclassify_uhd(raw)
        self.assertEqual([(c['uuid'], c['group'], c['name']) for c in raw],
                         [('hd', '央视', 'CCTV-1'), ('4k', '4K', 'CCTV-1 4K'), ('sd', '央视', 'CCTV-1（备用）')])
        raw[0].update(h=2160, is_4k=True)
        app._reclassify_uhd(raw)
        self.assertEqual(raw[0]['group'], '4K')


ORIGINAL_PROBE = app.probe_uhd
ORIGINAL_VALIDATE = app.validate_live
