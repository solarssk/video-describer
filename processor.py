"""
Batch processing logic — run_processing() and its helpers.

Kept separate from web_app.py so the Flask layer stays thin.
web_app.py calls run_processing() in a background thread and passes in
the emit callable, logger, stop_event, and the shared usage dict.
"""

import json
import logging
import math
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from batch_metadata import (
    append_metadata_footer,
    build_batch_state,
    build_manifest_files,
    counts_from_files,
    has_metadata_footer,
    mark_file,
    summary_description,
    utc_timestamp,
    write_json_atomic,
)
from output_paths import find_existing_output, output_txt_path

import psutil

import config_loader
from describe_videos import (
    WHISPER_AVAILABLE, WHISPER_BACKEND, IS_APPLE_SILICON,
    describe_photo, describe_video, find_media, transcribe_only_video,
    get_video_duration, get_video_metadata, get_video_stream_count,
)
from nle_export import export_sidecars
from providers import make_provider

IS_MACOS = platform.system() == 'Darwin'

BATCH_STATE_PATH = Path(__file__).parent / 'batch_state.json'

_logger = logging.getLogger(__name__)


# ── Thermal state ─────────────────────────────────────────────────────────────

def get_thermal_state() -> dict:
    """Return current CPU, RAM, and load metrics for thermal throttle decisions."""
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    try:
        load1 = psutil.getloadavg()[0]
    except (AttributeError, OSError):
        load1 = 0.0
    ncpu = os.cpu_count() or 1
    ratio = load1 / ncpu if ncpu else 0

    thermal_pressure = False
    if IS_MACOS:
        try:
            r = subprocess.run(['pmset', '-g', 'therm'], capture_output=True, text=True, timeout=2)
            out = r.stdout.lower()
            if 'cpu_speed_limit' in out:
                for line in out.split('\n'):
                    if 'cpu_speed_limit' in line:
                        try:
                            limit = int(line.split('=')[-1].strip())
                            if limit < 100:
                                thermal_pressure = True
                        except ValueError:
                            pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if thermal_pressure or ratio > 2.5 or cpu > 95:
        state, label = 'hot', 'high load / throttling'
    elif ratio > 1.5 or cpu > 80:
        state, label = 'warn', 'moderate load'
    else:
        state, label = 'ok', 'idle'

    return {
        'cpu': cpu,
        'ram': ram,
        'load': load1,
        'ncpu': ncpu,
        'thermal': state,
        'thermal_label': label,
        'thermal_pressure': thermal_pressure,
    }


# ── Sleep prevention ──────────────────────────────────────────────────────────

class _SleepBlock:
    """Handle for a best-effort macOS sleep-prevention process."""

    def __init__(self, handle=None):
        """Store the optional subprocess handle to release later."""
        self.handle = handle

    def release(self):
        """Terminate the sleep-prevention process if it was started."""
        if self.handle is not None:
            try:
                self.handle.terminate()
                self.handle.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.handle.kill()
            except Exception:
                pass
            finally:
                self.handle = None
                _logger.info("🔓 Caffeinate released — Mac can sleep again")


def _prevent_sleep() -> '_SleepBlock':
    """Start macOS caffeinate when available and return a release handle.

    Passes -w <pid> so caffeinate auto-exits if the Python process is killed
    unexpectedly (SIGKILL, crash) without reaching the finally block.
    """
    try:
        proc = subprocess.Popen(
            ['caffeinate', '-d', '-i', '-m', '-w', str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("🔒 Caffeinate active — Mac will not sleep during processing")
        return _SleepBlock(proc)
    except FileNotFoundError:
        return _SleepBlock()


# ── Batch state persistence ───────────────────────────────────────────────────

def _save_batch_state(config: dict, files: list, usage: dict, batch_id: str) -> None:
    """Persist the current schema-v2 batch manifest state."""
    state = build_batch_state(
        config=config,
        files=files,
        usage=usage,
        batch_id=batch_id,
    )
    try:
        write_json_atomic(BATCH_STATE_PATH, state)
    except OSError as e:
        print(f"⚠ Warning: could not save batch state: {e}")


def _clear_batch_state() -> None:
    """Remove the persisted batch resume state, ignoring missing files."""
    try:
        BATCH_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ── Cost + error helpers ──────────────────────────────────────────────────────

def _calc_cost(input_tok: int, output_tok: int, cfg: Optional[dict] = None) -> float:
    """Calculate provider cost in USD from token usage and configured prices."""
    cfg = cfg or config_loader.load_config()
    provider_name = cfg['ai']['provider']
    p = cfg['ai'][provider_name]
    return (input_tok * p['price_input_per_mtok_usd']
            + output_tok * p['price_output_per_mtok_usd']) / 1_000_000


_FATAL_API_PATTERNS = (
    'credit balance',
    'invalid api key',
    'invalid x-api-key',
    'authentication',
    'permission_error',
    'permission denied',
)


def _is_fatal_api_error(error_msg: str) -> bool:
    """Return True for provider errors that should stop the whole batch."""
    lower = (error_msg or '').lower()
    return any(p in lower for p in _FATAL_API_PATTERNS)


def _preflight_api(provider) -> tuple:
    """Run the provider's lightweight credential/model verification."""
    return provider.verify()


def _send_notifications(cfg: dict, status: str, processed: int, skipped: int,
                        errors: int, cost_usd: float, duration_sec: float,
                        source: str = '', files: Optional[list] = None) -> None:
    """Fire macOS notification and/or webhook after batch completes or fails."""
    notif = cfg.get('notifications', {})

    if notif.get('macos_notify') and IS_MACOS:
        file_word = 'file' if processed == 1 else 'files'
        if files and len(files) == 1:
            file_label = files[0]
        elif files:
            file_label = f'{processed} {file_word}'
        elif source:
            file_label = Path(source).name or source
        else:
            file_label = f'{processed} {file_word}'
        if status == 'done':
            subtitle = '✓ Done'
            mins, secs = int(duration_sec) // 60, int(duration_sec) % 60
            time_str = f'{mins}m {secs}s' if mins else f'{secs}s'
            msg = f'{file_label} · ${cost_usd:.3f} · {time_str}'
        else:
            subtitle = '⛔ Failed'
            msg = f'Stopped after {file_label}'
        try:
            subprocess.run(
                ['osascript', '-e',
                 f'display notification "{msg}" with title "Video Describer"'
                 f' subtitle "{subtitle}" sound name "Default"'],
                timeout=5, capture_output=True,
            )
            print(f'[notify] macOS notification sent: {subtitle} — {msg}')
        except Exception as exc:
            print(f'[notify] macOS notification failed: {exc}')

    url = notif.get('webhook_url', '').strip()
    if notif.get('webhook_enabled') and url and (status == 'done' or notif.get('webhook_on_error', True)):
        import datetime as _dt
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request
        parsed = urllib.parse.urlparse(url)
        target_host = parsed.netloc or '<invalid-host>'
        if parsed.scheme.lower() not in {'http', 'https'}:
            print(f'[notify] Webhook skipped — invalid scheme for host: {target_host}')
            return
        _fw = 'file' if processed == 1 else 'files'
        _mins, _secs = int(duration_sec) // 60, int(duration_sec) % 60
        _time_str = f'{_mins}m {_secs}s' if _mins else f'{_secs}s'
        if status == 'done':
            _color = 5763719   # 0x57F287 green
            _title = '✓ Batch complete'
        else:
            _color = 15548997  # 0xED4245 red
            _title = '⛔ Batch failed'
        _fields = [
            {'name': 'Processed', 'value': f'{processed} {_fw}', 'inline': True},
            {'name': 'Cost',      'value': f'${cost_usd:.3f}',   'inline': True},
            {'name': 'Duration',  'value': _time_str,            'inline': True},
        ]
        if skipped:
            _fields.append({'name': 'Skipped', 'value': str(skipped), 'inline': True})
        if errors:
            _fields.append({'name': 'Errors', 'value': str(errors), 'inline': True})
        if files and len(files) <= 5:
            _fields.append({'name': 'Files', 'value': '\n'.join(files), 'inline': False})
        elif source:
            _src = Path(source).name or source
            _fields.append({'name': 'Source', 'value': _src, 'inline': False})
        payload = {
            'embeds': [{
                'title':     _title,
                'color':     _color,
                'fields':    _fields,
                'footer':    {'text': 'Video Describer'},
                'timestamp': _dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            }],
            # raw data for non-Discord consumers
            'status': status,
            'processed': processed,
            'skipped': skipped,
            'errors': errors,
            'cost_usd': round(cost_usd, 4),
            'duration_sec': round(duration_sec, 1),
        }
        print(f'[notify] Webhook → {target_host}')
        try:
            req = urllib.request.Request(
                url,
                data=_json.dumps(payload).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'Video-Describer/1.0',
                },
                method='POST',
            )
            resp = urllib.request.urlopen(req, timeout=10)  # nosec B310
            print(f'[notify] Webhook sent — HTTP {resp.status}')
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')[:200]
            print(f'[notify] Webhook failed — HTTP {exc.code}: {body}')
        except Exception as exc:
            print(f'[notify] Webhook failed for {target_host}: {type(exc).__name__}')


# ── QueueLogger ───────────────────────────────────────────────────────────────

class QueueLogger:
    """Redirects print() to UI (emit_fn) and logger.
    Terminal output is handled by logger's console handler — no double write.
    """
    def __init__(self, logger, emit_fn):
        """Create a stdout-like adapter backed by app logging and SSE emit."""
        self._logger = logger
        self._emit = emit_fn

    def write(self, text: str):
        """Write one non-empty log line to both destinations."""
        if text and text.strip():
            t = text.rstrip()
            self._logger.info(t)
            self._emit({'type': 'warn', 'text': t} if t.startswith('⚠') else {'type': 'log', 'text': t})

    def flush(self):
        """Flush the real stdout stream used by the console handler."""
        sys.__stdout__.flush()


# ── Main processing loop ──────────────────────────────────────────────────────

def run_processing(config: dict, emit_fn, logger, stop_event: threading.Event,
                   usage: dict) -> None:
    """
    config    — batch config dict from /start request
    emit_fn   — callable(dict) that sends SSE messages and updates buffers
    logger    — app_logger from web_app
    stop_event — set() to request graceful stop
    usage     — shared {'input', 'output', 'cost_usd'} dict; modified in-place
                so /state and /metrics routes in web_app see live values
    """
    stop_event.clear()
    resume_cost_offset = float(config.get('resume_cost_usd') or 0)
    usage.clear()
    usage.update({'input': 0, 'output': 0, 'cost_usd': resume_cost_offset})
    heartbeat_stop = None
    batch_start = time.time()

    old_stdout = sys.stdout
    sys.stdout = QueueLogger(logger, emit_fn)

    sleep_block = _prevent_sleep()

    # ── Debug session header ──────────────────────────────────────────────────
    try:
        import psutil as _psutil
        _mem = _psutil.virtual_memory()
        _mem_total = f'{_mem.total / (1024**3):.1f} GB'
        _mem_free  = f'{_mem.available / (1024**3):.1f} GB free'
        _ram_info  = f'{_mem_total} ({_mem_free})'
    except Exception:
        _ram_info = 'RAM ?'
    _chip = ''
    try:
        _r = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                            capture_output=True, text=True, timeout=2)
        _chip = _r.stdout.strip()
    except Exception:
        _chip = platform.processor() or platform.machine()
    _py = f'Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'
    _os = f'macOS {platform.mac_ver()[0]}' if platform.system() == 'Darwin' else platform.system()
    logger.debug('=== session: %s | %s ===', uuid.uuid4(), time.strftime('%Y-%m-%dT%H:%M:%S'))
    logger.debug('%s | %s | %s | %s', _os, _py, _ram_info, _chip)
    # ─────────────────────────────────────────────────────────────────────────

    try:
        cfg = config_loader.load_config()
        system_prompt = config_loader.load_system_prompt()

        analyze_images = config.get('analyze_images', True)
        transcribe = config.get('transcribe', False)

        provider = None
        if analyze_images:
            _env_map = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY', 'gemini': 'GEMINI_API_KEY'}
            _active_provider = cfg.get('ai', {}).get('provider', 'anthropic')
            api_key = (
                cfg.get('connectors', {}).get(_active_provider, {}).get('api_key', '').strip()
                or os.environ.get(_env_map.get(_active_provider, ''), '')
            )
            if not api_key:
                emit_fn({'type': 'error', 'text': f'Missing {_active_provider.title()} API key. Add it in the Connectors tab.'})
                return

            try:
                provider = make_provider(cfg['ai']['provider'], cfg, api_key)
            except Exception as e:
                emit_fn({'type': 'error', 'text': f'Failed to init AI provider: {e}'})
                return

            print(f"Pre-flight: verifying {cfg['ai']['provider']} provider...")
            ok, err = _preflight_api(provider)
            if not ok:
                short = err.splitlines()[0][:200]
                emit_fn({'type': 'error', 'text': f'API not available: {short}'})
                print(f"⛔ Pre-flight failed: {err}")
                return
            emit_fn({'type': 'ok', 'text': '✓ API OK'})
        else:
            print("AI image analysis: OFF — running in transcript-only mode")
            if not transcribe:
                emit_fn({'type': 'error', 'text': 'Both AI and speech transcription are disabled — nothing to do'})
                return

        whisper_model_name = None
        openai_key = ''
        if transcribe:
            openai_key = cfg.get('connectors', {}).get('openai', {}).get('api_key', '').strip() \
                         or os.environ.get('OPENAI_API_KEY', '')
            if not WHISPER_AVAILABLE and not openai_key:
                hint = 'pip3 install mlx-whisper' if IS_APPLE_SILICON else 'pip3 install faster-whisper'
                emit_fn({'type': 'error', 'text': f'No Whisper backend. Run: {hint} or add an OpenAI API key in Connectors.'})
                return
            whisper_model_name = config.get('whisper_model') or cfg['whisper']['default_model']
            backend_label = WHISPER_BACKEND or 'openai-api'
            print(f"Whisper backend selected: {backend_label} — model '{whisper_model_name}'")

        file_filter = config.get('files', [])
        media = find_media([config['path']], file_filter=file_filter)
        if not media:
            emit_fn({'type': 'error', 'text': f"No video/photo files found in: {config['path']}"})
            return

        out_dir = Path(config['output_dir']) if config.get('output_dir') else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        is_resume_request = (
            'resume_from_index' in config
            or config.get('resume_next_filepath') is not None
        )
        previous_state = None
        try:
            if is_resume_request and BATCH_STATE_PATH.exists():
                previous_state = json.loads(BATCH_STATE_PATH.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            previous_state = None

        resume_from = int(config.get('resume_from_index', 0) or 0)
        resume_next_filepath = config.get('resume_next_filepath')
        if resume_next_filepath and resume_from > 0:
            if resume_from >= len(media) or str(media[resume_from][0]) != resume_next_filepath:
                for _idx, (_fp, _) in enumerate(media):
                    if str(_fp) == resume_next_filepath or _fp.name == Path(resume_next_filepath).name:
                        resume_from = _idx
                        break
                else:
                    emit_fn({
                        'type': 'error',
                        'text': (
                            'Cannot safely resume: saved next file is no longer in the selected folder. '
                            'Discard the saved batch state and start again.'
                        ),
                    })
                    return
        total_media = len(media)
        batch_id: str = (
            str(previous_state['batch_id'])
            if isinstance(previous_state, dict) and previous_state.get('batch_id')
            else str(uuid.uuid4())
        )
        manifest_files = build_manifest_files(
            media,
            out_dir,
            previous_state=previous_state if isinstance(previous_state, dict) else None,
            resume_from_index=resume_from,
        )
        pre_resume_media = media[:resume_from]
        if resume_from > 0:
            media = media[resume_from:]
            emit_fn({'type': 'ok', 'text': f'Resuming from file {resume_from + 1}/{total_media}'})

        videos = sum(1 for _, t in media if t == 'video')
        photos = sum(1 for _, t in media if t == 'photo')
        emit_fn({'type': 'ok', 'text': f'Found: {videos} video, {photos} photos.'})
        emit_fn({'type': 'total', 'total': total_media})

        _raw_budget = config.get('budget_usd')
        try:
            budget_usd = float(_raw_budget) if _raw_budget is not None else None
            if budget_usd is not None and (math.isnan(budget_usd) or math.isinf(budget_usd) or budget_usd < 0):
                budget_usd = None
        except (TypeError, ValueError):
            budget_usd = None

        if analyze_images and media:
            _EST_IN_PER_FRAME  = 600
            _EST_OUT_PER_FRAME = 60
            total_est_frames   = 0

            def _estimate_frame_count(fp: Path) -> int:
                dur = get_video_duration(str(fp))
                if dur <= 0:
                    return 1
                interval = int(config.get('interval', 5))
                max_frames = cfg['frames']['max_per_video']
                stream_count = max(1, get_video_stream_count(str(fp)))
                frames_per_stream = max(1, max_frames // stream_count)
                effective_interval = interval
                if dur / interval > frames_per_stream:
                    effective_interval = max(interval, math.ceil(dur / frames_per_stream))
                estimated_per_stream = min(math.ceil(dur / effective_interval), frames_per_stream)
                return estimated_per_stream * stream_count

            for fp, mt in media:
                if mt == 'video':
                    total_est_frames += _estimate_frame_count(fp)
                else:
                    total_est_frames += 1
            est_cost = _calc_cost(
                total_est_frames * _EST_IN_PER_FRAME,
                total_est_frames * _EST_OUT_PER_FRAME,
                cfg,
            )
            print(f"Estimated cost: ~${est_cost:.2f} ({total_est_frames} frames across {len(media)} files)")
            if budget_usd is not None and resume_cost_offset + est_cost > budget_usd:
                emit_fn({'type': 'error', 'text':
                         f'⛔ Estimated cost ${resume_cost_offset + est_cost:.2f} exceeds budget limit '
                         f'${budget_usd:.2f} — batch not started'})
                return

        _counts = counts_from_files(manifest_files)
        processed = _counts['processed']
        skipped   = _counts['skipped']
        errors    = _counts['errors']

        _provider_key = cfg['ai'].get('provider', 'anthropic')
        _model_label  = cfg['ai'].get(_provider_key, {}).get('model', '?')
        _budget_dbg   = f'  budget=${budget_usd:.2f}' if budget_usd is not None else ''
        logger.debug(
            f'[batch]  path={config["path"]}  model={_model_label}  '
            f'interval={config.get("interval", 5)}s  '
            f'max_frames={cfg["frames"]["max_per_video"]}  '
            f'files={total_media}{_budget_dbg}'
        )

        heartbeat_stop = threading.Event()
        file_start: list = [time.time()]
        current_step: list = ['']
        current_progress: list = [None, '']
        completed_times: list = []
        summary_entries: list = []
        if pre_resume_media and config.get('generate_summary'):
            for _idx, (_fp, _) in enumerate(pre_resume_media):
                _entry = manifest_files[_idx]
                _txt = Path(_entry['output'])
                if not _txt.exists():
                    continue
                try:
                    summary_entries.append((
                        _fp.name,
                        summary_description(_txt.read_text(encoding='utf-8')),
                    ))
                except OSError:
                    pass

        def _emit_step_status():
            if not current_step[0]:
                return
            elapsed = int(time.time() - file_start[0])
            em, es = divmod(elapsed, 60)
            eta_str = ''
            if completed_times:
                avg = sum(completed_times) / len(completed_times)
                remaining = len(media) - len(completed_times) - 1
                if remaining > 0:
                    eta_s = int(avg * remaining)
                    eta_m, eta_ss = divmod(eta_s, 60)
                    eta_str = f'remaining files: ~{eta_m}min {eta_ss:02d}s'
            emit_fn({
                'type': 'step_status',
                'step': current_step[0],
                'elapsed': f'{em}min {es:02d}s',
                'progress': current_progress[0],
                'progress_label': current_progress[1],
                'eta_files': eta_str,
            })

        def _step_cb(step: str):
            current_step[0] = step
            current_progress[0] = None
            current_progress[1] = ''
            _emit_step_status()

        def heartbeat():
            while not heartbeat_stop.is_set():
                heartbeat_stop.wait(2)
                if not heartbeat_stop.is_set():
                    _emit_step_status()

        threading.Thread(target=heartbeat, daemon=True).start()

        def _progress_cb(pct, label):
            current_progress[0] = pct
            current_progress[1] = label

        _call_start: list = [0.0]

        def _usage_cb(input_tok: int, output_tok: int):
            elapsed = time.time() - _call_start[0] if _call_start[0] else 0.0
            _call_start[0] = 0.0
            usage['input'] += input_tok
            usage['output'] += output_tok
            usage['cost_usd'] = resume_cost_offset + _calc_cost(
                usage['input'], usage['output'], cfg)
            call_cost = _calc_cost(input_tok, output_tok, cfg)
            logger.debug('  ↳ %s in / %s out tok — $%.4f — %.1f s',
                         f'{input_tok:,}', f'{output_tok:,}', call_cost, elapsed)
            emit_fn({'type': 'usage', **usage})

        def _sync_counts():
            nonlocal processed, skipped, errors
            _counts = counts_from_files(manifest_files)
            processed = _counts['processed']
            skipped = _counts['skipped']
            errors = _counts['errors']

        def _persist_state():
            _sync_counts()
            _save_batch_state(
                config=config,
                files=manifest_files,
                usage=usage,
                batch_id=batch_id,
            )

        for i, (file_path, media_type) in enumerate(media, 1):
            if stop_event.is_set():
                print("Stopped by user.")
                break

            if budget_usd is not None and analyze_images and usage['cost_usd'] >= budget_usd:
                msg = (f"Budget limit ${budget_usd:.2f} reached — "
                       f"processed {processed}/{total_media} files "
                       f"(${usage['cost_usd']:.2f} spent)")
                print(f"⚠ {msg}")
                emit_fn({'type': 'error', 'text': f'⛔ {msg}'})
                break

            if transcribe and whisper_model_name and i > 1:
                state = get_thermal_state()
                tiers = cfg['whisper']['fallback_tiers']
                if state['thermal'] == 'hot' and whisper_model_name in tiers:
                    idx = tiers.index(whisper_model_name)
                    if idx < len(tiers) - 1:
                        new_name = tiers[idx + 1]
                        print(f"⚠ System under load ({state['thermal_label']}) — switching Whisper from '{whisper_model_name}' to '{new_name}'")
                        whisper_model_name = new_name
                        print(f"✓ Next transcription will use '{new_name}' via {WHISPER_BACKEND or 'openai-api'}")

            output_path = output_txt_path(file_path, out_dir)

            abs_index = resume_from + i
            manifest_entry = manifest_files[abs_index - 1]
            emit_fn({'type': 'progress', 'current': abs_index, 'total': total_media, 'file': file_path.name})
            print(f"[{abs_index}/{total_media}] {file_path.name}")

            existing = find_existing_output(file_path, out_dir)
            if existing and not config.get('overwrite'):
                output_path = existing  # honour legacy path if that's what exists
                print(f"  Skipped — {existing.name} already exists")
                logger.debug(f'[skip:{file_path.name}]  .txt exists')
                mark_file(manifest_files, file_path, 'skipped', output=output_path)
                emit_fn({'type': 'skipped', 'file': file_path.name})
                _persist_state()

                # Silently upgrade legacy outputs that lack a metadata footer.
                try:
                    raw = output_path.read_text(encoding='utf-8')
                    if not has_metadata_footer(raw):
                        upgraded = append_metadata_footer(
                            raw,
                            source=file_path.name,
                            file_uuid=manifest_entry['uuid'],
                            batch_id=batch_id,
                            processed=utc_timestamp(),
                            model=_model_label,
                        )
                        output_path.write_text(upgraded, encoding='utf-8')
                        logger.debug('[skip:%s]  metadata footer added', file_path.name)
                except (OSError, UnicodeDecodeError):
                    pass

                if config.get('generate_summary'):
                    try:
                        summary_entries.append((
                            file_path.name,
                            summary_description(output_path.read_text(encoding='utf-8')),
                        ))
                    except OSError:
                        pass
                if media_type == 'video' and any(cfg.get('nle_export', {}).values()):
                    _dur, _fps = get_video_metadata(str(file_path))
                    try:
                        _sidecars = export_sidecars(output_path, file_path.name, _dur, _fps, cfg)
                        for _sc in _sidecars:
                            print(f"  NLE: {_sc.name}")
                        _error_flag = output_path.with_suffix('.sidecar_error')
                        if _error_flag.exists():
                            try:
                                _error_flag.unlink()
                            except OSError:
                                pass
                    except Exception as _sidecar_err:
                        _warn = str(_sidecar_err)
                        try:
                            output_path.with_suffix('.sidecar_error').write_text(_warn, encoding='utf-8')
                        except OSError as _flag_err:
                            logger.warning("Could not write sidecar error flag for %s: %s",
                                           file_path.name, _flag_err)
                        print(f"  ⚠ NLE export failed: {_warn}")
                        emit_fn({'type': 'log', 'text': f'⚠ NLE export failed for {file_path.name}: {_warn}'})
                continue

            if media_type == 'photo' and not analyze_images:
                print("  Skipped — photo requires AI analysis (currently disabled)")
                mark_file(manifest_files, file_path, 'skipped', output=output_path)
                emit_fn({'type': 'skipped', 'file': file_path.name})
                _persist_state()
                continue

            try:
                mark_file(manifest_files, file_path, 'in_progress', output=output_path)
                _persist_state()
                file_start[0] = time.time()
                current_progress[0] = None
                current_progress[1] = ''
                file_usage_before = dict(usage)

                # Per-file debug metadata
                try:
                    _fsize = file_path.stat().st_size
                    _fsize_str = (f'{_fsize/(1024**3):.2f} GB' if _fsize >= 1024**3
                                  else f'{_fsize/(1024**2):.1f} MB')
                    if media_type == 'video':
                        _fdur, _ffps = get_video_metadata(str(file_path))
                        _dur_str = f'{int(_fdur//60):02d}:{int(_fdur%60):02d}' if _fdur else '?'
                        logger.debug('[file:%s] size=%s duration=%s fps=%s',
                                     file_path.name, _fsize_str, _dur_str,
                                     f'{_ffps:.2f}' if _ffps else '?')
                    else:
                        logger.debug('[file:%s] size=%s', file_path.name, _fsize_str)
                except Exception:
                    pass

                if media_type == 'video':
                    if analyze_images:
                        if provider is None:
                            raise RuntimeError("No AI provider configured — cannot analyze images")
                        _call_start[0] = time.time()
                        desc = describe_video(
                            str(file_path), provider,
                            config['people'], config['context'],
                            int(config.get('interval', 5)),
                            whisper_model_name=whisper_model_name,
                            openai_api_key=openai_key,
                            whisper_timeout_sec=cfg['whisper'].get('timeout_sec', 300),
                            stop_event=stop_event,
                            step_cb=_step_cb,
                            progress_cb=_progress_cb,
                            usage_cb=_usage_cb,
                            cfg=cfg, system_prompt=system_prompt,
                        )
                    else:
                        if whisper_model_name is None:
                            raise RuntimeError("Whisper model not configured — cannot transcribe audio")
                        desc = transcribe_only_video(
                            str(file_path), whisper_model_name,
                            openai_api_key=openai_key,
                            whisper_timeout_sec=cfg['whisper'].get('timeout_sec', 300),
                            stop_event=stop_event,
                            step_cb=_step_cb,
                            progress_cb=_progress_cb,
                            cfg=cfg,
                        )
                else:
                    if provider is None:
                        raise RuntimeError("No AI provider configured — cannot analyze photo")
                    _step_cb(f"analyzing photo with {cfg['ai']['provider']}")
                    print("  Analyzing photo...")
                    _call_start[0] = time.time()
                    desc = describe_photo(
                        str(file_path), provider, config['people'], config['context'],
                        usage_cb=_usage_cb, cfg=cfg, system_prompt=system_prompt,
                    )

                current_step[0] = ''
                completed_times.append(time.time() - file_start[0])
                processed_at = utc_timestamp()
                desc_with_metadata = append_metadata_footer(
                    desc,
                    source=file_path.name,
                    file_uuid=manifest_entry['uuid'],
                    batch_id=batch_id,
                    processed=processed_at,
                    model=_model_label,
                )
                output_path.write_text(desc_with_metadata, encoding='utf-8')
                print(f"  Saved: {output_path.name}")

                if media_type == 'video' and any(cfg.get('nle_export', {}).values()):
                    _dur, _fps = get_video_metadata(str(file_path))
                    try:
                        _sidecars = export_sidecars(output_path, file_path.name, _dur, _fps, cfg)
                        for _sc in _sidecars:
                            print(f"  NLE: {_sc.name}")
                        _err_flag = output_path.with_suffix('.sidecar_error')
                        if _err_flag.exists():
                            _err_flag.unlink()
                    except Exception as _sidecar_err:
                        _warn = str(_sidecar_err)
                        try:
                            output_path.with_suffix('.sidecar_error').write_text(_warn, encoding='utf-8')
                        except OSError as _flag_err:
                            logger.warning("Could not write sidecar error flag for %s: %s",
                                           file_path.name, _flag_err)
                        print(f"  ⚠ NLE export failed: {_warn}")
                        emit_fn({'type': 'log', 'text': f'⚠ NLE export failed for {file_path.name}: {_warn}'})
                mark_file(manifest_files, file_path, 'done', output=output_path)
                first_line = summary_description(desc_with_metadata)
                summary_entries.append((file_path.name, first_line))
                file_in   = usage['input']    - file_usage_before['input']
                file_out  = usage['output']   - file_usage_before['output']
                file_tokens = file_in + file_out
                file_cost = usage['cost_usd'] - file_usage_before['cost_usd']
                logger.debug(
                    f'[file:{file_path.name}]  '
                    f'in={file_in}  out={file_out}  '
                    f'cost=${file_cost:.4f}  elapsed={completed_times[-1]:.1f}s'
                )
                emit_fn({'type': 'done_file', 'file': file_path.name, 'output': str(output_path),
                         'preview': first_line, 'file_tokens': file_tokens, 'file_cost': file_cost})
                _persist_state()

            except InterruptedError:
                current_step[0] = ''
                print("Stopped by user.")
                break

            except Exception as e:
                current_step[0] = ''
                err_msg = str(e)
                logger.exception('Error processing %s', file_path.name)
                print(f"  ERROR: {err_msg}")
                mark_file(manifest_files, file_path, 'error', output=output_path, error=err_msg)
                _sync_counts()
                emit_fn({'type': 'error_file', 'file': file_path.name, 'error': err_msg})
                _persist_state()
                if _is_fatal_api_error(err_msg):
                    print(f"⛔ Stopping batch: {err_msg}")
                    emit_fn({'type': 'error', 'text': f'⛔ {err_msg}'})
                    break

        else:
            _clear_batch_state()

        heartbeat_stop.set()

        input_path = Path(config['path'])
        if config.get('generate_summary') and summary_entries and input_path.is_dir():
            summary_dir = Path(config['output_dir']) if config.get('output_dir') else input_path
            summary_path = summary_dir / '_summary.txt'
            date_str = __import__('datetime').date.today().isoformat()
            _ai_provider = cfg['ai'].get('provider', 'anthropic')
            model_label = cfg['ai'].get(_ai_provider, {}).get('model', 'claude')
            cost_str = f"${usage['cost_usd']:.2f}"
            sep = '─' * 60
            lines = [
                f"{input_path.name} — {date_str}",
                sep,
            ]
            col = max((len(name) for name, _ in summary_entries), default=20) + 2
            for name, desc in summary_entries:
                lines.append(f"{name:<{col}}{desc}")
            lines += [
                '',
                f"Total: {processed} files · {cost_str} spent · {model_label}",
            ]
            summary_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            print(f"Summary saved: {summary_path.name}")

        logger.debug(
            f'[done]  processed={processed}  skipped={skipped}  errors={errors}  '
            f'in_tok={usage["input"]}  out_tok={usage["output"]}  '
            f'cost=${usage["cost_usd"]:.4f}'
        )
        print(f"\n--- Done: processed {processed}, skipped {skipped}, errors {errors} ---")
        _processed_names = [name for name, _ in summary_entries]
        _send_notifications(cfg, 'done', processed, skipped, errors,
                            usage.get('cost_usd', 0.0), time.time() - batch_start,
                            source=str(config.get('path', '')),
                            files=_processed_names)
        emit_fn({'type': 'done', 'processed': processed, 'skipped': skipped, 'errors': errors})

    except Exception as e:
        print(f"Fatal error: {e}")
        _safe_cfg       = locals().get('cfg') or {}
        _safe_processed = locals().get('processed') or 0
        _safe_skipped   = locals().get('skipped') or 0
        _safe_errors    = locals().get('errors') or 1
        _safe_source    = str((locals().get('config') or {}).get('path', '') or '')
        _send_notifications(
            _safe_cfg, 'error', _safe_processed, _safe_skipped, _safe_errors,
            usage.get('cost_usd', 0.0), time.time() - batch_start,
            source=_safe_source,
        )
        emit_fn({'type': 'error', 'text': str(e)})
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        sys.stdout = old_stdout
        if sleep_block is not None:
            try:
                sleep_block.release()
            except Exception:
                pass


def run_conversion(config: dict, emit_fn, stop_event: threading.Event) -> None:
    """Generate NLE sidecar files for already-processed media without re-running AI.

    Scans the configured path for media files, finds their existing .txt outputs,
    and calls export_sidecars() on each. No API calls are made.
    """
    cfg = config_loader.load_config()
    cfg.update(config)

    nle_cfg = cfg.get('nle_export', {})
    if not any(nle_cfg.values()):
        emit_fn({'type': 'error', 'text': 'No NLE formats enabled. Enable at least one in Settings → NLE Export.'})
        return

    media = find_media([config['path']])
    if not media:
        emit_fn({'type': 'error', 'text': f"No video/photo files found in: {config['path']}"})
        return

    out_dir = Path(config['output_dir']) if config.get('output_dir') else None
    total = len(media)
    converted = 0
    skipped = 0
    errors = 0
    batch_id = str(uuid.uuid4())

    emit_fn({'type': 'log', 'text': f'Found {total} media file(s) — scanning for existing descriptions…'})

    for i, (file_path, media_type) in enumerate(media, 1):
        if stop_event.is_set():
            break

        emit_fn({'type': 'progress', 'current': i, 'total': total, 'file': file_path.name})

        if media_type != 'video':
            skipped += 1
            continue

        existing = find_existing_output(file_path, out_dir)
        if not existing:
            skipped += 1
            continue

        try:
            raw = existing.read_text(encoding='utf-8')
            if not has_metadata_footer(raw):
                upgraded = append_metadata_footer(
                    raw, source=file_path.name,
                    file_uuid=str(uuid.uuid4()), batch_id=batch_id,
                    processed=utc_timestamp(), model='—',
                )
                existing.write_text(upgraded, encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            pass

        _ext_map = [
            ('fcpxml',  '.fcpxml'),
            ('edl',     '.edl'),
            ('fcp7xml', '.xml'),
        ]
        enabled_exts = [ext for key, ext in _ext_map if nle_cfg.get(key)]
        if enabled_exts and all(existing.with_suffix(ext).exists() for ext in enabled_exts):
            skipped += 1
            continue

        _dur, _fps = get_video_metadata(str(file_path))
        try:
            sidecars = export_sidecars(existing, file_path.name, _dur, _fps, cfg)
            if sidecars:
                for sc in sidecars:
                    emit_fn({'type': 'log', 'text': f'  {file_path.name} → {sc.name}'})
                converted += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            emit_fn({'type': 'log', 'text': f'⚠ {file_path.name}: {exc}'})

    emit_fn({'type': 'done', 'processed': converted, 'skipped': skipped, 'errors': errors})
