"""
Loads and saves configuration + system prompt.

Philosophy:
- config.default.json — versioned in git, source of truth for "Restore defaults"
- config.json        — local, user-editable, gitignored
- prompts/system.md  — extracted SYSTEM_PROMPT, editable as markdown
- VERSION            — plain text file with app version

If config.json does not exist, it is generated from defaults on first call.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / 'config.json'
DEFAULTS_PATH = ROOT / 'config.default.json'
PROMPT_PATH = ROOT / 'prompts' / 'system.md'
PROMPTS_DIR = ROOT / 'prompts'
VERSION_PATH = ROOT / 'VERSION'

# Supported preset languages — must match files prompts/system.<lang>.default.md
PROMPT_LANGUAGES = ('pl', 'en')
DEFAULT_PROMPT_LANG = 'pl'  # original language of the project author


def _preset_path(lang: str) -> Path:
    """Returns path to a prompt preset file for the given language."""
    return PROMPTS_DIR / f'system.{lang}.default.md'


def get_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding='utf-8').strip()
    except OSError:
        return 'unknown'


def load_defaults() -> dict:
    """Returns the factory settings dict (from config.default.json)."""
    return json.loads(DEFAULTS_PATH.read_text(encoding='utf-8'))


def load_default_prompt(lang: str = DEFAULT_PROMPT_LANG) -> str:
    """Returns the factory prompt preset for the given language.
    Falls back to current prompts/system.md if no preset file exists."""
    p = _preset_path(lang)
    if p.exists():
        return p.read_text(encoding='utf-8')
    # Backward compat: legacy single-default file
    legacy = PROMPTS_DIR / 'system.default.md'
    if legacy.exists():
        return legacy.read_text(encoding='utf-8')
    return PROMPT_PATH.read_text(encoding='utf-8')


def list_prompt_presets() -> list:
    """Returns list of available preset language codes."""
    return [lang for lang in PROMPT_LANGUAGES if _preset_path(lang).exists()]


def load_config() -> dict:
    """Loads config.json. Generates it from defaults if it does not exist.
    Migrates legacy schemas (e.g. flat 'claude' section → 'ai.anthropic')."""
    if not CONFIG_PATH.exists():
        defaults = load_defaults()
        save_config(defaults)
        return defaults
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        migrated = _migrate_legacy_schema(config)
        # Deep-merge with defaults so newly added fields don't crash the app
        defaults = load_defaults()
        merged = _deep_merge(defaults, migrated)
        # Persist migration so user's file matches what the app reads
        if migrated is not config:
            save_config(merged)
        return merged
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠ Failed to load config.json: {e} — using defaults")
        return load_defaults()


def _migrate_legacy_schema(cfg: dict) -> dict:
    """Migrates pre-v0.2 config (top-level 'claude' section) to new 'ai.anthropic' shape.
    Returns a NEW dict if migration happened, otherwise the original cfg object."""
    if 'claude' in cfg and 'ai' not in cfg:
        print("⚠ Migrating legacy config: claude → ai.anthropic")
        new = {k: v for k, v in cfg.items() if k != 'claude'}
        new['ai'] = {'provider': 'anthropic', 'anthropic': cfg['claude']}
        return new
    return cfg


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )


def load_system_prompt() -> str:
    """Loads the current system prompt from prompts/system.md.
    If missing, copies it from the default-language preset."""
    if not PROMPT_PATH.exists():
        default_text = load_default_prompt(DEFAULT_PROMPT_LANG)
        PROMPT_PATH.write_text(default_text, encoding='utf-8')
    try:
        return PROMPT_PATH.read_text(encoding='utf-8')
    except OSError:
        return ''


def save_system_prompt(text: str) -> None:
    PROMPT_PATH.write_text(text, encoding='utf-8')


def reset_config() -> dict:
    """Restores config.json to factory settings."""
    defaults = load_defaults()
    save_config(defaults)
    return defaults


def reset_system_prompt(lang: str = DEFAULT_PROMPT_LANG) -> str:
    """Restores the system prompt to a factory preset (PL or EN)."""
    preset = load_default_prompt(lang)
    PROMPT_PATH.write_text(preset, encoding='utf-8')
    return preset


def set_output_language(lang: str) -> dict:
    """Stores the output language independently from UI language."""
    lang = str(lang or '').strip().lower()
    if lang not in PROMPT_LANGUAGES:
        lang = DEFAULT_PROMPT_LANG
    cfg = load_config()
    cfg.setdefault('defaults', {})['output_language'] = lang
    save_config(cfg)
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively overrides base values with override. Used for config migration."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
