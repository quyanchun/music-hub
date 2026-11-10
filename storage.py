"""Disk budgets for reconstructible caches; never delete outside the data folders."""
import shutil
import time
import re

MIB = 1024 * 1024
MIN_FREE = 1024 * MIB
MAX_DATABASE = 128 * MIB
MAX_IMAGES = 64 * MIB


def require_space(folder, extra=0):
    if shutil.disk_usage(folder).free < MIN_FREE + extra:
        raise ValueError('磁盘剩余空间不足，暂停新增缓存，请清理空间后重试')


def sweep(folder, limit, ttl, protected=lambda path: False):
    now = time.time()
    files = []
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if not re.fullmatch(r'[0-9a-f]{32}(?:\.[0-9]+)?\.(?:mp3|aac|m4a|ogg|flac|wav|ape|part|image)', path.name):
            continue
        if path.suffix not in ('.mp3', '.aac', '.m4a', '.ogg', '.flac', '.wav', '.ape', '.part', '.image'):
            continue
        try:
            info = path.stat()
            files.append((info.st_mtime, path, info.st_size))
        except FileNotFoundError:
            pass
    total = sum(size for _, _, size in files)
    removed = 0
    for modified, path, size in sorted(files):
        age_limit = 3600 if path.suffix == '.part' else ttl
        if (now - modified > age_limit or total > limit) and not protected(path):
            try:
                path.unlink()
                total -= size
                removed += 1
            except FileNotFoundError:
                pass
    return {'bytes': total, 'removed': removed}
