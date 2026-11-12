# CLAUDE.md

<!-- BEGIN MANAGED by meta: profile project; blocks language, documentation, user_specific, naming; sha256 3412d697026c. Do not edit up to END MANAGED: change the blocks in meta. -->
## Language

All content in git-tracked files is written in English:

- Documentation
- Code (identifiers, strings)
- Inline comments
- Commit messages, PR descriptions, issue text

## Documentation

Follow the "Documentation" section of `CONTRIBUTING.md`: which document
holds what, one home per topic, and which documents a code change must
update in the same change.

## User-specific information stays out of git

Never put user-specific or environment-specific information into git-tracked
files. This includes documentation, inline code comments, `CLAUDE.md`, and any
other tracked file.

That covers, for example:

- Personal, customer, or employer names and identifiers
- Absolute local paths, hostnames, usernames, machine names
- Credentials, tokens, API keys, account or tenant IDs
- Personal workflow preferences, schedules, contacts, private URLs

Where it goes instead:

| Destination | Use for |
| --- | --- |
| Memory (`~/.claude/projects/.../memory/`) | Durable facts about the user and their preferences |
| `CLAUDE.local.md` | User-specific instructions for Claude Code |
| `local/` | User-specific files, config, and fixtures |

`CLAUDE.local.md` and `local/` must stay untracked. Keep both listed in
`.gitignore` and never `git add -f` them. Before committing, check the diff for
anything user- or environment-specific and move it to one of the destinations
above.

Examples in tracked files must be generic. Never use names, paths, or
structures from `local/`, `CLAUDE.local.md`, or other untracked or
user-specific content as examples. Prefer neutral placeholders (e.g.
`data_models`, `example_service`) or names of tracked files.

## Naming conventions

- Directory names are always lowercase.
- No spaces in directory names.
- Use `_` as the word separator instead of `-` (e.g. `data_models`).
- Exception: skill directories under `skills/` use `-` as the word separator
  (e.g. `skills/example-skill/`). The Agent Skills specification allows only
  lowercase letters, digits and hyphens in a skill `name`, and the `name` must
  match its directory name.
<!-- END MANAGED -->

## Naming: bundled admin skills

The skill exception above also covers the admin skills bundled with
`graph_cli_admin`: their directories use `-` as the word separator, like
those under `skills/`.
