# CLAUDE.md

## Language

All content in git-tracked files is written in English:

- Documentation
- Code (identifiers, strings)
- Inline comments
- Commit messages, PR descriptions, issue text

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
