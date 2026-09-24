# Contributing

Use the locked uv environment and run the lint, format, typing, test, and build commands in
the README. Default tests must remain offline, deterministic, and free of credentials.

Keep changes small and document protocol assumptions with dated primary sources. Add focused
regressions for parsing, leakage, or false acceptance bugs. Any change to pattern identity
requires a new schema/pattern version and explicit artifact compatibility handling.

Do not commit provider URLs with credentials, keys, local datasets, model artifacts, or
unverified benchmark claims. Small invented fixtures must be labeled synthetic. Preserve
failure cases and explain metric denominators when reporting evaluation results.
