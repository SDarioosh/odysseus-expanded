# Athena contract fixtures

Athena's MCP answers with `format="json"` (fictional seed data), copied from Athena's
`server/tests/contract/snapshots/` and regenerated there with `UPDATE_SNAPSHOTS=1`.
`tests/fork/test_athena_contract_fixtures.py` feeds them through the hub's parsers.

| File here | Athena snapshot | Call |
|---|---|---|
| `list_facts.json` | `mcp_list_facts__json.json` | `list_facts(format="json")` |
| `list_facts_confirmed.json` | `mcp_list_facts__json_confirmed.json` | `list_facts(confirmed=true, format="json")` |
| `list_action_items.json` | `mcp_list_action_items__json.json` | `list_action_items(format="json")` |
| `list_action_items_status_all.json` | `mcp_after_update_action_item__list_action_items_json.json` | `list_action_items(status="all", format="json")` after items 1 and 2 were set done and dismissed |
| `list_people.json` | `mcp_list_people__json.json` | `list_people(format="json")` |
| `list_people_after_merge.json` | `mcp_after_rename_merge__list_people_json.json` | `list_people(format="json")` after renaming Ben onto Alice (aliases) |

A PR pair that changes these tools' output refreshes the snapshots in Athena and these
copies here together.
