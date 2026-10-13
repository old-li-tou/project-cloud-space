# Project Cloud Space

A Windows desktop tool for archiving inactive project files to Baidu Netdisk. The UI and detailed documentation are currently in Chinese.

Drag files into a project's `云空间` folder. The app uploads them sequentially, downloads the full content to verify size and SHA-256, uploads and verifies a cloud recovery index, and only then releases the local originals.

**Archiving removes the local originals after verification. Start with disposable test files. Data and indexes in the same Netdisk account are not independent backups.**

## Requirements and quick start

- Windows 10/11 x64, Python 3.12.
- Your own Baidu Netdisk developer application and account authorization. API eligibility and quotas are controlled by Baidu.
- Run `setup.cmd`, then `start.cmd`.
- Use the “网盘授权…” button to enter your own application credentials and complete Baidu's authorization-code flow.
- Credentials are encrypted for the current Windows user with DPAPI, outside the source directory. No shared application secret or token is shipped.

Whole-project recovery scans cloud indexes and does not need the original computer's local archive records. Files are restored to their original relative paths and SHA-256 verified. Repeating recovery skips matching files and preserves conflicting local files.

The app must remain open for automatic uploading. It supports serial queues, bounded retries and rate-limit cooldowns. It does not provide virtual placeholders, cross-platform operation, automatic decompression or cross-restart chunk resume.

Agent commands include `status`, `cloud-projects`, `backup-indexes` and `recover-project`. See the [Chinese documentation](README.md) for installation, recovery and operational details.

Source: MIT, copyright 2026 old-li-tou. Dependencies retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). This is an independent project, not an official Baidu product.
