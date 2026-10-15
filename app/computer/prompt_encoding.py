"""Reversible prompt encoding, not a cache of decisions or a history truncation."""
from __future__ import annotations

import ast
from collections import Counter
from typing import Any

import json

REFERENCE_KEY = "$prompt_text_ref"
FORMAT = "prompt-text-dictionary-v1"


def observation_history(value: Any) -> Any:
    """Keep historical UIA scope/errors, never resend its stale element tree.

    This is explicit context selection before lossless encoding. The current live
    control budget remains in the AppAgent prompt; tool results/logs are unchanged.
    """
    def summary(result: Any) -> Any:
        if not isinstance(result, dict) or not isinstance(result.get("elements"), list):
            return result
        if not {"view", "complete", "truncated"} <= result.keys():
            return result
        return {**{key: child for key, child in result.items() if key != "elements"},
                "elements_omitted": len(result["elements"]), "history_only": True}

    if isinstance(value, list):
        return [observation_history(child) for child in value]
    if isinstance(value, dict):
        selected = dict(value)
        if str(value.get("action_string", "")).startswith("uia_observe("):
            result = value.get("result")
            if isinstance(result, dict) and result.get("status") == "success" and not result.get("error"):
                selected["result"] = {**result, "result": summary(result.get("result"))}
        return {key: observation_history(child) for key, child in selected.items()}
    if isinstance(value, str) and value.startswith("[Action] uia_observe(") and "\n[Result] " in value:
        prefix, literal = value.rsplit("\n[Result] ", 1)
        status = next((line.removeprefix("[Status] ") for line in prefix.splitlines()
                       if line.startswith("[Status] ")), "")
        if status not in {"success", "ResultStatus.SUCCESS"} or "[Error] " in prefix:
            return value
        try:
            result = ast.literal_eval(literal)
        except (ValueError, SyntaxError, RecursionError):
            return value
        return prefix + "\n[Result] " + repr(summary(result))
    return value


def encode_context(value: Any) -> Any:
    """Replace repeated long strings by explicit references, only if this saves bytes.

    Each exact value has a definition, including UIA results, errors and confirmation.
    Unknown shapes/short payloads pass through. A reserved key collision declines
    encoding, so literal user data can never be confused with a reference.
    """
    def reserved(item: Any) -> bool:
        if isinstance(item, dict):
            return any(str(key).startswith('$prompt_') for key in item) or any(
                reserved(child) for child in item.values())
        return isinstance(item, list) and any(reserved(child) for child in item)

    if reserved(value):
        return value
    original = value
    repetitions: Counter[str] = Counter()

    def collect(item: Any) -> None:
        if isinstance(item, str):
            repetitions[item] += 1
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            for child in item.values():
                collect(child)

    collect(value)

    def structural(item: Any) -> Any:
        if isinstance(item, str) and repetitions[item] > 1:
            return item
        if isinstance(item, str) and len(item) > 512 and '\n[Result] ' in item:
            prefix, result = item.rsplit('\n[Result] ', 1)
            try:
                parsed = ast.literal_eval(result)
            except (ValueError, SyntaxError, RecursionError):
                return item
            # Repr may contain tuples or non-string dict keys. JSON transport
            # changes those types even though literal_eval itself was reversible.
            # Unknown representations and reserved marker data stay literal.
            if repr(parsed) != result or reserved(parsed):
                return item
            try:
                transported = json.loads(dumps(parsed))
            except (TypeError, ValueError, OverflowError):
                return item
            if repr(transported) != result:
                return item
            return {'$prompt_representation': prefix, 'result': structural(parsed)}
        if isinstance(item, list):
            children = [structural(child) for child in item]
            if len(children) >= 8 and all(isinstance(child, dict) for child in children):
                columns = list(children[0])
                if all(list(child) == columns for child in children):
                    return {'$prompt_rows': columns,
                            'rows': [[child[key] for key in columns] for child in children]}
            return children
        if isinstance(item, dict):
            if any(key.startswith('$prompt_') for key in item):
                return item
            return {key: structural(child) for key, child in item.items()}
        return item

    value = structural(value)
    counts: Counter[str] = Counter()
    collision = False

    def count(item: Any) -> None:
        nonlocal collision
        if isinstance(item, str) and len(item) >= 256:
            counts[item] += 1
        elif isinstance(item, list):
            for child in item:
                count(child)
        elif isinstance(item, dict):
            collision = collision or REFERENCE_KEY in item
            for child in item.values():
                count(child)

    count(value)
    texts = [text for text, copies in counts.items() if copies > 1]
    if collision:
        return original
    if not texts:
        encoded = {'encoding': FORMAT,
                   'instructions': 'Expand $prompt_rows as dictionaries zipped with rows; $prompt_representation preserves the action prefix plus its structured Result.',
                   'texts': [], 'data': value}
        return encoded if len(dumps(encoded)) < len(dumps(original)) else original
    indices = {text: index for index, text in enumerate(texts)}

    def replace(item: Any) -> Any:
        if isinstance(item, str) and item in indices:
            return {REFERENCE_KEY: indices[item]}
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        return item

    encoded = {"encoding": FORMAT,
               "instructions": "Expand $prompt_text_ref using texts[index], $prompt_rows as dictionaries zipped with rows, and $prompt_representation as its action prefix plus structured Result. No evidence was omitted.",
               "texts": texts, "data": replace(value)}
    return encoded if len(dumps(encoded)) < len(dumps(original)) else original


def decode_context(value: Any) -> Any:
    """Reconstruct the original values for contract tests and diagnostics."""
    if not isinstance(value, dict) or value.get('encoding') != FORMAT:
        return value
    texts = value['texts']

    def expand(item: Any) -> Any:
        if isinstance(item, dict) and set(item) == {REFERENCE_KEY}:
            return texts[item[REFERENCE_KEY]]
        if isinstance(item, dict) and set(item) == {'$prompt_rows', 'rows'}:
            return [dict(zip(item['$prompt_rows'], expand(row), strict=True)) for row in item['rows']]
        if isinstance(item, dict) and set(item) == {'$prompt_representation', 'result'}:
            return item['$prompt_representation'] + '\n[Result] ' + repr(expand(item['result']))
        if isinstance(item, dict):
            return {key: expand(child) for key, child in item.items()}
        if isinstance(item, list):
            return [expand(child) for child in item]
        return item

    return expand(value['data'])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
