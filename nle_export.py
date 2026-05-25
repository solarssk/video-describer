"""NLE sidecar export — FCPXML, EDL (DaVinci Resolve), FCP7 XML (Premiere).

Each function takes a list of marker dicts:
    {'time_s': float, 'text': str, 'is_key': bool}
and writes one sidecar file next to the source .txt.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


# ── Timestamp parser ──────────────────────────────────────────────────────────

_TS_RE = re.compile(
    r'^(?P<star>★\s*)?(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})\s+(?P<text>.+)'
    r'|'
    r'^(?P<star2>★\s*)?(?P<m2>\d+):(?P<s2>\d{2})\s+(?P<text2>.+)',
)


def parse_timestamps(txt_content: str) -> list:
    """Parse timestamp lines from a .txt description and return marker dicts.

    Recognises MM:SS and HH:MM:SS prefixes. Lines starting with ★ are flagged
    as key moments (is_key=True).  Non-timestamp lines are ignored.
    """
    markers = []
    for line in txt_content.splitlines():
        line = line.strip()
        m = _TS_RE.match(line)
        if not m:
            continue
        if m.group('h') is not None:
            h = int(m.group('h'))
            mins = int(m.group('m'))
            secs = int(m.group('s'))
            text = m.group('text').strip()
            is_key = bool(m.group('star'))
        else:
            h = 0
            mins = int(m.group('m2'))
            secs = int(m.group('s2'))
            text = m.group('text2').strip()
            is_key = bool(m.group('star2'))
        # Strip leading ★ from text if it slipped through
        if text.startswith('★'):
            text = text[1:].strip()
            is_key = True
        time_s = h * 3600 + mins * 60 + secs
        markers.append({'time_s': time_s, 'text': text, 'is_key': is_key})
    return markers


# ── Helpers ───────────────────────────────────────────────────────────────────

def _timecode(time_s: float, fps: float) -> str:
    """Convert seconds to SMPTE non-drop timecode HH:MM:SS:FF."""
    total_frames = int(round(time_s * fps))
    ff = total_frames % int(fps)
    total_secs = total_frames // int(fps)
    ss = total_secs % 60
    mm = (total_secs // 60) % 60
    hh = total_secs // 3600
    return f'{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}'


def _truncate(text: str, max_len: int = 100) -> str:
    """Truncate marker text to fit NLE limits."""
    return text if len(text) <= max_len else text[:max_len - 1] + '…'


# ── FCPXML 1.11 (Final Cut Pro) ───────────────────────────────────────────────

def write_fcpxml(markers: list, clip_name: str, duration_s: float,
                 out_path: Path) -> None:
    """Write an FCPXML 1.11 sidecar file with clip markers.

    Time values are expressed in seconds (rational format n/1s).
    No FPS required — FCP resolves frame positions itself.
    Key moments get the 'chapter' marker role (shown in red in FCP).
    """
    root = ET.Element('fcpxml', version='1.11')
    resources = ET.SubElement(root, 'resources')
    fmt = ET.SubElement(resources, 'format', id='r1', name='FFVideoFormat1080p25',
                        frameDuration='100/2500s', width='1920', height='1080')
    _ = fmt  # silence unused-var warning

    library = ET.SubElement(root, 'library')
    event = ET.SubElement(library, 'event', name=Path(clip_name).stem)
    clip = ET.SubElement(event, 'asset-clip',
                         name=clip_name,
                         duration=f'{int(duration_s)}/1s',
                         format='r1',
                         tcFormat='NDF')

    for mk in markers:
        attrs = {
            'start':    f'{mk["time_s"]}/1s',
            'duration': '1/1s',
            'value':    _truncate(mk['text']),
        }
        if mk['is_key']:
            attrs['completed'] = '0'  # FCP shows uncompleted chapter markers in red
        ET.SubElement(clip, 'marker', **attrs)

    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    with out_path.open('wb') as f:
        f.write(b"<?xml version='1.0' encoding='UTF-8'?>\n")
        tree.write(f, encoding='utf-8', xml_declaration=False)


# ── EDL (DaVinci Resolve) ─────────────────────────────────────────────────────

def write_edl(markers: list, clip_name: str, fps: float, out_path: Path) -> None:
    """Write a CMX 3600 EDL with DaVinci Resolve marker extensions.

    Each marker becomes a one-frame cut entry with a Resolve |M: comment.
    Key moments use ResolveColorRed; regular moments use ResolveColorBlue.
    Import via Timelines > Import > Timeline Markers from EDL in DaVinci.
    """
    title = Path(clip_name).stem
    lines = [f'TITLE: {title}', 'FCM: NON-DROP FRAME', '']
    for idx, mk in enumerate(markers, start=1):
        tc_in  = _timecode(mk['time_s'],       fps)
        tc_out = _timecode(mk['time_s'] + 1.0, fps)
        color = 'ResolveColorRed' if mk['is_key'] else 'ResolveColorBlue'
        lines.append(
            f'{idx:03d}  AX  V  C  {tc_in} {tc_out} {tc_in} {tc_out}'
        )
        lines.append(f'* |M: {_truncate(mk["text"], 127)}')
        lines.append(f'* |C: {color}')
        lines.append('')
    out_path.write_text('\n'.join(lines), encoding='utf-8')


# ── FCP7 XML / xmeml (Adobe Premiere) ────────────────────────────────────────

def write_fcp7xml(markers: list, clip_name: str, fps: float,
                  out_path: Path) -> None:
    """Write an FCP7/xmeml XML file with sequence markers for Adobe Premiere.

    Marker positions are expressed in frame numbers (in/out).
    Import via File > Import in Premiere Pro.
    """
    fps_int = int(round(fps))
    xmeml = ET.Element('xmeml', version='2')
    seq = ET.SubElement(xmeml, 'sequence')
    ET.SubElement(seq, 'name').text = Path(clip_name).stem
    rate_el = ET.SubElement(seq, 'rate')
    ET.SubElement(rate_el, 'timebase').text = str(fps_int)
    ET.SubElement(rate_el, 'ntsc').text = 'FALSE'

    for mk in markers:
        frame_in  = int(round(mk['time_s'] * fps))
        frame_out = frame_in + 1
        marker_el = ET.SubElement(seq, 'marker')
        ET.SubElement(marker_el, 'name').text = _truncate(mk['text'])
        ET.SubElement(marker_el, 'in').text   = str(frame_in)
        ET.SubElement(marker_el, 'out').text  = str(frame_out)
        if mk['is_key']:
            ET.SubElement(marker_el, 'color').text = 'red'

    tree = ET.ElementTree(xmeml)
    ET.indent(tree, space='  ')
    with out_path.open('wb') as f:
        f.write(b"<?xml version='1.0' encoding='utf-8'?>\n")
        tree.write(f, encoding='utf-8', xml_declaration=False)


# ── Dispatcher ────────────────────────────────────────────────────────────────

def export_sidecars(txt_path: Path, clip_name: str, duration_s: float,
                    fps: float, cfg: dict) -> list:
    """Generate all enabled NLE sidecar files next to txt_path.

    cfg['nle_export'] controls which formats are written:
        {'fcpxml': True, 'edl': True, 'fcp7xml': False}

    Returns a list of Path objects that were written.
    """
    nle_cfg = cfg.get('nle_export', {})
    if not any(nle_cfg.values()):
        return []

    txt_content = txt_path.read_text(encoding='utf-8')
    markers = parse_timestamps(txt_content)
    if not markers:
        return []

    written = []
    base = txt_path.parent / txt_path.name  # e.g. video.mp4.txt → siblings named video.mp4.*

    if nle_cfg.get('fcpxml'):
        p = base.with_suffix('.fcpxml')
        write_fcpxml(markers, clip_name, duration_s, p)
        written.append(p)

    if nle_cfg.get('edl') and fps and fps > 0:
        p = base.with_suffix('.edl')
        write_edl(markers, clip_name, fps, p)
        written.append(p)

    if nle_cfg.get('fcp7xml') and fps and fps > 0:
        p = base.with_suffix('.xmeml')
        write_fcp7xml(markers, clip_name, fps, p)
        written.append(p)

    return written
