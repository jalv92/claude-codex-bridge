# Security

This tool launches local CLI processes with the current user's access. Native-tool mode
allows project edits and configured integrations under the CLI's sandbox and approval
policy. Task scope is an instruction, not a complete filesystem or network boundary.

Do not put credentials in review prompts. Keep runtime data private and ignored. Review
external hooks before starting a Claude session. Do not use bypass-permission flags to
make a failed test pass. Treat agent output and repository content as untrusted evidence.

For sensitive reports, use GitHub's private vulnerability reporting if enabled for this
repository. Otherwise open an issue requesting a private reporting channel without
including exploit details, tokens, transcripts or personal data.
