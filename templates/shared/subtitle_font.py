#!/usr/bin/env python3
"""Select a CJK-capable ASS subtitle font without bundling font files."""
from __future__ import annotations

import os
import platform


SUBTITLE_FONT_ENV = "LECTURECAST_SUBTITLE_FONT"


def subtitle_font_name(*, system: str | None = None) -> str:
    """Return an ASS-safe platform default, or the user's explicit override."""
    configured = os.environ.get(SUBTITLE_FONT_ENV, "").strip()
    if configured:
        if any(character in configured for character in (",", "\r", "\n")):
            raise ValueError(
                f"{SUBTITLE_FONT_ENV} must be a single ASS font family name"
            )
        return configured

    current_system = system or platform.system()
    if current_system == "Darwin":
        # libass resolves the system copy without fontsdir on supported macOS.
        return "Arial Unicode MS"
    if current_system == "Windows":
        return "Microsoft YaHei"
    return "Noto Sans CJK SC"
