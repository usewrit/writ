"""Delete-path index-resolution mirror for the coordinator's data endpoints.

DELETE /workflows/{id}/data resolves (run_id, record_index) refs through
services/extracted_data_table.pop_extracted_slots — the same stored-slot mapping
the flatten's coercion emits (DATA_REDESIGN_SPEC.md 1.5(6)). Pure, no DB: the
router only adds grouping/loading/commit around this function. Exercises the
coordinator's OWN service copy (a byte-identical twin of the backend's,
guarded by coordinator/tests/test_twin_service_identity.py).
"""
from services import extracted_data_table as edt


def _table_rd():
    return {"extracted_data": [
        ["Store", "Date", "Net Sales"],
        ["34008", "Jun 06, 2026", "$38.75"],
        ["34008", "Jun 07, 2026", "$12.00"],
    ]}


def test_pop_table_row_keeps_header():
    rd = _table_rd()
    assert edt.pop_extracted_slots(rd, [2]) == 1
    assert rd["extracted_data"] == [
        ["Store", "Date", "Net Sales"],
        ["34008", "Jun 06, 2026", "$38.75"],
    ]


def test_pop_table_only_header_left_nulls_extracted_data():
    rd = _table_rd()
    assert edt.pop_extracted_slots(rd, [1, 2]) == 2
    assert rd["extracted_data"] is None  # a lone header row is no data


def test_pop_multi_list_preserves_sibling_dataset():
    rd = {"extracted_data": {
        "workflows": [{"name": "wf1"}, {"name": "wf2"}],
        "targets": [{"name": "tg1"}],
    }}
    # sorted-key expansion order: targets (global 0), workflows (global 1, 2).
    assert edt.pop_extracted_slots(rd, [1]) == 1
    assert rd["extracted_data"] == {
        "workflows": [{"name": "wf2"}],
        "targets": [{"name": "tg1"}],
    }


def test_pop_multi_list_never_clears_all():
    rd = {"extracted_data": {
        "workflows": [{"name": "wf1"}],
        "targets": [{"name": "tg1"}, {"name": "tg2"}],
    }}
    assert edt.pop_extracted_slots(rd, [0, 1]) == 2
    assert rd["extracted_data"] == {"workflows": [{"name": "wf1"}], "targets": []}


def test_uid_resolution_maps_to_all_versions():
    # The DELETE record_uids path: resolve in the same pass the mutation uses.
    class _When:
        def __init__(self, iso):
            self._iso = iso

        def isoformat(self):
            return self._iso

    class _Task:
        def __init__(self, rid, at, extracted):
            self.id = rid
            self.completed_at = _When(at)
            self.created_at = None
            self.status = "completed"
            self.success = True
            self.duration_ms = None
            self.trigger_context = None
            self.result_data = {"extracted_data": extracted}

    tasks = [
        _Task(2, "2026-06-11T08:00:00Z", {"posts": [{"url": "https://k/a", "t": "A2"}]}),
        _Task(1, "2026-06-10T08:00:00Z", {"posts": [{"url": "https://k/a", "t": "A1"}]}),
    ]
    uid = edt.record_uid(["url"], {"url": "https://k/a"})
    # key pins the identity the uids were computed under (FE echoes it back).
    by_run, resolved, unmatched, identity = edt.resolve_record_uids(
        tasks, [uid, "f" * 16], key="url"
    )
    assert resolved == {uid: 2}
    assert unmatched == ["f" * 16]
    assert by_run == {1: [0], 2: [0]}
    assert identity == {"mode": "explicit", "fields": ["url"]}
