# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate the selected shared CLI core and this overlay's actual domain."""
import importlib
import os
from pathlib import Path
import sys


def _bundle_root():
    root = Path(__file__).resolve().parent.parent
    return root if (root / 'bundle.json').is_file() else None


def core_directory():
    """The shared scheduler stays external; never substitute a private core."""
    configured = os.environ.get('TOON_WORKFLOW_CLI_CORE_ROOT')
    path = Path(configured) if configured else Path(__file__).resolve().parents[3] / 'blenderctl'
    if not path.is_absolute():
        raise RuntimeError('TOON_WORKFLOW_CLI_CORE_ROOT must be an absolute CLI core directory')
    path = path.resolve(strict=True)
    if not path.is_dir() or not all((path / name).is_file() for name in ('filesystem.py', 'processes.py')):
        raise RuntimeError('Selected shared CLI core must contain filesystem.py and processes.py: ' + str(path))
    return path


def domain(submodule=None):
    """Import only this source tree's domain, or its real bundle namespace."""
    bundle = _bundle_root()
    root = bundle if bundle is not None else Path(__file__).resolve().parent.parent
    parent = root.parent if bundle is not None else root
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))
    if bundle is not None and not bundle.name.isidentifier():
        raise RuntimeError('The installed Toon bundle directory must be a Python package name')
    prefix = (bundle.name + '.' if bundle is not None else '') + 'toon_workflow_addon'
    module = importlib.import_module(prefix + ('.' + submodule if submodule else ''))
    source = getattr(module, '__file__', None)
    if source is None or not Path(source).resolve().is_relative_to(root / 'toon_workflow_addon'):
        raise RuntimeError('Toon domain import resolved outside this deployment')
    return module
