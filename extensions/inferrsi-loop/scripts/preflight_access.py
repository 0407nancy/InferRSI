"""Read-only access checks; never persist CLI output or credentials."""
import argparse
import json
from pathlib import Path
import socket
import subprocess

DEFAULT = Path(__file__).resolve().parents[1] / 'config/local-access.json'

def classify(message):
    text = message.lower()
    if any(x in text for x in ('operation not permitted', 'sandbox', 'credential store', 'keychain')):
        return 'permission_or_credential_store'
    if any(x in text for x in ('resolve', 'name or service', 'nodename')):
        return 'dns_or_network'
    if any(x in text for x in ('unauthorized', 'token', 'login', 'permission denied', '认证')):
        return 'authentication_or_access'
    return 'command_failed'

def check(config=DEFAULT):
    c = json.loads(Path(config).read_text())
    rows = []
    for key in ('ssh_guide', 'ssh', 'lark_skill', 'lark'):
        rows.append({'check': key, 'ok': Path(c[key]).is_file(), 'category': 'local_entry'})
    if c.get('jump_hostname'):
        try:
            socket.getaddrinfo(c['jump_hostname'], 22)
            rows.append({'check': 'dns', 'ok': True})
        except OSError:
            rows.append({'check': 'dns', 'ok': False, 'category': 'dns_or_network'})
    commands = [('ssh', [c['ssh'], '-T', c['host'], 'id -un; hostname']),
                ('lark_read', [c['lark'], 'docs', '+fetch', '--as', 'user', '--doc', c['document'],
                               '--scope', 'outline', '--max-depth', '1'])]
    for name, argv in commands:
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90)
            ok = r.returncode == 0
            category = 'ok' if ok else classify(r.stderr + r.stdout)
            if ok and name == 'ssh':
                ok = r.stdout.strip().splitlines() == [c['expected_user'], c['expected_hostname']]
                category = 'ok' if ok else 'identity_mismatch'
            if ok and name == 'lark_read':
                data = json.loads(r.stdout)
                ok = isinstance(data, dict) and data.get('ok') is True
                category = 'ok' if ok else 'api_error'
            rows.append({'check': name, 'ok': ok, 'category': category})
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            rows.append({'check': name, 'ok': False, 'category': type(e).__name__})
    return {'ready': all(r['ok'] for r in rows), 'checks': rows,
            'scope': 'current_process_read_access', 'document_write': 'not_tested',
            'recovery_cli_sandbox': 'not_tested'}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', default=str(DEFAULT))
    p.add_argument('--output')
    a = p.parse_args()
    result = check(a.config)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.output:
        Path(a.output).write_text(text)
    print(text)
    return 0 if result['ready'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
