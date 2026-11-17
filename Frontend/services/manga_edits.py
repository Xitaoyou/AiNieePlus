"""Small reversible field changes, addressed by stable page/region IDs."""

from copy import deepcopy


def value_at(document, path):
    value = document
    for key in path:
        if value is None:
            return None
        if isinstance(key, tuple):
            if key[0] == "order":
                return [item["id"] for item in value]
            value = next((item for item in value if item["id"] == key[1]), None)
        else:
            value = value.get(key)
    return value


def changes(before, after, path=()):
    if before == after:
        return []
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and before.keys() == after.keys()
    ):
        return [
            change
            for key in before
            if key not in {"revision", "stages"}
            for change in changes(before[key], after[key], path + (key,))
        ]
    if (
        isinstance(before, list)
        and isinstance(after, list)
        and all(isinstance(v, dict) and "id" in v for v in before + after)
    ):
        old, new = ({v["id"]: v for v in values} for values in (before, after))
        result = []
        for key in old.keys() | new.keys():
            locator = path + (("id", key),)
            if key not in old or key not in new:
                result.append((locator, deepcopy(old.get(key)), deepcopy(new.get(key))))
            else:
                result.extend(changes(old[key], new[key], locator))
        if list(old) != list(new):
            result.append((path + (("order",),), list(old), list(new)))
        return result
    return [(path, deepcopy(before), deepcopy(after))]


def apply(document, edits, forward=True):
    for path, before, after in edits:
        value = deepcopy(after if forward else before)
        current = document
        for key in path[:-1]:
            if isinstance(key, tuple):
                current = next((v for v in current if v["id"] == key[1]), None)
            else:
                current = current.get(key) if current is not None else None
            if current is None:
                break
        if current is None:
            continue
        key = path[-1]
        if isinstance(key, tuple) and key[0] == "order":
            rank = {key: i for i, key in enumerate(value)}
            current.sort(key=lambda item: rank.get(item["id"], len(rank)))
        elif isinstance(key, tuple):
            current[:] = [item for item in current if item["id"] != key[1]]
            if value is not None:
                current.append(value)
        else:
            current[key] = value
