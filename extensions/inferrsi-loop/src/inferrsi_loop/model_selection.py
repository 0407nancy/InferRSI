"""Inherit an identified Codex session, never guess from global defaults."""
import json
import os
from pathlib import Path
import sqlite3
from .common import LoopError, read, write


def session_context():
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread:
        return None
    home = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).expanduser()
    # Read only this thread's metadata; do not inspect auth or other conversations.
    for db in sorted(home.glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as conn:
                row = conn.execute('SELECT rollout_path FROM threads WHERE id=?', (thread,)).fetchone()
            if not row:
                continue
            latest = None
            with Path(row[0]).open() as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # An unfinished append is not a completed turn context.
                    if event.get('type') == 'turn_context':
                        payload = event.get('payload', {})
                        latest = {'model': payload.get('model'), 'effort': payload.get('effort', payload.get('reasoning_effort')),
                                  'thread_id': thread, 'turn_id': payload.get('turn_id'), 'source': 'codex_turn_context'}
            return latest
        except (sqlite3.Error, OSError):
            continue
    return None


def resolve(overrides=None, task=None, session=None):
    overrides, task = overrides or {}, task or {}
    value, origins = {}, {}
    for field, task_field in (('model','name'),('effort','effort')):
        choices = [('override',overrides.get(field)),('task',task.get(task_field)),
                   ('session',(session or {}).get(field))]
        chosen = next(((origin,item) for origin,item in choices if item is not None and item != ''),None)
        if chosen is None:
            raise LoopError('Cannot determine current Codex session '+field+'; launch from Codex or explicitly provide --model and --effort. Global defaults are not session evidence.')
        origins[field], value[field] = chosen
        if not isinstance(value[field],str) or not value[field].strip():
            raise LoopError('Invalid model selection: '+field)
    return dict(value, sources=origins, session=session)


def retain(folder, overrides, task, session=None):
    path = Path(folder)/'model-selection.json'
    if path.exists():
        result = read(path)
        for field in ('model','effort'):
            if overrides.get(field) and overrides[field] != result[field]:
                raise LoopError('Resume cannot change frozen '+field+'; create a new run')
        for field,key in (('model','name'),('effort','effort')):
            if task.get(key) and task[key] != result[field]:
                raise LoopError('Remote and retained model selections differ')
        return result
    result = resolve(overrides,task,session)
    write(path,result)
    return result
