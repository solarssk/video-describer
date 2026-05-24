#!/usr/bin/env python3
"""
Web interface for Video Describer.
Run:  python3 web_app.py
Open: http://localhost:5555
"""

import json
import logging
import math
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, render_template, request

import config_loader
from providers import make_provider

IS_MACOS = platform.system() == 'Darwin'


class _QuietAccessLogFilter(logging.Filter):
    """Silences access log for frequently polled endpoints —
    otherwise the terminal floods with GET /metrics every 3s."""
    NOISY = ('/metrics', '/state', '/status', '/stream', '/api-status')

    def filter(self, record):
        msg = record.getMessage()
        for path in self.NOISY:
            if f'GET {path} ' in msg or f'POST {path} ' in msg:
                return False
        return True


logging.getLogger('werkzeug').addFilter(_QuietAccessLogFilter())

sys.path.insert(0, os.path.dirname(__file__))
import warnings  # noqa: E402
warnings.filterwarnings('ignore', category=RuntimeWarning)

# Print "Starting up..." immediately — before the slow describe_videos import
# (mlx_whisper / anthropic / etc. take a few seconds to load).
# "Open in browser" is shown later, once the server is actually ready.
if __name__ == '__main__':
    sys.stdout.write('\n  Starting up...\r')
    sys.stdout.flush()

from describe_videos import (  # noqa: E402
    WHISPER_AVAILABLE, WHISPER_BACKEND, IS_APPLE_SILICON,
    MLX_WHISPER_AVAILABLE, FASTER_WHISPER_AVAILABLE,
    describe_photo, describe_video, find_media,
    transcribe_only_video,
    get_video_duration, get_video_stream_count,
)

app = Flask(__name__)

VERSION = config_loader.get_version()


def get_thermal_state() -> dict:
    """Returns {cpu, ram, load, ncpu, thermal, thermal_label}.

    thermal: 'ok' | 'warn' | 'hot' — based on load average and (on macOS) pmset.
    """
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    try:
        load1 = psutil.getloadavg()[0]  # cross-platform (emulated on Windows)
    except (AttributeError, OSError):
        load1 = 0.0
    ncpu = os.cpu_count() or 1
    ratio = load1 / ncpu if ncpu else 0

    # Thermal pressure only on macOS — pmset is Apple-specific
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

class _SleepBlock:
    """Prevents Mac from sleeping during processing. Call .release() when done."""
    def __init__(self, handle=None):
        self.handle = handle

    def release(self):
        if self.handle is not None:
            try:
                self.handle.terminate()
                self.handle.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.handle.kill()
            except Exception:
                pass


def _prevent_sleep() -> '_SleepBlock':
    """Starts caffeinate so the Mac won't sleep during processing."""
    try:
        proc = subprocess.Popen(
            ['caffeinate', '-d', '-i', '-m'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("🔒 Caffeinate active — Mac will not sleep during processing")
        return _SleepBlock(proc)
    except FileNotFoundError:
        return _SleepBlock()


log_queue: queue.Queue = queue.Queue()
is_processing = False
stop_event = threading.Event()
_start_lock = threading.Lock()

# Persisted state — survives browser reconnect
log_buffer: list = []          # last 500 log entries
results_buffer: list = []      # finished files
total_files_global: int = 0
progress_global: dict = {}     # {current, total, file}
usage_global: dict = {'input': 0, 'output': 0, 'cost_usd': 0.0}
LOG_BUFFER_MAX = 500

def _calc_cost(input_tok: int, output_tok: int, cfg: dict = None) -> float:
    cfg = cfg or config_loader.load_config()
    provider_name = cfg['ai']['provider']
    p = cfg['ai'][provider_name]
    return (input_tok * p['price_input_per_mtok_usd']
            + output_tok * p['price_output_per_mtok_usd']) / 1_000_000


# Error patterns that indicate it's pointless to continue the batch
# (vs. a per-file transient error like a corrupted MP4).
_FATAL_API_PATTERNS = (
    'credit balance',           # out of credits
    'invalid api key',
    'invalid x-api-key',
    'authentication',           # 401
    'permission_error',
    'permission denied',        # 403
)


def _is_fatal_api_error(error_msg: str) -> bool:
    """True if the error means the whole batch should stop (bad key / no credit)."""
    lower = (error_msg or '').lower()
    return any(p in lower for p in _FATAL_API_PATTERNS)


def _preflight_api(provider) -> tuple:
    """Minimal provider call to verify key + credit before doing any ffmpeg work.
    Returns (ok: bool, error_msg: str)."""
    return provider.verify()


def emit(msg: dict):
    """Sends a message to SSE and stores it in the buffer.
    Messages of type 'step_status', 'usage' and 'ping' are NOT stored in log_buffer.
    """
    global total_files_global, progress_global

    log_queue.put(msg)

    msg_type = msg.get('type')
    ephemeral = msg_type in ('step_status', 'usage', 'ping')

    if not ephemeral:
        log_buffer.append(msg)
        if len(log_buffer) > LOG_BUFFER_MAX:
            log_buffer.pop(0)

    if msg_type == 'total':
        total_files_global = msg['total']
    elif msg_type == 'progress':
        progress_global = msg
    elif msg_type == 'done_file':
        results_buffer.append(msg)
    elif msg_type == 'done':
        progress_global = {}


class QueueLogger:
    """Redirects print() to emit() and the original stdout."""
    def __init__(self):
        self.orig = sys.__stdout__

    def write(self, text: str):
        self.orig.write(text)
        self.orig.flush()
        if text and text.strip():
            t = text.rstrip()
            emit({'type': 'warn', 'text': t} if t.startswith('⚠') else {'type': 'log', 'text': t})

    def flush(self):
        self.orig.flush()


def run_processing(config: dict):
    global is_processing, usage_global
    is_processing = True
    stop_event.clear()
    usage_global = {'input': 0, 'output': 0, 'cost_usd': 0.0}
    heartbeat_stop = None

    old_stdout = sys.stdout
    sys.stdout = QueueLogger()

    # Prevents the computer from sleeping during processing (cross-platform).
    sleep_block = _prevent_sleep()

    try:
        # Fresh config + prompt — so UI Settings changes apply on next run
        cfg = config_loader.load_config()
        system_prompt = config_loader.load_system_prompt()

        analyze_images = config.get('analyze_images', True)
        transcribe = config.get('transcribe', False)

        provider = None
        if analyze_images:
            # Key priority: Connectors tab (persisted) → env var → legacy form field
            api_key = (
                cfg.get('connectors', {}).get('anthropic', {}).get('api_key', '').strip()
                or os.environ.get('ANTHROPIC_API_KEY', '')
                or config.get('api_key', '')
            )
            if not api_key:
                emit({'type': 'error', 'text': 'Missing Anthropic API key. Add it in the Connectors tab.'})
                return

            try:
                provider = make_provider(cfg['ai']['provider'], cfg, api_key)
            except Exception as e:
                emit({'type': 'error', 'text': f'Failed to init AI provider: {e}'})
                return

            # Pre-flight: verify provider credentials BEFORE loading Whisper or ffmpeg.
            print(f"Pre-flight: verifying {cfg['ai']['provider']} provider...")
            ok, err = _preflight_api(provider)
            if not ok:
                short = err.splitlines()[0][:200]
                emit({'type': 'error', 'text': f'API not available: {short}'})
                print(f"⛔ Pre-flight failed: {err}")
                return
            print("✓ API OK")
        else:
            print("AI image analysis: OFF — running in transcript-only mode")
            if not transcribe:
                emit({'type': 'error', 'text': 'Both AI and speech transcription are disabled — nothing to do'})
                return

        whisper_model_name = None
        openai_key = ''
        if transcribe:
            openai_key = cfg.get('connectors', {}).get('openai', {}).get('api_key', '').strip() \
                         or os.environ.get('OPENAI_API_KEY', '')
            if not WHISPER_AVAILABLE and not openai_key:
                hint = 'pip3 install mlx-whisper' if IS_APPLE_SILICON else 'pip3 install faster-whisper'
                emit({'type': 'error', 'text': f'No Whisper backend. Run: {hint} or add an OpenAI API key in Connectors.'})
                return
            whisper_model_name = config.get('whisper_model') or cfg['whisper']['default_model']
            backend_label = WHISPER_BACKEND or 'openai-api'
            print(f"Whisper backend selected: {backend_label} — model '{whisper_model_name}'")

        file_filter = config.get('files', [])  # [] = all, [...names] = subset
        media = find_media([config['path']], file_filter=file_filter)
        if not media:
            emit({'type': 'error', 'text': f"No video/photo files found in: {config['path']}"})
            return

        videos = sum(1 for _, t in media if t == 'video')
        photos = sum(1 for _, t in media if t == 'photo')
        print(f"Found: {videos} video, {photos} photos.")
        emit({'type': 'total', 'total': len(media)})

        # Validate and normalise budget_usd once — treat 0 as a valid limit
        _raw_budget = config.get('budget_usd')
        try:
            budget_usd = float(_raw_budget) if _raw_budget is not None else None
            if budget_usd is not None and (math.isnan(budget_usd) or math.isinf(budget_usd) or budget_usd < 0):
                budget_usd = None
        except (TypeError, ValueError):
            budget_usd = None

        # ── Pre-batch cost estimate ──────────────────────────────────────
        # Scan durations and estimate the number of frames that will be sent
        # to the AI provider, then compute a rough cost.  This gives the user
        # a heads-up before any heavy processing begins.
        # Rough token averages per frame: 600 input (image+context) + 60 output.
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
                    total_est_frames += 1  # photos count as one frame each
            est_cost = _calc_cost(
                total_est_frames * _EST_IN_PER_FRAME,
                total_est_frames * _EST_OUT_PER_FRAME,
                cfg,
            )
            print(f"Estimated cost: ~${est_cost:.2f} ({total_est_frames} frames across {len(media)} files)")
            if budget_usd is not None and est_cost > budget_usd:
                emit({'type': 'error', 'text':
                      f'⛔ Estimated cost ${est_cost:.2f} exceeds budget limit ${budget_usd:.2f} — batch not started'})
                return

        processed = skipped = errors = 0

        # Heartbeat — emits 'step_status' every 2s (ephemeral, not logged)
        heartbeat_stop = threading.Event()
        file_start: list = [time.time()]
        current_step: list = ['']           # updated by step_cb
        current_progress: list = [None, ''] # [percent_or_None, label]
        completed_times: list = []          # times of finished files → ETA
        summary_entries: list = []          # (filename, first_line) for _summary.txt

        def _emit_step_status():
            if not current_step[0]:
                return

            elapsed = int(time.time() - file_start[0])
            em, es = divmod(elapsed, 60)

            # ETA from previously completed files (cross-file)
            eta_str = ''
            if completed_times:
                avg = sum(completed_times) / len(completed_times)
                remaining = len(media) - len(completed_times) - 1
                if remaining > 0:
                    eta_s = int(avg * remaining)
                    eta_m, eta_ss = divmod(eta_s, 60)
                    eta_str = f'remaining files: ~{eta_m}min {eta_ss:02d}s'

            emit({
                'type': 'step_status',
                'step': current_step[0],
                'elapsed': f'{em}min {es:02d}s',
                'progress': current_progress[0],   # 0..1 or None
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

        def _usage_cb(input_tok: int, output_tok: int):
            global usage_global
            usage_global = {
                'input': usage_global['input'] + input_tok,
                'output': usage_global['output'] + output_tok,
                'cost_usd': 0.0,
            }
            usage_global['cost_usd'] = _calc_cost(usage_global['input'], usage_global['output'], cfg)
            emit({'type': 'usage', **usage_global})

        for i, (file_path, media_type) in enumerate(media, 1):
            if stop_event.is_set():
                print("Stopped by user.")
                break

            # Budget guard — check before each file so we stop gracefully mid-batch
            if budget_usd is not None and usage_global['cost_usd'] >= budget_usd:
                msg = (f"Budget limit ${budget_usd:.2f} reached — "
                       f"processed {processed}/{len(media)} files "
                       f"(${usage_global['cost_usd']:.2f} spent)")
                print(f"⚠ {msg}")
                emit({'type': 'error', 'text': f'⛔ {msg}'})
                break

            # Auto-fallback: if the system overheats, downgrade Whisper before next file
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

            out_dir = Path(config['output_dir']) if config.get('output_dir') else file_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            output_path = out_dir / (file_path.stem + '.txt')

            emit({'type': 'progress', 'current': i, 'total': len(media), 'file': file_path.name})
            print(f"[{i}/{len(media)}] {file_path.name}")

            if output_path.exists() and not config.get('overwrite'):
                print(f"  Skipped — {file_path.stem}.txt already exists")
                skipped += 1
                emit({'type': 'skipped', 'file': file_path.name})
                continue

            # Photos make no sense without AI analysis — skip with a clear log entry.
            if media_type == 'photo' and not analyze_images:
                print("  Skipped — photo requires AI analysis (currently disabled)")
                skipped += 1
                emit({'type': 'skipped', 'file': file_path.name})
                continue

            try:
                file_start[0] = time.time()
                current_progress[0] = None
                current_progress[1] = ''
                file_usage_before = dict(usage_global)

                if media_type == 'video':
                    if analyze_images:
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
                        # Whisper-only path — guaranteed by validation that a backend/key exists
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
                    _step_cb(f"analyzing photo with {cfg['ai']['provider']}")
                    print("  Analyzing photo...")
                    desc = describe_photo(
                        str(file_path), provider, config['people'], config['context'],
                        usage_cb=_usage_cb, cfg=cfg, system_prompt=system_prompt,
                    )

                current_step[0] = ''
                completed_times.append(time.time() - file_start[0])
                output_path.write_text(desc + '\n', encoding='utf-8')
                print(f"  Saved: {output_path.name}")
                processed += 1
                first_line = desc.split('\n')[0] if desc else ''
                if ' - ' in first_line:
                    first_line = first_line.split(' - ', 1)[1]
                summary_entries.append((file_path.name, first_line))
                file_tokens = (usage_global['input'] - file_usage_before['input']) + \
                              (usage_global['output'] - file_usage_before['output'])
                file_cost = usage_global['cost_usd'] - file_usage_before['cost_usd']
                emit({'type': 'done_file', 'file': file_path.name, 'output': str(output_path),
                      'preview': first_line, 'file_tokens': file_tokens, 'file_cost': file_cost})

            except InterruptedError:
                current_step[0] = ''
                print("Stopped by user.")
                break

            except Exception as e:
                current_step[0] = ''
                err_msg = str(e)
                print(f"  ERROR: {err_msg}")
                errors += 1
                emit({'type': 'error_file', 'file': file_path.name, 'error': err_msg})

                # Fatal API errors (no credit, bad key) — stop the whole batch,
                # otherwise we'd waste minutes of ffmpeg + Whisper on the next file
                # just to fail again at the Claude call.
                if _is_fatal_api_error(err_msg):
                    print(f"⛔ Stopping batch: {err_msg}")
                    emit({'type': 'error', 'text': f'⛔ {err_msg}'})
                    break

        heartbeat_stop.set()

        input_path = Path(config['path'])
        if config.get('generate_summary') and summary_entries and input_path.is_dir():
            summary_dir = Path(config['output_dir']) if config.get('output_dir') else input_path
            summary_path = summary_dir / '_summary.txt'
            date_str = __import__('datetime').date.today().isoformat()
            _ai_provider = cfg['ai'].get('provider', 'anthropic')
            model_label = cfg['ai'].get(_ai_provider, {}).get('model', 'claude')
            cost_str = f"${usage_global['cost_usd']:.2f}"
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

        print(f"\n--- Done: processed {processed}, skipped {skipped}, errors {errors} ---")
        emit({'type': 'done', 'processed': processed, 'skipped': skipped, 'errors': errors})

    except Exception as e:
        emit({'type': 'error', 'text': str(e)})
        print(f"Fatal error: {e}")
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        sys.stdout = old_stdout
        is_processing = False
        if sleep_block is not None:
            try:
                sleep_block.release()
            except Exception:
                pass


@app.route('/')
def index():
    return render_template('index.html',
                           whisper_available=WHISPER_AVAILABLE,
                           version=VERSION)


@app.route('/start', methods=['POST'])
def start():
    global is_processing, log_buffer, results_buffer, total_files_global, progress_global
    with _start_lock:
        if is_processing:
            return jsonify({'error': 'Processing already in progress'}), 400

    config = request.json or {}
    if not config.get('path'):
        return jsonify({'error': 'Please provide a folder or file path'}), 400

    # Both features default to ON for backward compat with old form payloads
    analyze_images = config.get('analyze_images', True)
    transcribe = config.get('transcribe', False)

    if not analyze_images and not transcribe:
        return jsonify({'error': 'Enable at least one: AI image analysis or speech transcription'}), 400

    if analyze_images:
        saved_cfg = config_loader.load_config()
        has_key = (
            saved_cfg.get('connectors', {}).get('anthropic', {}).get('api_key', '').strip()
            or os.environ.get('ANTHROPIC_API_KEY', '')
            or config.get('api_key', '')
        )
        if not has_key:
            return jsonify({'error': 'Anthropic API key required. Add it in the Connectors tab.'}), 400

    # Reset stanu
    log_buffer.clear()
    results_buffer.clear()
    total_files_global = 0
    progress_global = {}
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    thread = threading.Thread(target=run_processing, args=(config,), daemon=True)
    thread.start()
    return jsonify({'status': 'started'})


@app.route('/stop', methods=['POST'])
def stop():
    stop_event.set()
    return jsonify({'status': 'stopping'})


@app.route('/state')
def state():
    """Returns full current state — used on reconnect."""
    return jsonify({
        'processing': is_processing,
        'log': log_buffer,
        'results': results_buffer,
        'total': total_files_global,
        'progress': progress_global,
        'usage': usage_global,
    })


@app.route('/verify-key', methods=['POST'])
def verify_key():
    """Verifies that the provided API key works — minimal call to Anthropic."""
    key = (request.json or {}).get('api_key', '').strip()
    if not key:
        return jsonify({'ok': False, 'error': 'Empty key'}), 400
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1,
            messages=[{'role': 'user', 'content': 'hi'}],
        )
        return jsonify({
            'ok': True,
            'model': response.model,
            'input_tokens': response.usage.input_tokens,
            'output_tokens': response.usage.output_tokens,
        })
    except Exception as e:
        msg = str(e)
        if 'authentication' in msg.lower() or '401' in msg or 'invalid' in msg.lower():
            return jsonify({'ok': False, 'error': 'Invalid API key'}), 401
        return jsonify({'ok': False, 'error': msg}), 500


@app.route('/connectors', methods=['GET'])
def connectors_get():
    """Returns current connector status (keys masked for security)."""
    cfg = config_loader.load_config()
    conns = cfg.get('connectors', {})

    def _status(key_val: str, env_var: str) -> dict:
        key = key_val.strip() if key_val else ''
        env = os.environ.get(env_var, '').strip()
        active = bool(key or env)
        if key:
            # Show 12 bullets + last 4 chars so user can confirm which key is stored
            masked = '••••••••••••' + key[-4:]
        elif env:
            # Env key: we don't have the raw value, just show bullets
            masked = '•' * 16
        else:
            masked = ''
        return {'connected': active, 'masked': masked, 'from_env': bool(env and not key)}

    return jsonify({
        'anthropic': _status(conns.get('anthropic', {}).get('api_key', ''), 'ANTHROPIC_API_KEY'),
        'openai':    _status(conns.get('openai',    {}).get('api_key', ''), 'OPENAI_API_KEY'),
    })


@app.route('/connectors/save', methods=['POST'])
def connectors_save():
    """Saves connector API keys to config.json."""
    body = request.json or {}
    provider = body.get('provider', '').strip()
    key = body.get('api_key', '').strip()
    if provider not in ('anthropic', 'openai'):
        return jsonify({'error': 'Unknown provider'}), 400

    cfg = config_loader.load_config()
    if 'connectors' not in cfg:
        cfg['connectors'] = {}
    if provider not in cfg['connectors']:
        cfg['connectors'][provider] = {}
    cfg['connectors'][provider]['api_key'] = key
    config_loader.save_config(cfg)
    return jsonify({'ok': True})


@app.route('/connectors/verify', methods=['POST'])
def connectors_verify():
    """Verifies a connector API key (lightweight test call).
    If api_key is empty the endpoint falls back to the stored key from config/env
    so users can Verify without re-pasting a key they already saved.
    """
    body = request.json or {}
    provider = body.get('provider', '').strip()
    key = body.get('api_key', '').strip()

    if not key:
        # Try to load stored key
        _env_map = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY'}
        cfg = config_loader.load_config()
        key = (
            cfg.get('connectors', {}).get(provider, {}).get('api_key', '').strip()
            or os.environ.get(_env_map.get(provider, ''), '').strip()
        )
        if not key:
            return jsonify({'ok': False, 'error': 'No key configured'}), 400

    if provider == 'anthropic':
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            response = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=1,
                messages=[{'role': 'user', 'content': 'hi'}],
            )
            return jsonify({'ok': True, 'model': response.model})
        except Exception as e:
            msg = str(e)
            if 'authentication' in msg.lower() or '401' in msg or 'invalid' in msg.lower():
                return jsonify({'ok': False, 'error': 'Invalid API key'}), 401
            return jsonify({'ok': False, 'error': msg}), 500

    if provider == 'openai':
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key)
            # Cheapest possible call: list available models
            models = client.models.list()
            whisper_avail = any('whisper' in m.id for m in models.data)
            return jsonify({'ok': True, 'whisper': whisper_avail})
        except Exception as e:
            msg = str(e)
            if 'authentication' in msg.lower() or '401' in msg or 'invalid' in msg.lower():
                return jsonify({'ok': False, 'error': 'Invalid API key'}), 401
            return jsonify({'ok': False, 'error': msg}), 500

    return jsonify({'error': 'Unknown provider'}), 400


@app.route('/folder-info')
def folder_info():
    """Returns info about a given path: how many files, what types."""
    path = (request.args.get('path') or '').strip()
    if not path:
        return jsonify({'error': 'No path provided'}), 400
    try:
        p = Path(path)
        if not p.exists():
            return jsonify({'error': 'Path does not exist'}), 404

        media = find_media([path])
        videos = sum(1 for _, t in media if t == 'video')
        photos = sum(1 for _, t in media if t == 'photo')

        # File list — max 30 for preview
        files = []
        for f, t in media[:30]:
            try:
                size = f.stat().st_size
                size_str = f"{size/(1024**3):.1f} GB" if size >= 1024**3 else f"{size/(1024**2):.0f} MB"
            except OSError:
                size_str = '?'
            files.append({'name': f.name, 'type': t, 'size': size_str})

        return jsonify({
            'is_file': p.is_file(),
            'is_dir': p.is_dir(),
            'name': p.name or str(p),
            'count': len(media),
            'videos': videos,
            'photos': photos,
            'files': files,
            'has_more': len(media) > 30,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stream')
def stream():
    def generate():
        while True:
            try:
                msg = log_queue.get(timeout=1)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get('type') == 'done':
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


def _pick_path(kind: str) -> str:
    """Opens a native macOS folder/file picker via osascript.
    kind: 'folder' | 'file'. Returns the picked path or '' if cancelled.
    """
    script = {
        'folder': 'POSIX path of (choose folder with prompt "Select a folder with recordings")',
        'file':   'POSIX path of (choose file with prompt "Select a video or photo file")',
    }[kind]
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=300)
        return r.stdout.strip() if r.returncode == 0 else ''
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ''


@app.route('/pick-folder')
def pick_folder():
    return jsonify({'path': _pick_path('folder')})


@app.route('/pick-file')
def pick_file():
    return jsonify({'path': _pick_path('file')})


@app.route('/status')
def status():
    return jsonify({'processing': is_processing})


@app.route('/api-status')
def api_status():
    """Tells the frontend whether an API key is set via env (so Start can stay enabled
    even with an empty form field)."""
    return jsonify({'has_env_key': bool(os.environ.get('ANTHROPIC_API_KEY'))})


@app.route('/open-file', methods=['POST'])
def open_file():
    path = (request.json or {}).get('path', '').strip()
    if not path or not os.path.exists(path):
        return jsonify({'error': 'path not found'}), 400
    # Only allow opening .txt output files — prevents arbitrary file access via the API.
    if not path.endswith('.txt'):
        return jsonify({'error': 'only .txt output files can be opened'}), 403
    try:
        subprocess.Popen(['open', path])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/metrics')
def metrics():
    """Current system status — CPU%, RAM%, load, thermal."""
    return jsonify(get_thermal_state())


@app.route('/sysinfo')
def sysinfo():
    """Static system info — hardware capabilities, Whisper backend, ffmpeg availability."""
    ffmpeg_ok = False
    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=3)
        ffmpeg_ok = (r.returncode == 0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    chip = platform.machine()   # 'arm64' on Apple Silicon, 'x86_64' on Intel/AMD
    mem_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)

    whisper_label = {
        'mlx': 'Neural Engine (MLX)',
        'faster-whisper': 'CPU (int8)',
        None: 'not installed',
    }.get(WHISPER_BACKEND, WHISPER_BACKEND)

    # Connector key status
    cfg = config_loader.load_config()
    conns = cfg.get('connectors', {})
    anthropic_key = conns.get('anthropic', {}).get('api_key', '').strip() or os.environ.get('ANTHROPIC_API_KEY', '')
    openai_key    = conns.get('openai',    {}).get('api_key', '').strip() or os.environ.get('OPENAI_API_KEY', '')

    return jsonify({
        'platform': platform.system(),         # Darwin / Windows / Linux
        'chip': chip,
        'apple_silicon': IS_APPLE_SILICON,
        'ram_gb': mem_gb,
        'ffmpeg': ffmpeg_ok,
        'whisper_backend': WHISPER_BACKEND,    # 'mlx' | 'faster-whisper' | null
        'whisper_label': whisper_label,
        'mlx_available': MLX_WHISPER_AVAILABLE,
        'faster_whisper_available': FASTER_WHISPER_AVAILABLE,
        'anthropic_connected': bool(anthropic_key),
        'openai_connected': bool(openai_key),
    })


@app.route('/version')
def version():
    return jsonify({'version': VERSION})


@app.route('/config', methods=['GET'])
def config_get():
    return jsonify({
        'version': VERSION,
        'config': config_loader.load_config(),
        'defaults': config_loader.load_defaults(),
        'prompt': config_loader.load_system_prompt(),
        'prompt_presets': config_loader.list_prompt_presets(),
    })


@app.route('/config', methods=['POST'])
def config_save():
    body = request.json or {}
    if 'config' in body:
        try:
            config_loader.save_config(body['config'])
        except Exception as e:
            return jsonify({'error': f'Failed to save config: {e}'}), 400
    if 'prompt' in body:
        try:
            config_loader.save_system_prompt(body['prompt'])
        except Exception as e:
            return jsonify({'error': f'Failed to save prompt: {e}'}), 400
    return jsonify({'ok': True})


@app.route('/config/reset', methods=['POST'])
def config_reset():
    body = request.json or {}
    what = body.get('what', 'all')   # 'all' | 'config' | 'prompt'
    lang = body.get('lang', config_loader.DEFAULT_PROMPT_LANG)
    if what in ('all', 'config'):
        config_loader.reset_config()
    if what in ('all', 'prompt'):
        config_loader.reset_system_prompt(lang)
    return jsonify({
        'ok': True,
        'config': config_loader.load_config(),
        'prompt': config_loader.load_system_prompt(),
    })


def _preflight_startup() -> bool:
    """Runs pre-flight checks before the server starts.
    Prints each check live as it runs (label → pending → result).
    Returns False if a fatal error was found and the user chose not to continue.
    """
    _use_colour = sys.stdout.isatty()
    GREEN  = '\033[32m' if _use_colour else ''
    YELLOW = '\033[33m' if _use_colour else ''
    RED    = '\033[31m' if _use_colour else ''
    RESET  = '\033[0m'  if _use_colour else ''
    DIM    = '\033[2m'  if _use_colour else ''
    CYAN   = '\033[36m' if _use_colour else ''

    COL_W = 9   # label column (longest: "Whisper")
    VAL_W = 42  # value column

    fatal_reasons = []
    warnings = []

    def _info(label: str, value: str, colour: str = ''):
        """Print a plain info row (no status tick)."""
        print(f'  {DIM}{label:<{COL_W}}{RESET}{colour}{value}{RESET}')

    def _begin(label: str):
        """Print 'label   checking...' and hold the cursor on that line."""
        sys.stdout.write(f'  {DIM}{label:<{COL_W}}{RESET}checking...\r')
        sys.stdout.flush()

    def _done(label: str, value: str, status: str, colour: str):
        """Overwrite the pending line with the final result."""
        line = f'  {DIM}{label:<{COL_W}}{RESET}{colour}{value:<{VAL_W}}{RESET}  {colour}{status}{RESET}'
        sys.stdout.write(f'\r{line}\033[K\n')
        sys.stdout.flush()

    def _sep():
        print(f'  {DIM}{"─" * 52}{RESET}')

    # ── Machine info (instant — no _begin/_done needed) ────
    chip = ''
    ncpu = os.cpu_count() or 0
    mac_ver = ''
    hw_label = platform.system()

    if IS_MACOS:
        try:
            r = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                               capture_output=True, text=True, timeout=2)
            chip = r.stdout.strip()              # e.g. "Apple M3 Pro"
        except Exception:
            chip = platform.processor() or ''
        try:
            r = subprocess.run(['sysctl', '-n', 'hw.logicalcpu'],
                               capture_output=True, text=True, timeout=2)
            ncpu = int(r.stdout.strip())
        except Exception:
            pass
        mac_ver = platform.mac_ver()[0]          # e.g. "15.2.0"
        hw_label = 'Mac'
    else:
        chip = platform.processor() or platform.machine()

    cores_str = f'{ncpu} cores' if ncpu else ''
    os_str    = f'macOS {mac_ver}' if mac_ver else platform.system()
    machine_parts = [p for p in [chip, cores_str] if p]
    machine_line  = '  ·  '.join(machine_parts)

    print()
    _info(hw_label,   machine_line, CYAN)
    _info('OS',       os_str,       DIM)

    mem = psutil.virtual_memory()
    mem_gb       = mem.total / (1024 ** 3)
    mem_used_pct = mem.percent
    mem_colour   = RED if mem_used_pct > 90 else YELLOW if mem_used_pct > 80 else DIM
    _info('RAM', f'{mem_gb:.1f} GB  ({mem_used_pct:.0f}% used)', mem_colour)

    _sep()

    # ── Python version ──────────────────────────────────────
    _begin('Python')
    py = sys.version_info
    py_str = f'{py.major}.{py.minor}.{py.micro}'
    if py >= (3, 9):
        _done('Python', py_str, '✓', GREEN)
    else:
        _done('Python', py_str, '✗  requires 3.9+', RED)
        fatal_reasons.append('Python 3.9+ required (you have ' + py_str + ')')

    # ── ffmpeg ──────────────────────────────────────────────
    _begin('ffmpeg')
    ffmpeg_ver = None
    try:
        r = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            first = r.stdout.split('\n')[0]
            ffmpeg_ver = first.split('version ')[1].split(' ')[0] if 'version ' in first else 'found'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if ffmpeg_ver:
        _done('ffmpeg', ffmpeg_ver, '✓', GREEN)
    else:
        _done('ffmpeg', 'not found', '✗  required', RED)
        fatal_reasons.append(
            'ffmpeg not found — install it:\n'
            '     brew install ffmpeg'
        )

    # ── Whisper backend ─────────────────────────────────────
    _begin('Whisper')
    whisper_display = {
        'mlx':            'mlx-whisper  (Neural Engine)',
        'faster-whisper': 'faster-whisper  (CPU)',
        None:             'not installed',
    }.get(WHISPER_BACKEND, WHISPER_BACKEND)
    whisper_colour = GREEN if WHISPER_AVAILABLE else YELLOW
    whisper_status = '✓' if WHISPER_AVAILABLE else '—  optional'
    _done('Whisper', whisper_display, whisper_status, whisper_colour)
    if not WHISPER_AVAILABLE:
        warnings.append(
            'Whisper not installed — speech transcription will be unavailable.\n'
            f'     {"pip3 install mlx-whisper" if IS_APPLE_SILICON else "pip3 install faster-whisper"}'
        )

    # ── Config ──────────────────────────────────────────────
    _begin('Config')
    cfg = None
    try:
        cfg = config_loader.load_config()
        _done('Config', 'config.json', '✓', GREEN)
    except Exception as e:
        _done('Config', 'error — using defaults', f'⚠  {e}', YELLOW)
        warnings.append(f'config.json could not be loaded ({e}). Running with defaults.')

    # ── Settings from config ────────────────────────────────
    if cfg:
        _sep()
        # Whisper model + fallback chain
        if WHISPER_AVAILABLE:
            w_default  = cfg.get('whisper', {}).get('default_model', '?')
            w_tiers    = cfg.get('whisper', {}).get('fallback_tiers', [])
            tiers_str  = ' → '.join(w_tiers) if w_tiers else '—'
            _info('Model',    w_default,  GREEN if WHISPER_AVAILABLE else DIM)
            _info('Fallback', tiers_str,  DIM)
        # Claude model
        ai_provider = cfg.get('ai', {}).get('provider', 'anthropic')
        ai_model    = cfg.get('ai', {}).get(ai_provider, {}).get('model', '?')
        _info('Claude', ai_model, DIM)

    print()

    # ── Warnings ────────────────────────────────────────────
    for w in warnings:
        print(f'{YELLOW}⚠  {w}{RESET}')
    if warnings:
        print()

    # ── Fatal errors ────────────────────────────────────────
    if fatal_reasons:
        for reason in fatal_reasons:
            print(f'{RED}✗  {reason}{RESET}')
        print()
        try:
            ans = input(f'{YELLOW}Start anyway? [y/N]{RESET} ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ''
        print()
        return ans == 'y'

    return True


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5555))

    if not _preflight_startup():
        sys.exit(1)

    print(f'  Open in browser: http://localhost:{port}\n')
    app.run(host='127.0.0.1', debug=False, port=port, threaded=True)
