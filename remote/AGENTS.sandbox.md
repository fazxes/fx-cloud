# fx-cloud sandbox

This machine is a Vercel Sandbox that works for one agent on the user's Mac. It is not the Mac. The agent there sends heavy commands and tasks here with `fx-cloud`.

- **Platform:** Linux x86_64 (Ubuntu). Builds and tests run here. Work that needs macOS, such as AppKit, Swift, Xcode, or the screen, cannot run here. Say so in your report; the Mac handles it.
- **Copies:** each folder from the Mac is here at the same path, and the Mac is its source of truth. Leave your changes uncommitted in the copy; the Mac takes them back with `fx-cloud pull --patch`. Do not commit, push, or reset in these copies.
- **Git:** there is no commit signing key. Commits and pull requests stay on the Mac.
- **fx-cloud:** it is not installed here. Where other rules say to run a command with `fx-cloud run`, run the command directly.
- **Lifetime:** this sandbox ends after a while without commands, or when the agent runs `fx-cloud done`. Its files and processes go with it. Anything that must stay goes back to the Mac with `fx-cloud pull`.
