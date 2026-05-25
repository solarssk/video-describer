#!/usr/bin/env python3
"""
Video Describer — generates descriptions of GoPro / Insta360 recordings
with timestamps, using Claude AI + (optionally) Whisper for transcription.

Usage: python3 describe_videos.py [files_or_folder] [options]
"""

import argparse
import base64
import importlib.util
import math
import multiprocessing as mp
import queue
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

from timefmt import fmt_ts

warnings.filterwarnings('ignore', category=RuntimeWarning)

# ── Whisper backend detection ──────────────────────────────────────────────
# On Apple Silicon (M-series) we use mlx-whisper — runs on the Neural Engine, much faster.
# On Intel Mac we fall back to faster-whisper on CPU.

IS_APPLE_SILICON = (platform.system() == 'Darwin' and platform.machine() == 'arm64')
IS_MACOS         = (platform.system() == 'Darwin')

# VideoToolbox: hardware video decoder available on all Apple Silicon (and Intel) Macs.
# Offloads H.264/HEVC decode from CPU to the Media Engine — reduces CPU + memory pressure.
# ffmpeg falls back to software silently if the codec isn't supported by VT.
_FFMPEG_HWACCEL = ['-hwaccel', 'videotoolbox'] if IS_MACOS else []

MLX_WHISPER_AVAILABLE = False
FASTER_WHISPER_AVAILABLE = False

if IS_APPLE_SILICON:
    MLX_WHISPER_AVAILABLE = importlib.util.find_spec('mlx_whisper') is not None

if not MLX_WHISPER_AVAILABLE:
    FASTER_WHISPER_AVAILABLE = importlib.util.find_spec('faster_whisper') is not None

WHISPER_AVAILABLE = MLX_WHISPER_AVAILABLE or FASTER_WHISPER_AVAILABLE
WHISPER_BACKEND = (
    'mlx' if MLX_WHISPER_AVAILABLE else
    'faster-whisper' if FASTER_WHISPER_AVAILABLE else
    None
)

# Short model name  →  mlx-community HuggingFace repo
_MLX_MODEL_MAP = {
    'tiny':     'mlx-community/whisper-tiny-mlx',
    'base':     'mlx-community/whisper-base-mlx',
    'small':    'mlx-community/whisper-small-mlx',
    'medium':   'mlx-community/whisper-medium-mlx',
    'large':    'mlx-community/whisper-large-mlx',
    'large-v2': 'mlx-community/whisper-large-v2-mlx',
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
}


def _mlx_model_id(name: str) -> str:
    """Returns the HuggingFace model ID for mlx-whisper given a short name."""
    return _MLX_MODEL_MAP.get(name, name)  # if already a full ID, pass through


class _Segment:
    """Normalised segment — works for both mlx-whisper (dict) and faster-whisper (object)."""
    __slots__ = ('start', 'end', 'text')

    def __init__(self, start: float, end: float, text: str):
        self.start = start
        self.end = end
        self.text = text

    @classmethod
    def from_mlx(cls, d: dict) -> '_Segment':
        return cls(d['start'], d['end'], d['text'])

    @classmethod
    def from_faster(cls, seg) -> '_Segment':
        return cls(seg.start, seg.end, seg.text)


class _MLXWhisperModel:
    """Thin wrapper around mlx_whisper with the same interface as faster-whisper WhisperModel."""

    def __init__(self, model_name: str):
        import mlx_whisper  # eager import — validates backend is truly importable in this process
        self._mlx_whisper = mlx_whisper
        self._model_id = _mlx_model_id(model_name)

    def transcribe(self, audio_path: str, language: str = 'pl',
                   beam_size: int = 5, vad_filter: bool = True,
                   vad_parameters: dict = None):
        result = self._mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self._model_id,
            language=language,
            verbose=False,
        )
        segments = [_Segment.from_mlx(s) for s in result.get('segments', [])]
        return segments, None   # faster-whisper returns (segments, info) — keep compat


class _FasterWhisperWrapper:
    """Wraps faster_whisper.WhisperModel so segments are normalised _Segment objects."""

    def __init__(self, model_name: str):
        from faster_whisper import WhisperModel
        self._model = WhisperModel(model_name, device='cpu', compute_type='int8')

    def transcribe(self, audio_path: str, language: str = 'pl',
                   beam_size: int = 5, vad_filter: bool = True,
                   vad_parameters: dict = None):
        segs, info = self._model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters=vad_parameters or {},
        )
        return [_Segment.from_faster(s) for s in segs], info


class _OpenAIWhisperModel:
    """Uses OpenAI Whisper API (cloud) for transcription.
    Fallback when no local backend is available and an OpenAI key is provided."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def transcribe(self, audio_path: str, language: str = 'pl',
                   beam_size: int = 5, vad_filter: bool = True,
                   vad_parameters: dict = None):
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        client = OpenAI(api_key=self._api_key)
        with open(audio_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language=language,
                response_format='verbose_json',
                timestamp_granularities=['segment'],
            )
        segments = [_Segment(s.start, s.end, s.text) for s in (result.segments or [])]
        return segments, None


def load_whisper_model(model_name: str, openai_api_key: str = None):
    """Factory — returns the best available Whisper backend for this machine.

    Priority: mlx (Neural Engine) → faster-whisper (CPU) → OpenAI API (cloud).
    openai_api_key is only used when no local backend is available.
    """
    if MLX_WHISPER_AVAILABLE:
        try:
            print(f"  Whisper backend: mlx (Neural Engine) — model {_mlx_model_id(model_name)}")
            return _MLXWhisperModel(model_name)
        except ImportError as e:
            print(f"  mlx_whisper import failed ({e}) — falling back to next backend")
    if FASTER_WHISPER_AVAILABLE:
        try:
            print(f"  Whisper backend: faster-whisper (CPU int8) — model {model_name}")
            return _FasterWhisperWrapper(model_name)
        except ImportError as e:
            print(f"  faster_whisper import failed ({e}) — falling back to OpenAI API")
    key = openai_api_key or os.environ.get('OPENAI_API_KEY', '').strip()
    if key:
        print("  Whisper backend: OpenAI API (cloud) — whisper-1")
        return _OpenAIWhisperModel(key)
    raise RuntimeError(
        "No Whisper backend available. "
        "Install mlx-whisper (Apple Silicon) or faster-whisper, "
        "or add an OpenAI API key in the Connectors tab."
    )

from config_loader import load_config, load_system_prompt  # noqa: E402
from providers import AIProvider, make_provider  # noqa: E402

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.insv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}

# These values are reloaded from config.json on every describe_video() call.
# Kept here as fallback for CLI use without an explicit config arg.
_DEFAULT_CFG = load_config()
DEFAULT_PEOPLE = "; ".join(f"{p['name']} - {p['desc']}" for p in _DEFAULT_CFG['defaults']['people'])
DEFAULT_CONTEXT = _DEFAULT_CFG['defaults']['context']
MAX_FRAMES = _DEFAULT_CFG['frames']['max_per_video']
SYSTEM_PROMPT = load_system_prompt()


def get_video_stream_count(video_path: str) -> int:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v',
         '-show_entries', 'stream=index', '-of', 'csv=p=0', video_path],
        capture_output=True, text=True
    )
    streams = [s for s in result.stdout.strip().split('\n') if s.strip()]
    return max(len(streams), 1)


def get_video_duration(video_path: str) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', video_path],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _run_ffmpeg(cmd: list, stop_event=None, duration_sec: float = None,
                progress_cb=None) -> None:
    """Runs ffmpeg via Popen. Killable via stop_event.

    If duration_sec + progress_cb are given, parses -progress pipe:1
    and calls progress_cb(percent_0_to_1, current_sec).
    """
    track_progress = bool(progress_cb and duration_sec and duration_sec > 0)
    if track_progress:
        # Insert -progress as a global flag right after 'ffmpeg'
        cmd = [cmd[0], '-progress', 'pipe:1', '-nostats'] + cmd[1:]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if track_progress else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr_chunks: list = []

    def _read_stderr():
        for line in proc.stderr:
            stderr_chunks.append(line)

    def _read_progress():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith('out_time_us='):
                try:
                    us = int(line.split('=', 1)[1])
                    sec = us / 1_000_000
                    pct = min(1.0, sec / duration_sec)
                    progress_cb(pct, sec)
                except (ValueError, IndexError):
                    pass

    threading.Thread(target=_read_stderr, daemon=True).start()
    if track_progress:
        threading.Thread(target=_read_progress, daemon=True).start()

    while proc.poll() is None:
        if stop_event and stop_event.is_set():
            proc.kill()
            raise InterruptedError("Stopped by user")
        time.sleep(0.3)

    if proc.returncode != 0:
        raise RuntimeError(''.join(stderr_chunks))


def extract_frames(video_path: str, output_dir: str, interval: int,
                   stop_event=None, progress_cb=None,
                   max_frames: int = None, width_px: int = None,
                   jpeg_quality: int = None) -> list:
    """Extracts frames every `interval` seconds.
    Returns a list of (timestamp_sec, frame_path, camera_label).
    For multi-stream files (Insta360 dual-lens) extracts each camera separately.
    progress_cb(percent_0_to_1, current_sec) is called if provided.
    """
    max_frames = max_frames or MAX_FRAMES
    width_px = width_px or _DEFAULT_CFG['frames']['video_width_px']
    jpeg_quality = jpeg_quality or _DEFAULT_CFG['frames']['jpeg_quality']

    duration = get_video_duration(video_path)
    if duration <= 0:
        print("  Warning: cannot determine video duration, assuming 1 hour")
        duration = 3600.0

    stream_count = get_video_stream_count(video_path)
    multi_cam = stream_count > 1
    if multi_cam:
        print(f"  Detected {stream_count} video streams (Insta360 dual-lens)")

    # With multiple cameras, split max_frames between them
    frames_per_cam = max_frames // stream_count if multi_cam else max_frames
    effective_interval = interval
    if duration / interval > frames_per_cam:
        effective_interval = max(interval, math.ceil(duration / frames_per_cam))
        if effective_interval != interval:
            print(f"⚠ Long video ({duration/60:.0f} min), interval adjusted: {interval}s → {effective_interval}s (max_frames cap)")

    all_frames = []

    for stream_idx in range(stream_count):
        cam_label = f'CAM{stream_idx + 1}' if multi_cam else ''
        prefix = f'cam{stream_idx + 1}_' if multi_cam else ''
        output_pattern = os.path.join(output_dir, f'{prefix}frame_%05d.jpg')

        cmd = (
            ['ffmpeg'] + _FFMPEG_HWACCEL + [
            '-i', video_path,
            '-map', f'0:v:{stream_idx}',
            '-vf', f'fps=1/{effective_interval},scale={width_px}:-2',
            '-q:v', str(jpeg_quality),
            '-hide_banner', '-loglevel', 'error',
            output_pattern,
        ])

        # Per-stream progress: cam 1 → 0-50%, cam 2 → 50-100% (for dual-lens)
        def _stream_cb(pct, sec, sidx=stream_idx, slabel=cam_label):
            if progress_cb:
                if multi_cam:
                    overall = (sidx + pct) / stream_count
                    label = f'{slabel} {fmt_ts(sec)}/{fmt_ts(duration)}'
                else:
                    overall = pct
                    label = f'{fmt_ts(sec)}/{fmt_ts(duration)}'
                progress_cb(overall, label)

        _run_ffmpeg(cmd, stop_event=stop_event,
                    duration_sec=duration, progress_cb=_stream_cb)

        for i, frame_file in enumerate(sorted(Path(output_dir).glob(f'{prefix}frame_*.jpg'))):
            timestamp = i * effective_interval
            all_frames.append((float(timestamp), str(frame_file), cam_label))

    # Sort by time, then by camera — so frames from the same moment are grouped
    all_frames.sort(key=lambda x: (x[0], x[2]))
    return all_frames


def extract_audio(video_path: str, output_dir: str,
                  stop_event=None, progress_cb=None, duration: float = None) -> str:
    """Extracts audio as mono WAV 16kHz (format required by Whisper)."""
    audio_path = os.path.join(output_dir, 'audio.wav')
    dur = duration if duration is not None else get_video_duration(video_path)

    def _cb(pct, sec):
        if progress_cb:
            progress_cb(pct, f'{fmt_ts(sec)}/{fmt_ts(dur)}')

    _run_ffmpeg(
        ['ffmpeg'] + _FFMPEG_HWACCEL + [
        '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le',
        '-ar', '16000', '-ac', '1',
        '-hide_banner', '-loglevel', 'error',
        audio_path,
    ], stop_event=stop_event, duration_sec=dur, progress_cb=_cb)
    return audio_path


def transcribe_audio(audio_path: str, model) -> list:
    """Transcribes audio. Returns a list of normalised _Segment objects."""
    segments, _ = model.transcribe(
        audio_path,
        language='pl',
        beam_size=5,
        vad_filter=True,        # skips silence and wind noise — mlx-whisper ignores this gracefully
        vad_parameters={"min_silence_duration_ms": 500},
    )
    return list(segments)


def _transcribe_audio_worker(result_queue, audio_path: str,
                             model_name: str, openai_api_key: str = None):
    """Child-process entrypoint. Returns only JSON/pickle-friendly data."""
    try:
        sys.stdout = sys.__stdout__
        model = load_whisper_model(model_name, openai_api_key=openai_api_key)
        segments = transcribe_audio(audio_path, model)
        result_queue.put({
            'ok': True,
            'segments': [
                {'start': seg.start, 'end': seg.end, 'text': seg.text}
                for seg in segments
            ],
        })
    except BaseException as e:
        result_queue.put({'ok': False, 'error': str(e)})


def transcribe_audio_with_timeout(audio_path: str, model_name: str,
                                  openai_api_key: str = None,
                                  timeout_sec: int = 300,
                                  stop_event=None,
                                  progress_cb=None,
                                  duration: float = None) -> tuple:
    """Run Whisper in a child process so timeout/stop can terminate it safely.

    Returns (segments, timed_out). On timeout, returns ([], True).
    """
    # spawn: child starts a fresh interpreter, so ObjC/Metal init never races
    # with Flask's threads (fork + ObjC threads → SIGABRT on macOS).
    # mlx_whisper is imported lazily inside _MLXWhisperModel.__init__, so
    # the parent never holds a Metal context that the child would inherit.
    ctx = mp.get_context('spawn')
    result_queue = ctx.Queue()
    proc = ctx.Process(
        target=_transcribe_audio_worker,
        args=(result_queue, audio_path, model_name, openai_api_key),
        daemon=True,
    )
    started = time.time()
    timeout_sec = int(timeout_sec or 0)
    proc.start()

    def _terminate():
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=3)

    try:
        while True:
            if stop_event and stop_event.is_set():
                _terminate()
                raise InterruptedError("Stopped by user")

            elapsed = time.time() - started
            if timeout_sec > 0 and elapsed >= timeout_sec:
                _terminate()
                return [], True

            try:
                result = result_queue.get(timeout=1)
            except queue.Empty:
                if progress_cb and duration and duration > 0:
                    est_pct = min(0.95, elapsed / max(duration, 1))
                    progress_cb(est_pct, f'~{int(est_pct * 100)}% (estimate)')
                if not proc.is_alive():
                    proc.join(timeout=1)
                    if proc.exitcode not in (0, None):
                        _SIG = {
                            -6:  'SIGABRT (ObjC/Metal crash)',
                            -9:  'SIGKILL (out of memory or killed)',
                            -11: 'SIGSEGV (segfault)',
                            -15: 'SIGTERM (terminated)',
                        }
                        code = proc.exitcode
                        label = _SIG.get(code) or (f'signal {-code}' if code < 0 else f'exit {code}')
                        raise RuntimeError(f"Whisper crashed ({label}) — transcription skipped for this file")
                continue

            proc.join(timeout=3)
            if not result.get('ok'):
                raise RuntimeError(result.get('error') or 'Whisper transcription failed')
            segments = [
                _Segment(item['start'], item['end'], item['text'])
                for item in result.get('segments', [])
            ]
            return segments, False
    finally:
        if proc.is_alive():
            _terminate()


def format_transcript(segments: list) -> str:
    lines = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            lines.append(f"[{fmt_ts(seg.start)}] {text}")
    return '\n'.join(lines)


def _output_language(cfg: dict = None) -> str:
    cfg = cfg or _DEFAULT_CFG
    lang = str(cfg.get('defaults', {}).get('output_language', 'pl')).lower()
    return lang if lang in ('pl', 'en') else 'pl'


def _content_texts(lang: str) -> dict:
    if lang == 'en':
        return {
            'video_intro': (
                'Below are frames from the recording "{filename}", sampled every few dozen seconds.\n'
                'Context: {context}\n'
                'People who may appear: {people}\n\n'
                'Each frame is preceded by its timestamp:'
            ),
            'transcript_label': (
                '\n\nSpeech transcript from the recording '
                '(automatic, may contain errors):\n{transcript}'
            ),
            'transcript_instruction': (
                '- Include what people say in the recording (transcript above)\n'
            ),
            'video_instruction': (
                '\n\nBased on the frames above{transcript_part}, write a description in this format:\n\n'
                '{filename} - [overall description: what kind of recording this is, who, where, mood '
                '- as many sentences as needed, one is fine]\n'
                'MM:SS event description  (or HH:MM:SS for recordings longer than 1 hour)\n'
                'MM:SS event description\n'
                '...\n\n'
                'Critical formatting rules:\n'
                '- If this is simple riding b-roll with no events: the first line is enough; add no timestamps or 1-2 max\n'
                '- If something happens: first line + timestamps for each important event\n'
                '- Add a timestamp only when something truly changes, not mechanically every 30 seconds\n'
                '{transcript_instruction}'
                '- No headings or markdown\n'
                '- The description length should be proportional to how much happens in the recording'
            ),
            'photo_intro': (
                'Below is the photo "{filename}".\n'
                'Context: {context}\n'
                'People who may appear: {people}'
            ),
            'photo_instruction': (
                'Describe this photo in one line in this format:\n\n'
                '{filename} - [description: what is visible, who is in the photo, where, what is happening, mood]\n\n'
                'Rules:\n'
                '- One paragraph, no timestamps\n'
                '- Be concrete: people, place, equipment, weather, emotions\n'
                '- No headings or markdown'
            ),
        }
    return {
        'video_intro': (
            'Poniżej klatki z nagrania "{filename}" robione co kilkadziesiąt sekund.\n'
            'Kontekst: {context}\n'
            'Osoby które mogą się pojawić: {people}\n\n'
            'Każda klatka poprzedzona jest jej timestampem:'
        ),
        'transcript_label': (
            '\n\nTranskrypcja mowy z nagrania '
            '(automatyczna, może zawierać błędy):\n{transcript}'
        ),
        'transcript_instruction': (
            '- Uwzględnij w opisie to co mówią ludzie na nagraniu (transkrypcja powyżej)\n'
        ),
        'video_instruction': (
            '\n\nNa podstawie powyższych klatek{transcript_part} napisz opis w tym formacie:\n\n'
            '{filename} - [ogólny opis: co to za nagranie, kto, gdzie, nastrój - tyle zdań ile potrzeba, może być jedno]\n'
            'MM:SS opis zdarzenia  (lub HH:MM:SS dla nagrań powyżej godziny)\n'
            'MM:SS opis zdarzenia\n'
            '...\n\n'
            'Krytyczne zasady formatowania:\n'
            '- Jeśli to przebitka jazdy bez zdarzeń: pierwsza linia wystarczy, timestampów nie dodawaj lub dodaj 1-2 max\n'
            '- Jeśli coś się dzieje: pierwsza linia + timestampy dla każdego istotnego zdarzenia\n'
            '- Timestamp tylko gdy naprawdę coś się zmienia (nie co 30 sekund mechanicznie)\n'
            '{transcript_instruction}'
            '- Nie dodawaj nagłówków ani markdown\n'
            '- Długość opisu powinna być proporcjonalna do tego ile się dzieje w nagraniu'
        ),
        'photo_intro': (
            'Poniżej zdjęcie "{filename}".\n'
            'Kontekst: {context}\n'
            'Osoby które mogą się pojawić: {people}'
        ),
        'photo_instruction': (
            'Opisz to zdjęcie w jednej linii w formacie:\n\n'
            '{filename} - [opis: co widać, kto jest na zdjęciu, gdzie, co się dzieje, jaki nastrój]\n\n'
            'Zasady:\n'
            '- Jeden akapit, bez timestampów\n'
            '- Opisuj konkretnie: osoby, miejsce, sprzęt, pogodę, emocje\n'
            '- Nie dodawaj nagłówków ani markdown'
        ),
    }


def transcript_only_text(filename: str, transcript: str, timed_out: bool = False,
                         output_language: str = 'pl') -> str:
    if output_language == 'en':
        header = f"{filename} - speech transcript (no image analysis)"
        body = transcript or (
            "Transcription did not finish (timeout)."
            if timed_out else
            "No speech detected in the recording."
        )
        return f"{header}\n\n{body}"
    header = f"{filename} - transkrypcja mowy (bez analizy obrazu)"
    body = transcript or (
        "Transkrypcja nie została ukończona (timeout)."
        if timed_out else
        "Brak mowy w nagraniu."
    )
    return f"{header}\n\n{body}"


def build_content(frames: list, filename: str, people: str, context: str,
                  transcript: str = None, output_language: str = 'pl') -> list:
    """Builds the content blocks list with interleaved timestamps and frames."""
    has_transcript = bool(transcript and transcript.strip())
    texts = _content_texts(output_language)

    content = [
        {
            "type": "text",
            "text": texts['video_intro'].format(
                filename=filename, context=context, people=people,
            )
        }
    ]

    for timestamp, frame_path, cam_label in frames:
        label = f"{cam_label} " if cam_label else ""
        content.append({"type": "text", "text": f"\n[{label}{fmt_ts(timestamp)}]"})
        with open(frame_path, 'rb') as f:
            data = base64.standard_b64encode(f.read()).decode('utf-8')
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data}
        })

    if has_transcript:
        content.append({
            "type": "text",
            "text": texts['transcript_label'].format(transcript=transcript)
        })

    instruction_extra = (
        texts['transcript_instruction']
        if has_transcript else ""
    )

    content.append({
        "type": "text",
        "text": texts['video_instruction'].format(
            filename=filename,
            transcript_part=(
                ' and the speech transcript'
                if output_language == 'en' and has_transcript else
                ' i transkrypcji mowy'
                if has_transcript else
                ''
            ),
            transcript_instruction=instruction_extra,
        )
    })

    return content


def describe_video(video_path: str, provider: AIProvider,
                   people: str, context: str, interval: int,
                   whisper_model_name: str = None,
                   openai_api_key: str = None,
                   whisper_timeout_sec: int = None,
                   stop_event=None,
                   step_cb=None, progress_cb=None, usage_cb=None,
                   cfg: dict = None, system_prompt: str = None) -> str:
    cfg = cfg or _DEFAULT_CFG
    system_prompt = system_prompt or SYSTEM_PROMPT
    provider_name = cfg['ai']['provider']
    output_language = _output_language(cfg)
    max_tokens = cfg['ai'][provider_name]['max_tokens_video']

    filename = Path(video_path).name
    file_size_mb = Path(video_path).stat().st_size / (1024 * 1024)

    def _step(name: str):
        if step_cb:
            step_cb(name)
        if progress_cb:
            progress_cb(None, '')  # reset progress on each new step

    def _progress(pct, label):
        if progress_cb:
            progress_cb(pct, label)

    def _check_stop():
        if stop_event and stop_event.is_set():
            raise InterruptedError("Stopped by user")

    with tempfile.TemporaryDirectory() as tmp_dir:
        duration = get_video_duration(video_path)
        dur_str = fmt_ts(duration) if duration > 0 else '?'
        size_str = f"{file_size_mb/1024:.1f} GB" if file_size_mb >= 1024 else f"{file_size_mb:.0f} MB"
        print(f"  File: {size_str}, duration: {dur_str}")

        t0 = time.time()
        _step('extracting frames')
        print(f"  Extracting frames every {interval}s...")
        frames = extract_frames(
            video_path, tmp_dir, interval,
            stop_event=stop_event, progress_cb=_progress,
            max_frames=cfg['frames']['max_per_video'],
            width_px=cfg['frames']['video_width_px'],
            jpeg_quality=cfg['frames']['jpeg_quality'],
        )
        print(f"  ✓ {len(frames)} frames extracted ({time.time()-t0:.0f}s)")

        if not frames:
            _step('')
            return f"{filename} - Failed to extract frames from video."

        _check_stop()

        transcript = None
        if whisper_model_name:
            t0 = time.time()
            _step('extracting audio')
            print("  Extracting audio...")
            audio_path = extract_audio(video_path, tmp_dir,
                                       stop_event=stop_event,
                                       progress_cb=_progress, duration=duration)
            print(f"  ✓ Audio ready ({time.time()-t0:.0f}s)")

            _check_stop()

            t0 = time.time()
            _step('Transcribing speech')
            print("  Transcribing speech (Whisper)...")
            segments, timed_out = transcribe_audio_with_timeout(
                audio_path,
                whisper_model_name,
                openai_api_key=openai_api_key,
                timeout_sec=whisper_timeout_sec or cfg['whisper'].get('timeout_sec', 300),
                stop_event=stop_event,
                progress_cb=_progress,
                duration=duration,
            )
            if timed_out:
                print(f"  ⚠ Transcription timed out after {whisper_timeout_sec or cfg['whisper'].get('timeout_sec', 300)}s — skipping audio, continuing")
            else:
                transcript = format_transcript(segments)
                word_count = len(transcript.split()) if transcript else 0
                elapsed = time.time() - t0
                if word_count > 0:
                    print(f"  ✓ {len(segments)} speech segments, {word_count} words ({elapsed:.0f}s)")
                else:
                    print(f"  No speech detected ({elapsed:.0f}s)")

        _check_stop()

        t0 = time.time()
        _step(f'sending to {provider_name}')
        print(f"  Sending {len(frames)} frames to {provider_name}...")
        content = build_content(
            frames, filename, people, context, transcript,
            output_language=output_language,
        )

        response = provider.describe(content, system_prompt, max_tokens)
        print(f"  ✓ Description ready ({time.time()-t0:.0f}s)")
        if usage_cb:
            usage_cb(response.input_tokens, response.output_tokens)
        _step('')

        return response.text


def describe_photo(photo_path: str, provider: AIProvider,
                   people: str, context: str, usage_cb=None,
                   cfg: dict = None, system_prompt: str = None) -> str:
    cfg = cfg or _DEFAULT_CFG
    system_prompt = system_prompt or SYSTEM_PROMPT
    photo_width = cfg['frames']['photo_width_px']
    provider_name = cfg['ai']['provider']
    output_language = _output_language(cfg)
    max_tokens = cfg['ai'][provider_name]['max_tokens_photo']

    filename = Path(photo_path).name

    with tempfile.TemporaryDirectory() as tmp_dir:
        resized = os.path.join(tmp_dir, 'photo.jpg')
        subprocess.run([
            'ffmpeg', '-i', photo_path,
            '-vf', f'scale={photo_width}:-2',
            '-q:v', '3',
            '-hide_banner', '-loglevel', 'error',
            resized
        ], check=True)

        with open(resized, 'rb') as f:
            data = base64.standard_b64encode(f.read()).decode('utf-8')

    texts = _content_texts(output_language)
    content = [
        {
            "type": "text",
            "text": texts['photo_intro'].format(
                filename=filename, context=context, people=people,
            )
        },
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data}
        },
        {
            "type": "text",
            "text": texts['photo_instruction'].format(filename=filename)
        }
    ]

    t0 = time.time()
    response = provider.describe(content, system_prompt, max_tokens)
    print(f"  ✓ Description ready ({time.time()-t0:.0f}s)")
    if usage_cb:
        usage_cb(response.input_tokens, response.output_tokens)

    return response.text


def transcribe_only_video(video_path: str, whisper_model_name: str,
                          openai_api_key: str = None,
                          whisper_timeout_sec: int = None,
                          stop_event=None, step_cb=None, progress_cb=None,
                          cfg: dict = None) -> str:
    """Audio-only mode: extract audio, run Whisper, return formatted transcript.
    No Claude call, no API key needed. Used when 'analyze_images' is OFF."""
    cfg = cfg or _DEFAULT_CFG
    output_language = _output_language(cfg)
    filename = Path(video_path).name

    def _step(name: str):
        if step_cb:
            step_cb(name)
        if progress_cb:
            progress_cb(None, '')

    def _progress(pct, label):
        if progress_cb:
            progress_cb(pct, label)

    def _check_stop():
        if stop_event and stop_event.is_set():
            raise InterruptedError("Stopped by user")

    with tempfile.TemporaryDirectory() as tmp_dir:
        duration = get_video_duration(video_path)
        dur_str = fmt_ts(duration) if duration > 0 else '?'
        file_size_mb = Path(video_path).stat().st_size / (1024 * 1024)
        size_str = f"{file_size_mb/1024:.1f} GB" if file_size_mb >= 1024 else f"{file_size_mb:.0f} MB"
        print(f"  File: {size_str}, duration: {dur_str}")

        t0 = time.time()
        _step('extracting audio')
        print("  Extracting audio...")
        audio_path = extract_audio(video_path, tmp_dir,
                                   stop_event=stop_event,
                                   progress_cb=_progress, duration=duration)
        print(f"  ✓ Audio ready ({time.time()-t0:.0f}s)")

        _check_stop()

        t0 = time.time()
        _step('Transcribing speech')
        print("  Transcribing speech (Whisper)...")
        segments, timed_out = transcribe_audio_with_timeout(
            audio_path,
            whisper_model_name,
            openai_api_key=openai_api_key,
            timeout_sec=whisper_timeout_sec or cfg['whisper'].get('timeout_sec', 300),
            stop_event=stop_event,
            progress_cb=_progress,
            duration=duration,
        )
        if timed_out:
            print(f"  ⚠ Transcription timed out after {whisper_timeout_sec or cfg['whisper'].get('timeout_sec', 300)}s — skipping audio")
            transcript = ''
        else:
            transcript = format_transcript(segments)
            word_count = len(transcript.split()) if transcript else 0
            elapsed = time.time() - t0
            if word_count > 0:
                print(f"  ✓ {len(segments)} speech segments, {word_count} words ({elapsed:.0f}s)")
            else:
                print(f"  No speech detected ({elapsed:.0f}s)")

        _step('')

        return transcript_only_text(filename, transcript, timed_out, output_language)


def find_media(paths: list, file_filter: list = None) -> list:
    """Returns a list of (Path, 'video'|'photo') sorted by filename.

    file_filter: optional list of filenames (basename only). When non-empty,
                 only files whose name is in the list are included.
                 Empty list / None means include everything.
    """
    filter_set = set(file_filter) if file_filter else None
    media = []
    for path_str in paths:
        path = Path(path_str)
        if path.is_file() and not path.name.startswith('._'):
            if filter_set and path.name not in filter_set:
                continue
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                media.append((path, 'video'))
            elif path.suffix.lower() in IMAGE_EXTENSIONS:
                media.append((path, 'photo'))
        elif path.is_dir():
            for f in sorted(path.iterdir()):
                if f.name.startswith('._'):
                    continue
                if filter_set and f.name not in filter_set:
                    continue
                if f.suffix.lower() in VIDEO_EXTENSIONS:
                    media.append((f, 'video'))
                elif f.suffix.lower() in IMAGE_EXTENSIONS:
                    media.append((f, 'photo'))
    seen = set()
    result = []
    for item in media:
        if item[0] not in seen:
            seen.add(item[0])
            result.append(item)
    return sorted(result, key=lambda x: x[0].name)


def main():
    parser = argparse.ArgumentParser(
        description='Automatically describes GoPro / Insta360 recordings using Claude AI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 describe_videos.py /Volumes/GoPro/DCIM/
  python3 describe_videos.py VID_001.mp4 VID_002.mp4
  python3 describe_videos.py . --people "Filip,Jadzia,Lukasz,Milosz" --context "motorcycle trip, Poland to Oman"
  python3 describe_videos.py . --transcribe                         # + speech transcription
  python3 describe_videos.py . --transcribe --whisper-model large-v3  # more accurate model
  python3 describe_videos.py . --interval 60    # one frame per minute
  python3 describe_videos.py . --output-dir ~/Desktop/descriptions/
        """
    )
    parser.add_argument('paths', nargs='+', help='Video files or folder with recordings')
    parser.add_argument('--people', default=DEFAULT_PEOPLE,
                        help=f'People in the recording (default: "{DEFAULT_PEOPLE}")')
    parser.add_argument('--context', default=DEFAULT_CONTEXT,
                        help=f'Recording context (default: "{DEFAULT_CONTEXT}")')
    parser.add_argument('--interval', type=int, default=5,
                        help='Frame interval in seconds (default: 5)')
    parser.add_argument('--transcribe', action='store_true',
                        help='Enable speech transcription via Whisper')
    parser.add_argument('--whisper-model', default='medium',
                        choices=['tiny', 'base', 'small', 'medium', 'large-v3'],
                        help='Whisper model (default: medium). large-v3 = most accurate')
    parser.add_argument('--output-dir', default=None,
                        help='Output folder (default: next to the video)')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing .txt files')

    args = parser.parse_args()

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("Missing ANTHROPIC_API_KEY. Set the environment variable:")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        print("API key: https://console.anthropic.com/settings/api-keys")
        sys.exit(1)

    cfg = _DEFAULT_CFG
    provider = make_provider(cfg['ai']['provider'], cfg, api_key)

    if args.transcribe:
        if not WHISPER_AVAILABLE:
            print("No Whisper backend available.")
            print("  Apple Silicon: pip3 install mlx-whisper")
            print("  Other:         pip3 install faster-whisper")
            sys.exit(1)
        print(f"Transcription backend selected: {WHISPER_BACKEND} — model '{args.whisper_model}'")
        print("Model loads inside an isolated worker process per file.\n")

    media = find_media(args.paths)
    if not media:
        print("No video or photo files found.")
        sys.exit(1)

    videos_count = sum(1 for _, t in media if t == 'video')
    photos_count = sum(1 for _, t in media if t == 'photo')
    print(f"Found: {videos_count} video, {photos_count} photos.")
    print(f"Context: {args.context}")
    print(f"People: {args.people}")
    if args.transcribe:
        print(f"Transcription: enabled (model: {args.whisper_model})")
    print()

    processed = skipped = errors = 0

    for i, (file_path, media_type) in enumerate(media, 1):
        if args.output_dir:
            out_dir = Path(args.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / (file_path.stem + '.txt')
        else:
            output_path = file_path.parent / (file_path.stem + '.txt')

        label = 'video' if media_type == 'video' else 'photo'
        print(f"[{i}/{len(media)}] {file_path.name} ({label})")

        if output_path.exists() and not args.overwrite:
            print(f"  Skipped - {output_path.name} already exists (--overwrite to replace)\n")
            skipped += 1
            continue

        try:
            if media_type == 'video':
                description = describe_video(
                    str(file_path), provider,
                    args.people, args.context,
                    args.interval,
                    whisper_model_name=args.whisper_model if args.transcribe else None,
                    whisper_timeout_sec=cfg['whisper'].get('timeout_sec', 300),
                )
            else:
                print("  Analyzing photo with provider...")
                description = describe_photo(
                    str(file_path), provider,
                    args.people, args.context
                )

            output_path.write_text(description + '\n', encoding='utf-8')
            print(f"  Saved: {output_path}\n")
            processed += 1

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            print(f"  ERROR: {e}\n")
            errors += 1

    print(f"--- Done: processed {processed}, skipped {skipped}, errors {errors} ---")


if __name__ == '__main__':
    main()
