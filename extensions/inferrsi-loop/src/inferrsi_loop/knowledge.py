"""Read-only InferRSI evidence retrieval; its text is evidence, not policy."""
from pathlib import Path
import sys

from .common import read, run_command, write


def enabled(config):
    return config.get('knowledge', {}).get('enabled', True)


def prepare_policy(config):
    """One policy for every prompt/bundle/evidence entry; no change to task facts."""
    import copy
    cfg = copy.deepcopy(config)
    policy = cfg.setdefault('knowledge', {})
    policy.setdefault('enabled', True)
    if type(policy['enabled']) is not bool:
        raise LoopError('knowledge.enabled must be boolean')
    if not policy['enabled']:
        # Historical references may contain solutions; only explicit factual roots survive.
        cfg['knowledge_sources'] = []
        cfg['history_files'] = []
        cfg.pop('knowledge_directory', None)
        cfg.pop('knowledge_query', None)
        cfg['reference_directory'] = policy.get('factual_reference_directory')
        recovery = cfg.setdefault('local_recovery', {})
        recovery['evidence_roots'] = list(dict.fromkeys(
            [cfg['project']['path']] +
            ([cfg['model_assets']['root']] if cfg.get('model_assets', {}).get('root') else []) +
            policy.get('factual_evidence_roots', [])))
    return cfg


def writeback(config, run, history):
    """Append evidence-grounded experiences once; both arms retain their own feedback."""
    import time
    run = Path(run)
    root = run / 'knowledge'
    index_path = root / 'catalog.json'
    index = read(index_path) if index_path.exists() else {'schema_version': 1, 'entries': [], 'unindexed': []}
    existing = {e['id']: e for e in index['entries']}
    changed = False
    for item in history:
        number = item.get('round')
        proposal_path = run / 'rounds' / str(number) / 'proposal.json'
        if not proposal_path.exists():
            continue
        proposal = read(proposal_path)
        identity = digest([run.name, number, item.get('stage'), item.get('protocol_version'),
                           item.get('observation'), item.get('source_hash')])[:24]
        entry_path = root / 'content' / (identity + '.json')
        value = {'schema_version': 1, 'experience_id': identity, 'run_id': run.name,
                 'candidate': number, 'knowledge_enabled': enabled(config),
                 'hypothesis': proposal['hypothesis'], 'rationale': proposal.get('rationale', ''),
                 'changed_files': proposal.get('changed_files', []),
                 'conditions': {'baseline': digest(manifest(run / 'frozen/baseline')),
                                'runtime': config.get('runtime', {}), 'scope': item.get('scope'),
                                'protocol': item.get('protocol'), 'coverage': item.get('coverage')},
                 'outcome': item.get('verdict', 'INCONCLUSIVE'), 'reason': item.get('reason'),
                 'assessment': item.get('assessment', {}),
                 'evidence': {'decision': item.get('decision'), 'campaign': item.get('campaign'),
                              'observation': item.get('observation'),
                              'patch': str(run / 'rounds' / str(number) / 'candidate.patch')},
                 'interpretation': 'Scoped observation; infrastructure/missing evidence is not a disproved strategy.'}
        if identity in existing:
            if read(entry_path) != value:
                raise LoopError('Retained experience changed; preserve prior observations')
            continue
        write(entry_path, value)
        index['entries'].append({'id': identity, 'source_id': run.name, 'origin': str(entry_path),
            'title': proposal['hypothesis'], 'environment': config.get('environment_id', 'recorded_in_conditions'),
            'content': str(entry_path.relative_to(root)), 'sha256': sha(entry_path),
            'evidence_status': 'measured' if item.get('assessment', {}).get('metrics') else 'unmeasured'})
        existing[identity] = index['entries'][-1]
        changed = True
    if changed:
        index['updated_at'] = time.time()
        write(index_path, index)
        write(root / 'coverage.json', {'sources': [{'source': {'id': run.name}, 'indexed': len(index['entries']), 'omissions': []}],
                                       'complete_for_declared_text': True})
    return root


def own_context(run, query):
    root = Path(run) / 'knowledge'
    if not (root / 'catalog.json').exists():
        return None
    value = project_context(root, query)
    value['catalog'] = 'references/own-feedback/catalog.json'
    value['coverage']['details'] = 'references/own-feedback/coverage.json'
    return value


def retrieve(query, output):
    output = Path(output)
    receipt = output / "knowledge.json"
    if receipt.exists():
        return read(receipt)
    result = run_command([sys.executable, "-m", "inferrsi", "knowledge", "search", query,
                          "--limit", "3", "--json"], Path.cwd(), output / "search", 30)
    payload = {"query": query, "status": result["status"], "evidence": None}
    if result["status"] == "completed":
        try:
            payload["evidence"] = read(output / "search/stdout.log")
        except ValueError:
            payload["status"] = "invalid_json"
    write(receipt, payload)
    return payload


# Project evidence is separate from the installed, general InferRSI library.
# Ranking reuses InferRSI Find; this adapter only collects explicitly allowed sources.
import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from .common import LoopError, digest, manifest, sha

SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', 'venv', 'venvs', 'node_modules',
             'site-packages', 'frozen', 'candidate', 'baseline', 'workspace'}
TEXT_SUFFIXES = {'.json', '.jsonl', '.txt', '.md', '.log', '.patch', '.py', '.sh'}


def fetch_documents(sources, binary='lark-cli'):
    """Local Mac only. Keep complete CLI envelopes and explicit unread-resource facts."""
    def call(args):
        p = subprocess.run([binary, *args, '--as', 'user'], capture_output=True, text=True, timeout=120)
        try:
            value = json.loads(p.stdout)
        except ValueError as exc:
            raise LoopError('Knowledge source returned no JSON; check local access') from exc
        if p.returncode or value.get('ok') is not True:
            raise LoopError('Knowledge source could not be read; check local access')
        return value
    snapshots = {}
    workbooks = {}
    for source in sources:
        if source['kind'] != 'lark':
            continue
        value = call(['docs', '+fetch', '--doc', source['url'], '--detail', 'with-ids'])
        doc = value['data']['document']
        root = ET.fromstring('<root>' + doc['content'] + '</root>')
        embedded = []
        for node in root.iter('sheet'):
            token, sheet = node.get('token'), node.get('sheet-id')
            item = {'token': token, 'sheet_id': sheet, 'status': 'unread'}
            if source.get('read_sheets', False):
                try:
                    if token not in workbooks:
                        workbooks[token] = call(['sheets', '+workbook-info', '--spreadsheet-token', token])
                    result = call(['sheets', '+csv-get', '--spreadsheet-token', token,
                                   '--sheet-id', sheet, '--max-chars', '500000'])
                    data = result['data']
                    partial = data.get('has_more') or data.get('truncated') or data.get('complete') is False
                    item.update(status='partial' if partial else 'read', response=result,
                                workbook=workbooks[token])
                except (LoopError, OSError, subprocess.SubprocessError) as exc:
                    item['reason'] = type(exc).__name__
            embedded.append(item)
        snapshots[source['id']] = {'source': source, 'fetched_at': time.time(),
                                   'response': value, 'embedded': embedded}
    return snapshots


def document_sections(snapshot):
    doc = snapshot['response']['data']['document']
    root = ET.fromstring('<root>' + doc['content'] + '</root>')
    headings, body, anchor = [], [], ''
    def result():
        return (' / '.join(x[1] for x in headings) or 'document', anchor, '\n'.join(body))
    for node in root:
        if re.fullmatch(r'h[1-9]', node.tag):
            if body:
                yield result()
            level = int(node.tag[1:])
            headings = [x for x in headings if x[0] < level]
            headings.append((level, ''.join(node.itertext())))
            anchor, body = node.get('id', ''), []
        else:
            # Preserve cell boundaries; extracted text is evidence, never executable instructions.
            body.append(' '.join(node.itertext()))
    if body:
        yield result()
    for item in snapshot.get('embedded', []):
        if item['status'] in ('read', 'partial'):
            yield 'embedded sheet ' + item['sheet_id'], '', json.dumps(item, ensure_ascii=False)


def freeze_project(sources, output, snapshots=None, *, exclude=(), max_bytes=64*1024*1024):
    """Inventory every declared root; copy bounded text, report omissions, never execute it."""
    output = Path(output).resolve()
    if output.exists():
        raise LoopError('Knowledge snapshot must be new')
    snapshots = snapshots or {}
    ids = [s['id'] for s in sources]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in ids):
        raise LoopError('Knowledge source IDs must be unique simple names')
    output.mkdir(parents=True)
    entries, coverage, pending, used = [], [], [], 0
    excluded = [Path(x).resolve() for x in (*exclude, output)]

    def retain(source, origin, title, data, **facts):
        nonlocal used
        fingerprint = hashlib.sha256(data).hexdigest()
        name = 'content/' + fingerprint + '.txt'
        destination = output / name
        if not destination.exists():
            if used + len(data) > max_bytes:
                return False
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(data)
            used += len(data)
        if origin.endswith('.json'):
            try:
                value = json.loads(data)
                if isinstance(value, dict):
                    facts['declared_outcome'] = value.get('verdict', value.get('status', 'unspecified'))
                    if isinstance(value.get('hypothesis'), str):
                        title += ' | ' + value['hypothesis'][:500]
            except ValueError:
                pass
        entries.append({'id': digest([source['id'], origin, title])[:20], 'source_id': source['id'],
                        'origin': origin, 'title': title, 'environment': source.get('environment', 'unknown'),
                        'content': name, 'sha256': fingerprint, 'evidence_status': 'recorded_not_revalidated', **facts})
        return True

    for source in sources:
        report = {'source': source, 'indexed': 0, 'omissions': [], 'excluded_files': 0, 'excluded_directories': 0}
        coverage.append(report)
        before = len(entries)
        if source['kind'] == 'lark':
            snapshot = snapshots.get(source['id'])
            if snapshot is None:
                raise LoopError('Missing local document snapshot: ' + source['id'])
            if snapshot['source']['url'] != source['url'] or snapshot['response'].get('ok') is not True:
                raise LoopError('Document snapshot does not match its configured source')
            write(output / ('documents/' + source['id'] + '.json'), snapshot)
            doc = snapshot['response']['data']['document']
            for title, anchor, text in document_sections(snapshot):
                if not retain(source, source['url'] + ('#' + anchor if anchor else ''), title, text.encode(),
                              revision=doc.get('revision_id'), document_id=doc.get('document_id')):
                    report['omissions'].append({'path': title, 'reason': 'total_byte_budget'})
            report['omissions'] += [{'path': item['token'] + '/' + item['sheet_id'], 'reason': 'embedded_' + item['status']}
                                     for item in snapshot.get('embedded', []) if item['status'] != 'read']
        elif source['kind'] == 'directory':
            root = Path(source['path']).resolve()
            if not root.is_dir():
                if source.get('required', True):
                    raise LoopError('Missing knowledge directory: ' + str(root))
                report['omissions'].append({'path': str(root), 'reason': 'missing_directory'})
                continue
            def error(exc):
                report['omissions'].append({'path': exc.filename, 'reason': type(exc).__name__})
            for current, dirs, files in os.walk(root, onerror=error, followlinks=False):
                current = Path(current)
                keep = [d for d in dirs if d not in SKIP_DIRS and not (current/d).is_symlink()
                        and not any((current/d).resolve().is_relative_to(p) for p in excluded)]
                report['excluded_directories'] += len(dirs) - len(keep)
                dirs[:] = sorted(keep)
                for name in sorted(files):
                    path = current / name
                    if path.is_symlink() or name.startswith('.') or path.suffix not in TEXT_SUFFIXES:
                        report['excluded_files'] += 1
                        continue
                    try:
                        size = path.stat().st_size
                        if size > source.get('max_file_bytes', 2*1024*1024):
                            report['omissions'].append({'path': str(path), 'reason': 'large_text', 'bytes': size})
                            continue
                        pending.append((source, report, root, path, size))
                    except (OSError, UnicodeError) as exc:
                        report['omissions'].append({'path': str(path), 'reason': type(exc).__name__})
        else:
            raise LoopError('Unknown knowledge source kind')
        report['indexed'] = len(entries) - before
    # Globally prioritize compact results/plans/code before raw logs. One early
    # source's large logs must not starve later roots of their experiment results.
    def priority(item):
        path = item[3]
        outcome = any(word in path.name.lower() for word in
                      ('result', 'summary', 'decision', 'proposal', 'verdict', 'metrics', 'acceptance'))
        return (path.suffix in ('.log', '.jsonl') and not outcome, item[4], str(path))
    pending.sort(key=priority)
    for source, report, root, path, _ in pending:
        try:
            data = path.read_bytes()
            data.decode('utf-8')
            if retain(source, str(path), str(path.relative_to(root)), data, modified_at=path.stat().st_mtime):
                report['indexed'] += 1
            else:
                report['omissions'].append({'path': str(path), 'reason': 'total_byte_budget'})
        except (OSError, UnicodeError) as exc:
            report['omissions'].append({'path': str(path), 'reason': type(exc).__name__})
    write(output / 'catalog.json', {'schema_version': 1, 'created_at': time.time(), 'entries': entries,
        'unindexed': [dict(item, source_id=c['source']['id']) for c in coverage for item in c['omissions']]})
    write(output / 'coverage.json', {'sources': coverage, 'text_bytes': used,
          'complete_for_declared_text': not any(c['omissions'] for c in coverage),
          'limits': 'Only declared roots/documents; excludes checkpoints, binary traces, copied source workspaces. Recorded evidence is not a new validation.'})
    return str(output)


def project_context(directory, query, *, limit=20, context_bytes=32000):
    """Use the installed InferRSI Find ranker, with all project records available on disk."""
    from inferrsi.knowledge.knowledge_find import Corpus, Document, rank
    directory = Path(directory)
    entries = read(directory / 'catalog.json')['entries']
    texts = []
    documents = []
    for entry in entries:
        path = directory / entry['content']
        if sha(path) != entry['sha256']:
            raise LoopError('Project knowledge changed after freezing')
        text = path.read_text()
        texts.append(text)
        documents.append(Document(identity=entry['id'], kind='record', state='historical_observation',
            path=entry['content'], title=entry['title'], summary=entry['origin'],
            fields={'title': entry['title'], 'body': text, 'applicability': entry['environment']}))
    _, _, ranked = rank(query, Corpus(documents))
    selected, used = [], 0
    for _, _, index, _ in ranked[:limit]:
        entry = entries[index]
        text = texts[index]
        size = min(len(text.encode()), 4000, max(0, context_bytes-used))
        if not size:
            break
        excerpt = text.encode()[:size].decode('utf-8', errors='ignore')
        selected.append(dict(entry, excerpt=excerpt, truncated=len(excerpt) < len(text)))
        used += len(excerpt.encode())
    coverage = read(directory/'coverage.json')
    return {'query': query, 'ranker': 'inferrsi.knowledge.knowledge_find', 'corpus_size': len(entries),
            'matches': len(ranked), 'items': selected,
            'coverage': {'sources': [{'id': c['source']['id'], 'indexed': c['indexed'], 'omitted': len(c['omissions'])}
                                     for c in coverage['sources']],
                         'details': 'references/project-knowledge/coverage.json'},
            'catalog': 'references/project-knowledge/catalog.json',
            'note': 'Search the complete catalog and cited content before proposing; snippets are not the complete history. Old/mixed environments are not current validation.'}
