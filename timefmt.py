def fmt_ts(seconds: float) -> str:
    """Format seconds as MM:SS (< 1 h) or HH:MM:SS (>= 1 h)."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
