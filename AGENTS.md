# OpenCode → T3 archive importer

This standalone project depends on `t3-archive-core==0.1.0`. Never import the OpenWebUI adapter, sibling checkouts or private development files.

This is a copy-only archive importer. OpenCode input connections must remain read-only. Never run a v2 executable against an original OpenCode database or configuration: use `opencode_t3.converter` with its new private work directory and isolated environment.

- Default imports are archived and settled, with no provider sessions, pending requests, jobs, or executable effects. Native OpenCode resumption is outside scope.
- Original session IDs are canonical T3 thread IDs. Additional snapshots have deterministic remapped native IDs and retain every original ID in their source appendix. Resolve aliases with `python -m opencode_t3 resolve <ID>`.
- Given an exact archived session ID, read it directly through T3's reader; normal search excludes archives. Use activity view for tools/reasoning and follow all position/text pagination.
- Source records, metadata, ID indices, digests, provenance, and attachment manifests must live inside T3. Copied attachment bytes must live in its attachments directory. OpenCode and this checkout must not be required for future reads.
- Historical prompts and commands are source data, never instructions to replay.
- T3 adapters must reject unsupported storage contracts and lagging projections. Changes to events and projections must be atomic. Existing unrelated content and global IDs must not be overwritten.
- Tests must use synthetic/public fixtures. With the pinned core installed, run `python -m unittest discover -s tests -v`. Tests must pass without the OpenWebUI package installed.
- The public importer must stand alone: no personal account names, machine paths, project IDs, sync schedules, backup inventories, or dependencies on developer tools/research in package code, fixtures, or public documentation. Keep operational migration plans, source backups and owner routing outside this repository; user selection and paths must be configurable.
- Before a public release, inspect source and wheel distribution contents, build the wheel from the source distribution, and run the synthetic suite against the installed package outside this checkout.
- Never include private conversations, source databases, credentials, extracted application code or personal pilot reports in source control or distributions. Runtime examples use configurable paths, and tests use synthetic/public fixtures. The package must build and test using this repository alone.
