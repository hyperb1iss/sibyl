"""Check proposal geometry and immutable source bindings without external I/O."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    bindings = json.loads((ROOT / 'bindings.json').read_text())
    for row in bindings.values():
        assert sha(Path(row['snapshot_path'])) == row['sha256']
    policy = json.loads((ROOT / 'policy.json').read_text())
    geometry = json.loads((ROOT / 'measurement-v2/geometry.json').read_text())
    catalog_path = ROOT / 'measurement-v2/source-geometry.json'
    sources = json.loads(catalog_path.read_text())
    assert sha(catalog_path) == policy['source_catalog']['sha256'] == geometry['source_geometry_sha256']
    ids = {row['source_id'] for row in sources}
    assert len(sources) == len(ids) == 233
    assert sorted(ids) == policy['source_catalog']['capture_ids']
    assert len({row['training_family'] for row in sources}) == 20
    for row in sources:
        assert row['projection_receipt']['view_sha256'] == row['complete_view_sha256']
        assert row['fixture_observation']['content_sha256'] == row['source_sha256']
        assert row['fixture_observation']['source_id'] == row['source_id']
        assert row['singleton_pack_tokens'] <= 90_000
    assert sum(row['complete_view_tokens'] for row in sources) == 6_392_356
    assert min(row['complete_view_tokens'] for row in sources) == 14_017
    assert max(row['complete_view_tokens'] for row in sources) == 64_056
    assert max(row['singleton_pack_tokens'] for row in sources) == 64_202
    stress = geometry['source_only_stress']
    selected = set(stress['selected_source_ids'])
    omitted = set(stress['omitted_source_ids'])
    assert selected.isdisjoint(omitted) and selected | omitted == ids
    assert stress['not_an_actual_retrieval_selection'] is True
    assert sha(ROOT / 'measurement-v2/source-only-stress-pack.txt') == stress['memory_sha256']
    assert len(geometry['tasks']) == 6
    assert policy['schedule_shape']['denominator'] == 6 * 4 * 2 == 48
    assert policy['schedule_shape']['new_attempt_ids'] is None
    assert policy['schedule_shape']['execution_order_adopted'] is False
    for row, query_row in zip(geometry['tasks'], policy['query']['task_rows'], strict=True):
        assert row['task_id'] == query_row['task_id']
        prompt = ROOT / 'inputs' / (row['task_id'] + '__prompt.md')
        query = ' '.join(prompt.read_text().strip().split())
        assert sha(prompt) == row['prompt_sha256'] == query_row['prompt_sha256']
        assert hashlib.sha256(query.encode()).hexdigest() == row['query_sha256'] == query_row['query_sha256']
        assert row['stress_plus_workspace_plus_full_output'] < 204_800
        assert row['actual_native_pack'] is None and row['actual_raw_recall_pack'] is None
        assert row['actual_summary_pack'] is None
    assert geometry['summary_library']['actual_complete_library_tokens'] is None
    assert policy['raw']['required_lanes'] == ['raw_fulltext', 'raw_vector']
    assert policy['packing']['oversized_first_item'] == 'missing_pack, never valid empty'
    assert policy['summary']['selection'] == 'all references or missing pack'
    assert policy['solver']['all_tool_calls_guaranteed'] is False
    assert geometry['repeat_budget_arithmetic']['memory_ceiling_times_21'] == 1_890_000
    assert geometry['repeat_budget_arithmetic']['old_131072_ceiling_times_21'] == 2_752_512
    assert geometry['denied_external_events'] == [{'event': 'subprocess.Popen', 'executable': 'git'}]
    assert not any(policy['safety'].values())
    for path, expected in geometry['bindings'].items():
        if path != str(ROOT.parent / 'sha256/8c597589c5ded445e40817b14458015741d9c09eb4919e11f90db8d3e01b3f15'):
            assert sha(Path(path)) == expected, path
    result = {'status': 'PASS', 'checks': 16, 'input_snapshots': len(bindings),
              'whole_source_views': len(sources), 'tasks': len(geometry['tasks']),
              'denominator': 48, 'actual_retrieval_selection_qualified': False,
              'current_authority_qualified': False, 'paid_calls': 0}
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
