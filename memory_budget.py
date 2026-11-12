"""Read the container's memory budget without a monitoring process or cache."""
from pathlib import Path


class BackgroundPaused(Exception):
    pass


def memory_snapshot():
    for root, usage, limit in (
        ('/sys/fs/cgroup', 'memory.current', 'memory.max'),
        ('/sys/fs/cgroup/memory', 'memory.usage_in_bytes', 'memory.limit_in_bytes'),
    ):
        try:
            root = Path(root)
            maximum = int((root / limit).read_text())
            current = int((root / usage).read_text())
            stat = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
            working = max(0, current - int(stat.get('inactive_file', stat.get('total_inactive_file', 0))))
            if maximum < 1 << 50:
                return {'limit': maximum, 'working': working, 'charged': current}
        except (OSError, ValueError):
            continue
    return {}


def background_allowed():
    state = memory_snapshot()
    return not state or state['working'] < min(state['limit'] * .75, state['limit'] - 40 * 1024 * 1024)
