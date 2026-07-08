---
name: python-engineering
description: Python engineering standards for Jasmehr's projects, especially ClipForge (video clip generation with ffmpeg). Use whenever writing, debugging, or extending Python code — scripts, CLI tools, subprocess/ffmpeg work, file processing, progress tracking, automation. Trigger for any .py file work.
---

# Python Engineering (ClipForge-first)

## Purpose
Consistent, production-grade Python matching Jasmehr's environment and conventions. Complements fable-5 (which governs process); this skill adds Python/ClipForge specifics.

## Environment facts (assume unless told otherwise)
- Windows 11 as primary OS: use pathlib everywhere, never hardcode "/" paths, careful subprocess quoting, shell=False
- ffmpeg invoked via subprocess for video work
- Logging via the logging module, never print() for status in library code
- Single-user CLI tools: fail loudly with clear messages, no silent fallbacks

## Workflow
1. Follow fable-5 rules first (read before write, root cause before fix).
2. ffmpeg tasks: build the argument list explicitly (list form, not shell string), log the full command at DEBUG, capture stderr, surface ffmpeg's actual error on failure.
3. Long-running work (encoding, batch clips): integrate with the existing progress tracking module; report via callbacks or the established ProgressTracker pattern, never bare prints.
4. Type hints on public functions; docstrings stating inputs, outputs, raised exceptions.
5. Every file/subprocess/network operation: explicit error handling with a decision (retry, recover, or fail with message).

## Rules
- pathlib.Path for all paths; .resolve() before passing to ffmpeg
- No bare except; catch specific exceptions
- Windows gotchas: file locks on open handles, long path limits, CRLF; mention when relevant
- Prefer stdlib; flag any new dependency before adding it

## Common mistakes to avoid
- Shell-string ffmpeg commands that break on spaces in filenames
- Swallowing ffmpeg stderr so failures look like "returncode 1" with no context
- print() progress that breaks when the tool later gets a GUI
- Assuming POSIX paths or forgetting Windows reserved filenames

## Quality checklist
- [ ] Runs on Windows without path issues
- [ ] ffmpeg failures show the underlying error
- [ ] Progress integrates with existing tracker
- [ ] fable-5 self-verification performed

## Integrates with
fable-5 (process discipline), jasmehr-context (global rules).
