"""Three-way merge: user edits win only fields they actually changed."""

from copy import deepcopy


def merge_edits(base, local, remote):
    if local == base:
        return deepcopy(remote)
    if isinstance(base, dict) and isinstance(local, dict) and isinstance(remote, dict):
        result = deepcopy(remote)
        for key in base.keys() - local.keys():
            result.pop(key, None)
        for key, value in local.items():
            if key in {"revision", "stages"}:
                continue
            if key not in base:
                result[key] = deepcopy(value)
            elif value != base[key]:
                result[key] = merge_edits(base[key], value, remote.get(key))
        return result
    if all(isinstance(v, list) for v in (base, local, remote)) and all(
        isinstance(i, dict) and "id" in i
        for values in (base, local, remote)
        for i in values
    ):
        originals, edited, latest = (
            {i["id"]: i for i in values} for values in (base, local, remote)
        )
        result = []
        for item in remote:
            key = item["id"]
            if key in originals and key not in edited:
                continue
            result.append(
                merge_edits(originals[key], edited[key], item)
                if key in originals and key in edited
                else deepcopy(edited.get(key, item))
            )
        for item in local:
            if item["id"] not in latest and (
                item["id"] not in originals or item != originals[item["id"]]
            ):
                result.append(deepcopy(item))
        # Reordering during a job is an edit too. Keep remote-only additions,
        # while retaining the order chosen for the user's surviving pages/boxes.
        if [i["id"] for i in local if i["id"] in originals] != [
            i["id"] for i in base if i["id"] in edited
        ]:
            by_id = {i["id"]: i for i in result}
            result = [by_id[i["id"]] for i in local if i["id"] in by_id] + [
                i for i in result if i["id"] not in edited
            ]
        return result
    return deepcopy(local)
