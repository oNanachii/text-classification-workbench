#!/usr/bin/env python3
"""Dry-run by default. --live explicitly sends ONE authored example to TypeSafe."""
import argparse
import asyncio
import getpass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from decision import DecisionService, DecisionSettings, MODEL, PROMPT_SHA256, valid_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly make one potentially billed TypeSafe request; prompts securely for a key')
    parser.add_argument('--sample-id', default='authored-ham-01')
    args = parser.parse_args()
    fixture = json.loads((ROOT / 'examples/jev_authored_sms.json').read_text())
    examples = {row['id']: row for row in fixture['examples']}
    if args.sample_id not in examples:
        parser.error('Unknown authored sample ID')
    row = examples[args.sample_id]
    result = {'mode': 'live' if args.live else 'dry-run', 'data_version': fixture['data_version'],
              'sample_id': row['id'], 'model_version': MODEL, 'prompt_sha256': PROMPT_SHA256,
              'expected_label': row['expected_label'], 'quality_evaluation': False}
    if not args.live:
        result.update({'network_requests': 0, 'status': 'request_prepared_only'})
        print(json.dumps(result, indent=2))
        return 0
    if not sys.stdin.isatty():
        print('Live verification requires an interactive terminal for secure key entry.', file=sys.stderr)
        return 2
    key = getpass.getpass('TypeSafe Key (sends ONE authored English SMS and may consume quota): ')
    if not valid_key(key):
        print('Invalid credential format.', file=sys.stderr)
        return 2
    try:
        decision = asyncio.run(DecisionService(DecisionSettings(True, False)).jev(row['text'], key))
    finally:
        key = None
    result.update({name: decision.get(name) for name in ('status', 'error_code', 'label', 'elapsed_ms', 'usage', 'outcome_unknown')})
    result['matches_authored_expectation'] = decision['label'] == row['expected_label'] if decision['status'] == 'ok' else None
    print(json.dumps(result, indent=2))
    return 0 if decision['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
