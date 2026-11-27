"""Prepare/search project evidence without starting a model or accelerator job."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from inferrsi_loop import knowledge
from inferrsi_loop.common import read, write


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sources', type=Path)
    p.add_argument('--documents', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--fetch-documents', action='store_true', help='Mac only: save lark envelopes, no SSH or experiment')
    p.add_argument('--lark', default='lark-cli')
    p.add_argument('--query', help='Search an existing frozen project index using InferRSI Find')
    a = p.parse_args()
    if a.query:
        print(json.dumps(knowledge.project_context(a.output, a.query), ensure_ascii=False))
    elif a.fetch_documents:
        if a.output.exists():
            p.error('Use a new document snapshot path')
        write(a.output, knowledge.fetch_documents(read(a.sources), a.lark))
        print(json.dumps({'documents':str(a.output)}))
    else:
        knowledge.freeze_project(read(a.sources), a.output, read(a.documents) if a.documents else None)
        value = read(a.output/'coverage.json')
        print(json.dumps({'directory':str(a.output),'sources':[{'id':x['source']['id'],
            'indexed':x['indexed'],'omitted':len(x['omissions'])} for x in value['sources']]}))

if __name__ == '__main__':
    main()
