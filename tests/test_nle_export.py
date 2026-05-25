"""Tests for nle_export — timestamp parser and sidecar writers."""

import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nle_export import (
    parse_timestamps,
    write_fcpxml,
    write_edl,
    write_fcp7xml,
    export_sidecars,
)


# ── parse_timestamps ──────────────────────────────────────────────────────────

TXT_SAMPLE = """\
VID_20250829 — departure day

00:15 Filip buckles the roll bags
★ 02:30 they pull out onto the street, morning light
08:12 highway entry ramp
★ 1:15:00 border crossing — long queue
not a timestamp line
"""


def test_parse_basic():
    markers = parse_timestamps(TXT_SAMPLE)
    assert len(markers) == 4


def test_parse_times():
    markers = parse_timestamps(TXT_SAMPLE)
    assert markers[0]['time_s'] == 15
    assert markers[1]['time_s'] == 150
    assert markers[2]['time_s'] == 492
    assert markers[3]['time_s'] == 4500   # 1:15:00


def test_parse_key_flag():
    markers = parse_timestamps(TXT_SAMPLE)
    assert not markers[0]['is_key']
    assert markers[1]['is_key']
    assert not markers[2]['is_key']
    assert markers[3]['is_key']


def test_parse_star_stripped_from_text():
    markers = parse_timestamps(TXT_SAMPLE)
    assert not markers[1]['text'].startswith('★')
    assert not markers[3]['text'].startswith('★')


def test_parse_empty():
    assert parse_timestamps('') == []
    assert parse_timestamps('No timestamps here.\nJust prose.') == []


# ── write_fcpxml ──────────────────────────────────────────────────────────────

@pytest.fixture
def markers():
    return [
        {'time_s': 15,  'text': 'Filip buckles the roll bags', 'is_key': False},
        {'time_s': 150, 'text': 'they pull out',               'is_key': True},
    ]


def test_fcpxml_valid_xml(tmp_path, markers):
    out = tmp_path / 'test.fcpxml'
    write_fcpxml(markers, 'test.mp4', 600.0, out)
    tree = ET.parse(out)
    root = tree.getroot()
    assert root.tag == 'fcpxml'
    assert root.attrib['version'] == '1.11'


def test_fcpxml_marker_count(tmp_path, markers):
    out = tmp_path / 'test.fcpxml'
    write_fcpxml(markers, 'test.mp4', 600.0, out)
    tree = ET.parse(out)
    clip = tree.find('.//asset-clip')
    assert clip is not None
    found = clip.findall('marker')
    assert len(found) == 2


def test_fcpxml_key_marker_attribute(tmp_path, markers):
    out = tmp_path / 'test.fcpxml'
    write_fcpxml(markers, 'test.mp4', 600.0, out)
    tree = ET.parse(out)
    clip = tree.find('.//asset-clip')
    m_regular = clip.findall('marker')[0]
    m_key     = clip.findall('marker')[1]
    assert 'completed' not in m_regular.attrib
    assert m_key.attrib.get('completed') == '0'


# ── write_edl ─────────────────────────────────────────────────────────────────

def test_edl_title(tmp_path, markers):
    out = tmp_path / 'test.edl'
    write_edl(markers, 'test.mp4', 25.0, out)
    content = out.read_text()
    assert 'TITLE: test' in content


def test_edl_marker_colors(tmp_path, markers):
    out = tmp_path / 'test.edl'
    write_edl(markers, 'test.mp4', 25.0, out)
    content = out.read_text()
    assert 'ResolveColorBlue' in content
    assert 'ResolveColorRed' in content


def test_edl_timecode_format(tmp_path, markers):
    out = tmp_path / 'test.edl'
    write_edl(markers, 'test.mp4', 25.0, out)
    content = out.read_text()
    # Expect HH:MM:SS:FF pattern — e.g. 00:00:15:00
    assert re.search(r'\d{2}:\d{2}:\d{2}:\d{2}', content)


# ── write_fcp7xml ─────────────────────────────────────────────────────────────

def test_fcp7xml_valid_xml(tmp_path, markers):
    out = tmp_path / 'test.xmeml'
    write_fcp7xml(markers, 'test.mp4', 25.0, out)
    tree = ET.parse(out)
    assert tree.getroot().tag == 'xmeml'


def test_fcp7xml_frame_positions(tmp_path, markers):
    out = tmp_path / 'test.xmeml'
    write_fcp7xml(markers, 'test.mp4', 25.0, out)
    tree = ET.parse(out)
    seq = tree.find('sequence')
    first = seq.findall('marker')[0]
    assert first.find('in').text  == '375'   # 15s * 25fps
    assert first.find('out').text == '376'


def test_fcp7xml_key_color(tmp_path, markers):
    out = tmp_path / 'test.xmeml'
    write_fcp7xml(markers, 'test.mp4', 25.0, out)
    tree = ET.parse(out)
    seq = tree.find('sequence')
    m_list = seq.findall('marker')
    assert m_list[0].find('color') is None
    assert m_list[1].find('color').text == 'red'


# ── export_sidecars dispatcher ────────────────────────────────────────────────

def test_export_sidecars_none_enabled(tmp_path, markers):
    txt = tmp_path / 'clip.mp4.txt'
    txt.write_text(TXT_SAMPLE, encoding='utf-8')
    cfg = {'nle_export': {'fcpxml': False, 'edl': False, 'fcp7xml': False}}
    result = export_sidecars(txt, 'clip.mp4', 600.0, 25.0, cfg)
    assert result == []


def test_export_sidecars_fcpxml_only(tmp_path):
    txt = tmp_path / 'clip.mp4.txt'
    txt.write_text(TXT_SAMPLE, encoding='utf-8')
    cfg = {'nle_export': {'fcpxml': True, 'edl': False, 'fcp7xml': False}}
    result = export_sidecars(txt, 'clip.mp4', 600.0, 25.0, cfg)
    assert len(result) == 1
    assert result[0].suffix == '.fcpxml'
    assert result[0].exists()


def test_export_sidecars_all_enabled(tmp_path):
    txt = tmp_path / 'clip.mp4.txt'
    txt.write_text(TXT_SAMPLE, encoding='utf-8')
    cfg = {'nle_export': {'fcpxml': True, 'edl': True, 'fcp7xml': True}}
    result = export_sidecars(txt, 'clip.mp4', 600.0, 25.0, cfg)
    assert len(result) == 3
    suffixes = {p.suffix for p in result}
    assert suffixes == {'.fcpxml', '.edl', '.xmeml'}


def test_export_sidecars_no_timestamps(tmp_path):
    txt = tmp_path / 'clip.mp4.txt'
    txt.write_text('No timestamps here.', encoding='utf-8')
    cfg = {'nle_export': {'fcpxml': True, 'edl': True, 'fcp7xml': True}}
    result = export_sidecars(txt, 'clip.mp4', 600.0, 25.0, cfg)
    assert result == []
