"""Stable resource and writable-data locations for source and packaged runs."""
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', SOURCE_ROOT))
FROZEN = bool(getattr(sys, 'frozen', False))
APP_ROOT = Path(sys.executable).resolve().parent if FROZEN else SOURCE_ROOT
