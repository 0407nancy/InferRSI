"""Small configuration checks shared by preparation and the existing launcher."""
from pathlib import Path
import re
from .common import LoopError, read


def filled(value, location='config'):
    if isinstance(value, dict):
        for key, item in value.items():
            filled(item, location + '.' + key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            filled(item, location + '[' + str(i) + ']')
    elif isinstance(value, str) and re.search(r'<[A-Z][A-Z0-9_]*>', value):
        raise LoopError('Fill template field before execution: ' + location)


def launch(path):
    value = read(path)
    filled(value)
    for key in ('ssh', 'host', 'expected_user', 'expected_hostname', 'ssh_guide', 'lark',
                'lark_skill', 'document', 'container', 'remote_python', 'remote_package',
                'remote_runs', 'local_runs', 'codex'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise LoopError('Missing launch configuration: ' + key)
    for key in ('ssh', 'ssh_guide', 'lark', 'lark_skill', 'remote_python', 'remote_package', 'remote_runs', 'local_runs', 'codex'):
        if not Path(value[key]).is_absolute():
            raise LoopError('Use an absolute path for launch.' + key)
    return value
