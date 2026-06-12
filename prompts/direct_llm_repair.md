# Direct LLM Repair Baseline Prompt

You are repairing a known flaky test.

Input includes test code, logs, and basic context. Generate a minimal unified diff.

Do not receive or infer an explicit stability intent before patching.

Return raw JSON only. Do not wrap the answer in Markdown. Do not include explanation outside the JSON.
If you cannot identify a safe minimal patch, still return the JSON object with an empty `patch` and explain why in `repair_rationale`.

Patch format requirements:

- The `patch` string must be a complete unified diff, not a headerless hunk.
- Include `diff --git a/<path> b/<path>`, `--- a/<path>`, `+++ b/<path>`, and `@@ ... @@` lines.
- For test-only repairs, use `patch_instructions.primary_target_file` as `<path>`.

```json
{
  "patch": "...",
  "repair_rationale": "...",
  "safety_notes": ["..."]
}
```

Safety constraints still apply: do not skip tests, delete tests, delete core assertions, or only add arbitrary sleep/timeout.
