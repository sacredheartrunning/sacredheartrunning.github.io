"""Publish generated pages to Sacred Heart's existing GitHub Pages repository.

python publish.py --prepare  builds and stages files for local review only.
python publish.py           builds, commits, and pushes the website.
Git must be installed and signed in with access to the Sacred Heart organization.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from run import ROOT, SETTINGS, build_site


def git(repo,*args):
    result=subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    return result.stdout.strip()


def code_files(root):
    # Explicitly include project code/configuration; never upload data or credentials.
    names = ['run.py', 'publish.py', 'milesplit.py', 'presentation.py',
             'settings.json', 'START_HERE.txt', 'Start Sacred Heart.bat',
             'manual_results_template.csv', '.gitignore']
    files = [root / name for name in names]
    for folder, extensions in [('assets', {'.css', '.js', '.png', '.txt'}),
                               ('tests', {'.py'})]:
        files.extend(p for p in (root / folder).rglob('*')
                     if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts)
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f'Missing or invalid code file: {path.name}')
    return files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--code-only', action='store_true', help='Back up application source to GitHub instead of publishing pages')
    args=parser.parse_args()
    repo=ROOT/'publishing'
    url=SETTINGS['publishing_repository']
    if not (repo/'.git').exists():
        subprocess.run(['git','clone',url,str(repo)],check=True)
    if git(repo,'remote','get-url','origin') != url:
        raise RuntimeError('Publishing remote differs from Sacred Heart settings.')
    if git(repo,'branch','--show-current') != 'main':
        raise RuntimeError('Publishing checkout must be on main.')
    if git(repo,'status','--porcelain'):
        raise RuntimeError('Publishing checkout has uncommitted changes. Review them first.')
    git(repo,'pull','--ff-only','origin','main')
    source=ROOT if args.code_only else build_site().parent
    manifest=repo/('.source-files.txt' if args.code_only else '.generated-files.txt')
    old=set(manifest.read_text(encoding='utf-8').splitlines()) if manifest.exists() else set()
    files=code_files(ROOT) if args.code_only else [p for p in source.rglob('*') if p.is_file() and (not p.name.startswith('.') or p.name=='.nojekyll')]
    prefix=Path('source') if args.code_only else Path('.')
    current={(prefix/p.relative_to(source)).as_posix() for p in files}
    for relative in old-current:
        target=(repo/relative).resolve()
        if not target.is_relative_to(repo.resolve()) or '.git' in target.relative_to(repo.resolve()).parts:
            raise RuntimeError('Invalid generated-file manifest entry.')
        if target.is_file():
            target.unlink()
    for path in files:
        dest=repo/prefix/path.relative_to(source)
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,dest)
    manifest.write_text('\n'.join(sorted(current))+'\n',encoding='utf-8')
    git(repo,'add','--all')
    print(git(repo,'diff','--cached','--stat'))
    if args.prepare:
        print('Prepared and staged only. Review, commit, and push from the publishing folder.')
        return
    if git(repo,'diff','--cached','--name-only'):
        git(repo,'commit','-m','Back up Sacred Heart application source' if args.code_only else 'Update Sacred Heart cross country reports')
    git(repo,'push','origin','main')
    if args.code_only:
        print('Code backed up to GitHub: '+url.removesuffix('.git')+'/tree/main/source')
        print('Database and source results are not included. Copy the entire local Sacred Heart folder for a full backup.')
    else:
        print('Pushed website. GitHub Pages deployment may take a few minutes: '+SETTINGS['website_url'])


if __name__=='__main__':
    try:
        main()
    except (OSError,RuntimeError,subprocess.CalledProcessError) as exc:
        print(f'Publishing stopped: {exc}',file=sys.stderr)
        if isinstance(exc,subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr,file=sys.stderr)
        sys.exit(1)
