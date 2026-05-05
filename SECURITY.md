# Security Policy

## Supported Versions

FastStack currently provides security updates for version **1.6.3 and earlier**.

| Version | Supported |
| ------- | --------- |
| 1.6.3 and earlier | :white_check_mark: |
| Unreleased development branches | :white_check_mark: |

When a future release is published, this table may be updated to describe which release lines continue to receive security fixes.

## Reporting a Vulnerability

Please report security vulnerabilities by opening public GitHub issue or PR.

Include:

- A clear description of the vulnerability.
- Steps to reproduce the issue.
- The affected FastStack version or commit.
- The operating system and Python version used.
- Whether the issue can lead to code execution, file deletion, data exposure, unsafe subprocess execution, or other user impact.
- Any proof-of-concept files or commands needed to reproduce the issue.

## Response Expectations

I will try to acknowledge valid vulnerability reports within 7 days.

If the vulnerability is accepted, I will work on a fix and may ask for additional reproduction details. Security fixes may be applied to supported versions when practical.

If the report is declined, I will explain why, for example if the behavior is not security-sensitive, cannot be reproduced, or depends on unsupported usage.

## Disclosure Policy

Once a fix is available, the vulnerability will be documented in release notes.

## Scope

Security issues may include, but are not limited to:

- Unsafe handling of external executable paths.
- Unsafe subprocess invocation.
- File deletion or recycle-bin behavior that could affect unintended files.
- Loading crafted image, metadata, sidecar, or configuration files in a way that causes code execution, data loss, or disclosure.

General bugs, crashes, UI problems, and performance issues should be reported as GitHub issues.
