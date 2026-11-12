"""Durable small-file writes shared by discovery and catalog updates."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, data, mode=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('refusing to replace a symbolic link: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            if mode is not None: os.fchmod(handle.fileno(), mode)
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, str(path)); sync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def atomic_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
