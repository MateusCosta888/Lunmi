import json

import pytest

from app.computer.prompt_encoding import decode_context, dumps, encode_context


def test_history_selects_only_successful_observation_elements_without_mutation():
    from app.computer.prompt_encoding import observation_history
    tree = {"elements": [{"id": "1", "name": "old", "type": "Edit"}],
            "view": "raw", "complete": False, "truncated": True, "errors": ["timeout"],
            "window": {"handle": 1, "pid": 2}}
    observed = {"action_string": "uia_observe(root_id='1')",
                "result": {"status": "success", "error": None, "result": tree}, "repeat_time": 2}
    failed = {**observed, "result": {"status": "error", "error": "provider", "result": tree}}
    other = {**observed, "action_string": "uia_verify(id='1')"}
    text = "[Action] uia_observe()\n[Status] success\n[Result] " + repr(tree)
    selected = observation_history([observed, failed, other, text])
    summary = selected[0]["result"]["result"]
    assert "elements" not in summary and summary["elements_omitted"] == 1
    assert summary["history_only"] and summary["errors"] == ["timeout"]
    assert summary["window"] == tree["window"] and not summary["complete"]
    assert selected[1:3] == [failed, other]
    assert "elements_omitted" in selected[3] and "'name': 'old'" not in selected[3]
    assert observed["result"]["result"] is tree and "elements" in tree
    assert decode_context(encode_context(selected)) == selected


def test_repeated_observation_is_lossless_and_smaller():
    text = '[Result] ' + 'UIA control and state ' * 1800
    original = [{'step': i, 'user_confirm': 'No' if i == 1 else 'Yes',
                 'actions': [{'result': text, 'error': 'not verified', 'arguments': {'text': 'á  b\n c'}}]}
                for i in range(8)]
    packed = encode_context(original)
    assert decode_context(json.loads(dumps(packed))) == original
    assert len(dumps(packed)) < len(dumps(original)) * 0.2
    assert original[1]['user_confirm'] == 'No'


def test_short_or_unique_context_is_untouched():
    for value in ([{'text': 'x'}] * 2, [{'text': 'x' * 500}], 'literal content'):
        assert encode_context(value) == value


def test_user_data_cannot_spoof_dictionary_references():
    value = [{'$prompt_text_ref': 0, 'result': 'x' * 800}] * 3
    assert encode_context(value) == value


@pytest.mark.parametrize('result', [
    [{'id': str(i), 'name': 'n' + str(i), 'bounds': (1, 2, 3, 4)} for i in range(12)],
    [{i: 'n' * 60} for i in range(12)],
    [{'text': b'x' * 60} for _ in range(12)],
    [{'text': {'x' * 60}} for _ in range(12)],
    [{'$prompt_rows': ['name'], 'rows': [['x' * 60]]} for _ in range(12)],
])
def test_python_result_unknown_shapes_stay_literal_through_json(result):
    original = [{'action': 'uia_observe\n[Result] ' + repr(result)}]
    assert encode_context(original) == original
    assert decode_context(json.loads(dumps(encode_context(original)))) == original


def test_json_safe_result_rows_reconstruct_exactly_after_transport():
    result = [{'id': str(i), 'name': 'á ' + str(i), 'type': 'Button',
               'enabled': True, 'offscreen': False} for i in range(24)]
    original = [{'action': 'uia_observe\n[Result] ' + repr(result)}]
    packed = encode_context(original)
    assert len(dumps(packed)) < len(dumps(original))
    assert decode_context(json.loads(dumps(packed))) == original


def test_vendor_enum_representation_is_summarized_without_losing_status_or_scope():
    from app.computer.prompt_encoding import observation_history
    tree = {"elements": [{"id": "1", "name": "old", "type": "Edit"}],
            "view": "control", "complete": False, "truncated": True}
    action = "[Action] uia_observe()\n[Status] ResultStatus.SUCCESS\n[Result] " + repr(tree)
    result = observation_history(action)
    assert "elements_omitted" in result and "'name': 'old'" not in result
    assert "[Status] ResultStatus.SUCCESS" in result
    failure = action.replace("ResultStatus.SUCCESS", "ResultStatus.FAILURE")
    assert observation_history(failure) == failure
