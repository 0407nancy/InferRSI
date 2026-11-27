from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from .common import LoopError, digest, read, write


def enqueue(run):
    run = Path(run)
    summary = read(run / "summary.json")
    # Only explicit experiment fields are rendered; never publish raw logs or credentials.
    payload = {key: summary.get(key) for key in (
        "run_id", "status", "completed_rounds", "last_verdict", "last_stage", "reason", "model")}
    if (run / "results.json").exists():
        payload['report'] = read(run / "results.json")
        # Report-only collection can regenerate results.json; keep editorial notes separately.
        previous = read(run / 'sync-outbox.json') if (run / 'sync-outbox.json').exists() else {}
        notes = dict(previous.get('report', {}).get('report_notes', {}))
        notes.update(payload['report'].get('report_notes', {}))
        if (run / 'report-notes.json').exists():
            notes.update(read(run / 'report-notes.json'))
        if notes:
            payload['report']['report_notes'] = notes
            write(run / 'report-notes.json', notes)
    write(run / "sync-outbox.json", payload)
    return payload


def attempt_reason(payload):
    report = payload.get('report', {})
    explicit = report.get('report_notes', {}).get('attempt_reason')
    if explicit:
        return explicit
    items = report.get('accepted', []) + report.get('validated_pending_review', []) + report.get('other_candidates', [])
    return '；'.join(f"第{x['round']}项：{x.get('rationale') or x['hypothesis']}" for x in items) or '原记录未提供策略依据，待补充。'


def render(payload):
    """One sibling h2 per run; metrics remain scoped to their original experiment."""
    report = payload.get('report', {})
    summary = report.get('summary', payload)
    marker = 'InferRSI-loop:' + payload['run_id']
    started = report.get('started_at')
    when = datetime.fromtimestamp(started, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') if started is not None else '时间未记录'
    items = report.get('accepted', []) + report.get('validated_pending_review', []) + report.get('other_candidates', [])
    notes = report.get('report_notes', {})
    title = notes.get('title', '推理优化实验')
    parts = ['<h2>' + escape(f'{when}｜{title}｜{marker}') + '</h2>']
    def paragraph(text):
        parts.append('<p>' + escape(str(text)) + '</p>')
    paragraph('开始时间：' + when + '（Asia/Shanghai）；run ID：' + payload['run_id'])
    explanation = notes.get('description') or '；'.join(x['hypothesis'] for x in items) or report.get('goal') or payload.get('reason', '未记录优化说明')
    paragraph('优化说明：' + explanation)
    paragraph('尝试原因：' + attempt_reason(payload))
    paragraph(f"结果概述：状态={payload['status']}；候选数={summary.get('candidate_count', '未记录')}；有效性能实验数={summary.get('effective_experiments', '未记录')}；局部达标={len(report.get('candidate_optimizations', []))}；协议修复={summary.get('protocol_repairs', '未记录')}。")
    rows = []
    evidence = []
    status_names = {'candidate_optimization':'局部候选', 'rejected':'未达标', 'pending_numerical_review':'数值待确认', 'inconclusive':'评测未完成', 'unmeasured_missing_evidence_or_protocol':'未测量', 'awaiting_measurement':'未测量', 'accepted':'已验收', 'validated_pending_human_review':'待人工确认'}
    for item in items:
        decisions = item.get('decisions', {})
        label = notes.get('items', {}).get(str(item['round']), item['hypothesis'])
        for stage, decision in (decisions.items() or [('screen', {})]):
            assessment = decision.get('assessment', {})
            metric = assessment.get('metrics')
            timing = f"{metric['baseline']:.4g} → {metric['candidate']:.4g} {metric['unit']}" if metric else '未测量'
            gain = f"{assessment['improvement_pct']:.2f}%" if metric and assessment.get('improvement_pct') is not None else '—'
            quality = '通过' if assessment.get('quality_passed') is True else '失败' if assessment.get('quality_passed') is False else '待确认' if decision.get('reason') == 'needs_numerical_review' else '未完成'
            reason = decision.get('reason') or item.get('feedback', {}).get('assessment', {}).get('reason') or item.get('feedback', {}).get('reason', '')
            conclusion = status_names.get(item['status'], item['status'])
            rows.append([f"{item['round']}/{stage}", label, timing, gain, quality, conclusion])
            if reason: evidence.append(f"第{item['round']}项/{stage}：{reason}")
            if decision.get('evidence'): evidence.append('评测结果：' + decision['evidence'])
            if assessment.get('scope'): evidence.append('测量范围：' + assessment['scope'])
        if item.get('patch'): evidence.append('补丁：' + item['patch'])
    for item in report.get('ablation', {}).get('rows', []):
        a = item.get('assessment', {}); m = a.get('metrics')
        rows.append([item['stage'], f"{item['reference']} → {item['candidate']}", f"{m['baseline']:.4g} → {m['candidate']:.4g} {m['unit']}" if m else '未测量', f"{a['improvement_pct']:.2f}%" if m else '—', '见原始验证结果', item['verdict']])
    def cells(values, tag):
        return '<tr>' + ''.join(f'<{tag}><p>{escape(str(v))}</p></{tag}>' for v in values) + '</tr>'
    if rows:
        parts.append('<table><thead>' + cells(['优化项/阶段','具体改动','基线→候选耗时','耗时下降','数值验证','结论'], 'th') + '</thead><tbody>' + ''.join(cells(row, 'td') for row in rows) + '</tbody></table>')
    for text in evidence: paragraph(text)
    if report.get('run'): paragraph('结果文件：' + report['run'] + '/results.json')
    paragraph(notes.get('limitations', '局部结果仅覆盖所列样本与模块；不代表真实请求、TP 通信或 E2E 收益。数值通过也不等于完整模型质量通过。'))
    paragraph('记录结束：' + marker + ':end')
    return ''.join(parts)


def sync(run, document, binary="lark-cli"):
    import json
    run = Path(run).resolve()
    payload = read(run / "sync-outbox.json")
    marker = "InferRSI-loop:" + payload["run_id"]
    if not re.fullmatch(r"InferRSI-loop:[a-zA-Z0-9_-]+", marker):
        raise LoopError("Invalid report identity")
    receipt = run / "sync-receipt.json"
    previous_receipt = read(receipt) if receipt.exists() else {}
    if not shutil.which(binary):
        result = {"status": "pending", "reason": "lark_cli_unavailable"}
        write(receipt, result)
        return result
    def call(args):
        p = subprocess.run([binary, "docs", *args, "--as", "user", "--doc", document],
                           capture_output=True, text=True, timeout=90, cwd=run)
        data = json.loads(p.stdout)
        if p.returncode or not data.get("ok"):
            raise LoopError("Feishu operation failed; report remains pending")
        return data["data"]["document"]
    def fetch(args):
        d = call(['+fetch', *args, '--detail', 'full'])
        return d, ET.fromstring(d['content'] or '<fragment/>')
    _, root = fetch(['--scope', 'keyword', '--keyword', marker])
    headings = [x for x in root.iter('h2') if marker in ''.join(x.itertext())]
    ends = [x for x in root.iter('p') if ''.join(x.itertext()) == '记录结束：' + marker + ':end']
    old = [x for x in root.iter('p') if ''.join(x.itertext()).startswith(marker + ' |')]
    if len(headings)>1 or len(ends)>1 or len(old)>1:
        raise LoopError('Duplicate report identity')
    args = ['+update', '--command', 'append']
    if headings or ends:
        if not headings or not ends: raise LoopError('Incomplete report section; inspect before replacing')
        _, section = fetch(['--scope','section','--start-block-id',headings[0].get('id')])
        reasons = [''.join(x.itertext())[len('尝试原因：'):] for x in section.findall('p')
                   if ''.join(x.itertext()).startswith('尝试原因：')]
        if len(reasons) > 1:
            raise LoopError('Multiple attempt reasons; inspect before replacing')
        # A legacy/manual reason wins over generated proposals, including edits after a prior sync.
        if reasons and (previous_receipt.get('document') != document
                        or reasons[0] != previous_receipt.get('attempt_reason')
                        or previous_receipt.get('attempt_reason_source') == 'preserved'):
            notes = payload.setdefault('report', {}).setdefault('report_notes', {})
            notes['attempt_reason'] = reasons[0]
            write(run / 'report-notes.json', notes)
            write(run / 'sync-outbox.json', payload)
        args = ['+update','--command','block_replace','--start-block-id',headings[0].get('id'),'--end-block-id',ends[0].get('id')]
    elif old:
        # Migrate an old single-paragraph report, including its manually added heading.
        _, outline = fetch(['--scope','outline','--max-depth','2'])
        previous = [x for x in outline.iter('h2') if payload['run_id'] in ''.join(x.itertext())]
        if len(previous)>1: raise LoopError('Ambiguous legacy section')
        args = ['+update','--command','block_replace']
        args += ['--start-block-id',previous[0].get('id'),'--end-block-id',old[0].get('id')] if previous else ['--block-id',old[0].get('id')]
    content = render(payload)
    signature = digest({'format': 3, 'document': document, 'content': content})
    if headings and previous_receipt.get('digest') == signature:
        return previous_receipt
    xml = run / 'sync-payload.xml'; xml.write_text(content)
    call(args + ['--doc-format','xml','--content','@'+str(xml)])
    _, found = fetch(['--scope','keyword','--keyword',marker])
    headers = [x for x in found.iter('h2') if marker in ''.join(x.itertext())]
    if len(headers)!=1: raise LoopError('Report heading could not be verified')
    verified, actual = fetch(['--scope','section','--start-block-id',headers[0].get('id')])
    expected = ET.fromstring('<fragment>'+content+'</fragment>')
    expected_text = {''.join(x.itertext()) for x in expected.iter() if x.tag in ('p','h2')}
    actual_text = {''.join(x.itertext()) for x in actual.iter() if x.tag in ('p','h2')}
    if not expected_text <= actual_text: raise LoopError('Report content could not be verified')
    result = {'status':'synced','digest':signature,'revision':verified.get('revision_id'),'format':3,
              'document':document, 'attempt_reason':attempt_reason(payload),
              'attempt_reason_source':'preserved' if payload.get('report', {}).get('report_notes', {}).get('attempt_reason') else 'proposal'}
    write(receipt,result)
    return result
