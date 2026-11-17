"""The manga rule editor operates on one ordinary public post-translation table."""

import re
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from Backend.application.pipeline_common import PipelineError
from Backend.domain.config.tables import TableConfigHelper
from Shared.manga_lettering import DEFAULT_PUNCTUATION_RULES

TABLE_ID = str(uuid5(NAMESPACE_URL, "AiNiee/manga/punctuation"))


def get_state(config):
    tables = TableConfigHelper.get_tables(config, "post_translation")
    table = next((table for table in tables if table["id"] == TABLE_ID), None)
    return {
        "table_id": TABLE_ID,
        "enabled": bool(table and table["enabled"]),
        "rows": deepcopy(table["rows"] if table else DEFAULT_PUNCTUATION_RULES),
    }


def update_rules(config, enabled, rows=None, baseline=None):
    if rows is not None:
        for row in rows:
            if row.get("regex"):
                try:
                    re.compile(row["src"])
                    re.sub(row["src"], row["dst"], "")
                except (re.error, IndexError) as error:
                    raise PipelineError(
                        "INVALID_REPLACEMENT_RULE", f"替换正则无效：{error}", 422
                    ) from error
    tables = TableConfigHelper.get_tables(config, "post_translation")
    table = next((table for table in tables if table["id"] == TABLE_ID), None)
    if table is None:
        table = TableConfigHelper.new_table(
            "post_translation", "漫画标点替换", DEFAULT_PUNCTUATION_RULES, enabled
        )
        table["id"] = TABLE_ID
        tables.append(table)
    if rows is not None:
        table["rows"] = (
            TableConfigHelper.merge_edited_rows(
                "post_translation", baseline, rows, table["rows"]
            )
            if baseline is not None
            else TableConfigHelper.normalize_rows("post_translation", rows)
        )
    table["enabled"] = enabled
    config["post_translation_tables"] = tables
    # Do not touch any other table's rows, enable switch or active selection.
    return get_state(config)
