# Intent-Only Repair Baseline Prompt

You are repairing a known flaky test.

Input includes test code, logs, basic context, an inferred stability intent, and optionally a stability specification. Do not request additional intent-guided context.

Generate a minimal unified diff that restores the stability intent.

Return raw JSON only. Do not wrap the answer in Markdown. Do not include explanation outside the JSON.
If the provided stability intent points to a concrete code location or assertion, attempt a concrete minimal unified diff. Return an empty `patch` only when the target source location is unavailable or every plausible change would violate the safety constraints; explain the missing evidence in `repair_rationale`.
The target flaky method is `sample.test_method`. Do not patch another test method in the same file.
If `stability_specification` is present, the patch must satisfy its `stability_spec`, use one allowed patch transform, and avoid all forbidden transforms.

Patch format requirements:

- The `patch` string must be a complete unified diff, not a headerless hunk.
- Include `diff --git a/<path> b/<path>`, `--- a/<path>`, `+++ b/<path>`, and numeric `@@ -old_start,old_count +new_start,new_count @@` hunk headers.
- For test-only repairs, use `patch_instructions.primary_target_file` as `<path>`.
- Anchor the patch to exact lines in `sample.test_code`.
- Prefer one small hunk inside `sample.target_method_code`.
- Use `sample.target_method_numbered_code` only to infer hunk line numbers; do not include line-number prefixes in the patch.
- If the needed removed lines do not appear exactly in `sample.target_method_code`, return an empty patch instead of patching a different method.
- Do not edit unrelated whitespace or add helper methods/imports unless required and already supported by the visible context.
- Do not output placeholder hunk headers such as `@@ -<line_number>,<old_lines> +... @@`.
- Do not output a patch that only changes whitespace or formatting.

```json
{
  "patch": "...",
  "repair_rationale": "...",
  "safety_notes": ["..."]
}
```
