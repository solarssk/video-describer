"""Tests for nle_export — timestamp parser and sidecar writers."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from nle_export import (
    _sanitize_edl,
    export_sidecars,
    parse_timestamps,
    write_edl,
    write_fcp7xml,
    write_fcpxml,
)

TXT_SAMPLE = """\
VID_20250829 — departure day

00:15 Filip buckles the roll bags
★ 02:30 they pull out onto the street, morning light
08:12 highway entry ramp
★ 1:15:00 border crossing — long queue
not a timestamp line
"""


def _markers():
    return [
        {'time_s': 15,  'text': 'Filip buckles the roll bags', 'is_key': False},
        {'time_s': 150, 'text': 'they pull out',               'is_key': True},
    ]


class TestParseTimestamps(unittest.TestCase):

    def test_count(self):
        self.assertEqual(len(parse_timestamps(TXT_SAMPLE)), 4)

    def test_times(self):
        m = parse_timestamps(TXT_SAMPLE)
        self.assertEqual(m[0]['time_s'], 15)
        self.assertEqual(m[1]['time_s'], 150)
        self.assertEqual(m[2]['time_s'], 492)
        self.assertEqual(m[3]['time_s'], 4500)  # 1:15:00

    def test_key_flag(self):
        m = parse_timestamps(TXT_SAMPLE)
        self.assertFalse(m[0]['is_key'])
        self.assertTrue(m[1]['is_key'])
        self.assertFalse(m[2]['is_key'])
        self.assertTrue(m[3]['is_key'])

    def test_star_stripped_from_text(self):
        m = parse_timestamps(TXT_SAMPLE)
        self.assertFalse(m[1]['text'].startswith('★'))
        self.assertFalse(m[3]['text'].startswith('★'))

    def test_empty(self):
        self.assertEqual(parse_timestamps(''), [])
        self.assertEqual(parse_timestamps('No timestamps here.\nJust prose.'), [])

    def test_dash_only_text_skipped(self):
        """Marker with only a dash (no text) must be skipped entirely."""
        self.assertEqual(parse_timestamps('00:15 —'), [])
        self.assertEqual(parse_timestamps('00:15 -'), [])

    def test_sorted_by_time(self):
        """Out-of-order timestamps must be returned sorted ascending."""
        txt = '05:00 later\n01:00 earlier\n03:00 middle\n'
        m = parse_timestamps(txt)
        times = [mk['time_s'] for mk in m]
        self.assertEqual(times, sorted(times))

    def test_leading_dash_stripped(self):
        """Leading — – - before text is removed from marker text."""
        cases = [
            '00:30 — em dash text',
            '00:30 – en dash text',
            '00:30 - hyphen text',
        ]
        for line in cases:
            m = parse_timestamps(line)
            self.assertEqual(len(m), 1)
            self.assertFalse(m[0]['text'].startswith(('-', '—', '–')),
                             f'dash not stripped in: {line!r}')


class TestWriteFcpxml(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_valid_xml(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        root = ET.parse(out).getroot()  # nosec B314
        self.assertEqual(root.tag, 'fcpxml')
        self.assertEqual(root.attrib['version'], '1.11')

    def test_marker_count(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        self.assertIsNotNone(clip)
        total = len(clip.findall('marker')) + len(clip.findall('chapter-marker'))
        self.assertEqual(total, 2)

    def test_key_moment_uses_chapter_marker(self):
        """Key moments must be <chapter-marker>, regular moments <marker>."""
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        self.assertEqual(len(clip.findall('marker')), 1)
        self.assertEqual(len(clip.findall('chapter-marker')), 1)

    def test_frame_duration_integer_fps(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        fmt = ET.parse(out).find('.//format')  # nosec B314
        self.assertEqual(fmt.attrib['frameDuration'], '1/25s')

    def test_frame_duration_ntsc_fps(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=29.97)
        fmt = ET.parse(out).find('.//format')  # nosec B314
        self.assertEqual(fmt.attrib['frameDuration'], '1001/30000s')

    def test_asset_element_present(self):
        """<asset> must exist in <resources> with an src file URI."""
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        root = ET.parse(out).getroot()  # nosec B314
        asset = root.find('.//resources/asset')
        self.assertIsNotNone(asset)
        self.assertTrue(asset.attrib.get('src', '').startswith('file://'))

    def test_asset_clip_has_ref(self):
        """<asset-clip> must have ref='r2' linking to the <asset>."""
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        self.assertEqual(clip.attrib.get('ref'), 'r2')

    def test_explicit_video_path_used_in_src(self):
        """When video_path is given, its URI appears in the asset src."""
        out = self.tmp / 'test.fcpxml'
        vpath = self.tmp / 'myclip.mp4'
        write_fcpxml(_markers(), 'myclip.mp4', 600.0, out, fps=25.0, video_path=vpath)
        asset = ET.parse(out).find('.//resources/asset')  # nosec B314
        self.assertIn('myclip.mp4', asset.attrib['src'])

    def test_asset_has_video(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        asset = ET.parse(out).find('.//resources/asset')  # nosec B314
        self.assertEqual(asset.attrib.get('hasVideo'), '1')

    def test_asset_clip_has_offset_and_start(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        self.assertEqual(clip.attrib.get('offset'), '0s')
        self.assertEqual(clip.attrib.get('start'), '0s')

    def test_doctype_in_fcpxml(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out, fps=25.0)
        self.assertIn(b'<!DOCTYPE fcpxml>', out.read_bytes())


class TestWriteEdl(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_title(self):
        out = self.tmp / 'test.edl'
        write_edl(_markers(), 'test.mp4', 25.0, out)
        self.assertIn('TITLE: test', out.read_text())

    def test_marker_colors(self):
        out = self.tmp / 'test.edl'
        write_edl(_markers(), 'test.mp4', 25.0, out)
        content = out.read_text()
        self.assertIn('ResolveColorBlue', content)
        self.assertIn('ResolveColorRed', content)

    def test_timecode_format(self):
        out = self.tmp / 'test.edl'
        write_edl(_markers(), 'test.mp4', 25.0, out)
        self.assertRegex(out.read_text(), r'\d{2}:\d{2}:\d{2}:\d{2}')

    def test_fractional_fps_timecode(self):
        """29.97 fps: base=round(fps)=30; 1s in=00:00:01:00, out=00:00:01:01 (one frame)."""
        out = self.tmp / 'test.edl'
        write_edl([{'time_s': 1, 'text': 'x', 'is_key': False}], 'x.mp4', 29.97, out)
        content = out.read_text()
        self.assertIn('00:00:01:00', content)
        self.assertIn('00:00:01:01', content)

    def test_unicode_sanitized_in_edl(self):
        """Em-dashes, ellipsis and curly quotes are replaced with ASCII equivalents."""
        out = self.tmp / 'test.edl'
        # Use escape sequences so the source stays ASCII-safe
        em_dash = '—'
        ellipsis = '…'
        lquote = '“'
        rquote = '”'
        text = f'long {em_dash} road{ellipsis} {lquote}great{rquote}'
        markers = [{'time_s': 10, 'text': text, 'is_key': False}]
        write_edl(markers, 'clip.mp4', 25.0, out)
        content = out.read_text()
        self.assertNotIn(em_dash, content)
        self.assertNotIn(ellipsis, content)
        self.assertNotIn(lquote, content)
        self.assertIn('long - road...', content)

    def test_long_text_truncation_stays_ascii(self):
        out = self.tmp / 'test.edl'
        markers = [{'time_s': 10, 'text': 'x' * 200, 'is_key': False}]
        write_edl(markers, 'clip.mp4', 25.0, out)
        content = out.read_text()
        self.assertNotIn('…', content)   # no Unicode ellipsis
        line = [ln for ln in content.splitlines() if ln.startswith('* |M:')][0]
        self.assertTrue(line.endswith('...'))
        self.assertLessEqual(len(line), len('* |M: ') + 127)

    def test_from_clip_name_comment(self):
        """Each EDL event must include a FROM CLIP NAME comment for Resolve."""
        out = self.tmp / 'test.edl'
        write_edl(_markers(), 'myclip.mp4', 25.0, out)
        self.assertIn('* FROM CLIP NAME: myclip.mp4', out.read_text())


class TestSanitizeEdl(unittest.TestCase):

    def test_em_dash(self):
        self.assertEqual(_sanitize_edl('a — b'), 'a - b')

    def test_en_dash(self):
        self.assertEqual(_sanitize_edl('a – b'), 'a - b')

    def test_ellipsis(self):
        self.assertEqual(_sanitize_edl('wait…'), 'wait...')

    def test_smart_quotes(self):
        # Use Unicode escapes so source stays ASCII-safe
        left = '“'   # "
        right = '”'  # "
        self.assertEqual(_sanitize_edl(f'{left}hello{right}'), '"hello"')

    def test_star(self):
        self.assertEqual(_sanitize_edl('★ key moment'), '* key moment')

    def test_plain_text_unchanged(self):
        self.assertEqual(_sanitize_edl('hello world'), 'hello world')


class TestWriteFcp7xml(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_valid_xml(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        self.assertEqual(ET.parse(out).getroot().tag, 'xmeml')  # nosec B314

    def test_frame_positions(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        seq = ET.parse(out).find('sequence')  # nosec B314
        first = seq.findall('marker')[0]
        self.assertEqual(first.find('in').text, '375')   # 15s * 25fps
        self.assertEqual(first.find('out').text, '-1')   # point marker

    def test_key_color(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        seq = ET.parse(out).find('sequence')  # nosec B314
        m_list = seq.findall('marker')
        self.assertIsNone(m_list[0].find('color'))
        self.assertEqual(m_list[1].find('color').text, 'red')

    def test_ntsc_true_for_fractional_fps(self):
        """29.97 fps must produce ntsc=TRUE."""
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 29.97, out)
        rate = ET.parse(out).find('sequence/rate')  # nosec B314
        self.assertEqual(rate.find('ntsc').text, 'TRUE')
        self.assertEqual(rate.find('timebase').text, '30')

    def test_ntsc_false_for_integer_fps(self):
        """25 fps must produce ntsc=FALSE."""
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        rate = ET.parse(out).find('sequence/rate')  # nosec B314
        self.assertEqual(rate.find('ntsc').text, 'FALSE')

    def test_version_is_4(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        self.assertEqual(ET.parse(out).getroot().attrib['version'], '4')  # nosec B314

    def test_markers_have_comment(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        seq = ET.parse(out).find('sequence')  # nosec B314
        first = seq.findall('marker')[0]
        self.assertEqual(first.find('comment').text, first.find('name').text)

    def test_has_clipitem_with_file_pathurl(self):
        """Full structure must include media/video/track/clipitem/file/pathurl."""
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        pathurl = ET.parse(out).find(  # nosec B314
            'sequence/media/video/track/clipitem/file/pathurl'
        )
        self.assertIsNotNone(pathurl)
        self.assertTrue(pathurl.text.startswith('file://'))

    def test_doctype_in_premiere_xml(self):
        out = self.tmp / 'test.xmeml'
        write_fcp7xml(_markers(), 'test.mp4', 25.0, out)
        self.assertIn(b'<!DOCTYPE xmeml>', out.read_bytes())


class TestExportSidecars(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _txt(self, content=TXT_SAMPLE):
        p = self.tmp / 'clip.mp4.txt'
        p.write_text(content, encoding='utf-8')
        return p

    def test_none_enabled(self):
        cfg = {'nle_export': {'fcpxml': False, 'edl': False, 'fcp7xml': False}}
        self.assertEqual(export_sidecars(self._txt(), 'clip.mp4', 600.0, 25.0, cfg), [])

    def test_fcpxml_only(self):
        cfg = {'nle_export': {'fcpxml': True, 'edl': False, 'fcp7xml': False}}
        result = export_sidecars(self._txt(), 'clip.mp4', 600.0, 25.0, cfg)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].suffix, '.fcpxml')
        self.assertTrue(result[0].exists())

    def test_all_enabled(self):
        cfg = {'nle_export': {'fcpxml': True, 'edl': True, 'fcp7xml': True}}
        result = export_sidecars(self._txt(), 'clip.mp4', 600.0, 25.0, cfg)
        self.assertEqual(len(result), 3)
        self.assertEqual({p.suffix for p in result}, {'.fcpxml', '.edl', '.xml'})

    def test_no_timestamps(self):
        cfg = {'nle_export': {'fcpxml': True, 'edl': True, 'fcp7xml': True}}
        result = export_sidecars(self._txt('No timestamps.'), 'clip.mp4', 600.0, 25.0, cfg)
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
