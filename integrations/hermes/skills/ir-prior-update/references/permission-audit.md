# Permission checks

Use this reference for an access or launcher failure, not as a mandatory full audit before every command.

1. Confirm the local user and the ping owner are Zhuxiangrong. Use the stable project path from SKILL.md; resolve symlinks before diagnosing access. Old deployment paths and cross-account instructions are historical.
2. Check the failing file's actual owner, every parent in its real path, relevant ACL masks, mount read-only status and available space. Read access to a database does not establish write access to its parent or private artifacts.
3. Owner0600 files, owner0700 directories and an owner-only socket are expected for the same UID. Do not loosen permissions, recreate cross-account ACL grants, switch accounts, remove locks or change scientific configuration as a workaround.
4. Direct stages need their state/log/metadata/storage/release directories plus launcher caches. Even a metadata preview may need container runtime directories; distinguish a launcher failure from a scientific-stage failure.
5. If an artifact belongs to another UID, preserve it and report the exact path and owner/admin action required. Return whatever trusted read-only status is available, marking what could not run or be verified. Do not read credentials to investigate filesystem access.
