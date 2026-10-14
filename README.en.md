# Project Cloud Space

A Windows desktop tool for archiving inactive project files to Baidu Netdisk. The UI and detailed documentation are currently in Chinese.

Drag files into a project's `云空间` folder. The app uploads them sequentially, downloads the full content to verify size and SHA-256, uploads and verifies a cloud recovery index, and only then releases the local originals.

**Archiving removes the local originals after verification. Start with disposable test files. Data and indexes in the same Netdisk account are not independent backups.**

## Real-world example: 23.77 GiB to 5.58 GiB locally

On 2026-10-13, a personal development project containing firmware, server and hardware-test workspaces used the optional `project-slimming` skill to propose a cleanup. The user approved the actions before execution.

| Metric | Before | After |
|---|---:|---:|
| Local project logical file size | **~23.77 GiB** | **~5.58 GiB** |

This run reduced local logical file size by approximately **76.5%**:

- **Direct cleanup:** 18,260 Python cache and selected host-test output files, about **200.64 MiB**. Corresponding source files were present; caches can grow back.
- **Archive and release:** duplicate dependencies were consolidated into two distinct snapshots, and historical builds were grouped by experiment. **12 independent ZIP files totaled ~1.56 GiB**. After verification, 33 directories containing 241,456 original files (~18.01 GiB) were released. This archive batch saved **~17.99 GiB net**, accounting for retained local manifests and verification materials.

Source code, Git history, uncommitted tracked changes, primary development dependencies, the server environment, and the recordings and test evidence excluded from this batch were retained. Historical workspaces need their dependency snapshots restored before rebuilding.

A pilot archive was uploaded, restored and checked against all **52 member hashes** before the full batch. All 12 production archives passed local member verification, full cloud readback and index verification before originals were released. HEAD and tracked content were unchanged across the 24 repositories checked for that batch. Recovery mappings and instructions were retained locally.

These anonymized figures come from one real session and its local verification records. They measure logical file sizes, not physical disk allocation or a guaranteed saving. No full-project restore drill, application runtime test or historical environment rebuild was performed. The Agent handled deduplication, packaging and approved cleanup outside the watched folder; the app handled uploads, readback, indexes and release of the files it managed.

## Requirements and quick start

**Newly created applications have not been validated.** The 2026-10-14 fresh-computer acceptance used an existing Baidu application and verified archives under `/项目云空间/` at the Netdisk root. The tester reported a Baidu notice that new applications default to an application-specific directory, but no new-application comparison was completed; consult your Baidu console for the applicable permissions. This version uses a fixed `/项目云空间/` root and does not yet support applications restricted to their own directory. Such applications may be unable to archive with this release. Start with disposable test files, and do not manually relocate existing cloud data or indexes.

- Windows 10/11 x64, Python 3.12.
- Your own Baidu Netdisk developer application and account authorization. API eligibility and quotas are controlled by Baidu.
- Run `setup.cmd`, then `start.cmd`.
- Use the “网盘授权…” button to enter your own application credentials and complete Baidu's authorization-code flow.
- Credentials are encrypted for the current Windows user with DPAPI, outside the source directory. No shared application secret or token is shipped.

Whole-project recovery scans cloud indexes and does not need the original computer's local archive records. Files are restored to their original relative paths and SHA-256 verified. Repeating recovery skips matching files and preserves conflicting local files.

The app must remain open for automatic uploading. It supports serial queues, bounded retries and rate-limit cooldowns. It does not provide virtual placeholders, cross-platform operation, automatic decompression or cross-restart chunk resume.

Agent commands include `status`, `cloud-projects`, `backup-indexes` and `recover-project`. See the [Chinese documentation](README.md) for installation, recovery and operational details.

An optional [project-slimming skill](skills/project-slimming/SKILL.md) provides project disk analysis, tiered cleanup proposals and archive grouping/size recommendations. Copy its folder into your Codex skills directory and invoke `$project-slimming` in the target project. It defaults to read-only analysis; packaging, moving and deleting follow the user's approved scope. The skill and its reports are primarily in Chinese. See the [installation and invocation instructions](README.md#可选让-agent-分析项目瘦身).

Source: MIT, copyright 2026 old-li-tou. Dependencies retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). This is an independent project, not an official Baidu product.
