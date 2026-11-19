"""Read the effective approval policy of a local Codex desktop chat."""

import json
import os
from pathlib import Path
from uuid import UUID

from ledger import message


def runtime_check(chat_id):
    try:
        chat_id = str(UUID(chat_id))
        codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        transcripts = list((codex_home / 'sessions').glob(f'*/*/*/rollout-*-{chat_id}.jsonl'))
        if len(transcripts) != 1:
            return message('runtime_unverified')
        policy = None
        with transcripts[0].open(encoding='utf-8') as transcript:
            for line in transcript:
                row = json.loads(line)
                if row.get('type') == 'turn_context':
                    policy = row['payload'].get('approval_policy')
    except (TypeError, ValueError, AttributeError, KeyError, OSError):
        return message('runtime_unverified')

    if policy == 'never':
        return message('runtime_unattended', approval_policy=policy)
    if policy in ('on-request', 'on-failure', 'untrusted', 'granular'):
        return message('runtime_requires_approval', approval_policy=policy)
    return message('runtime_unverified')
