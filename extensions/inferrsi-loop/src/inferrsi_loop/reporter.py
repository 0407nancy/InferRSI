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


def row_reasons(payload):
    root = ET.fromstring('<fragment>' + render(payload) + '</fragment>')
    return {''.join(row[0].itertext()): ''.join(row[1].itertext())
            for row in root.findall('.//tbody/tr') if len(row) == 4}


def render(payload):
    """Four columns, sibling run sections, and links to complete retained evidence."""
    report = payload.get('report', {})
    summary = report.get('summary', payload)
    marker = 'InferRSI-loop:' + payload['run_id']
    started = report.get('started_at')
    when = datetime.fromtimestamp(started, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') if started is not None else '时间未记录'
    notes = report.get('report_notes', {})
    condition = 'on' if report.get('knowledge_enabled', summary.get('knowledge_enabled', True)) else 'off'
    parts = ['<h2>' + escape(f'{when}｜{payload["run_id"]}｜Knowledge {condition}｜{marker}') + '</h2>']
    def paragraph(text):
        parts.append('<p>' + escape(str(text)) + '</p>')
    paragraph('状态：' + payload['status'] + f"；候选 {summary.get('candidate_count', 0)}；有效实验 {summary.get('effective_experiments', 0)}")
    rows, evidence = [], []
    status_names = {'candidate_optimization':'局部候选', 'rejected':'未达标', 'pending_numerical_review':'数值待确认', 'inconclusive':'评测未完成', 'unmeasured_missing_evidence_or_protocol':'未测量', 'awaiting_measurement':'未测量', 'accepted':'已验收', 'validated_pending_human_review':'待人工确认'}
    for item in report.get('accepted', []) + report.get('validated_pending_review', []) + report.get('other_candidates', []):
        for stage, decision in (item.get('decisions', {}).items() or [('screen', {})]):
            assessment = decision.get('assessment', {})
            metric = assessment.get('metrics')
            timing = f"{metric['baseline']:.4g} → {metric['candidate']:.4g} {metric['unit']}；收益 {assessment['improvement_pct']:.2f}%" if metric and assessment.get('improvement_pct') is not None else '未测量'
            quality = '通过' if assessment.get('quality_passed') is True else '失败' if assessment.get('quality_passed') is False else '未完成'
            label = f"{item['round']}/{stage} " + notes.get('items', {}).get(str(item['round']), item['hypothesis'])
            rationale = notes.get('row_reasons', {}).get(label) or notes.get('attempt_reason') or item.get('rationale') or '原记录未提供依据'
            reason = decision.get('reason') or item.get('feedback', {}).get('reason', '')
            conclusion = status_names.get(item['status'], item['status'])
            if not metric and reason:
                conclusion += '：' + reason
            if decision.get('evidence_verified') is False:
                conclusion = '证据不完整：' + reason
            if decision.get('cases'):
                for case in decision['cases']:
                    case_label = label + ' [' + '/'.join(case.get('tasks',[])) + ':' + case['id'] + ']'
                    means = case.get('means')
                    value = f"{means['baseline']:.4g} → {means['candidate']:.4g} {metric['unit']}" if means and metric else '数值边界用例/未测量'
                    if means and metric and means['baseline'] > 0 and means['candidate'] > 0:
                        gain = (1-means['candidate']/means['baseline'])*100 if metric['direction']=='minimize' else (means['candidate']/means['baseline']-1)*100
                        value += f'；收益 {gain:.2f}%'
                    statuses = [x.get('numerical_status') for x in case.get('numerical',{}).values()]
                    case_quality = '通过' if len(statuses)==2 and all(x=='pass' for x in statuses) else '失败/未完成'
                    rows.append([case_label, notes.get('row_reasons',{}).get(case_label,rationale), value+'；数值：'+case_quality, conclusion+'（汇总判定）'])
            else:
                rows.append([label, rationale, timing + '；数值：' + quality, conclusion])
            if decision.get('evidence'): evidence.append(decision['evidence'])
        if item.get('patch'): evidence.append(item['patch'])
    for item in report.get('ablation', {}).get('rows', []):
        a = item.get('assessment', {}); m = a.get('metrics')
        label = f"消融/{item['stage']} {item['reference']} → {item['candidate']}"
        result = f"{m['baseline']:.4g} → {m['candidate']:.4g} {m['unit']}" if m else '未测量'
        if item.get('removed') and item.get('measured'):
            conclusion = f"该项组合内贡献 {item['marginal_gain_pct']:.2f}%"
        else:
            conclusion = item['verdict']
        rows.append([label, notes.get('row_reasons',{}).get(label, '验证组合收益及各项贡献'), result, conclusion])
        if item.get('decision'): evidence.append(item['decision'])
    def cells(values, tag):
        return '<tr>' + ''.join(f'<{tag}><p>{escape(str(v))}</p></{tag}>' for v in values) + '</tr>'
    if rows:
        parts.append('<table><thead>' + cells(['优化项','尝试原因','实测结果','结论'], 'th') + '</thead><tbody>' + ''.join(cells(row, 'td') for row in rows) + '</tbody></table>')
    else:
        paragraph('尚无完成的候选；状态：' + payload['status'])
    if notes.get('attempt_reason'):
        paragraph('尝试原因：' + notes['attempt_reason'])  # Preserve a manually supplemented legacy paragraph.
    if report.get('run'): evidence.insert(0, report['run'] + '/results.json')
    if evidence: paragraph('证据：' + '；'.join(dict.fromkeys(evidence)))
    paragraph('局部测量不代表真实请求或整模型收益；short E2E 不等于官方完整请求验收。')
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
        # Preserve manually edited cells independently of generated proposal rationale.
        prior = previous_receipt.get('row_reasons', {})
        notes = payload.setdefault('report', {}).setdefault('report_notes', {})
        for row in section.findall('.//tbody/tr'):
            if len(row) != 4:
                continue
            label, reason = [''.join(cell.itertext()) for cell in list(row)[:2]]
            if label not in prior or reason != prior[label] or label in notes.get('row_reasons', {}):
                notes.setdefault('row_reasons', {})[label] = reason
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
    signature = digest({'format': 4, 'document': document, 'content': content})
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
    result = {'status':'synced','digest':signature,'revision':verified.get('revision_id'),'format':4, 'row_reasons':row_reasons(payload),
              'document':document, 'attempt_reason':attempt_reason(payload),
              'attempt_reason_source':'preserved' if payload.get('report', {}).get('report_notes', {}).get('attempt_reason') else 'proposal'}
    write(receipt,result)
    return result
