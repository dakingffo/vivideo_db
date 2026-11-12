"""Writable runtime locations, independent of the package installation directory."""
import os
from pathlib import Path

from platformdirs import user_data_path


def checkout_root():
    root=Path(__file__).resolve().parents[2]
    if (root/'pyproject.toml').is_file() and (root/'python'/'vivideo').resolve()==Path(__file__).resolve().parent:
        return root
    return None


def data_root():
    configured=os.getenv('VIVIDEO_DATA_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    source=checkout_root()
    return (source/'data' if source else user_data_path('vivideo',appauthor=False)).resolve()


def bundled_tool(name):
    source=checkout_root()
    path=source/'.tools'/'media'/'bin'/name if source else None
    return str(path) if path and path.is_file() else None
