# Spike folder

Research notes and experimental scripts. **Use the `cursorscope` CLI for day-to-day analysis.**

## Research

- **`RESEARCH.md`** — public schema and attribution notes (anonymized)
- **`RESEARCH.local.md`** — machine-specific case study (**gitignored**)
- **`snapshots/`** — local DB copies and JSON reports (**gitignored**)

## Destructive ops (experimental)

`prune_repos.py` mutates live `state.vscdb`. It is **not** part of the supported read-only CLI.

```bash
# Preview (read-only; also in main CLI)
cursorscope plan-remove --repo "/path/to/old-project"

# Dry-run (default)
python spike/prune_repos.py --repo "/path/to/old-project"

# Execute (quit Cursor first)
python spike/prune_repos.py --repo "/path/to/old-project" --execute --confirm DELETE
```

Backups: `~/.cursorscope/backups/` (newest kept by default after prune).  
Audit logs: `~/.cursorscope/history/prune-*.json`.

```bash
cursorscope cleanup --backups              # remove all prune backups
cursorscope cleanup --backups --keep-backups 1
```

## New experiments

Add ad-hoc scripts here when probing unknown Cursor storage shapes. When logic stabilizes, move it into `cursorscope/` and remove the spike copy.
