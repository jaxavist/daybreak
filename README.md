# Jira Automation Migration Toolkit

Comprehensive tooling for migrating Jira automation rules from Data Center to Cloud. Built for the Daybreak Games DC→Cloud migration, reusable for any JCMA migration engagement.

---

## Primary Tool

### `migrate_automations.py` — All-in-One Migration Script

Handles the full automation migration lifecycle: pre-migration classification, ID remapping, post-migration validation, repair, project rescoping, and bulk enablement.

**Requirements:**
```
Python 3.8+
pip install requests
```

---

## Modes

### Pre-Migration

| Mode | Purpose |
|---|---|
| `--classify FILE` | Classify DC export rules by JCMA migration support level (fully/partially/unsupported) |
| `--compare` | Compare field/status/project/issue type/resolution/link type IDs between DC and Cloud |

### Post-Migration (Live Cloud API)

| Mode | Purpose |
|---|---|
| `--audit` | Scan Cloud automation rules for stale DC IDs |
| `--fix` | Apply DC→Cloud ID mappings to live rules via API |
| `--repair` | Apply manual override mappings for known gaps |
| `--validate` | Check all rules against Cloud registries (fields, statuses, projects, users, URLs) |
| `--enable-clean` | Bulk-enable rules that pass validation |
| `--auto-rescope` | Fix project scoping by matching rule names against DC export |
| `--rescope` | Move rules matching a name filter from one project to another |
| `--purge` | Delete all automation rules on target site |

### Offline JSON Processing

| Mode | Purpose |
|---|---|
| `--json-fix FILE` | Remap all IDs in an exported automation JSON before importing |

---

## Quick Start

### 1. Pre-Migration: Classify Rules

Export automation rules from DC (Jira Admin > System > Automation > Export), then:

```bash
python3 migrate_automations.py --classify PROD_EXPORT.json \
    --source-site jira.example.com --source-is-dc \
    --target-site jira.example.com \
    --dc-token "$DC_PAT" --token "$CLOUD_PAT" \
    --export classification_results.json
```

### 2. Post-JCMA Migration: Validate

After JCMA migrates the automations (all rules arrive disabled on Cloud):

```bash
# Validate against Cloud registries
python3 migrate_automations.py --validate \
    --target-site example.atlassian.net \
    --token "$CLOUD_PAT"

# Fix any remaining DC ID references
python3 migrate_automations.py --fix \
    --source-site jira.example.com --source-is-dc \
    --target-site example.atlassian.net \
    --dc-token "$DC_PAT" --token "$CLOUD_PAT"

# Repair known manual overrides
python3 migrate_automations.py --repair \
    --target-site example.atlassian.net \
    --token "$CLOUD_PAT"
```

### 3. Verify Project Scoping

If JCMA doesn't preserve project scoping correctly:

```bash
python3 migrate_automations.py --auto-rescope --dry-run \
    --source-site jira.example.com --source-is-dc \
    --target-site example.atlassian.net \
    --dc-token "$DC_PAT" --token "$CLOUD_PAT" \
    --dc-rules-json PROD_EXPORT.json
```

### 4. Bulk-Enable Clean Rules

```bash
python3 migrate_automations.py --enable-clean \
    --target-site example.atlassian.net \
    --token "$CLOUD_PAT"
```

---

## What It Maps

| Category | Method |
|---|---|
| Custom Fields | Fetched from DC and Cloud APIs, matched by name + type |
| Statuses | Fetched and matched by name + category |
| Projects | Fetched and matched by project key |
| Issue Types | Fetched and matched by name |
| Resolutions | Fetched and matched by name |
| Link Types | Fetched and matched by name |
| Users | Embedded DC username → Cloud account ID mapping |
| Portals | Matched by project key, replaced in `/portal/N/` URL patterns |
| URLs | Domain string replacement (DC → Cloud) |
| Cloud IDs | UUID replacement in ARI strings |
| Webhooks | DC object format → Cloud flat string conversion |
| Emails | Clears DC `from`/`replyTo` fields for Cloud default sender |

---

## Structural Fixes

Beyond ID remapping, the script handles DC→Cloud format differences:

- **`webhookUrl`**: Converts DC object format `{"key":"url"}` to Cloud flat string `"url"`
- **Email `from` fields**: Removes DC-specific sender addresses so Cloud uses defaults
- **ScriptRunner rules**: Filters out rules with `com.onresolve.jira.groovy.groovyrunner` actions
- **DC-format project references**: Handles both `"projectId":"ID"` (DC) and `project/ID` (Cloud ARI) patterns
- **Issue type/resolution context**: Replaces IDs only in correct field-type contexts to prevent false matches

---

## JCMA Migration Support Classification

The `--classify` mode categorizes rules per [Atlassian's JCMA documentation](https://support.atlassian.com/migration/docs/how-to-migrate-automation-rules/):

| Level | Meaning |
|---|---|
| **Fully Supported** | Migrates completely, works the same on Cloud |
| **Partially Supported** | Migrates but may behave differently — review after migration |
| **Unsupported** | Won't migrate — needs manual recreation on Cloud |

Common unsupported step types:
- ScriptRunner Groovy actions
- Microsoft Teams webhook messages
- Send Web Request / Outgoing Webhook
- Archive/Restore work item triggers

---

## Options Reference

```
--source-site       DC or Cloud source site URL
--target-site       Cloud target site URL
--source-is-dc      Source is Data Center (uses Bearer auth)
--token             Cloud API token
--dc-token          DC Personal Access Token
--email             Email for Cloud Basic auth (default: jkane@adaptavist.com)
--dry-run           Preview changes without applying
--export FILE       Save mapping or classification to JSON
--dc-rules-json     DC automation export JSON (for --auto-rescope)
--bridge-site       DC site for name resolution when source env is gone
--bridge-is-dc      Bridge site is Data Center
```

---

## Legacy Scripts

These were earlier iterations, preserved for reference:

| Script | Purpose |
|---|---|
| `fix_automation_ids.py` | v1: Offline JSON fixer with hardcoded DC→S1 mappings |
| `audit_fix_automations.py` | v3: Dynamic field mapping and Cloud API audit/fix |
| `compare_and_fix.py` | v4: Cross-environment comparison (Cloud-to-Cloud) |
| `map_jsm_portal_urls.py` | JSM portal URL mapping utility |

---

## Additional Files

| File | Purpose |
|---|---|
| `automation_post_migration_cleanup.html` | Confluence runbook subpage with post-migration action items |

---

## Lessons Learned

1. **Use JCMA for ID mapping** — it handles fields, statuses, projects, users, issue types, resolutions, and link types natively. Manual JSON export/import bypasses this and causes ID collisions.
2. **Project scoping** — JCMA should preserve project scoping. If it doesn't, `--auto-rescope` with the DC export JSON can fix it, but the Cloud API may not support scope changes via PUT (may require re-import).
3. **Flow actors are not migrated** — set them manually on Cloud post-migration.
4. **Email from addresses** — Cloud import auto-populates a default prefix. Accept or configure in Cloud settings.
5. **Microsoft Teams webhooks** — unsupported by JCMA. Reconfigure using Cloud-native Teams integration.
6. **JEditor Description bug** — post-migration, reset Description renderer in JSM Field Configurations (JSDCLOUD-11377).
