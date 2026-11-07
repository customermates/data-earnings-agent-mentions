"""Shared output helpers for the data series: CSV writing, the public GitHub repo of a series, run state.

Each series keeps its state in ~/.local/share/customermates/data/<series>/ (cache, last run) and publishes its
derived dataset (our own counts plus a source URL per row, never a copy of a source's list) to its open repo
github.com/customermates/<repo>, checked out under ~/.local/share/customermates/data/repos/<repo>.
`publish_repo` commits and pushes only when a file changed. Deterministic only.
"""
import csv
import datetime
import fcntl
import io
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(os.environ.get('CM_DATA_HOME') or Path.home() / '.local/share/customermates/data')
ORG = 'customermates'


def state_dir(series):
    d = ROOT / series
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    os.replace(tmp, path)


class RunLock:
    """One run per series at a time (a timer tick never overlaps a manual run)."""

    def __init__(self, series):
        self.path = state_dir(series) / 'run.lock'

    def __enter__(self):
        self.fh = open(self.path, 'w')
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('another run of this series is active') from None
        return self

    def __exit__(self, *exc):
        self.fh.close()


def csv_text(rows, columns):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ('' if row.get(k) is None else row.get(k)) for k in columns})
    return buf.getvalue()


def _git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=check)


def publish_repo(repo_name, files, message, *, push=True):
    """Writes {relative path: text} into the repo checkout, commits and pushes when anything changed.

    Returns 'unchanged', 'committed' or 'pushed'; 'no-repo' when the checkout does not exist yet
    (create it once with: gh repo create customermates/<repo> --public, then git clone into ROOT/repos)."""
    repo = ROOT / 'repos' / repo_name
    if not (repo / '.git').exists():
        return 'no-repo'
    _git(repo, 'pull', '-q', '--rebase', 'origin', 'main', check=False)
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, 'add', '-A')
    if not _git(repo, 'status', '--porcelain').stdout.strip():
        return 'unchanged'
    _git(repo, '-c', 'user.name=Customermates Data', '-c', 'user.email=mail@customermates.com', 'commit', '-q', '-m', message)
    if not push:
        return 'committed'
    if _git(repo, 'push', '-q', 'origin', 'HEAD:main', check=False).returncode != 0:
        _git(repo, 'pull', '-q', '--rebase', 'origin', 'main', check=False)
        _git(repo, 'push', '-q', 'origin', 'HEAD:main')
    return 'pushed'


def code_files(series_dir, names):
    """Crawler source for the open repo: the series' own files plus the shared fetch and publish helpers."""
    lib = Path(__file__).resolve().parent
    out = {'crawler/' + n: (Path(series_dir) / n).read_text() for n in names}
    for n in ('fetch.py', 'publish.py'):
        out['crawler/lib/' + n] = (lib / n).read_text()
    return out


LICENSE_NOTE = """Data (CSV and JSON files): Creative Commons Attribution 4.0 International (CC BY 4.0),
https://creativecommons.org/licenses/by/4.0/ - credit "Customermates Data" with a link to this repository.

Code (crawler/): MIT License. Copyright (c) Customermates. Permission is hereby granted, free of charge, to any
person obtaining a copy of this software, to deal in the software without restriction, subject to including this
notice. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

The underlying source data belongs to its publishers (see README); this repository holds derived counts and links.
"""


def today():
    return datetime.date.today().isoformat()
