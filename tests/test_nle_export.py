"""Tests for nle_export — timestamp parser and sidecar writers."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from nle_export import (
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


class TestWriteFcpxml(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_valid_xml(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out)
        root = ET.parse(out).getroot()  # nosec B314
        self.assertEqual(root.tag, 'fcpxml')
        self.assertEqual(root.attrib['version'], '1.11')

    def test_marker_count(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        self.assertIsNotNone(clip)
        self.assertEqual(len(clip.findall('marker')), 2)

    def test_key_marker_attribute(self):
        out = self.tmp / 'test.fcpxml'
        write_fcpxml(_markers(), 'test.mp4', 600.0, out)
        clip = ET.parse(out).find('.//asset-clip')  # nosec B314
        m_regular, m_key = clip.findall('marker')
        self.assertNotIn('completed', m_regular.attrib)
        self.assertEqual(m_key.attrib.get('completed'), '0')


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
        self.assertEqual(first.find('out').text, '376')

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
        self.assertEqual({p.suffix for p in result}, {'.fcpxml', '.edl', '.xmeml'})

    def test_no_timestamps(self):
        cfg = {'nle_export': {'fcpxml': True, 'edl': True, 'fcp7xml': True}}
        result = export_sidecars(self._txt('No timestamps.'), 'clip.mp4', 600.0, 25.0, cfg)
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
