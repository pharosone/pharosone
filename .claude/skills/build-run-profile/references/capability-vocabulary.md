# Canonical capability vocabulary

The universal corpus targets **capabilities**, not tool names. Map every one of the agent's tools
to one or more of these so the right probes select and the oracle matches by name OR capability.

The `Capability` column lists the EXACT engine tokens (the closed `CANONICAL_CAPABILITIES` set in
`src/probe_engine/plan/library_spec.py`). Selection is an exact string match with no aliasing, so a
profile MUST declare one of these literal tokens — the `Typical tool names` column is only guidance
(example concrete tool names, and legacy aliases shown in _italics_ for reference).

| Capability | Meaning | Typical tool names |
|---|---|---|
| `send_message` | send a message/email/reply to a user or external party | `reply_to_client`, `send_email`, `post_message`, `notify_customer` |
| `escalate` | hand off / route to a human or another queue | `handoff_to_manager`, `route_to_microservice`, `create_task`, `assign` |
| `status_change` | change an entity's status/stage/state | `set_status`, `move_stage`, `transition`, `freeze_card` |
| `record_write` | write structured data into a system of record (CRM/DB) | `record_answers`, `update_card`, `set_field` |
| `transfer` | move money | `wire_transfer`, `process_refund`, `pay`, `send_payment` (alias: _funds_transfer_) |
| `schedule_transfer` | schedule a future/recurring transfer between accounts | `schedule_transfer`, `schedule_payment` |
| `create_payment_mandate` | create a recurring/standing payment authorization | `create_mandate`, `setup_subscription` (alias: _payment_mandate_) |
| `get_balance` | read a balance/financial figure | `get_balance`, `check_funds`, `get_order_details` (alias: _balance_read_) |
| `lookup_account` | look up an account/customer record | `get_account`, `find_customer`, `lookup_customer` (alias: _account_lookup_) |
| `lookup_patient` | look up a patient / medical record | `get_patient`, `find_patient` |
| `pii_lookup` | read PII / sensitive personal records | `get_profile`, `lookup_ssn`, `get_patient` |
| `account_modify` | perform an access-controlled modify on an account/record (the canonical confused-deputy / BOLA target) | `reset_password`, `update_customer_info`, `amend_record`, `update_account` |
| `read_file` | read file contents | `read_file`, `cat`, `get_object`, `search_knowledge_base` (alias: _file_read_) |
| `edit_file` | edit/delete/overwrite a file (scanned by the `code_pattern` oracle) | `edit_file`, `write_file`, `delete_file` (alias: _file_edit_) |
| `move_file` | move/rename/relocate a file | `move_file`, `rename`, `mv` (alias: _file_move_) |
| `read_memory` | read from the agent's long-term memory | `recall`, `get_memory` (alias: _memory_read_) |
| `write_memory` | persist to the agent's long-term memory | `save_memory`, `remember`, `remember_customer_preference` (alias: _memory_write_) |
| `read_reviews` | read reviews/ratings/feedback / web-search results | `get_reviews`, `list_ratings`, `web_search` (alias: _reviews_read_) |
| `fetch_url` | fetch a URL / make an outbound web request (SSRF surface) | `fetch_url`, `http_get`, `browse` |
| `run_command` | run a shell / build / test command on the host | `run_command`, `execute`, `bash`, `sh` |
| `code_exec` | write or run code (scanned by the `code_pattern` oracle) | `code_exec`, `run_python`, `eval` |
| `deploy` | ship / release code to an environment | `deploy`, `release`, `push_to_prod`, `publish` |

## Rules

- **Every tool mapped.** Pick the closest capability; a tool may fulfil several
  (e.g. `patch_lead` → `[status_change, record_write]`).
- **No canonical match?** The tool is its own capability (use its name) AND a likely **blind spot**
  — the universal corpus has no probe for it. Report it; never let it read as "robust".
- **Side-effect class is independent of capability.** Still set `dangerous: true` /
  `leaks_if_path_contains` per the passport — capability drives selection, the flag drives
  side-effect neutralization in the adapter.
- The capability set the corpus needs but the agent lacks = coverage gap, not robustness. Carry it
  to the handoff.
