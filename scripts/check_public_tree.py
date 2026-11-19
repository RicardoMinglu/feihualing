"""Fail closed when a staged Git tree contains local or private data."""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED_ROOT_FILES = {
    '.env.example', '.gitignore', 'README.md',
    'requirements.in', 'requirements.lock',
}
ALLOWED_STATIC_ASSETS = {
    'admin.css', 'admin.js', 'app.css', 'landscape-a.jpg', 'landscape-b.jpg',
    'landscape-c.jpg', 'live.css', 'live.js', 'lucide-LICENSE', 'lucide.min.js',
}
ALLOWED_SUFFIXES = {
    'app': {'.py'},
    'scripts': {'.py', '.sh', '.js'},
    'tests': {'.py', '.js', '.json', '.wav'},
    'licenses': {'.txt'},
    'previews': {'.html'},
    'static': {'.html', '.js', '.css', '.jpg', ''},
}
PRIVATE_PARTS = {
    'data', 'docs', 'backups', 'output', 'models', 'screenshots', 'raw', 'wheels',
    '__pycache__', '.venv', '.playwright-cli', '.private',
}
SECRET = re.compile(
    rb'-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|'
    rb'gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}'
)
ACCOUNT_FIELD = re.compile(
    rb'"(?:username|password|password_hash|token_hash|session_token|'
    rb'email|phone|contact|feedback|users|sessions)"\s*:', re.IGNORECASE
)


def main():
    files = subprocess.check_output(['git', 'ls-files', '--cached', '-z'], cwd=ROOT).split(b'\0')
    problems = []
    for raw in filter(None, files):
        name = raw.decode('utf-8')
        path = pathlib.PurePosixPath(name)
        special_asset = (len(path.parts) == 3 and path.parts[:2] == ('static', 'assets')
                         and path.name in ALLOWED_STATIC_ASSETS)
        if (
            any(part in PRIVATE_PARTS for part in path.parts)
            or name.startswith('static/assets/support-wechat-')
            or path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.csv', '.jsonl', '.pem', '.key', '.env', '.zip'}
            or path.name.startswith('.env') and name != '.env.example'
            or (len(path.parts) == 1 and name not in ALLOWED_ROOT_FILES)
            or (len(path.parts) > 1 and path.parts[0] not in ALLOWED_SUFFIXES)
            or (len(path.parts) > 1 and path.suffix not in ALLOWED_SUFFIXES[path.parts[0]])
            or len(path.parts) > 2 and path.parts[:2] == ('static', 'assets') and not special_asset
        ):
            problems.append(f'forbidden path: {name}')
            continue
        content = subprocess.check_output(['git', 'show', f':{name}'], cwd=ROOT)
        if len(content) > 2_000_000:
            problems.append(f'oversized file: {name}')
        if SECRET.search(content):
            problems.append(f'possible credential: {name}')
        if path.suffix == '.json' and ACCOUNT_FIELD.search(content):
            problems.append(f'possible account data: {name}')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        return 1
    print(f'Public tree check passed: {sum(bool(item) for item in files)} files')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
