"""Offline whole-view and complete-controller-request geometry, never a recall run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
REPO = Path('/Users/bliss/dev/sibyl')
SOLVER = BASE / 'solver-bytecode-source-20260914/source'
INVENTORY = BASE / 'ordinary-full-cohort-workload-20260914/sources-92000-v1.jsonl'
ARCHIVE = BASE / 'sha256/8c597589c5ded445e40817b14458015741d9c09eb4919e11f90db8d3e01b3f15'
ASSETS = BASE / 'transfer-paid-continuation'
MATERIAL = BASE / 'transfer-final-launch/material/tasks'
TASKS = ('venue-capacity-report', 'rack-power-windows', 'inclusive-slot-occupancy',
         'hex-stream-journal', 'record-separator-channel', 'independent-message-decoder')
MEMORY_CEILING = 90_000
CONTEXT_GUARD = 204_800
BUDGET = {'input_tokens': 2_000_000, 'output_tokens': 8_000, 'tool_calls': 20, 'cost_usd': 2.0}
HEADER = ('Historical complete controller views. Reported outcomes are historical evidence, not '
          'guarantees. Resolve $ref through each view\'s values and $literal as literal object '
          'entries. Original audit and transport fields remain hash-bound outside these views.\n')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    if output.parent != ROOT or output.exists() or output.is_symlink():
        raise ValueError('Use a fresh direct child of this artifact lane')
    output.mkdir(mode=0o700)

    denied = []

    def audit(event, arguments):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'}:
            denied.append({'event': event, 'executable': str(arguments[0]) if event == 'subprocess.Popen' else None})
            raise PermissionError('External I/O is forbidden in recall-policy geometry')

    sys.addaudithook(audit)
    sys.path[:0] = [str(SOLVER), str(REPO / 'packages/python/sibyl-core/src'), str(REPO / 'apps/api/src')]
    from tokenizers import Tokenizer
    from jinja2.sandbox import ImmutableSandboxedEnvironment
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.episode_evidence import project_episode, encode_episode_views, episode_projection_receipt
    from benchmarks.agent_tasks.coding_controller import Controller, Usage
    import benchmarks.agent_tasks.coding_controller as controller_owner
    import sibyl_core.tasks.episode_evidence as projection_owner

    assert Path(controller_owner.__file__).resolve() == SOLVER / 'benchmarks/agent_tasks/coding_controller.py'
    assert file_sha(Path(projection_owner.__file__)) == 'fc0abb8b4de2d67757d4c7daceaf568ae25ef6bd33e6cb48679407e4ae3231d5'
    assert file_sha(INVENTORY) == '5d7670ac29958c1c1cce65aeb1872414d1288b571d90165aecc1d19c6942ba04'
    assert file_sha(ARCHIVE) == ARCHIVE.name
    assert file_sha(ASSETS / 'tokenizer.json') == '19564a48c4f71a2a1b937cce34c737a1e662b171c5f5d7edf641a15cd896f07d'
    tokenizer = Tokenizer.from_file(str(ASSETS / 'tokenizer.json'))
    config = json.loads((ASSETS / 'tokenizer_config.json').read_text())
    environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    environment.filters['tojson'] = lambda value, **kw: json.dumps(value, ensure_ascii=False, **kw)
    template = environment.from_string(config['chat_template'])

    def count(value):
        return len(tokenizer.encode(value, add_special_tokens=False).ids)

    def request(prompt, memory):
        controller = object.__new__(Controller)
        controller.request = {'controller_model': 'qwen/qwen3-coder-next', 'seed': 0,
                              'prompt': prompt, 'memory_pack': memory}
        controller.usage = Usage()
        controller._budget = dict(BUDGET)
        controller.options = SimpleNamespace(provider_only=False)
        body = controller._body(controller._messages(), BUDGET['output_tokens'])
        wire = json.dumps(body, allow_nan=False).encode()
        rendered = template.render(messages=body['messages'], tools=body['tools'], add_generation_prompt=True)
        return {'body_sha256': sha(wire), 'rendered_sha256': sha(rendered.encode()),
                'initial_request_tokens': count(rendered), 'memory_tokens': count(memory)}

    retained = {row['task_id']: row for row in map(json.loads, INVENTORY.read_text().splitlines())}
    assert len(retained) == 233 and len({r['source_id'] for r in retained.values()}) == 233
    source_bytes = {}
    with tarfile.open(ARCHIVE, 'r|*') as archive:
        for member in archive:
            if member.name.startswith('attempts/') and member.name.endswith('/signed-episode.bin'):
                task = member.name.split('/')[1]
                source_bytes[task] = archive.extractfile(member).read()
    assert source_bytes.keys() == retained.keys()
    rows = []
    blocks = {}
    for index, task in enumerate(sorted(retained)):
        row = retained[task]
        artifact = source_bytes.pop(task)
        assert sha(artifact) == row['source_sha256']
        projection = project_episode(row['source_id'], artifact, prefix=f's{index:03d}')
        assert len(projection.view['events']) == row['event_count']
        encoded_view = encode_episode_views([projection])
        encoded = canonical(encoded_view)
        block = f'<source id="{row["source_id"]}" sha256="{row["source_sha256"]}">\n{encoded}\n</source>\n'
        receipt = episode_projection_receipt([(row['source_id'], artifact)], [projection], encoded_view)
        blocks[row['source_id']] = block
        rows.append({'source_id': row['source_id'], 'training_task': task, 'training_family': row['family'],
                     'source_sha256': row['source_sha256'], 'revision': row['retained_revision'],
                     'fixture_observation': row['fixture_observation'], 'event_count': row['event_count'],
                     'complete_view_sha256': sha(encoded.encode()), 'complete_view_chars': len(encoded),
                     'complete_view_tokens': count(encoded), 'block_sha256': sha(block.encode()),
                     'block_tokens': count(block), 'singleton_pack_tokens': count(HEADER + block),
                     'projection_receipt': receipt})
        if len(rows) % 50 == 0:
            print(json.dumps({'sources_measured': len(rows)}), flush=True)
    assert len({r['training_family'] for r in rows}) == 20
    assert max(r['singleton_pack_tokens'] for r in rows) < MEMORY_CEILING
    source_json = json.dumps(rows, sort_keys=True, indent=2) + '\n'
    (output / 'source-geometry.json').write_text(source_json)

    # A source-only packing stress control. This order is not the recall policy.
    # Keep whole blocks; stop on the first overflow so no smaller-item preference is introduced.
    ordering = sorted(rows, key=lambda row: (-row['block_tokens'], row['source_id']))
    stress_memory = HEADER
    selected = []
    for row in ordering:
        candidate = stress_memory + blocks[row['source_id']]
        if count(candidate) > MEMORY_CEILING:
            break
        selected.append(row['source_id'])
        stress_memory = candidate
    omitted = [row['source_id'] for row in ordering if row['source_id'] not in selected]
    assert set(selected).isdisjoint(omitted) and len(selected) + len(omitted) == 233
    (output / 'source-only-stress-pack.txt').write_text(stress_memory)

    task_rows = []
    for task in TASKS:
        directory = MATERIAL / task
        prompt_bytes = (directory / 'prompt.md').read_bytes()
        prompt = prompt_bytes.decode()
        # Same fixed whitespace normalization as compile_context._query_for(..., None).
        query = ' '.join(prompt.strip().split())
        workspace_rows = []
        for name in ('app.py', 'application.py', 'resolver.py', 'public_checks.py'):
            content = (directory / 'workspace' / name).read_bytes()
            workspace_rows.append({'path': name, 'sha256': sha(content), 'tokens': count(content.decode())})
        workspace = sum(row['tokens'] for row in workspace_rows)
        empty = request(prompt, '')
        stress = request(prompt, stress_memory)
        singleton = [request(prompt, HEADER + blocks[row['source_id']])['initial_request_tokens'] for row in rows]
        total = stress['initial_request_tokens'] + workspace + BUDGET['output_tokens']
        assert total < CONTEXT_GUARD
        task_rows.append({'task_id': task, 'prompt_sha256': sha(prompt_bytes), 'query_sha256': sha(query.encode()),
                          'query': query, 'workspace': workspace_rows, 'workspace_tokens': workspace,
                          'no_memory': empty, 'source_only_stress': stress,
                          'all_233_singleton_initial_request_tokens_min': min(singleton),
                          'all_233_singleton_initial_request_tokens_max': max(singleton),
                          'stress_plus_workspace_plus_full_output': total,
                          'initial_fit_guard_tokens': CONTEXT_GUARD,
                          'actual_native_pack': None, 'actual_raw_recall_pack': None, 'actual_summary_pack': None})
    summary_families = sorted({row['training_family'] for row in rows})
    summary_frame = ('Historical family references. Each reference is a fallible condensation of '
                     'training evidence. Every retained training family is included.\n')
    for family in summary_families:
        summary_frame += f'<summary id="{family}" sha256="' + ('0' * 64) + '">\n\n</summary>\n'
    result = {'schema': 'sibyl-full-cohort-recall-geometry-v1', 'status': 'offline_source_geometry_only',
              'timestamp_utc': datetime.now(timezone.utc).isoformat(),
              'source_count': len(rows), 'training_families': len(summary_families),
              'source_geometry_sha256': sha(source_json.encode()),
              'source_tokens': {'min': min(r['complete_view_tokens'] for r in rows),
                                'max': max(r['complete_view_tokens'] for r in rows),
                                'sum': sum(r['complete_view_tokens'] for r in rows)},
              'max_singleton_pack_tokens': max(r['singleton_pack_tokens'] for r in rows),
              'proposed_memory_ceiling_tokens': MEMORY_CEILING,
              'proposed_retained_controller_budget': BUDGET,
              'source_only_stress': {'ordering': 'descending whole-block token size, source ID tie-break; geometry only, never retrieval',
                  'selected_source_ids': selected, 'omitted_source_ids': omitted,
                  'memory_sha256': sha(stress_memory.encode()), 'memory_tokens': count(stress_memory),
                  'not_an_actual_retrieval_selection': True},
              'summary_library': {'references': 20, 'proposed_each_token_ceiling': 4096,
                  'summed_separate_text_token_ceilings': 81920, 'empty_framing_reference_tokens': count(summary_frame),
                  'memory_headroom_above_summed_text_ceilings': MEMORY_CEILING - 81920,
                  'actual_complete_library_tokens': None,
                  'limit': 'Separate text counts plus empty framing are not a guaranteed BPE bound; count the actual complete library before acceptance.'},
              'repeat_budget_arithmetic': {'hypothetical_requests': 21,
                  'memory_ceiling_times_21': MEMORY_CEILING * 21,
                  'cumulative_input_remainder_before_other_messages': BUDGET['input_tokens'] - MEMORY_CEILING * 21,
                  'summary_text_ceilings_times_21': 81920 * 21,
                  'old_131072_ceiling_times_21': 131072 * 21,
                  'guarantees_all_20_tools_fit': False,
                  'limit': 'These are arithmetic planning terms, not provider usage forecasts. The unchanged controller repeats memory plus growing history, and stops when any reported budget binds.'},
              'tasks': task_rows, 'actual_recall_candidates_observed': False, 'current_source_authority_checked': False,
              'provider_calls': 0, 'database_connections': 0, 'new_reservations': 0, 'solver_dispatches': 0,
              'denied_external_events': denied,
              'bindings': {str(path): file_sha(path) for path in [ARCHIVE, INVENTORY, ASSETS / 'tokenizer.json',
                  ASSETS / 'tokenizer_config.json', Path(controller_owner.__file__), Path(projection_owner.__file__), Path(__file__)]}}
    (output / 'geometry.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('status', 'source_tokens', 'max_singleton_pack_tokens', 'summary_library', 'repeat_budget_arithmetic', 'denied_external_events')}, indent=2), flush=True)


if __name__ == '__main__':
    main()
