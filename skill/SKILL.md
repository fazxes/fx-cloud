---
name: fx-cloud
description: Run builds, tests, and other heavy commands in a Vercel Sandbox with `fx-cloud run`. Use before any build, test suite, benchmark, fuzz run, or other long or CPU-heavy command.
---

# fx-cloud

Put `fx-cloud run` in front of heavy commands, and run it from the project folder:

```sh
fx-cloud run zig build test
fx-cloud run npm test
fx-cloud run 'cd tests/e2e && bun test'
```

Output and the exit code come back as if the command ran here.

Keep these on the Mac: git, file edits, file reads, short scripts, and anything that needs macOS or the screen.

- A command made a file you need here: `fx-cloud pull <path>`
- The sandbox changed files and `fx-cloud run` says so: `fx-cloud pull --patch --apply`
- You are finished with the folder: `fx-cloud done`

If fx-cloud says it is not set up, tell the user to run `fx-cloud init`.
