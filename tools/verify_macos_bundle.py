"""Verify both macOS CPU slices without scanning networks or opening the GUI."""

import pathlib
import subprocess


def main():
    root = pathlib.Path('dist/WiFiAnalyzer.app')
    if not root.is_dir():
        raise SystemExit('Missing macOS application')
    seen = set()
    count = 0
    for path in root.rglob('*'):
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        with path.open('rb') as source:
            magic = source.read(4)
        if magic not in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                         b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf'):
            continue
        slices = subprocess.check_output(['lipo', '-archs', str(path)], text=True).split()
        if not {'arm64', 'x86_64'} <= set(slices):
            raise SystemExit(f'Missing universal2 CPU slice: {path}: {slices}')
        count += 1
    if not count:
        raise SystemExit('No Mach-O binaries found')
    print(f'Verified {count} universal2 Mach-O binaries; Apple Silicon runtime requires separate hardware testing')


if __name__ == '__main__':
    main()
