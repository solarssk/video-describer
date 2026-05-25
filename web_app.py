#!/usr/bin/env python3
"""
Web interface for Video Describer.
Run:  python3 web_app.py
Open: http://localhost:5555
"""

import json
import logging
import logging.handlers
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import psutil
from flask import Flask, Response, jsonify, render_template, request

import config_loader
from processor import (
    BATCH_STATE_PATH,
    _clear_batch_state,
    get_thermal_state,
    run_processing as _run_processing,
)

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
    find_media,
)

app = Flask(__name__)

# ── Log folder + daily rotation ──────────────────────────────────────────────
# logs/app.log — active file; rotated copies get a .YYYY-MM-DD suffix.
# 30 days of history kept. Terminal also receives DEBUG messages (tokens, cost)
# that never appear in the UI.
_LOG_DIR = Path(__file__).parent / 'logs'
try:
    _LOG_DIR.mkdir(exist_ok=True)
except OSError:
    pass
_LOG_PATH = _LOG_DIR / 'app.log'

_LOG_FMT = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

app_logger = logging.getLogger('video_describer')
app_logger.setLevel(logging.DEBUG)
app_logger.propagate = False

try:
    _file_handler = logging.handlers.TimedRotatingFileHandler(
        _LOG_PATH, when='midnight', backupCount=30, encoding='utf-8',
    )
    _file_handler.setFormatter(_LOG_FMT)
    app_logger.addHandler(_file_handler)
except OSError:
    pass  # non-writable directory — skip file logging, app still starts

_console_handler = logging.StreamHandler(sys.__stdout__)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
app_logger.addHandler(_console_handler)


VERSION = config_loader.get_version()
_PICKER_TIMEOUT_SECONDS = 300
_PICKER_HELPER_SOURCE = Path(__file__).parent / 'tools' / 'macos_path_picker.swift'
_PICKER_HELPER_BINARY = Path(tempfile.gettempdir()) / 'video-describer' / 'macos_path_picker'
_picker_helper_lock = threading.Lock()
_last_picker_dir = ''
_last_picker_dir_lock = threading.Lock()


def _applescript_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _picker_default_dir() -> str:
    with _last_picker_dir_lock:
        default_dir = _last_picker_dir
    if not default_dir:
        return ''
    try:
        if os.path.isdir(default_dir) and os.access(default_dir, os.R_OK | os.X_OK):
            return default_dir
        return ''
    except OSError:
        return ''


def _picker_helper_path() -> str:
    if not IS_MACOS or not _PICKER_HELPER_SOURCE.exists():
        return ''
    swiftc = shutil.which('swiftc')
    if not swiftc:
        return ''
    xcrun = shutil.which('xcrun')
    compiler = [xcrun, 'swiftc'] if xcrun else [swiftc]

    try:
        source_mtime = _PICKER_HELPER_SOURCE.stat().st_mtime
        if _PICKER_HELPER_BINARY.exists() and _PICKER_HELPER_BINARY.stat().st_mtime >= source_mtime:
            return str(_PICKER_HELPER_BINARY)
    except OSError:
        return ''

    with _picker_helper_lock:
        try:
            source_mtime = _PICKER_HELPER_SOURCE.stat().st_mtime
            if _PICKER_HELPER_BINARY.exists() and _PICKER_HELPER_BINARY.stat().st_mtime >= source_mtime:
                return str(_PICKER_HELPER_BINARY)
            _PICKER_HELPER_BINARY.parent.mkdir(parents=True, exist_ok=True)
            build_env = os.environ.copy()
            build_env.setdefault('CLANG_MODULE_CACHE_PATH', str(_PICKER_HELPER_BINARY.parent / 'clang-cache'))
            build_env.setdefault('SWIFT_MODULE_CACHE_PATH', str(_PICKER_HELPER_BINARY.parent / 'swift-cache'))
            r = subprocess.run(
                compiler + [str(_PICKER_HELPER_SOURCE), '-O', '-o', str(_PICKER_HELPER_BINARY)],
                capture_output=True, text=True, timeout=60, env=build_env,
            )
            if r.returncode != 0:
                app_logger.warning('Swift picker helper build failed: %s', (r.stderr or '').strip())
                return ''
            return str(_PICKER_HELPER_BINARY)
        except (OSError, subprocess.TimeoutExpired) as e:
            app_logger.warning('Swift picker helper unavailable: %s', e)
            return ''


def _remember_picked_dir(kind: str, picked_path: str) -> None:
    global _last_picker_dir

    next_default_dir = picked_path if kind == 'folder' else os.path.dirname(picked_path)
    if next_default_dir and os.path.isdir(next_default_dir):
        with _last_picker_dir_lock:
            _last_picker_dir = next_default_dir


def _run_swift_picker(kind: str, default_dir: str):
    helper = _picker_helper_path()
    if not helper:
        return None

    args = [helper, kind]
    if default_dir:
        args.append(default_dir)

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=_PICKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {
            'ok': False,
            'cancelled': False,
            'path': '',
            'code': 'timeout',
            'error': 'Picker timed out. Try again.',
        }
    except (FileNotFoundError, OSError):
        return None

    if r.returncode == 0:
        picked_path = r.stdout.strip()
        return {'ok': True, 'path': picked_path} if picked_path else {'ok': False, 'cancelled': True, 'path': ''}
    if r.returncode == 2:
        return {'ok': False, 'cancelled': True, 'path': ''}

    return {
        'ok': False,
        'cancelled': False,
        'path': '',
        'code': 'picker_helper_error',
        'error': (r.stderr or '').strip() or 'Picker failed.',
    }


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


@app.route('/')
def index():
    return render_template('index.html',
                           whisper_available=WHISPER_AVAILABLE,
                           version=VERSION)


@app.route('/batch-state', methods=['GET'])
def batch_state():
    if BATCH_STATE_PATH.exists():
        try:
            return jsonify(json.loads(BATCH_STATE_PATH.read_text(encoding='utf-8')))
        except (OSError, json.JSONDecodeError):
            pass
    return jsonify(None)


@app.route('/batch-state/discard', methods=['POST'])
def batch_state_discard():
    _clear_batch_state()
    return jsonify({'ok': True})


@app.route('/start', methods=['POST'])
def start():
    global is_processing, log_buffer, results_buffer, total_files_global, progress_global
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
        )
        if not has_key:
            return jsonify({'error': 'Anthropic API key required. Add it in the Connectors tab.'}), 400

    with _start_lock:
        if is_processing:
            return jsonify({'error': 'Processing already in progress'}), 400

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
        is_processing = True

    def _run_batch(cfg):
        global is_processing
        try:
            _run_processing(cfg, emit, app_logger, stop_event, usage_global)
        finally:
            with _start_lock:
                is_processing = False

    try:
        thread = threading.Thread(target=_run_batch, args=(config,), daemon=True)
        thread.start()
    except Exception:
        with _start_lock:
            is_processing = False
        raise
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


def _pick_path(kind: str) -> dict:
    """Opens a native macOS folder/file picker."""
    default_dir = _picker_default_dir()

    helper_result = _run_swift_picker(kind, default_dir)
    if helper_result is not None:
        if helper_result.get('ok') and helper_result.get('path'):
            _remember_picked_dir(kind, helper_result['path'])
        return helper_result

    prompt = {
        'folder': 'Select a folder with recordings',
        'file': 'Select a video or photo file',
    }[kind]

    picker_cmd = f'choose {kind} with prompt {_applescript_quote(prompt)}'
    if default_dir:
        picker_cmd += f' default location POSIX file {_applescript_quote(default_dir)}'

    script = '\n'.join([
        'activate',
        f'POSIX path of ({picker_cmd})',
    ])

    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=_PICKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {
            'ok': False,
            'cancelled': False,
            'path': '',
            'code': 'timeout',
            'error': 'Picker timed out. Try again.',
        }
    except FileNotFoundError:
        return {
            'ok': False,
            'cancelled': False,
            'path': '',
            'code': 'missing_osascript',
            'error': 'macOS osascript command was not found.',
        }
    except OSError as e:
        return {
            'ok': False,
            'cancelled': False,
            'path': '',
            'code': 'system_error',
            'error': str(e),
        }

    if r.returncode != 0:
        stderr = (r.stderr or '').strip()
        if 'User canceled' in stderr or '-128' in stderr:
            return {'ok': False, 'cancelled': True, 'path': ''}
        return {
            'ok': False,
            'cancelled': False,
            'path': '',
            'code': 'osascript_error',
            'error': stderr or 'Picker failed.',
        }

    picked_path = r.stdout.strip()
    if not picked_path:
        return {'ok': False, 'cancelled': True, 'path': ''}

    _remember_picked_dir(kind, picked_path)

    return {'ok': True, 'path': picked_path}


@app.route('/pick-folder')
def pick_folder():
    return jsonify(_pick_path('folder'))


@app.route('/pick-file')
def pick_file():
    return jsonify(_pick_path('file'))


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
        config_loader.set_output_language(lang)
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

    if IS_MACOS:
        _picker_helper_path()

    print(f'  Open in browser: http://localhost:{port}\n')
    app_logger.info(f'=== video-describer started · port {port} · logs: {_LOG_DIR} ===')
    from waitress import serve
    serve(app, host='127.0.0.1', port=port, threads=4, channel_timeout=3600)
