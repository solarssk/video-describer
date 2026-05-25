"""
Batch processing logic — run_processing() and its helpers.

Kept separate from web_app.py so the Flask layer stays thin.
web_app.py calls run_processing() in a background thread and passes in
the emit callable, logger, stop_event, and the shared usage dict.
"""

import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    try:
        proc = subprocess.Popen(
            ['caffeinate', '-d', '-i', '-m'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("🔒 Caffeinate active — Mac will not sleep during processing")
        return _SleepBlock(proc)
    except FileNotFoundError:
        return _SleepBlock()


# ── Batch state persistence ───────────────────────────────────────────────────

def _save_batch_state(config: dict, next_index: int, total: int,
                      processed: int, skipped: int, errors: int,
                      usage: dict, next_filename=None) -> None:
    import copy
    import datetime
    safe_config = copy.deepcopy(config)
    safe_config.pop('api_key', None)
    for section in ('ai', 'connectors'):
        for provider_cfg in safe_config.get(section, {}).values():
            if isinstance(provider_cfg, dict):
                provider_cfg.pop('api_key', None)
    state = {
        'config': safe_config,
        'next_index': next_index,
        'next_filepath': next_filename,
        'total': total,
        'processed': processed,
        'skipped': skipped,
        'errors': errors,
        'cost_usd': usage['cost_usd'],
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    try:
        tmp = BATCH_STATE_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(BATCH_STATE_PATH)
    except OSError as e:
        print(f"⚠ Warning: could not save batch state: {e}")


def _clear_batch_state() -> None:
    try:
        BATCH_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ── Cost + error helpers ──────────────────────────────────────────────────────

def _calc_cost(input_tok: int, output_tok: int, cfg: dict = None) -> float:
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
    lower = (error_msg or '').lower()
    return any(p in lower for p in _FATAL_API_PATTERNS)


def _preflight_api(provider) -> tuple:
    return provider.verify()


# ── QueueLogger ───────────────────────────────────────────────────────────────

class QueueLogger:
    """Redirects print() to UI (emit_fn) and logger.
    Terminal output is handled by logger's console handler — no double write.
    """
    def __init__(self, logger, emit_fn):
        self._logger = logger
        self._emit = emit_fn

    def write(self, text: str):
        if text and text.strip():
            t = text.rstrip()
            self._logger.info(t)
            self._emit({'type': 'warn', 'text': t} if t.startswith('⚠') else {'type': 'log', 'text': t})

    def flush(self):
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

    old_stdout = sys.stdout
    sys.stdout = QueueLogger(logger, emit_fn)

    sleep_block = _prevent_sleep()

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
            print("✓ API OK")
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

        resume_from = int(config.get('resume_from_index', 0) or 0)
        resume_next_filepath = config.get('resume_next_filepath')
        if resume_next_filepath and resume_from > 0:
            if resume_from >= len(media) or str(media[resume_from][0]) != resume_next_filepath:
                for _idx, (_fp, _) in enumerate(media):
                    if str(_fp) == resume_next_filepath or _fp.name == Path(resume_next_filepath).name:
                        resume_from = _idx
                        break
                else:
                    resume_from = 0
        total_media = len(media)
        pre_resume_media = media[:resume_from]
        if resume_from > 0:
            media = media[resume_from:]
            print(f"Resuming from file {resume_from + 1}/{total_media}")

        videos = sum(1 for _, t in media if t == 'video')
        photos = sum(1 for _, t in media if t == 'photo')
        print(f"Found: {videos} video, {photos} photos.")
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

        processed = int(config.get('resume_processed', 0) or 0)
        skipped   = int(config.get('resume_skipped',   0) or 0)
        errors    = int(config.get('resume_errors',    0) or 0)

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
            _out_base = Path(config['output_dir']) if config.get('output_dir') else None
            for _fp, _ in pre_resume_media:
                _txt = find_existing_output(_fp, _out_base)
                if _txt is None:
                    continue
                try:
                    _line = _txt.read_text(encoding='utf-8').split('\n')[0]
                    if ' - ' in _line:
                        _line = _line.split(' - ', 1)[1]
                    summary_entries.append((_fp.name, _line))
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

        def _usage_cb(input_tok: int, output_tok: int):
            usage['input'] += input_tok
            usage['output'] += output_tok
            usage['cost_usd'] = resume_cost_offset + _calc_cost(
                usage['input'], usage['output'], cfg)
            call_cost = _calc_cost(input_tok, output_tok, cfg)
            logger.debug(f'  ↳ {input_tok:,} in / {output_tok:,} out tok — ${call_cost:.4f}')
            emit_fn({'type': 'usage', **usage})

        out_dir = Path(config['output_dir']) if config.get('output_dir') else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

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
            emit_fn({'type': 'progress', 'current': abs_index, 'total': total_media, 'file': file_path.name})
            print(f"[{abs_index}/{total_media}] {file_path.name}")

            existing = find_existing_output(file_path, out_dir)
            if existing and not config.get('overwrite'):
                output_path = existing  # honour legacy path if that's what exists
                print(f"  Skipped — {existing.name} already exists")
                logger.debug(f'[skip:{file_path.name}]  .txt exists')
                skipped += 1
                emit_fn({'type': 'skipped', 'file': file_path.name})
                _save_batch_state(config, abs_index, total_media, processed, skipped, errors, usage,
                                  next_filename=str(media[i][0]) if i < len(media) else None)
                if config.get('generate_summary'):
                    try:
                        _line = output_path.read_text(encoding='utf-8').split('\n')[0]
                        if ' - ' in _line:
                            _line = _line.split(' - ', 1)[1]
                        summary_entries.append((file_path.name, _line))
                    except OSError:
                        pass
                continue

            if media_type == 'photo' and not analyze_images:
                print("  Skipped — photo requires AI analysis (currently disabled)")
                skipped += 1
                emit_fn({'type': 'skipped', 'file': file_path.name})
                _save_batch_state(config, abs_index, total_media, processed, skipped, errors, usage,
                                  next_filename=str(media[i][0]) if i < len(media) else None)
                continue

            try:
                file_start[0] = time.time()
                current_progress[0] = None
                current_progress[1] = ''
                file_usage_before = dict(usage)

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
                processed += 1
                first_line = desc.split('\n')[0] if desc else ''
                if ' - ' in first_line:
                    first_line = first_line.split(' - ', 1)[1]
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
                _save_batch_state(config, abs_index, total_media, processed, skipped, errors, usage,
                                  next_filename=str(media[i][0]) if i < len(media) else None)

            except InterruptedError:
                current_step[0] = ''
                print("Stopped by user.")
                break

            except Exception as e:
                current_step[0] = ''
                err_msg = str(e)
                print(f"  ERROR: {err_msg}")
                errors += 1
                emit_fn({'type': 'error_file', 'file': file_path.name, 'error': err_msg})
                # abs_index - 1 so resume retries the failed file on next run
                _save_batch_state(config, abs_index - 1, total_media, processed, skipped, errors, usage,
                                  next_filename=str(file_path))
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
        emit_fn({'type': 'done', 'processed': processed, 'skipped': skipped, 'errors': errors})

    except Exception as e:
        emit_fn({'type': 'error', 'text': str(e)})
        print(f"Fatal error: {e}")
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        sys.stdout = old_stdout
        if sleep_block is not None:
            try:
                sleep_block.release()
            except Exception:
                pass
