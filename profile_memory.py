"""Run in an isolated container; inspect transient allocations without user data output."""
import gc
import json
from pathlib import Path
import sys
import tracemalloc


def rss():
    return next(int(line.split()[1]) for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))


def main():
    results = []
    def sample(stage):
        current, peak = tracemalloc.get_traced_memory()
        results.append({'stage': stage, 'rss_kib': rss(), 'python_current': current, 'python_peak': peak})
    tracemalloc.start()
    sample('stdlib')
    import service
    sample('service_import')
    from catalog import Catalog
    from library import Library
    catalog = Catalog('/experiment/data')
    sample('catalog_open')
    library = Library(catalog)
    sample('library_build')
    results[-1]['songs'] = len(library.songs)
    del library
    gc.collect()
    sample('library_released')
    from music_dl.addons.netease import NeteaseApi
    sample('netease_import')
    for name in ('kuwo', 'qq', 'kugou', 'migu'):
        __import__(name)
    sample('all_providers_import')
    print(json.dumps({'stages': results, 'cli_loaded': 'music_dl.__main__' in sys.modules}, indent=2))


if __name__ == '__main__':
    main()
