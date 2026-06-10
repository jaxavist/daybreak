# Jira Automation Remediation Scripts

Scripts for auditing and fixing Jira Cloud automation rules after DC→Cloud migration. These tools help identify and correct automation rules that still reference Data Center environment IDs (custom fields, projects, statuses, users, portal URLs).

---

## Overview

When migrating Jira from Data Center to Cloud, automation rule IDs often become invalid because:
- **Custom field IDs** change (e.g., `customfield_10101` → `customfield_10147`)
- **Project IDs** change (e.g., `13100` → `10034`)
- **Status IDs** change (e.g., `10000` → `10061`)
- **User IDs** change (DC usernames → Cloud account IDs)
- **JSM portal URLs** change (portal IDs and paths differ)

These scripts automatically scan, audit, and fix automation rules to use correct Cloud IDs.

---

## Prerequisites

- Python 3.8+
- `requests` library (`pip install requests`)

---

## Environment Setup

### Cloud Authentication

```bash
export ATLASSIAN_EMAIL="your-email@company.com"
export ATLASSIAN_API_TOKEN="your-api-token"
```

### Data Center (optional, for dynamic mapping)

```bash
export DC_API_TOKEN="your-dc-personal-access-token"
```

---

## Scripts

### 1. **compare_and_fix.py** — Compare Environments & Fix Automations

**Purpose:** Compare custom fields, statuses, and project IDs between two Jira Cloud environments (or DC↔Cloud). Builds a mapping and fixes automation rules on the target site.

**Usage:**

```bash
# Compare Sandbox vs Production (no automation changes):
python3 compare_and_fix.py --compare \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "YOUR_API_TOKEN"

# Audit target automations for source IDs:
python3 compare_and_fix.py --audit \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "YOUR_API_TOKEN"

# Fix automations on target (with confirmation):
python3 compare_and_fix.py --fix \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "YOUR_API_TOKEN"

# Dry run (preview changes):
python3 compare_and_fix.py --fix --dry-run \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "YOUR_API_TOKEN"

# DC as source (needs separate DC PAT):
python3 compare_and_fix.py --audit \
    --source-site jira-test.daybreakgames.com --source-is-dc \
    --target-site daybreakgames.atlassian.net \
    --token "CLOUD_TOKEN" --dc-token "DC_PAT"

# Export mapping to JSON:
python3 compare_and_fix.py --compare \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "YOUR_API_TOKEN" --export mapping.json
```

**Output:**
- Detailed report of field/status/project ID changes
- List of ambiguous matches requiring manual review
- Audit of automation rules with bad references
- Fixed automation rules (on confirmation)

---

### 2. **audit_fix_automations.py** — Dynamic Field Mapping & Automation Fix

**Purpose:** Dynamically fetches all custom fields from both DC and Cloud APIs, builds a complete DC→Cloud field mapping by matching on name + type, then scans and fixes automation rules on Cloud.

**Usage:**

```bash
# Audit only (find bad field references):
python3 audit_fix_automations.py --audit --token "CLOUD_TOKEN"

# Fix automations (with confirmation):
python3 audit_fix_automations.py --fix --token "CLOUD_TOKEN"

# Dry run (preview changes):
python3 audit_fix_automations.py --fix --dry-run --token "CLOUD_TOKEN"

# Scan all projects (not just ITSM/SSSD):
python3 audit_fix_automations.py --audit --all --token "CLOUD_TOKEN"

# Target one specific rule:
python3 audit_fix_automations.py --audit --rule-uuid "abc-123-def" --token "CLOUD_TOKEN"

# Use custom DC token (Bearer auth):
python3 audit_fix_automations.py --fix --dc-token "YOUR_DC_PAT" --token "CLOUD_TOKEN"

# Skip DC fetch (Cloud-only mode):
python3 audit_fix_automations.py --audit --skip-dc --token "CLOUD_TOKEN"
```

**Output:**
- Mapping of DC fields to Cloud equivalents (with ambiguities flagged)
- Report of automation rules with bad field references
- Fixed automation rules (on confirmation)

---

### 3. **fix_automation_ids.py** — Offline JSON Fixer

**Purpose:** Apply all DC→Cloud ID replacements (custom fields, projects, statuses, users, URLs, portals) to an exported automation rules JSON file. No API calls — runs offline on a JSON export.

**Usage:**

```bash
# Fix automation rules JSON (writes to *_corrected.json):
python3 fix_automation_ids.py automation-rules.json

# Fix with custom output path:
python3 fix_automation_ids.py automation-rules.json automation-rules-fixed.json
```

**ID Mappings (hardcoded):**
- Custom fields (10 mappings)
- Projects (3 mappings: ITSM, SSSD, FSD)
- Statuses (24 mappings)
- Users (65+ named users + JIRAUSER IDs)
- URLs (Jira and Confluence domain replacements)
- Portals (JSM portal ID replacements)

**Output:**
- Corrected JSON with all replacements applied
- Summary of replacement counts by category
- ⚠ Warning for unmapped references (JIRAUSER IDs, legacy URLs, etc.)

---

### 4. **map_jsm_portal_urls.py** — JSM Portal URL Mapping

**Purpose:** Map Data Center JSM portal/request-type URLs to Cloud equivalents via REST API. Helps identify correct portal IDs and request type IDs for automation rule updates.

**Usage:**

```bash
# Query Cloud and optionally resolve DC portal names:
export JIRA_CLOUD_SITE=daybreakgames.atlassian.net
export JIRA_EMAIL=you@daybreakgames.com
export JIRA_API_TOKEN=...
export JIRA_DC_SITE=jira.daybreakgames.com  # optional

python3 map_jsm_portal_urls.py
```

**Output:**
- List of all Cloud service desks (portals) with IDs
- List of Cloud request types per service desk
- Suggested DC → Cloud URL replacements
- Portal ID mappings with notes

---

## Workflow Example

### Scenario: Audit and fix automation rules after DC→Cloud migration

**Step 1: Compare environments and build mapping**

```bash
python3 compare_and_fix.py --compare \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "$ATLASSIAN_API_TOKEN" \
    --export mapping.json
```

**Step 2: Audit automations on target (Cloud)**

```bash
python3 audit_fix_automations.py --audit \
    --token "$ATLASSIAN_API_TOKEN"
```

**Step 3: Preview fixes with dry-run**

```bash
python3 compare_and_fix.py --fix --dry-run \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "$ATLASSIAN_API_TOKEN"
```

**Step 4: Apply fixes**

```bash
python3 compare_and_fix.py --fix \
    --source-site daybreakgames-sandbox.atlassian.net \
    --target-site daybreakgames.atlassian.net \
    --token "$ATLASSIAN_API_TOKEN"
```

**Step 5: Verify (run audit again)**

```bash
python3 audit_fix_automations.py --audit \
    --token "$ATLASSIAN_API_TOKEN"
```

---

## Offline Mode: Using fix_automation_ids.py

If you prefer to work with an exported JSON file:

1. **Export** automation rules from Cloud Jira (`System` → `Automation` → `Export`)
2. **Run** the offline fixer:
   ```bash
   python3 fix_automation_ids.py automation-rules.json
   ```
3. **Review** the corrected JSON and warnings
4. **Import** the corrected JSON back into Cloud Jira

---

## Troubleshooting

### "UNKNOWN_MEDIA_ID" or JSON decode errors

- Ensure your API token is valid and has automation rule permissions
- For DC access, confirm your Personal Access Token is correct

### Ambiguous field matches

- Multiple fields match the same name in Cloud
- Manually review and specify the correct mapping, or use type-based filtering

### Unmapped users or IDs

- `fix_automation_ids.py` will warn about unmapped JIRAUSER IDs, service accounts, etc.
- Manually update these IDs before re-importing

### Rule still has bad references after fix

- Some IDs may not be in the mapping (e.g., custom fields created after migration)
- Manually inspect the rule in Cloud Jira and update as needed

---

## Notes

- **Always use `--dry-run` first** to preview changes before applying fixes
- **Backup automation rules** before running fix operations
- **Test on a sandbox first** (e.g., Sandbox → Production workflow)
- **Review warnings** for unmapped IDs; these may need manual intervention
- All scripts are **read-only by default**; use `--fix` or provide output path to modify

---

## Support

For issues or questions:
1. Check the script's docstring (`python script.py --help`)
2. Review the workflow example above
3. Enable dry-run mode to preview changes safely
