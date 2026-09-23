"""List standard-library extension dependencies from a selected Python runtime."""
import argparse
from pathlib import Path
import sys


def main():
    import pefile
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--python-home', type=Path, default=Path(sys.base_prefix))
    args = cli.parse_args()
    library = args.python_home / 'Library' / 'bin'
    if not library.is_dir():
        cli.error('Selected runtime has no Library/bin directory (this tool is for conda builds).')
    available = {p.name.lower() for p in library.glob('*.dll')}
    required = set()
    for path in (args.python_home / 'DLLs').glob('*.pyd'):
        with pefile.PE(str(path), fast_load=True) as pe:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
            required.update(item.dll.decode().lower() for item in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []))
    for name in sorted(required & available):
        if not name.startswith(('api-ms-win-', 'msvcp', 'vcruntime', 'ucrtbase')):
            print(name)


if __name__ == '__main__':
    main()
