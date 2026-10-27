#!/usr/bin/env python3
"""Build Decky, PC helper and source archives from this directory."""
import hashlib
import argparse
from pathlib import Path
import shutil
import zipfile
from build import ROOT, build

VERSION = '0.3.0'


def archive(path, files):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for source, name in sorted(files, key=lambda item:item[1]):
            out.write(source, name)
    with zipfile.ZipFile(path) as check:
        assert check.testzip() is None


def main():
    parser = argparse.ArgumentParser(description='Build DeckPilot release archives')
    parser.add_argument('--output', type=Path, default=ROOT.parent/'releases')
    parser.add_argument('--copy-to', type=Path, help='Also copy archives and checksums to this directory')
    args = parser.parse_args()
    out = args.output.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error('--output must be outside the source directory')
    build()
    out.mkdir(parents=True, exist_ok=True)
    (ROOT/'tools/Setup-Linux.desktop').chmod(0o755)
    cmd = ROOT/'tools/Setup-Windows.cmd'
    cmd.write_bytes(cmd.read_text().replace('\r\n', '\n').replace('\n','\r\n').encode('ascii'))
    runtime = ['plugin.json','package.json','main.py','dist/index.js','LICENSE','README.md','README.en.md','CHANGELOG.md',
               'locales/ru.json','locales/en.json',
               'tools/setup-linux.sh','tools/setup-arch.sh','tools/Setup-Linux.desktop',
               'tools/setup-windows.ps1','tools/Setup-Windows.cmd']
    screenshots = list((ROOT/'assets/screenshots').glob('*.png'))
    runtime += [str(path.relative_to(ROOT)) for path in screenshots]
    plugin = out/f'deckpilot-{VERSION}.zip'
    archive(plugin, [(ROOT/name, 'DeckPilot/'+name) for name in runtime])
    host = out/f'deckpilot-host-{VERSION}.zip'
    names = ['setup-linux.sh','Setup-Linux.desktop','setup-windows.ps1','Setup-Windows.cmd']
    files = [(ROOT/'tools'/name, 'DeckPilot Host/'+name) for name in names]
    files += [(ROOT/'tools/HOST-README.md','DeckPilot Host/HOST-README.md'),
              (ROOT/'tools/HOST-README.en.md','DeckPilot Host/HOST-README.en.md'),
              (ROOT/'README.md','DeckPilot Host/README.md'),
              (ROOT/'README.en.md','DeckPilot Host/README.en.md'),(ROOT/'LICENSE','DeckPilot Host/LICENSE')]
    files += [(path, 'DeckPilot Host/'+str(path.relative_to(ROOT))) for path in screenshots]
    archive(host,files)
    source = out/f'deckpilot-source-{VERSION}.zip'
    files = [(p, 'DeckPilot/'+str(p.relative_to(ROOT))) for p in ROOT.rglob('*')
             if p.is_file() and not {'.git', '__pycache__', 'node_modules', '.env', '.DS_Store'}.intersection(p.relative_to(ROOT).parts)
             and p.suffix != '.pyc']
    archive(source,files)
    paths = [plugin,host,source]
    checksums=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in paths)
    (out/'SHA256SUMS').write_text(checksums)
    for p in paths+[out/'SHA256SUMS']:
        print(p)
        if args.copy_to:
            args.copy_to.mkdir(parents=True, exist_ok=True)
            target=args.copy_to/(p.name if p.name!='SHA256SUMS' else f'deckpilot-{VERSION}-SHA256SUMS.txt')
            shutil.copy2(p,target)


if __name__=='__main__':
    main()
