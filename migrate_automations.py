#!/usr/bin/env python3
"""
Daybreak Games — DC-to-Cloud Automation Migrator (v5.1)
=======================================================
Compares Jira IDs between environments and fixes automation rules.

Supports:
  - Live API scanning and patching on Cloud
  - Offline JSON file processing for export/import workflows
  - Bridge mode: when source env is gone, uses DC as a name bridge
    with the old migration mapping to resolve S1 IDs → S2 IDs
  - Custom field, status, project, portal, URL, and Cloud ID remapping

Usage:
    # Compare DC vs Cloud (both environments accessible):
    python3 migrate_automations.py --compare \
        --source-site jira-test.daybreakgames.com --source-is-dc \
        --target-site daybreakgames-sandbox-2.atlassian.net \
        --dc-token "$DC_PAT_JIRA" --token "$CLOUD_JIRA_PAT"

    # Fix a JSON file when source is gone (bridge mode):
    python3 migrate_automations.py --json-fix exported_rules.json \
        --target-site daybreakgames-sandbox-2.atlassian.net \
        --bridge-site jira-test.daybreakgames.com --bridge-is-dc \
        --dc-token "$DC_PAT_JIRA" --token "$CLOUD_JIRA_PAT"

    # Fix live automations on Cloud:
    python3 migrate_automations.py --fix \
        --source-site jira-test.daybreakgames.com --source-is-dc \
        --target-site daybreakgames-sandbox-2.atlassian.net \
        --dc-token "$DC_PAT_JIRA" --token "$CLOUD_JIRA_PAT"

Requirements:
    pip install requests
"""

import json
import re
import sys
import os
import copy
import argparse
import requests
from collections import defaultdict


# ═══════════════════════════════════════════════════════════════
# Sandbox 1 → DC Reverse Mapping (from first migration)
# These map Sandbox 1 IDs back to DC IDs for fields that changed.
# Fields NOT listed here had the same ID on both DC and S1.
# ═══════════════════════════════════════════════════════════════

S1_TO_DC_FIELDS = {
    "customfield_10147": "customfield_10101",   # Teams
    "customfield_10133": "customfield_10102",   # Stakeholders
    "customfield_10136": "customfield_10203",   # Platforms
    "customfield_10034": "customfield_11002",   # Request participants
    "customfield_10010": "customfield_11003",   # Customer Request Type
    "customfield_10003": "customfield_11008",   # Approvers
    "customfield_10140": "customfield_12479",   # Change start date
    "customfield_10131": "customfield_12480",   # Change completion date
    "customfield_10165": "customfield_13411",   # Shared Services Teams
    "customfield_10332": "customfield_15300",   # Waiting On
}

S1_TO_DC_STATUSES = {
    "10061": "10000",   # To Do
    "10042": "10001",   # Done
    "10066": "10002",   # In Review
    "10109": "10207",   # Fixed
    "10062": "10300",   # Blocked
    "10044": "11403",   # Waiting for support
    "10045": "11404",   # Waiting for customer
    "10046": "11405",   # Pending
    "10047": "11407",   # Escalated
    "10048": "11408",   # Canceled
    "10043": "11409",   # Declined
    "10050": "11410",   # Awaiting approval
    "10051": "11411",   # Planning
    "10052": "11412",   # Awaiting implementation
    "10053": "11413",   # Implementing
    "10054": "11414",   # Peer review / approval
    "10055": "11415",   # Work in progress
    "10056": "11416",   # Completed
    "10057": "11417",   # Under investigation
    "10058": "11418",   # Under review
    "10049": "12103",   # Final Review & Approval
    "11199": "13000",   # Invoice Processing
}

S1_TO_DC_PROJECTS = {
    "10034": "13100",   # ITSM
    "10041": "13202",   # SSSD
    "10049": "13200",   # FSD
}

# Manual overrides for IDs that can't be resolved by name matching
# (DC IDs that leaked into S1 JSON, or name mismatches between DC and Cloud)
MANUAL_FIELD_OVERRIDES = {
    "customfield_11003": "customfield_10010",   # Customer Request Type (DC name) = Request Type (Cloud name)
    "customfield_10117": "customfield_10035",   # Request participants (S1 ID leaked through bridge mode)
}

MANUAL_STATUS_OVERRIDES = {
    "11415": "10003",   # Work in progress (DC ID leaked through first migration)
    "10146": "10147",   # Fulfilled (S1 ID → S2 ID, off-by-one)
    "10126": "10127",   # Order Placed (S1 ID → S2 ID, off-by-one)
}

S1_CLOUD_ID = "37b586d4-858b-41c8-914a-274e219122cd"

S1_PORTAL_MAP = {
    "1": "6",    # ITSM: S1 portal 1 → DC portal 6
    "34": "9",   # SSSD: S1 portal 34 → DC portal 9
    "67": "7",   # FSD:  S1 portal 67 → DC portal 7
}

# DC Username → Cloud Account ID mapping (global across all Cloud sites)
DC_USER_MAP = {
    "hhuynh":         "712020:fc282b81-8689-4043-99a7-96cc35f46fe2",
    "dscaduto":       "557058:1f9060d1-9046-450a-ad12-125a25d4c02d",
    "csavage":        "70121:05dfd084-d344-476f-9bee-c71e8ab163c7",
    "gbjornsson":     "70121:60720f73-2415-437f-85b7-d9e9d9221213",
    "ewebb":          "620bdee1bba9ca0070ca6cf6",
    "nbeaton":        "640f48aeb05b4e3e7da8509f",
    "rtruong":        "60e87f2384c992007197221d",
    "jchan":          "70121:1df21e23-e8fb-40dc-9d28-59e1900cad71",
    "smelton":        "557058:9fe9d10e-4cae-4f0e-9744-3ca2719a50a4",
    "jfermo":         "712020:f7933b9b-d3df-482b-8a87-b816f6f6a27d",
    "rmase":          "712020:de93cb27-e833-40d0-a16e-d5efd044f170",
    "rwager":         "712020:f2cd3229-21eb-4c99-ae0f-ae9b9f90c194",
    "smcwherter":     "712020:a94490f7-6569-4124-803a-eab34678d784",
    "dgonzales":      "70121:8ac9f2ee-fe08-4b12-a368-2357007294e9",
    "rkline":         "712020:8548b2a8-d837-45af-8654-00289e3711e9",
    "tpettigrew":     "712020:6bb07dba-ccb1-46b0-968e-943b4e16b6cf",
    "fbaecker":       "5bc63308c8f90064f0caa59a",
    "csickels":       "712020:40e79083-f95c-4f79-89a9-1f7163f61b42",
    "jfox":           "712020:b0cd0dd0-8d14-4e11-a215-1b32f065f890",
    "hfung":          "712020:4d1b0cfd-22c6-4c8e-9cf0-da0ff02506ec",
    "jcracchiolo":    "712020:7e053196-4952-4db7-b3cd-d12089f16493",
    "rciccolini":     "712020:c0e74257-251b-4649-a861-c09c613770d9",
    "dyoussefi":      "712020:a28fa742-2683-4a25-bd35-3a7d0e63c5ac",
    "sluciani":       "712020:98f7cbf8-aade-4608-a8e1-fde5129dcf24",
    "eramos":         "712020:17331283-8caf-4163-8dac-305dee2c8cac",
    "jlauterwasser":  "712020:675354e1-b883-4ea5-8643-2b9c72b75575",
    "ahuse":          "712020:2e5c5ed1-f332-4be8-b35d-714eed8fb592",
    "ptighe":         "712020:83340abe-168e-4384-9b47-2ac47f74ccff",
    "jfloyd":         "70121:9fbe2efb-bfe8-4455-b5f7-8658216313a5",
    "zilyes":         "712020:cfcb419b-aec4-476e-bc60-2f9abde7df5c",
    "tsaiyed":        "712020:3a4994c8-35b4-4608-b057-24577a2951d6",
    "jconrado":       "557058:c6c9c668-6b29-40b1-80ba-7dedb8571fd1",
    "mmahler":        "712020:2382dd85-3a9f-4422-b715-65a75d1ab3a2",
    "amartini":       "60a6b0153fae6f0068ee8eff",
    "agrow":          "712020:61f57e8a-af1d-4cfa-ae66-970e4cca6023",
    "tneises":        "712020:4d6395bf-bf5c-41b6-b8f0-6696381a06f2",
    "jpablo":         "712020:55141467-3200-4dd2-b237-975b26b28daa",
    "jlee":           "712020:5f7e2b47-8a20-4180-bbcf-c60fe3fdf7e9",
    "jschwarz":       "712020:9d514e43-ca37-4750-9e29-06d6c6afd02f",
    "tatkinson":      "638f6907f6c85b343c0ce7c1",
    "rpartridge":     "712020:b3abae4e-58bb-4817-b59d-0d90ea5bc6d1",
    "bchampagne":     "712020:6435b76e-61ab-4071-8dc2-c614d70fc86f",
    "jsnook":         "712020:6f25deaa-ba8e-41b6-a4ad-3aa39d325f5e",
    "poleary":        "712020:aaae31cb-269f-44d1-adf3-268d1b3422c4",
    "bthompson":      "712020:e8be5629-8e9c-485f-875c-1592ca04953d",
    "admin":          "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "scriptdaylight": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "mpreston":       "5f69593729ef280070289fe6",
    "JIRAUSER15400":  "712020:d4ab1147-6a2a-4955-8cab-39b13f69e5d9",
    "JIRAUSER15680":  "712020:f3e0bdb9-1404-4148-b64d-01fbfa9adf6f",
    "JIRAUSER15800":  "64232fc90152b5f4f9f2d69f",
    "JIRAUSER15820":  "712020:8c2a99b0-d7fa-48d2-bd68-0c3d401a5131",
    "JIRAUSER15900":  "5fd2348334847e0069f2f8db",
    "JIRAUSER16026":  "712020:4dbc0ef6-f3e6-43f9-a130-5053c92ab99a",
    "JIRAUSER16413":  "712020:ff4985cd-2e3f-4963-87c3-a2ca037922a4",
    "JIRAUSER16800":  "712020:c1185e34-949f-488d-aa59-ddd3763b0cf9",
    "JIRAUSER16901":  "712020:60dfb142-ad68-438c-8e6a-8abf502ca0cc",
    "JIRAUSER17701":  "712020:173a7a5d-9ac3-4c44-83ba-db9b1c25dad3",
    "JIRAUSER18301":  "712020:0ae98bca-8f7b-44f5-a8a1-90ed4fcc798e",
    "JIRAUSER18328":  "62df200b4b574e9f2caf61b2",
    "JIRAUSER18826":  "712020:9b401a08-3035-4f26-9401-30c8b171809a",
    "JIRAUSER18869":  "712020:ae668666-612b-419c-be5b-6371bb0f6e0c",
    "JIRAUSER19907":  "712020:c057d01a-634b-49d8-9fc5-9c222376ff46",
    "JIRAUSER20007":  "5b633d02c564683b7b905daa",
    "JIRAUSER20013":  "5dc9a77d97a0a20c663fe62b",
    "JIRAUSER20306":  "712020:c102c143-a186-46ab-a3e0-adc4980a256c",
    "JIRAUSER20307":  "712020:58c9b865-6321-41c0-a179-bb05ff322350",
    "JIRAUSER20316":  "712020:e8a86208-9559-422c-b166-c7ecf6b19149",
    # Unmapped users — fallback to Automation for Jira service account
    "lfalls":         "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "dlibby":         "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "cmabalot":       "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "jsexton":        "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "kcollins":       "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "userplaceholder01": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER16401":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER17611":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER17903":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18847":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18853":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER19124":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER19311":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER20303":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER20615":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER16501":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER20627":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18706":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER17604":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "scriptperforce":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "buildmaster":    "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "technicaldirector": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "audiodirector":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "creativedirector": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "artdirector":    "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "corporatesignoff": "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "nparmeter":      "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "asites":         "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "eslaughter":     "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "jbenjamin":      "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "rbartos":        "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "sdale":          "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "gpongracz":      "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "bkashefi":       "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "sparsons":       "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER15201":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER16200":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18468":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER17600":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18702":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "JIRAUSER18832":  "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "pmatthys":       "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
}


# ═══════════════════════════════════════════════════════════════
# Tree Walker
# ═══════════════════════════════════════════════════════════════

def walk_strings(obj, transform_fn):
    total = 0
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, str):
                new_val, c = transform_fn(val)
                if c:
                    obj[key] = new_val
                    total += c
            else:
                total += walk_strings(val, transform_fn)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_item, c = transform_fn(item)
                if c:
                    obj[i] = new_item
                    total += c
            else:
                total += walk_strings(item, transform_fn)
    return total


# ═══════════════════════════════════════════════════════════════
# Data Fetchers
# ═══════════════════════════════════════════════════════════════

def get_cloud_id(site, auth):
    resp = requests.get(f"https://{site}/_edge/tenant_info",
                        headers={"Accept": "application/json"})
    if resp.ok:
        return resp.json().get("cloudId")
    resp = requests.get(f"https://{site}/rest/api/3/serverInfo",
                        auth=auth, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("cloudId")


def fetch_fields(site, auth=None, headers=None, is_dc=False):
    api = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api}/field"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    out = {}
    for f in resp.json():
        fid = f.get("id", "")
        if fid.startswith("customfield_"):
            out[fid] = {
                "id": fid,
                "name": f.get("name", f.get("untranslatedName", "")).strip(),
                "type": f.get("schema", {}).get("custom", ""),
                "schema_type": f.get("schema", {}).get("type", ""),
            }
    return out


def fetch_statuses(site, auth=None, headers=None, is_dc=False):
    api = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api}/status"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    return {
        s["id"]: {
            "id": s["id"],
            "name": s.get("name", s.get("untranslatedName", "")).strip(),
            "category": s.get("statusCategory", {}).get("name", ""),
        }
        for s in resp.json()
    }


def fetch_projects(site, auth=None, headers=None, is_dc=False):
    if is_dc:
        url = f"https://{site}/rest/api/2/project"
    else:
        url = f"https://{site}/rest/api/3/project/search?maxResults=200"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    data = resp.json()
    projects = data if isinstance(data, list) else data.get("values", [])
    return {
        p["id"]: {"id": p["id"], "key": p.get("key", ""), "name": p.get("name", "")}
        for p in projects
    }


def fetch_service_desks(site, auth=None, headers=None):
    url = f"https://{site}/rest/servicedeskapi/servicedesk"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    if not resp.ok:
        return {}
    return {
        v["projectKey"]: {
            "id": str(v["id"]), "projectId": str(v["projectId"]),
            "name": v["projectName"], "key": v["projectKey"],
        }
        for v in resp.json().get("values", [])
    }


def fetch_issue_types(site, auth=None, headers=None, is_dc=False):
    api = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api}/issuetype"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    return {
        str(it["id"]): {"id": str(it["id"]), "name": it.get("name", "").strip(),
                         "subtask": it.get("subtask", False)}
        for it in resp.json()
    }


def fetch_resolutions(site, auth=None, headers=None, is_dc=False):
    api = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api}/resolution"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    data = resp.json()
    items = data if isinstance(data, list) else data.get("values", [])
    return {
        str(r["id"]): {"id": str(r["id"]), "name": r.get("name", "").strip()}
        for r in items
    }


def fetch_link_types(site, auth=None, headers=None, is_dc=False):
    api = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api}/issueLinkType"
    kw = {"headers": {"Accept": "application/json"}}
    if auth:
        kw["auth"] = auth
    if headers:
        kw["headers"].update(headers)
    resp = requests.get(url, **kw)
    resp.raise_for_status()
    return {
        str(lt["id"]): {"id": str(lt["id"]), "name": lt.get("name", "").strip(),
                         "inward": lt.get("inward", ""), "outward": lt.get("outward", "")}
        for lt in resp.json().get("issueLinkTypes", [])
    }


# ═══════════════════════════════════════════════════════════════
# ID Extractors (from JSON)
# ═══════════════════════════════════════════════════════════════

def extract_field_ids(obj, ids=None):
    if ids is None:
        ids = set()
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                for m in re.finditer(r'customfield_\d+', v):
                    ids.add(m.group())
            else:
                extract_field_ids(v, ids)
    elif isinstance(obj, list):
        for item in obj:
            extract_field_ids(item, ids)
    return ids


def extract_status_ids(obj, ids=None):
    """Extract status IDs from known status contexts in the JSON."""
    if ids is None:
        ids = set()
    if isinstance(obj, dict):
        ds = obj.get("destinationStatus")
        if isinstance(ds, dict):
            val = ds.get("value", "")
            if val and val.isdigit():
                ids.add(val)

        is_status = (obj.get("selectedFieldType") == "status" or
                     (isinstance(obj.get("selectedField"), dict) and
                      obj["selectedField"].get("value") == "status"))
        cv = obj.get("compareValue")
        if is_status and isinstance(cv, dict):
            val = cv.get("value", "")
            if val.startswith("["):
                try:
                    for v in json.loads(val):
                        if isinstance(v, str) and v.isdigit():
                            ids.add(v)
                except (json.JSONDecodeError, TypeError):
                    pass
            elif val.isdigit():
                ids.add(val)

        # toStatus / fromStatus in triggers
        for key in ("toStatus", "fromStatus"):
            statuses = obj.get(key)
            if isinstance(statuses, list):
                for s in statuses:
                    if isinstance(s, dict):
                        val = s.get("value", "")
                        if val and val.isdigit():
                            ids.add(val)

        for v in obj.values():
            if isinstance(v, (dict, list)):
                extract_status_ids(v, ids)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                extract_status_ids(item, ids)
    return ids


def extract_project_ids(obj, ids=None):
    """Extract project IDs from ARI strings."""
    if ids is None:
        ids = set()
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                for m in re.finditer(r'project/(\d+)', v):
                    ids.add(m.group(1))
            else:
                extract_project_ids(v, ids)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                for m in re.finditer(r'project/(\d+)', item):
                    ids.add(m.group(1))
            else:
                extract_project_ids(item, ids)
    return ids


def extract_portal_ids(obj, ids=None):
    """Extract portal IDs from /portal/N/ URL patterns."""
    if ids is None:
        ids = set()
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                for m in re.finditer(r'/portal/(\d+)[/"\)<]', v):
                    ids.add(m.group(1))
            else:
                extract_portal_ids(v, ids)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                for m in re.finditer(r'/portal/(\d+)[/"\)<]', item):
                    ids.add(m.group(1))
            else:
                extract_portal_ids(item, ids)
    return ids


# ═══════════════════════════════════════════════════════════════
# Mapping Builders
# ═══════════════════════════════════════════════════════════════

def _match_by_name(source_items, target_items, extra_match_key=None):
    tgt_by_name = defaultdict(list)
    for tid, t in target_items.items():
        tgt_by_name[t["name"].lower()].append(t)

    mapping, ambiguous, missing = {}, [], []
    same = 0

    for sid, s in source_items.items():
        if sid in target_items and target_items[sid]["name"].lower() == s["name"].lower():
            same += 1
            continue
        candidates = tgt_by_name.get(s["name"].lower(), [])
        if len(candidates) == 1:
            t = candidates[0]
            if sid != t["id"]:
                mapping[sid] = {"target_id": t["id"], "name": s["name"]}
        elif len(candidates) > 1 and extra_match_key:
            typed = [c for c in candidates if c.get(extra_match_key) == s.get(extra_match_key)]
            if len(typed) == 1:
                mapping[sid] = {"target_id": typed[0]["id"], "name": s["name"]}
            else:
                ambiguous.append({"source_id": sid, "name": s["name"],
                                  "candidates": [c["id"] for c in candidates]})
        elif len(candidates) > 1:
            ambiguous.append({"source_id": sid, "name": s["name"],
                              "candidates": [c["id"] for c in candidates]})
        else:
            missing.append({"source_id": sid, "name": s["name"]})
    return mapping, ambiguous, missing, same


def build_project_mapping(src_projects, tgt_projects):
    tgt_by_key = {t["key"]: t for t in tgt_projects.values()}
    mapping, missing = {}, []
    same = 0
    for sid, s in src_projects.items():
        t = tgt_by_key.get(s["key"])
        if t:
            if sid != t["id"]:
                mapping[sid] = {"target_id": t["id"], "name": f"{s['key']} ({s['name']})"}
            else:
                same += 1
        else:
            missing.append({"source_id": sid, "key": s["key"], "name": s["name"]})
    return mapping, missing, same


def build_portal_mapping(src_desks, tgt_desks):
    mapping = {}
    for key, src in src_desks.items():
        tgt = tgt_desks.get(key)
        if tgt and src["id"] != tgt["id"]:
            mapping[src["id"]] = {"target_id": tgt["id"], "name": f"{key} portal"}
    return mapping


def build_bridged_field_mapping(json_field_ids, bridge_fields, target_fields, s1_to_dc):
    """Build S1→S2 mapping using DC as a name bridge."""
    s2_by_name = defaultdict(list)
    for s2_id, info in target_fields.items():
        s2_by_name[info["name"].lower()].append(info)

    mapping, unmapped = {}, []
    already_ok = 0

    for s1_id in sorted(json_field_ids):
        if s1_id in target_fields:
            already_ok += 1
            continue
        dc_id = s1_to_dc.get(s1_id, s1_id)
        dc_info = bridge_fields.get(dc_id)
        if not dc_info:
            unmapped.append({"s1_id": s1_id, "dc_id": dc_id, "reason": "not found on DC"})
            continue
        name = dc_info["name"]
        candidates = s2_by_name.get(name.lower(), [])
        if len(candidates) == 1:
            mapping[s1_id] = {"target_id": candidates[0]["id"], "name": name}
        elif len(candidates) > 1:
            dc_type = dc_info.get("type", "")
            typed = [c for c in candidates if c.get("type") == dc_type]
            if len(typed) == 1:
                mapping[s1_id] = {"target_id": typed[0]["id"], "name": name}
            else:
                unmapped.append({"s1_id": s1_id, "name": name,
                                 "candidates": [c["id"] for c in candidates]})
        else:
            unmapped.append({"s1_id": s1_id, "dc_id": dc_id, "name": name,
                             "reason": "not found on S2"})
    return mapping, unmapped, already_ok


def build_bridged_status_mapping(json_status_ids, bridge_statuses, target_statuses, s1_to_dc):
    """Build S1→S2 status mapping using DC as a name bridge."""
    s2_by_name = defaultdict(list)
    for s2_id, info in target_statuses.items():
        s2_by_name[info["name"].lower()].append(info)

    mapping, unmapped = {}, []
    already_ok = 0

    for s1_id in sorted(json_status_ids):
        if s1_id in target_statuses:
            already_ok += 1
            continue
        dc_id = s1_to_dc.get(s1_id, s1_id)
        dc_info = bridge_statuses.get(dc_id)
        if not dc_info:
            unmapped.append({"s1_id": s1_id, "dc_id": dc_id, "reason": "not found on DC"})
            continue
        name = dc_info["name"]
        candidates = s2_by_name.get(name.lower(), [])
        if len(candidates) == 1:
            mapping[s1_id] = {"target_id": candidates[0]["id"], "name": name}
        elif len(candidates) > 1:
            dc_cat = dc_info.get("category", "")
            same_cat = [c for c in candidates if c.get("category") == dc_cat]
            if len(same_cat) == 1:
                mapping[s1_id] = {"target_id": same_cat[0]["id"], "name": name}
            else:
                unmapped.append({"s1_id": s1_id, "name": name,
                                 "candidates": [c["id"] for c in candidates]})
        else:
            unmapped.append({"s1_id": s1_id, "dc_id": dc_id, "name": name,
                             "reason": "not found on S2"})
    return mapping, unmapped, already_ok


def build_bridged_project_mapping(json_project_ids, bridge_projects, target_projects, s1_to_dc):
    """Build S1→S2 project mapping using DC as a bridge."""
    s2_by_key = {p["key"]: p for p in target_projects.values()}
    mapping, unmapped = {}, []
    already_ok = 0

    for s1_id in sorted(json_project_ids):
        if s1_id in target_projects:
            already_ok += 1
            continue
        dc_id = s1_to_dc.get(s1_id, s1_id)
        dc_info = bridge_projects.get(dc_id)
        if not dc_info:
            unmapped.append({"s1_id": s1_id, "dc_id": dc_id, "reason": "not found on DC"})
            continue
        key = dc_info["key"]
        s2_info = s2_by_key.get(key)
        if s2_info:
            mapping[s1_id] = {"target_id": s2_info["id"],
                              "name": f"{key} ({dc_info['name']})"}
        else:
            unmapped.append({"s1_id": s1_id, "key": key, "name": dc_info["name"],
                             "reason": "project not on S2"})
    return mapping, unmapped, already_ok


def build_bridged_portal_mapping(json_portal_ids, bridge_desks, target_desks, s1_portal_to_dc):
    """Build S1→S2 portal mapping using DC as bridge."""
    dc_portal_to_key = {}
    for key, desk in bridge_desks.items():
        dc_portal_to_key[desk["id"]] = key

    mapping, unmapped = {}, []
    already_ok = 0

    for s1_portal in sorted(json_portal_ids):
        dc_portal = s1_portal_to_dc.get(s1_portal, s1_portal)
        key = dc_portal_to_key.get(dc_portal)
        if not key:
            unmapped.append({"s1_portal": s1_portal, "dc_portal": dc_portal})
            continue
        tgt = target_desks.get(key)
        if tgt:
            if s1_portal != tgt["id"]:
                mapping[s1_portal] = {"target_id": tgt["id"], "name": f"{key} portal"}
            else:
                already_ok += 1
        else:
            unmapped.append({"s1_portal": s1_portal, "key": key, "reason": "not on S2"})
    return mapping, unmapped, already_ok


# ═══════════════════════════════════════════════════════════════
# DC-Only Component Filter
# ═══════════════════════════════════════════════════════════════

DC_ONLY_TYPES = {
    "com.onresolve.jira.groovy.groovyrunner:execute-script-issue-action",
    "com.onresolve.jira.groovy.groovyrunner:execute-script-condition",
    "com.onresolve.jira.groovy.groovyrunner:execute-script-validator",
    "com.onresolve.jira.groovy.groovyrunner:execute-script-function",
    "com.innovalog.jmwe.jira-misc-workflow-extensions:",
}


def find_dc_only_types(obj, found=None):
    """Recursively find DC-only component types in a rule."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        ctype = obj.get("type", "")
        if any(ctype.startswith(dc) for dc in DC_ONLY_TYPES):
            found.add(ctype)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                find_dc_only_types(v, found)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                find_dc_only_types(item, found)
    return found


def filter_dc_only_rules(rules):
    """Remove rules that use DC-only plugin actions. Returns (kept, removed)."""
    kept, removed = [], []
    for rule in rules:
        dc_types = find_dc_only_types(rule)
        if dc_types:
            removed.append({"name": rule.get("name", "?"), "types": list(dc_types)})
        else:
            kept.append(rule)
    return kept, removed


# ═══════════════════════════════════════════════════════════════
# JCMA Migration Classification (per Atlassian docs)
# ═══════════════════════════════════════════════════════════════

FULLY_SUPPORTED = {
    # Triggers
    "jira.sla.breach", "jira.sla.threshold",
    "jira.sprint.event:created", "jira.sprint.event:started", "jira.sprint.event:completed",
    "jira.version.event:created", "jira.version.event:released", "jira.version.event:unreleased",
    "jira.version.event:updated", "jira.worklog.event:created",
    # Actions
    "jira.issue.assign", "jira.issue.clone", "jira.issue.create",
    "jira.issue.create.subtask", "jira.create.variable",
    "jira.issue.delete.comment", "jira.issue.delete.attachment",
    "jira.issue.edit", "jira.issue.log", "jira.issue.log.work",
    "jira.issue.lookup", "jira.issue.add.watcher",
    "jira.issue.slack.message",
    # Conditions
    "jira.comparator.condition", "jira.issue.attachment.condition",
    "jira.jql.condition", "jira.user.condition",
    "jira.condition.container.block", "jira.condition.if.block",
    "jira.condition.else.block",
    "jira.issue.condition",
}

PARTIALLY_SUPPORTED = {
    # Triggers
    "jira.issue.field.changed", "jira.incoming.webhook", "jira.manual.trigger",
    "jira.multiple.issue.event", "jira.jql.scheduled",
    "jira.issue.event.trigger:assigned", "jira.issue.event.trigger:commented",
    "jira.issue.event.trigger:created", "jira.issue.event.trigger:deleted",
    "jira.issue.link.deleted", "jira.issue.linked", "jira.issue.moved",
    "jira.issue.event.trigger:transitioned", "jira.issue.event.trigger:updated",
    # Actions
    "jira.issue.servicedesk.customer.add", "jira.issue.comment",
    "jira.issue.servicedesk.request.create", "jira.version.create",
    "jira.issue.delete", "jira.issue.deletelink", "jira.issue.link",
    "jira.issue.refetch", "jira.version.release",
    "jira.issue.outgoing.email", "jira.issue.entity.property",
    "jira.issue.transition", "jira.version.unrelease",
    # Conditions
    "jira.issue.related", "jira.issues.related.condition",
}

UNSUPPORTED = {
    # Triggers
    "jira.issue.archived", "jira.issue.property.updated", "jira.issue.restored",
    # Actions
    "jira.issue.archive", "jira.issue.webhook",
    "jira.issue.publish.event", "jira.issue.twilio",
    "jira.issue.web.request",
    # Microsoft Teams (multiple possible type strings)
    "com.codebarrel.automation.rulecomponent.jira.action.msteams",
    "jira.issue.msteams",
}

# Also unsupported: ScriptRunner, JMWE, and other DC-only plugins
UNSUPPORTED_PREFIXES = [
    "com.onresolve.jira.groovy.groovyrunner:",
    "com.innovalog.jmwe.",
    "com.codebarrel.automation.rulecomponent.jira.action.msteams",
]


def classify_component(comp_type):
    """Classify a component type as FULL, PARTIAL, UNSUPPORTED, or UNKNOWN."""
    if comp_type in FULLY_SUPPORTED:
        return "FULL"
    if comp_type in PARTIALLY_SUPPORTED:
        return "PARTIAL"
    if comp_type in UNSUPPORTED:
        return "UNSUPPORTED"
    for prefix in UNSUPPORTED_PREFIXES:
        if comp_type.startswith(prefix):
            return "UNSUPPORTED"
    return "UNKNOWN"


def classify_rule(rule):
    """Classify a rule and all its components. Returns (overall, details)."""
    details = []

    def walk_components(obj):
        if isinstance(obj, dict):
            comp_type = obj.get("type", "")
            component = obj.get("component", "")
            if comp_type and component in ("TRIGGER", "ACTION", "CONDITION",
                                            "BRANCH", "CONDITION_BLOCK"):
                level = classify_component(comp_type)
                details.append({
                    "type": comp_type,
                    "component": component,
                    "level": level,
                })
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk_components(v)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk_components(item)

    walk_components(rule.get("trigger", {}))
    for comp in rule.get("components", []):
        walk_components(comp)

    if any(d["level"] == "UNSUPPORTED" for d in details):
        overall = "UNSUPPORTED"
    elif any(d["level"] == "UNKNOWN" for d in details):
        overall = "UNKNOWN"
    elif any(d["level"] == "PARTIAL" for d in details):
        overall = "PARTIAL"
    elif details:
        overall = "FULL"
    else:
        overall = "UNKNOWN"

    return overall, details


def classify_export(rules, enabled_only=True):
    """Classify all rules in an export. Returns classification report."""
    results = {"FULL": [], "PARTIAL": [], "UNSUPPORTED": [], "UNKNOWN": []}

    for rule in rules:
        state = rule.get("state", "DISABLED")
        if enabled_only and state != "ENABLED":
            continue
        name = rule.get("name", "?")
        projects = rule.get("projects", [])
        proj_keys = [p.get("projectId", "?") for p in projects]

        overall, details = classify_rule(rule)

        unsupported_steps = [d for d in details if d["level"] == "UNSUPPORTED"]
        partial_steps = [d for d in details if d["level"] == "PARTIAL"]
        unknown_steps = [d for d in details if d["level"] == "UNKNOWN"]

        results[overall].append({
            "name": name,
            "state": state,
            "projects": proj_keys,
            "overall": overall,
            "total_steps": len(details),
            "unsupported": unsupported_steps,
            "partial": partial_steps,
            "unknown": unknown_steps,
            "all_details": details,
        })

    return results


# ═══════════════════════════════════════════════════════════════
# Replacement Engine
# ═══════════════════════════════════════════════════════════════

def replace_fields(obj, field_map):
    sorted_map = sorted(field_map.items(), key=lambda x: len(x[0]), reverse=True)
    def xform(s):
        c = 0
        for src, info in sorted_map:
            if src in s:
                s = s.replace(src, info["target_id"])
                c += 1
        return s, c
    return walk_strings(obj, xform)


def replace_cf_jql(obj, field_map):
    num_map = {}
    for src, info in field_map.items():
        num_map[src.replace("customfield_", "")] = info["target_id"].replace("customfield_", "")
    if not num_map:
        return 0
    pattern = re.compile(r'cf\[(\d+)\]')
    def xform(s):
        c = 0
        def repl(m):
            nonlocal c
            n = m.group(1)
            if n in num_map:
                c += 1
                return f"cf[{num_map[n]}]"
            return m.group(0)
        s = pattern.sub(repl, s)
        return s, c
    return walk_strings(obj, xform)


def replace_status_ids(obj, status_map):
    count = 0
    if not isinstance(obj, dict):
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    count += replace_status_ids(item, status_map)
        return count

    ds = obj.get("destinationStatus")
    if isinstance(ds, dict):
        val = ds.get("value", "")
        if val in status_map:
            ds["value"] = status_map[val]["target_id"]
            count += 1

    is_status = (obj.get("selectedFieldType") == "status" or
                 (isinstance(obj.get("selectedField"), dict) and
                  obj["selectedField"].get("value") == "status"))
    cv = obj.get("compareValue")
    if is_status and isinstance(cv, dict):
        val = cv.get("value", "")
        if cv.get("multiValue") and val.startswith("["):
            try:
                vals = json.loads(val)
                new_vals = []
                for v in vals:
                    if v in status_map:
                        new_vals.append(status_map[v]["target_id"])
                        count += 1
                    else:
                        new_vals.append(v)
                cv["value"] = json.dumps(new_vals)
            except (json.JSONDecodeError, TypeError):
                if val in status_map:
                    cv["value"] = status_map[val]["target_id"]
                    count += 1
        elif val in status_map:
            cv["value"] = status_map[val]["target_id"]
            count += 1

    # toStatus / fromStatus in triggers
    for key in ("toStatus", "fromStatus"):
        statuses = obj.get(key)
        if isinstance(statuses, list):
            for s in statuses:
                if isinstance(s, dict):
                    val = s.get("value", "")
                    if val in status_map:
                        s["value"] = status_map[val]["target_id"]
                        count += 1

    for v in obj.values():
        if isinstance(v, (dict, list)):
            count += replace_status_ids(v, status_map)
    return count


def replace_project_aris(obj, project_map):
    """Replace project IDs in both Cloud ARI format and DC format.
    Cloud: project/ID in ARI strings
    DC:    "projectId": "ID" and "value": "ID" in project contexts
    """
    if not project_map:
        return 0
    count = 0

    # Pass 1: Walk the tree and replace projectId values and project field values directly
    def _walk_project_ids(o):
        nonlocal count
        if isinstance(o, dict):
            # "projectId": "13100" in projects arrays
            if "projectId" in o and isinstance(o["projectId"], str):
                pid = o["projectId"]
                if pid in project_map:
                    o["projectId"] = project_map[pid]["target_id"]
                    count += 1
            # {"type":"ID","value":"13100"} in project field SET operations
            if (o.get("fieldType") == "project" or o.get("fieldId") == "project"):
                val = o.get("value")
                if isinstance(val, dict) and val.get("type") == "ID":
                    vid = val.get("value", "")
                    if vid in project_map:
                        val["value"] = project_map[vid]["target_id"]
                        count += 1
            for v in o.values():
                if isinstance(v, (dict, list)):
                    _walk_project_ids(v)
        elif isinstance(o, list):
            for item in o:
                if isinstance(item, (dict, list)):
                    _walk_project_ids(item)

    _walk_project_ids(obj)

    # Pass 2: Replace project/ID patterns in ARI strings
    pairs = sorted(
        [(f"project/{s}", f"project/{i['target_id']}") for s, i in project_map.items()],
        key=lambda x: len(x[0]), reverse=True,
    )
    def xform(s):
        c = 0
        for old, new in pairs:
            if old in s:
                s = s.replace(old, new)
                c += 1
        return s, c
    count += walk_strings(obj, xform)
    return count


def replace_portal_urls(obj, portal_map):
    pairs = []
    for src, info in portal_map.items():
        pairs.append((f"/portal/{src}/", f"/portal/{info['target_id']}/"))
        pairs.append((f"/portal/{src}\"", f"/portal/{info['target_id']}\""))
        pairs.append((f"/portal/{src})", f"/portal/{info['target_id']})"))
        pairs.append((f"/portal/{src}<", f"/portal/{info['target_id']}<"))
    if not pairs:
        return 0
    def xform(s):
        c = 0
        for old, new in pairs:
            if old in s:
                s = s.replace(old, new)
                c += 1
        return s, c
    return walk_strings(obj, xform)


def replace_domain(obj, src_domain, tgt_domain):
    if not src_domain or not tgt_domain or src_domain == tgt_domain:
        return 0
    def xform(s):
        if src_domain in s:
            return s.replace(src_domain, tgt_domain), s.count(src_domain)
        return s, 0
    return walk_strings(obj, xform)


def replace_cloud_id(obj, src_cid, tgt_cid):
    if not src_cid or not tgt_cid or src_cid == tgt_cid:
        return 0
    def xform(s):
        if src_cid in s:
            return s.replace(src_cid, tgt_cid), s.count(src_cid)
        return s, 0
    return walk_strings(obj, xform)


def clear_email_from(obj):
    """Remove 'from' fields on email actions so Cloud uses the default sender."""
    count = 0
    if isinstance(obj, dict):
        if obj.get("type") == "jira.issue.outgoing.email" and isinstance(obj.get("value"), dict):
            val = obj["value"]
            for key in ("from", "fromName", "replyTo"):
                if key in val:
                    del val[key]
                    count += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                count += clear_email_from(v)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                count += clear_email_from(item)
    return count


def flatten_webhook_urls(obj):
    """Convert webhookUrl from DC object format to Cloud flat string."""
    count = 0
    if isinstance(obj, dict):
        if "webhookUrl" in obj and isinstance(obj["webhookUrl"], dict):
            wh = obj["webhookUrl"]
            url = wh.get("url") or wh.get("key") or next(iter(wh.values()), "")
            obj["webhookUrl"] = url
            count += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                count += flatten_webhook_urls(v)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                count += flatten_webhook_urls(item)
    return count


def replace_users(obj, user_map):
    """Replace DC usernames with Cloud account IDs in all contexts."""
    count = 0
    if not user_map:
        return 0
    if isinstance(obj, dict):
        for key in ("actorAccountId", "authorAccountId"):
            if key in obj and isinstance(obj[key], str) and obj[key] in user_map:
                obj[key] = user_map[obj[key]]
                count += 1
        if obj.get("type") == "ACCOUNT_ID" and obj.get("value", "") in user_map:
            obj["value"] = user_map[obj["value"]]
            count += 1
        if obj.get("type") == "ID" and isinstance(obj.get("value"), str):
            val = obj["value"]
            if val in user_map:
                obj["value"] = user_map[val]
                count += 1
            elif val.startswith("["):
                try:
                    vals = json.loads(val)
                    new_vals, changed = [], False
                    for v in vals:
                        if v in user_map:
                            new_vals.append(user_map[v])
                            changed = True
                            count += 1
                        else:
                            new_vals.append(v)
                    if changed:
                        obj["value"] = json.dumps(new_vals)
                except (json.JSONDecodeError, TypeError):
                    pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                count += replace_users(v, user_map)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                count += replace_users(item, user_map)
    return count


def replace_contextual_ids(obj, context_maps):
    """Replace IDs based on field type context (issue types, resolutions, etc).
    context_maps: {field_type: {old_id: {"target_id": new_id, ...}}}
    """
    count = 0
    if not context_maps:
        return 0
    if isinstance(obj, dict):
        context = obj.get("fieldType") or obj.get("fieldId") or obj.get("selectedFieldType") or ""
        id_map = context_maps.get(context, {})
        if id_map:
            val = obj.get("value")
            if isinstance(val, dict) and val.get("type") == "ID":
                vid = val.get("value", "")
                if vid in id_map:
                    val["value"] = id_map[vid]["target_id"]
                    count += 1
            cv = obj.get("compareValue")
            if isinstance(cv, dict):
                vid = cv.get("value", "")
                if isinstance(vid, str) and vid in id_map:
                    cv["value"] = id_map[vid]["target_id"]
                    count += 1
                elif isinstance(vid, str) and vid.startswith("["):
                    try:
                        vals = json.loads(vid)
                        new_vals, changed = [], False
                        for v in vals:
                            if v in id_map:
                                new_vals.append(id_map[v]["target_id"])
                                changed = True
                                count += 1
                            else:
                                new_vals.append(v)
                        if changed:
                            cv["value"] = json.dumps(new_vals)
                    except (json.JSONDecodeError, TypeError):
                        pass
        for v in obj.values():
            if isinstance(v, (dict, list)):
                count += replace_contextual_ids(v, context_maps)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                count += replace_contextual_ids(item, context_maps)
    return count


def replace_link_types(obj, linktype_map):
    """Replace link type IDs in 'linkType':'direction:ID' patterns."""
    count = 0
    if not linktype_map:
        return 0
    if isinstance(obj, dict):
        lt = obj.get("linkType")
        if isinstance(lt, str) and ":" in lt:
            direction, lt_id = lt.split(":", 1)
            if lt_id in linktype_map:
                obj["linkType"] = f"{direction}:{linktype_map[lt_id]['target_id']}"
                count += 1
        for v in obj.values():
            if isinstance(v, (dict, list)):
                count += replace_link_types(v, linktype_map)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                count += replace_link_types(item, linktype_map)
    return count


def apply_all(obj, field_map, status_map, project_map, portal_map,
              src_domain, tgt_domain, src_cloud_id=None, tgt_cloud_id=None,
              user_map=None, context_maps=None, linktype_map=None):
    counts = {}
    counts["webhooks"] = flatten_webhook_urls(obj)
    counts["emails"] = clear_email_from(obj)
    counts["users"] = replace_users(obj, user_map or {})
    counts["fields"] = replace_fields(obj, field_map)
    counts["cf_jql"] = replace_cf_jql(obj, field_map)
    counts["statuses"] = replace_status_ids(obj, status_map)
    counts["projects"] = replace_project_aris(obj, project_map)
    counts["portals"] = replace_portal_urls(obj, portal_map)
    counts["contextual"] = replace_contextual_ids(obj, context_maps or {})
    counts["link_types"] = replace_link_types(obj, linktype_map or {})
    counts["urls"] = replace_domain(obj, src_domain, tgt_domain)
    counts["cloud_ids"] = replace_cloud_id(obj, src_cloud_id, tgt_cloud_id)
    return counts


# ═══════════════════════════════════════════════════════════════
# Scanner (Audit)
# ═══════════════════════════════════════════════════════════════

def find_bad_field_refs(obj, valid_ids, path=""):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, str):
                for m in re.finditer(r'customfield_\d+', v):
                    if m.group() not in valid_ids:
                        hits.append((p, m.group()))
            else:
                hits.extend(find_bad_field_refs(v, valid_ids, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(find_bad_field_refs(item, valid_ids, f"{path}[{i}]"))
    return hits


def find_domain_refs(obj, domain, path=""):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, str) and domain in v:
                hits.append((p, domain))
            elif isinstance(v, (dict, list)):
                hits.extend(find_domain_refs(v, domain, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str) and domain in item:
                hits.append((f"{path}[{i}]", domain))
            elif isinstance(item, (dict, list)):
                hits.extend(find_domain_refs(item, domain, f"{path}[{i}]"))
    return hits


# ═══════════════════════════════════════════════════════════════
# Automation Client (Cloud REST API)
# ═══════════════════════════════════════════════════════════════

class AutomationClient:
    def __init__(self, site, cloud_id, email, token):
        self.base = f"https://{site}/gateway/api/automation/public/jira/{cloud_id}/rest/v1"
        self.s = requests.Session()
        self.s.auth = (email, token)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def list_rules(self):
        rules, cursor = [], None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = self.s.get(f"{self.base}/rule/summary", params=params)
            resp.raise_for_status()
            data = resp.json()
            rules.extend(data.get("data", []))
            nxt = data.get("links", {}).get("next", "")
            if nxt and "cursor=" in nxt:
                cursor = nxt.split("cursor=")[1].split("&")[0]
            else:
                break
        return rules

    def get_rule(self, uuid):
        resp = self.s.get(f"{self.base}/rule/{uuid}")
        resp.raise_for_status()
        return resp.json()

    def update_rule(self, uuid, data):
        resp = self.s.put(f"{self.base}/rule/{uuid}", json=data)
        resp.raise_for_status()
        return resp.json()

    def delete_rule(self, uuid):
        resp = self.s.delete(f"{self.base}/rule/{uuid}")
        resp.raise_for_status()


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        description="Map and fix Jira automation IDs across environments",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    mode = p.add_argument_group("mode (pick one)")
    mode.add_argument("--compare", action="store_true", help="Show ID mappings between environments")
    mode.add_argument("--audit", action="store_true", help="Scan Cloud automations for stale IDs")
    mode.add_argument("--fix", action="store_true", help="Scan + fix Cloud automations")
    mode.add_argument("--json-fix", metavar="FILE", help="Fix IDs in an exported automation JSON file")
    mode.add_argument("--purge", action="store_true", help="Delete ALL automation rules on the target site")
    mode.add_argument("--validate", action="store_true", help="Validate imported rules against Cloud registries")
    mode.add_argument("--enable-clean", action="store_true", help="Enable all rules that pass validation (no issues found)")
    mode.add_argument("--repair", action="store_true", help="Fix known status/field mapping gaps on live Cloud rules")
    mode.add_argument("--rescope", nargs=3, metavar=("NAME_FILTER", "FROM_PROJECT_KEY", "TO_PROJECT_KEY"),
                       help="Move rules matching NAME_FILTER from one project to another. E.g.: --rescope Procurement_ FSD SSSD")
    mode.add_argument("--auto-rescope", action="store_true",
                       help="Match rules by name against DC, find correct project, rescope on target")
    mode.add_argument("--classify", metavar="FILE",
                       help="Classify DC export rules by JCMA migration support level")

    env = p.add_argument_group("environments")
    env.add_argument("--source-site", help="Source site (required for --compare/--audit/--fix)")
    env.add_argument("--target-site", required=True, help="Target Cloud site")
    env.add_argument("--source-is-dc", action="store_true")
    env.add_argument("--bridge-site", help="DC site for name resolution when source is gone")
    env.add_argument("--bridge-is-dc", action="store_true")

    auth = p.add_argument_group("authentication")
    auth.add_argument("--token", required=True, help="Cloud API token")
    auth.add_argument("--dc-token", help="DC PAT (for DC source or bridge)")
    auth.add_argument("--email", default="jkane@adaptavist.com")

    opts = p.add_argument_group("options")
    opts.add_argument("--dry-run", action="store_true")
    opts.add_argument("--export", metavar="FILE", help="Save mapping to JSON file")
    opts.add_argument("--dc-rules-json", metavar="FILE", help="DC automation export JSON (for --auto-rescope)")

    args = p.parse_args()

    if not any([args.compare, args.audit, args.fix, args.json_fix, args.purge, args.validate, args.enable_clean, args.repair, args.rescope, args.auto_rescope, args.classify]):
        p.print_help()
        sys.exit(1)

    cloud_auth = (args.email, args.token)

    # ═════════════════════════════════════════════════════════
    # CLASSIFY MODE: classify rules by JCMA migration support
    # ═════════════════════════════════════════════════════════
    if args.classify:
        print(f"\n{'=' * 65}")
        print(f"JCMA MIGRATION READINESS CLASSIFICATION")
        print(f"  Source: {args.classify}")
        print("=" * 65)

        with open(args.classify, "r") as f:
            data = json.load(f)
        rules = data.get("rules", [])
        enabled = [r for r in rules if r.get("state") == "ENABLED"]
        disabled = [r for r in rules if r.get("state") != "ENABLED"]
        print(f"\n  Total rules: {len(rules)}")
        print(f"  Enabled:     {len(enabled)}")
        print(f"  Disabled:    {len(disabled)}")

        # Fetch DC projects for key lookup if source-site provided
        dc_id_to_key = {}
        if args.source_site:
            if args.source_is_dc:
                dc_tok = args.dc_token or args.token
                src_auth, src_hdrs = None, {"Authorization": f"Bearer {dc_tok}"}
            else:
                src_auth, src_hdrs = cloud_auth, None
            try:
                dc_projects = fetch_projects(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
                dc_id_to_key = {p["id"]: p["key"] for p in dc_projects.values()}
            except Exception:
                pass

        results = classify_export(rules, enabled_only=True)

        print(f"\n{'=' * 65}")
        print(f"CLASSIFICATION RESULTS (ENABLED rules only)")
        print("=" * 65)
        print(f"\n  Fully Supported:      {len(results['FULL'])} — will migrate cleanly via JCMA")
        print(f"  Partially Supported:  {len(results['PARTIAL'])} — will migrate, may need post-migration review")
        print(f"  Unsupported:          {len(results['UNSUPPORTED'])} — steps won't migrate, need manual recreation")
        print(f"  Unknown:              {len(results['UNKNOWN'])} — unrecognized step types, review manually")

        if results["FULL"]:
            print(f"\n{'_' * 65}")
            print(f"FULLY SUPPORTED ({len(results['FULL'])} rules) — no action needed:")
            for r in sorted(results["FULL"], key=lambda x: x["name"]):
                proj = dc_id_to_key.get(r["projects"][0], r["projects"][0]) if r["projects"] else "GLOBAL"
                print(f"    {r['name']}  [{proj}]")

        if results["PARTIAL"]:
            print(f"\n{'_' * 65}")
            print(f"PARTIALLY SUPPORTED ({len(results['PARTIAL'])} rules) — review after migration:")
            for r in sorted(results["PARTIAL"], key=lambda x: x["name"]):
                proj = dc_id_to_key.get(r["projects"][0], r["projects"][0]) if r["projects"] else "GLOBAL"
                partial_types = set(d["type"] for d in r["partial"])
                print(f"    {r['name']}  [{proj}]")
                for pt in sorted(partial_types):
                    print(f"      ~ {pt}")

        if results["UNSUPPORTED"]:
            print(f"\n{'_' * 65}")
            print(f"UNSUPPORTED ({len(results['UNSUPPORTED'])} rules) — need manual recreation:")
            for r in sorted(results["UNSUPPORTED"], key=lambda x: x["name"]):
                proj = dc_id_to_key.get(r["projects"][0], r["projects"][0]) if r["projects"] else "GLOBAL"
                unsup_types = set(d["type"] for d in r["unsupported"])
                print(f"    {r['name']}  [{proj}]")
                for ut in sorted(unsup_types):
                    print(f"      X {ut}")

        if results["UNKNOWN"]:
            print(f"\n{'_' * 65}")
            print(f"UNKNOWN ({len(results['UNKNOWN'])} rules) — unrecognized types:")
            for r in sorted(results["UNKNOWN"], key=lambda x: x["name"]):
                proj = dc_id_to_key.get(r["projects"][0], r["projects"][0]) if r["projects"] else "GLOBAL"
                unknown_types = set(d["type"] for d in r["unknown"])
                print(f"    {r['name']}  [{proj}]")
                for ut in sorted(unknown_types):
                    print(f"      ? {ut}")

        # Summary of unsupported step types across all rules
        all_unsup = defaultdict(int)
        for r in results["UNSUPPORTED"]:
            for d in r["unsupported"]:
                all_unsup[d["type"]] += 1
        if all_unsup:
            print(f"\n{'_' * 65}")
            print(f"UNSUPPORTED STEP TYPES (summary):")
            for t, count in sorted(all_unsup.items(), key=lambda x: -x[1]):
                print(f"    {t} — used in {count} rule(s)")

        # Export classification as JSON if requested
        if args.export:
            with open(args.export, "w") as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\n  Classification exported to {args.export}")

        print()
        return

    # ═════════════════════════════════════════════════════════
    # PURGE MODE: delete all automations on target
    # ═════════════════════════════════════════════════════════
    if args.purge:
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        print(f"\n{'=' * 65}")
        print(f"PURGE ALL AUTOMATIONS ON: {args.target_site}")
        print("=" * 65)

        print("  Fetching rule list...")
        summaries = client.list_rules()
        print(f"  Found {len(summaries)} rule(s)")

        if not summaries:
            print("  Nothing to delete.")
            return

        for s in summaries[:10]:
            print(f"    - {s.get('name', '?')}")
        if len(summaries) > 10:
            print(f"    ... and {len(summaries) - 10} more")

        print(f"\n  DELETE ALL {len(summaries)} RULES? This cannot be undone. [yes/N] ", end="")
        if input().strip().lower() != "yes":
            print("  Aborted.")
            return

        deleted, failed = 0, 0
        for s in summaries:
            uuid = s.get("uuid")
            name = s.get("name", "?")
            try:
                client.delete_rule(uuid)
                deleted += 1
                print(f"    Deleted: {name}")
            except requests.HTTPError as e:
                failed += 1
                print(f"    FAILED:  {name} — {e.response.status_code}")

        print(f"\n{'=' * 65}")
        print(f"Results: {deleted} deleted, {failed} failed")
        print("=" * 65 + "\n")
        return

    # ═════════════════════════════════════════════════════════
    # VALIDATE MODE: check imported rules against Cloud registries
    # ═════════════════════════════════════════════════════════
    if args.validate:
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        print(f"\n{'=' * 65}")
        print(f"VALIDATING AUTOMATIONS ON: {args.target_site}")
        print("=" * 65)

        print("\n── Loading Cloud registries ──")
        cloud_fields = fetch_fields(args.target_site, auth=cloud_auth)
        cloud_statuses = fetch_statuses(args.target_site, auth=cloud_auth)
        cloud_projects = fetch_projects(args.target_site, auth=cloud_auth)
        cloud_itypes = fetch_issue_types(args.target_site, auth=cloud_auth)
        cloud_resolutions = fetch_resolutions(args.target_site, auth=cloud_auth)
        cloud_ltypes = fetch_link_types(args.target_site, auth=cloud_auth)
        print(f"  Fields: {len(cloud_fields)} | Statuses: {len(cloud_statuses)} | Projects: {len(cloud_projects)}")
        print(f"  Issue Types: {len(cloud_itypes)} | Resolutions: {len(cloud_resolutions)} | Link Types: {len(cloud_ltypes)}")

        cloud_field_ids = set(cloud_fields.keys())
        cloud_status_ids = set(cloud_statuses.keys())
        cloud_project_ids = set(cloud_projects.keys())
        cloud_itype_ids = set(cloud_itypes.keys())
        cloud_resolution_ids = set(cloud_resolutions.keys())
        cloud_ltype_ids = set(cloud_ltypes.keys())

        # Pattern for Cloud account IDs (not DC usernames)
        cloud_acct_pattern = re.compile(r'^(\d+:[0-9a-f-]+|[0-9a-f]{20,}|5[0-9a-f]{23,})$')

        print("\n── Fetching rules ──")
        summaries = client.list_rules()
        print(f"  Total rules: {len(summaries)}")

        issues_found = []
        clean = 0

        for summary in summaries:
            uuid = summary.get("uuid")
            name = summary.get("name", "?")
            try:
                full = client.get_rule(uuid)
            except requests.HTTPError:
                continue

            rule_issues = []

            # 1. Check custom field refs
            for path, ref in find_bad_field_refs(full, cloud_field_ids):
                rule_issues.append(("FIELD", ref, f"not in Cloud registry"))

            # 2. Check status IDs
            rule_statuses = extract_status_ids(full)
            for sid in rule_statuses:
                if sid not in cloud_status_ids:
                    rule_issues.append(("STATUS", sid, "not in Cloud registry"))

            # 3. Check project IDs in ARIs
            rule_projects = extract_project_ids(full)
            for pid in rule_projects:
                if pid not in cloud_project_ids:
                    rule_issues.append(("PROJECT", pid, "not in Cloud registry"))

            # 4. Check for DC usernames (non-Cloud-format user IDs)
            def find_dc_users(obj, found=None):
                if found is None:
                    found = set()
                if isinstance(obj, dict):
                    for key in ("actorAccountId", "authorAccountId"):
                        val = obj.get(key, "")
                        if val and not cloud_acct_pattern.match(val):
                            found.add(val)
                    if obj.get("type") == "ID" and isinstance(obj.get("value"), str):
                        val = obj["value"]
                        if val and not val.startswith("[") and not cloud_acct_pattern.match(val) and not val.isdigit():
                            found.add(val)
                    if obj.get("type") == "ACCOUNT_ID" and isinstance(obj.get("value"), str):
                        val = obj["value"]
                        if val and not cloud_acct_pattern.match(val):
                            found.add(val)
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            find_dc_users(v, found)
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, (dict, list)):
                            find_dc_users(item, found)
                return found

            dc_users = find_dc_users(full)
            for u in dc_users:
                rule_issues.append(("DC_USER", u, "DC username not mapped to Cloud account"))

            # 5. Check for old domain URLs
            for domain in ("daybreakgames-sandbox.atlassian.net", "jira-test.daybreakgames.com",
                           "jira.daybreakgames.com"):
                old_urls = find_domain_refs(full, domain)
                for path, d in old_urls:
                    rule_issues.append(("OLD_URL", d, "old domain reference"))

            # 6. Check Cloud ID
            old_cloud_ids = find_domain_refs(full, S1_CLOUD_ID)
            for path, cid in old_cloud_ids:
                rule_issues.append(("OLD_CLOUD_ID", S1_CLOUD_ID, "Sandbox 1 Cloud ID still present"))

            if rule_issues:
                seen = set()
                deduped = []
                for issue in rule_issues:
                    key = (issue[0], issue[1])
                    if key not in seen:
                        seen.add(key)
                        deduped.append(issue)
                issues_found.append({"name": name, "uuid": uuid, "issues": deduped})
                print(f"  x {name} — {len(deduped)} issue(s)")
            else:
                clean += 1

        print(f"\n{'=' * 65}")
        print(f"VALIDATION RESULTS")
        print("=" * 65)
        print(f"  Clean:  {clean} rule(s)")
        print(f"  Issues: {len(issues_found)} rule(s)")

        if issues_found:
            # Aggregate by issue type
            by_type = defaultdict(list)
            for iss in issues_found:
                for itype, ref, desc in iss["issues"]:
                    by_type[itype].append({"rule": iss["name"], "ref": ref, "desc": desc})

            for itype in ("FIELD", "STATUS", "PROJECT", "DC_USER", "OLD_URL", "OLD_CLOUD_ID"):
                items = by_type.get(itype, [])
                if items:
                    print(f"\n  {itype} issues ({len(items)}):")
                    seen_refs = {}
                    for item in items:
                        ref = item["ref"]
                        if ref not in seen_refs:
                            seen_refs[ref] = []
                        seen_refs[ref].append(item["rule"])
                    for ref, rules_list in seen_refs.items():
                        print(f"    {ref} — in {len(rules_list)} rule(s): {', '.join(rules_list[:3])}" +
                              (f" +{len(rules_list)-3} more" if len(rules_list) > 3 else ""))

        print()
        return

    # ═════════════════════════════════════════════════════════
    # ENABLE-CLEAN MODE: validate then enable rules with no issues
    # ═════════════════════════════════════════════════════════
    if args.enable_clean:
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        print(f"\n{'=' * 65}")
        print(f"ENABLE CLEAN RULES ON: {args.target_site}")
        print("=" * 65)

        print("\n── Loading Cloud registries ──")
        cloud_fields = fetch_fields(args.target_site, auth=cloud_auth)
        cloud_statuses = fetch_statuses(args.target_site, auth=cloud_auth)
        cloud_field_ids = set(cloud_fields.keys())
        cloud_status_ids = set(cloud_statuses.keys())
        cloud_acct_pattern = re.compile(r'^(\d+:[0-9a-f-]+|[0-9a-f]{20,}|5[0-9a-f]{23,})$')

        print("  Fetching rules...")
        summaries = client.list_rules()
        print(f"  Total rules: {len(summaries)}")

        clean_rules = []
        issue_rules = []

        for summary in summaries:
            uuid = summary.get("uuid")
            name = summary.get("name", "?")
            try:
                full = client.get_rule(uuid)
            except requests.HTTPError:
                continue

            has_issue = False
            if find_bad_field_refs(full, cloud_field_ids):
                has_issue = True
            rule_statuses = extract_status_ids(full)
            for sid in rule_statuses:
                if sid not in cloud_status_ids:
                    has_issue = True
            for domain in ("daybreakgames-sandbox.atlassian.net", "jira-test.daybreakgames.com"):
                if find_domain_refs(full, domain):
                    has_issue = True

            if has_issue:
                issue_rules.append(name)
            else:
                clean_rules.append({"uuid": uuid, "name": name, "rule": full})

        print(f"\n  Clean (will enable): {len(clean_rules)}")
        print(f"  Issues (will skip):  {len(issue_rules)}")

        if not clean_rules:
            print("  No clean rules to enable.")
            return

        if not args.dry_run:
            print(f"\n  Enable {len(clean_rules)} clean rules? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("  Aborted.")
                return

        enabled, failed = 0, 0
        for r in clean_rules:
            rule = r["rule"]
            if rule.get("state") == "ENABLED":
                continue
            rule["state"] = "ENABLED"
            if not args.dry_run:
                try:
                    client.update_rule(r["uuid"], rule)
                    enabled += 1
                    print(f"    Enabled: {r['name']}")
                except requests.HTTPError as e:
                    failed += 1
                    print(f"    FAILED:  {r['name']} — {e.response.status_code}")
            else:
                enabled += 1
                print(f"    Would enable: {r['name']}")

        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN: {enabled} would be enabled")
        else:
            print(f"Results: {enabled} enabled, {failed} failed")
        print("=" * 65)

        if issue_rules:
            print(f"\n  Skipped {len(issue_rules)} rules with issues:")
            for name in issue_rules:
                print(f"    - {name}")
        print()
        return

    # ═════════════════════════════════════════════════════════
    # REPAIR MODE: fix known status/field mapping gaps on live rules
    # ═════════════════════════════════════════════════════════
    if args.repair:
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        # Build repair maps from manual overrides
        status_repair = {k: {"target_id": v, "name": "manual repair"} for k, v in MANUAL_STATUS_OVERRIDES.items()}
        field_repair = {k: {"target_id": v, "name": "manual repair"} for k, v in MANUAL_FIELD_OVERRIDES.items() if k != v}

        print(f"\n{'=' * 65}")
        print(f"REPAIR KNOWN MAPPING GAPS ON: {args.target_site}")
        print("=" * 65)

        print(f"\n  Status repairs to apply:")
        for old, new in MANUAL_STATUS_OVERRIDES.items():
            print(f"    {old} -> {new}")
        print(f"  Field repairs to apply:")
        for old, new in MANUAL_FIELD_OVERRIDES.items():
            if old != new:
                print(f"    {old} -> {new}")

        print(f"\n  Fetching rules...")
        summaries = client.list_rules()
        print(f"  Total rules: {len(summaries)}")

        affected = []
        for summary in summaries:
            uuid = summary.get("uuid")
            name = summary.get("name", "?")
            try:
                full = client.get_rule(uuid)
            except requests.HTTPError:
                continue

            test = copy.deepcopy(full)
            counts = {}
            counts["statuses"] = replace_status_ids(test, status_repair)
            counts["fields"] = replace_fields(test, field_repair)
            total = sum(counts.values())

            if total > 0:
                affected.append({"uuid": uuid, "name": name, "full_rule": full, "counts": counts})
                parts = []
                if counts["statuses"]:
                    parts.append(f"{counts['statuses']} status(es)")
                if counts["fields"]:
                    parts.append(f"{counts['fields']} field(s)")
                print(f"    x {name} — {', '.join(parts)}")

        print(f"\n{'_' * 65}")
        print(f"  Rules needing repair: {len(affected)}")

        if not affected:
            print("  Nothing to repair.")
            return

        if not args.dry_run:
            print(f"\n  Repair {len(affected)} rule(s)? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("  Aborted.")
                return

        success, fail = 0, 0
        for r in affected:
            rule = r["full_rule"]
            replace_status_ids(rule, status_repair)
            replace_fields(rule, field_repair)

            if not args.dry_run:
                try:
                    client.update_rule(r["uuid"], rule)
                    success += 1
                    print(f"    Repaired: {r['name']}")
                except requests.HTTPError as e:
                    fail += 1
                    print(f"    FAILED:   {r['name']} — {e.response.status_code}: {e.response.text[:200]}")
            else:
                success += 1
                print(f"    Would repair: {r['name']}")

        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN: {success} would be repaired")
        else:
            print(f"Results: {success} repaired, {fail} failed")
        print("=" * 65 + "\n")
        return

    # ═════════════════════════════════════════════════════════
    # RESCOPE MODE: move rules from one project to another
    # ═════════════════════════════════════════════════════════
    if args.rescope:
        name_filter, from_key, to_key = args.rescope
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        # Look up project IDs by key
        projects = fetch_projects(args.target_site, auth=cloud_auth)
        key_to_id = {p["key"]: p["id"] for p in projects.values()}

        if from_key not in key_to_id:
            print(f"Error: project '{from_key}' not found on {args.target_site}")
            sys.exit(1)
        if to_key not in key_to_id:
            print(f"Error: project '{to_key}' not found on {args.target_site}")
            sys.exit(1)

        from_id = key_to_id[from_key]
        to_id = key_to_id[to_key]
        from_ari = f"ari:cloud:jira:{tgt_cloud_id}:project/{from_id}"
        to_ari = f"ari:cloud:jira:{tgt_cloud_id}:project/{to_id}"

        print(f"\n{'=' * 65}")
        print(f"RESCOPE RULES: '{name_filter}*' from {from_key} ({from_id}) to {to_key} ({to_id})")
        print("=" * 65)

        print(f"\n  From ARI: {from_ari}")
        print(f"  To ARI:   {to_ari}")

        print(f"\n  Fetching rules...")
        summaries = client.list_rules()

        # Build reverse lookup: project ID → key
        id_to_key = {p["id"]: p["key"] for p in projects.values()}

        matches = []
        all_matching = []
        for summary in summaries:
            name = summary.get("name", "")
            if name_filter.lower() not in name.lower():
                continue
            uuid = summary.get("uuid")
            try:
                full = client.get_rule(uuid)
            except requests.HTTPError:
                continue

            scope = full.get("ruleScope", {}).get("resources", [])
            home_ari = full.get("ruleHome", {}).get("ruleLifecycleHome", {}).get("locationARI", "")
            all_aris = list(scope) + ([home_ari] if home_ari else [])

            scope_desc = []
            for ari in set(all_aris):
                m = re.search(r'project/(\d+)', ari)
                if m:
                    pid = m.group(1)
                    scope_desc.append(f"{id_to_key.get(pid, '???')} ({pid})")
                else:
                    scope_desc.append(ari[:60])
            if not all_aris:
                scope_desc = ["GLOBAL (no project scope)"]

            all_matching.append({"uuid": uuid, "name": name, "full_rule": full,
                                 "scope": scope, "home_ari": home_ari,
                                 "all_aris": all_aris, "scope_desc": ", ".join(scope_desc)})
            is_global = not scope and not home_ari
            has_from = from_ari in scope or from_ari == home_ari
            if has_from or (from_key == "GLOBAL" and is_global) or (is_global and from_key != to_key):
                matches.append({"uuid": uuid, "name": name, "full_rule": full, "is_global": is_global})

        # Always show all matching rules and their scopes
        print(f"\n  All rules matching '{name_filter}' ({len(all_matching)} found):")
        for m in all_matching:
            marker = ""
            for match in matches:
                if match["uuid"] == m["uuid"]:
                    marker = " <-- WILL RESCOPE"
            print(f"    {m['name']}  [{m['scope_desc']}]{marker}")

        print(f"\n  Will rescope: {len(matches)}")
        for m in matches:
            print(f"    - {m['name']}")

        if not matches:
            print("  Nothing to rescope.")
            return

        if not args.dry_run:
            print(f"\n  Rescope {len(matches)} rule(s) from {from_key} to {to_key}? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("  Aborted.")
                return

        success, fail = 0, 0
        for m in matches:
            rule = m["full_rule"]

            # Update ruleScope — set or swap
            if "ruleScope" not in rule:
                rule["ruleScope"] = {"resources": []}
            scope = rule["ruleScope"].get("resources", [])
            if m.get("is_global") or not scope:
                rule["ruleScope"]["resources"] = [to_ari]
            else:
                rule["ruleScope"]["resources"] = [to_ari if r == from_ari else r for r in scope]

            # Update ruleHome
            home = rule.get("ruleHome", {})
            lh = home.get("ruleLifecycleHome", {})
            if not lh.get("locationARI") or lh.get("locationARI") == from_ari:
                lh["locationARI"] = to_ari

            # Update trigger eventFilters — set or swap
            trigger = rule.get("trigger", {})
            tv = trigger.get("value", {})
            if isinstance(tv, dict):
                ef = tv.get("eventFilters", [])
                if not ef or m.get("is_global"):
                    tv["eventFilters"] = [to_ari]
                else:
                    tv["eventFilters"] = [to_ari if f == from_ari else f for f in ef]

            if not args.dry_run:
                try:
                    client.update_rule(m["uuid"], rule)
                    success += 1
                    print(f"    Rescoped: {m['name']}")
                except requests.HTTPError as e:
                    fail += 1
                    print(f"    FAILED:   {m['name']} — {e.response.status_code}: {e.response.text[:200]}")
            else:
                success += 1
                print(f"    Would rescope: {m['name']}")

        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN: {success} would be rescoped")
        else:
            print(f"Results: {success} rescoped, {fail} failed")
        print("=" * 65 + "\n")
        return

    # ═════════════════════════════════════════════════════════
    # AUTO-RESCOPE: match rules by name against DC export, fix project scoping
    # ═════════════════════════════════════════════════════════
    if args.auto_rescope:
        if not args.dc_rules_json:
            print("Error: --dc-rules-json FILE required for --auto-rescope")
            print("  Provide the DC automation export JSON so we can match rule names to projects.")
            sys.exit(1)

        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)

        print(f"\n{'=' * 65}")
        print(f"AUTO-RESCOPE: Match rules against DC export, fix project scoping")
        print(f"  DC JSON: {args.dc_rules_json}")
        print(f"  Target:  {args.target_site}")
        print("=" * 65)

        # Load DC export and build name→project mapping
        with open(args.dc_rules_json, "r") as f:
            dc_data = json.load(f)
        dc_rules = dc_data.get("rules", [])
        print(f"\n  DC rules in export: {len(dc_rules)}")

        # Fetch projects from both DC and S2
        if args.source_is_dc and args.source_site:
            dc_tok = args.dc_token or args.token
            src_auth = None
            src_hdrs = {"Authorization": f"Bearer {dc_tok}"}
            dc_projects = fetch_projects(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=True)
        else:
            dc_projects = fetch_projects(args.target_site, auth=cloud_auth)  # fallback
        s2_projects = fetch_projects(args.target_site, auth=cloud_auth)

        dc_id_to_key = {p["id"]: p["key"] for p in dc_projects.values()}
        s2_key_to_id = {p["key"]: p["id"] for p in s2_projects.values()}
        s2_id_to_key = {p["id"]: p["key"] for p in s2_projects.values()}

        # Build DC rule name → correct S2 project mapping
        dc_name_to_s2_project = {}
        for dc_rule in dc_rules:
            name = dc_rule.get("name", "")
            dc_proj_list = dc_rule.get("projects", [])
            if dc_proj_list:
                dc_pid = str(dc_proj_list[0].get("projectId", ""))
                dc_key = dc_id_to_key.get(dc_pid)
                if dc_key and dc_key in s2_key_to_id:
                    dc_name_to_s2_project[name] = {
                        "s2_pid": s2_key_to_id[dc_key],
                        "key": dc_key,
                        "dc_pid": dc_pid,
                    }

        print(f"  DC rules with project mapping: {len(dc_name_to_s2_project)}")

        # Fetch S2 rules
        print("\n  Fetching S2 rules...")
        s2_summaries = client.list_rules()
        print(f"  S2 rules: {len(s2_summaries)}")

        # Compare scopes
        print("\n  Analyzing S2 rule scopes...")
        mismatches = []
        correct = 0
        no_dc_match = 0
        already_global = 0

        for summary in s2_summaries:
            uuid = summary.get("uuid")
            name = summary.get("name", "?")
            try:
                full = client.get_rule(uuid)
            except requests.HTTPError:
                continue

            dc_match = dc_name_to_s2_project.get(name)
            if not dc_match:
                no_dc_match += 1
                continue

            correct_s2_pid = dc_match["s2_pid"]
            correct_key = dc_match["key"]
            correct_ari = f"ari:cloud:jira:{tgt_cloud_id}:project/{correct_s2_pid}"

            # Get current scope
            scope_aris = full.get("ruleScope", {}).get("resources", [])
            home_ari = full.get("ruleHome", {}).get("ruleLifecycleHome", {}).get("locationARI", "")

            current_pids = set()
            for ari in scope_aris + ([home_ari] if home_ari else []):
                m = re.search(r'project/(\d+)', ari)
                if m:
                    current_pids.add(m.group(1))

            if correct_s2_pid in current_pids:
                correct += 1
                continue

            current_key = "GLOBAL"
            current_pid = None
            if current_pids:
                current_pid = list(current_pids)[0]
                current_key = s2_id_to_key.get(current_pid, f"??? ({current_pid})")

            mismatches.append({
                "uuid": uuid, "name": name, "full_rule": full,
                "current_pid": current_pid, "current_key": current_key,
                "correct_pid": correct_s2_pid, "correct_key": correct_key,
                "correct_ari": correct_ari,
            })

        print(f"\n{'=' * 65}")
        print(f"  Correctly scoped:  {correct}")
        print(f"  Need rescoping:    {len(mismatches)}")
        print(f"  No DC match:       {no_dc_match} (rules not in DC export)")
        print("=" * 65)

        if mismatches:
            print(f"\n  Rules to rescope:")
            for m in mismatches:
                print(f"    {m['name']}")
                print(f"      {m['current_key']} -> {m['correct_key']} ({m['correct_pid']})")

        if not mismatches:
            print("  Nothing to rescope.")
            return

        if not args.dry_run:
            print(f"\n  Rescope {len(mismatches)} rule(s)? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("  Aborted.")
                return

        success, fail = 0, 0
        for m in mismatches:
            rule = m["full_rule"]
            new_ari = m["correct_ari"]

            # Set ruleScope
            rule.setdefault("ruleScope", {})["resources"] = [new_ari]

            # Set ruleHome
            rule.setdefault("ruleHome", {}).setdefault("ruleLifecycleHome", {})["locationARI"] = new_ari

            # Set trigger eventFilters
            tv = rule.get("trigger", {}).get("value", {})
            if isinstance(tv, dict):
                tv["eventFilters"] = [new_ari]

            if not args.dry_run:
                try:
                    client.update_rule(m["uuid"], rule)
                    success += 1
                    print(f"    Rescoped: {m['name']} ({m['current_key']} -> {m['correct_key']})")
                except requests.HTTPError as e:
                    fail += 1
                    print(f"    FAILED:   {m['name']} — {e.response.status_code}: {e.response.text[:200]}")
            else:
                success += 1
                print(f"    Would rescope: {m['name']} ({m['current_key']} -> {m['correct_key']})")

        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN: {success} would be rescoped")
        else:
            print(f"Results: {success} rescoped, {fail} failed")
        print("=" * 65 + "\n")
        return

    bridge_mode = args.bridge_site and not args.source_site
    if not bridge_mode and not args.source_site:
        print("Error: --source-site or --bridge-site required")
        sys.exit(1)

    # ═════════════════════════════════════════════════════════
    # BRIDGE MODE: source is gone, use DC + old mapping
    # ═════════════════════════════════════════════════════════
    if bridge_mode:
        if not args.json_fix:
            print("Error: bridge mode only works with --json-fix")
            sys.exit(1)

        dc_tok = args.dc_token or args.token
        br_auth = None if args.bridge_is_dc else cloud_auth
        br_hdrs = {"Authorization": f"Bearer {dc_tok}"} if args.bridge_is_dc else None

        print(f"\n{'=' * 65}")
        print(f"BRIDGE MODE")
        print(f"  Bridge (DC): {args.bridge_site}")
        print(f"  Target:      {args.target_site}")
        print(f"  JSON file:   {args.json_fix}")
        print("=" * 65)

        # Load JSON
        with open(args.json_fix, "r") as f:
            data = json.load(f)
        rules = data.get("rules", [data] if "trigger" in data else [])
        print(f"\n  Rules in file: {len(rules)}")

        # Filter out DC-only plugin rules
        rules, removed = filter_dc_only_rules(rules)
        if removed:
            print(f"\n  Removed {len(removed)} rule(s) with DC-only plugins:")
            for r in removed:
                print(f"    - {r['name']}  ({', '.join(r['types'])})")
            data["rules"] = rules
            print(f"  Remaining: {len(rules)} rules")

        # Extract IDs from JSON
        print("\n── Extracting IDs from JSON ──")
        json_fields = extract_field_ids(data)
        json_statuses = extract_status_ids(data)
        json_projects = extract_project_ids(data)
        json_portals = extract_portal_ids(data)
        print(f"  Custom fields: {len(json_fields)}")
        print(f"  Status IDs:    {len(json_statuses)}")
        print(f"  Project IDs:   {len(json_projects)}")
        print(f"  Portal IDs:    {len(json_portals)}")

        # Fetch from bridge (DC) and target (S2)
        print("\n── Fetching registries ──")
        br_fields = fetch_fields(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_fields = fetch_fields(args.target_site, auth=cloud_auth)
        print(f"  Bridge fields: {len(br_fields)}  |  Target fields: {len(tgt_fields)}")

        br_statuses = fetch_statuses(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_statuses = fetch_statuses(args.target_site, auth=cloud_auth)
        print(f"  Bridge statuses: {len(br_statuses)}  |  Target statuses: {len(tgt_statuses)}")

        br_projects = fetch_projects(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_projects = fetch_projects(args.target_site, auth=cloud_auth)
        print(f"  Bridge projects: {len(br_projects)}  |  Target projects: {len(tgt_projects)}")

        br_desks = fetch_service_desks(args.bridge_site, auth=br_auth, headers=br_hdrs)
        tgt_desks = fetch_service_desks(args.target_site, auth=cloud_auth)
        print(f"  Bridge desks: {len(br_desks)}  |  Target desks: {len(tgt_desks)}")

        br_itypes = fetch_issue_types(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_itypes = fetch_issue_types(args.target_site, auth=cloud_auth)
        print(f"  Bridge issue types: {len(br_itypes)}  |  Target issue types: {len(tgt_itypes)}")

        br_resolutions = fetch_resolutions(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_resolutions = fetch_resolutions(args.target_site, auth=cloud_auth)
        print(f"  Bridge resolutions: {len(br_resolutions)}  |  Target resolutions: {len(tgt_resolutions)}")

        br_ltypes = fetch_link_types(args.bridge_site, auth=br_auth, headers=br_hdrs, is_dc=args.bridge_is_dc)
        tgt_ltypes = fetch_link_types(args.target_site, auth=cloud_auth)
        print(f"  Bridge link types: {len(br_ltypes)}  |  Target link types: {len(tgt_ltypes)}")

        # Build bridged mappings
        print(f"\n{'=' * 65}\nBUILDING BRIDGED MAPPINGS (S1 -> DC -> S2)\n{'=' * 65}")

        fm, f_unmapped, f_ok = build_bridged_field_mapping(json_fields, br_fields, tgt_fields, S1_TO_DC_FIELDS)
        sm, s_unmapped, s_ok = build_bridged_status_mapping(json_statuses, br_statuses, tgt_statuses, S1_TO_DC_STATUSES)
        pm, p_unmapped, p_ok = build_bridged_project_mapping(json_projects, br_projects, tgt_projects, S1_TO_DC_PROJECTS)
        ptm, pt_unmapped, pt_ok = build_bridged_portal_mapping(json_portals, br_desks, tgt_desks, S1_PORTAL_MAP)

        # Apply manual overrides for known mismatches
        for src_id, tgt_id in MANUAL_FIELD_OVERRIDES.items():
            if src_id in json_fields and src_id not in fm and src_id != tgt_id:
                fm[src_id] = {"target_id": tgt_id, "name": f"manual override"}
        for src_id, tgt_id in MANUAL_STATUS_OVERRIDES.items():
            if src_id not in sm:
                sm[src_id] = {"target_id": tgt_id, "name": f"manual override"}

        # Issue types, resolutions, link types via DC→S2 name matching
        itm_direct, _, _, _ = _match_by_name(br_itypes, tgt_itypes, "subtask")
        rm_direct, _, _, _ = _match_by_name(br_resolutions, tgt_resolutions)
        ltm_direct, _, _, _ = _match_by_name(br_ltypes, tgt_ltypes)

        context_maps = {}
        if itm_direct:
            context_maps["issuetype"] = itm_direct
        if rm_direct:
            context_maps["resolution"] = rm_direct

        print(f"\n  Custom Fields:  {f_ok} already on S2, {len(fm)} need remapping, {len(f_unmapped)} unmapped")
        print(f"  Statuses:       {s_ok} already on S2, {len(sm)} need remapping, {len(s_unmapped)} unmapped")
        print(f"  Projects:       {p_ok} already on S2, {len(pm)} need remapping, {len(p_unmapped)} unmapped")
        print(f"  Portals:        {pt_ok} already on S2, {len(ptm)} need remapping, {len(pt_unmapped)} unmapped")

        if fm:
            print(f"\n  Field ID changes:")
            for src, info in sorted(fm.items(), key=lambda x: x[1]["name"]):
                print(f"    {src} -> {info['target_id']}  ({info['name']})")

        if sm:
            print(f"\n  Status ID changes:")
            for src, info in sorted(sm.items(), key=lambda x: x[1]["name"]):
                print(f"    {src} -> {info['target_id']}  ({info['name']})")

        if pm:
            print(f"\n  Project ID changes:")
            for src, info in sorted(pm.items(), key=lambda x: x[1]["name"]):
                print(f"    {src} -> {info['target_id']}  ({info['name']})")

        if ptm:
            print(f"\n  Portal ID changes:")
            for src, info in ptm.items():
                print(f"    /portal/{src}/ -> /portal/{info['target_id']}/  ({info['name']})")

        if f_unmapped:
            print(f"\n  ** Unmapped fields (need manual review):")
            for u in f_unmapped:
                print(f"    {u}")
        if s_unmapped:
            print(f"\n  ** Unmapped statuses:")
            for u in s_unmapped:
                print(f"    {u}")
        if p_unmapped:
            print(f"\n  ** Unmapped projects:")
            for u in p_unmapped:
                print(f"    {u}")

        # Cloud ID and URL replacements
        tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
        src_domain = "daybreakgames-sandbox.atlassian.net"
        print(f"\n  Cloud ID: {S1_CLOUD_ID} -> {tgt_cloud_id}")
        print(f"  URL: {src_domain} -> {args.target_site}")

        # Apply all replacements
        print(f"\n{'=' * 65}")
        print(f"APPLYING REPLACEMENTS")
        print("=" * 65)

        total_counts = defaultdict(int)
        for rule in rules:
            name = rule.get("name", "?")
            counts = apply_all(rule, fm, sm, pm, ptm,
                               src_domain, args.target_site,
                               S1_CLOUD_ID, tgt_cloud_id,
                               user_map=DC_USER_MAP, context_maps=context_maps,
                               linktype_map=ltm_direct)
            rule_total = sum(counts.values())
            if rule_total:
                print(f"\n  [{name}] — {rule_total} replacement(s)")
                for cat, c in counts.items():
                    if c:
                        print(f"    {cat}: {c}")
            for cat, c in counts.items():
                total_counts[cat] += c

        out_file = args.json_fix.rsplit(".", 1)
        out_path = f"{out_file[0]}-fixed.{out_file[1]}" if len(out_file) == 2 else f"{args.json_fix}-fixed"

        if not args.dry_run:
            with open(out_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\n  Fixed file written to: {out_path}")
        else:
            print(f"\n  DRY RUN — would write to: {out_path}")

        print(f"\n{'=' * 65}")
        print(f"TOTALS: {dict(total_counts)}")
        print("=" * 65)

        if args.export:
            export_data = {
                "mode": "bridge", "bridge": args.bridge_site, "target": args.target_site,
                "fields": fm, "statuses": sm, "projects": pm, "portals": ptm,
                "unmapped_fields": f_unmapped, "unmapped_statuses": s_unmapped,
                "unmapped_projects": p_unmapped,
            }
            with open(args.export, "w") as f:
                json.dump(export_data, f, indent=2)
            print(f"  Mapping exported to {args.export}")

        print()
        return

    # ═════════════════════════════════════════════════════════
    # NORMAL MODE: both environments accessible
    # ═════════════════════════════════════════════════════════
    if args.source_is_dc:
        dc_tok = args.dc_token or args.token
        src_auth, src_hdrs = None, {"Authorization": f"Bearer {dc_tok}"}
    else:
        src_auth, src_hdrs = cloud_auth, None

    hdr = f"\n{'=' * 65}\nSOURCE: {args.source_site}\nTARGET: {args.target_site}\n{'=' * 65}"
    print(hdr)

    print("\n── Custom Fields ──")
    sf = fetch_fields(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    tf = fetch_fields(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(sf)}  |  Target: {len(tf)}")

    print("\n── Statuses ──")
    ss = fetch_statuses(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    ts = fetch_statuses(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(ss)}  |  Target: {len(ts)}")

    print("\n── Projects ──")
    sp = fetch_projects(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    tp = fetch_projects(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(sp)}  |  Target: {len(tp)}")

    print("\n── Service Desks ──")
    sd = fetch_service_desks(args.source_site, auth=src_auth, headers=src_hdrs)
    td = fetch_service_desks(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(sd)}  |  Target: {len(td)}")

    print("\n── Issue Types ──")
    sit = fetch_issue_types(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    tit = fetch_issue_types(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(sit)}  |  Target: {len(tit)}")

    print("\n── Resolutions ──")
    sr = fetch_resolutions(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    tr = fetch_resolutions(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(sr)}  |  Target: {len(tr)}")

    print("\n── Link Types ──")
    slt = fetch_link_types(args.source_site, auth=src_auth, headers=src_hdrs, is_dc=args.source_is_dc)
    tlt = fetch_link_types(args.target_site, auth=cloud_auth)
    print(f"  Source: {len(slt)}  |  Target: {len(tlt)}")

    print(f"\n{'=' * 65}\nBUILDING MAPPINGS\n{'=' * 65}")

    fm, f_ambig, f_miss, f_same = _match_by_name(sf, tf, "type")
    sm, s_ambig, s_miss, s_same = _match_by_name(ss, ts, "category")
    pm, p_miss, p_same = build_project_mapping(sp, tp)
    ptm = build_portal_mapping(sd, td)
    itm, it_ambig, it_miss, it_same = _match_by_name(sit, tit, "subtask")
    rm, r_ambig, r_miss, r_same = _match_by_name(sr, tr)
    ltm, lt_ambig, lt_miss, lt_same = _match_by_name(slt, tlt)

    # Build context maps for contextual replacement
    context_maps = {}
    if itm:
        context_maps["issuetype"] = itm
    if rm:
        context_maps["resolution"] = rm

    print(f"\n  Custom Fields:  {f_same} same, {len(fm)} changed, {len(f_ambig)} ambiguous, {len(f_miss)} missing on target")
    print(f"  Statuses:       {s_same} same, {len(sm)} changed, {len(s_ambig)} ambiguous, {len(s_miss)} missing on target")
    print(f"  Projects:       {p_same} same, {len(pm)} changed, {len(p_miss)} missing on target")
    print(f"  Portals:        {len(ptm)} changed")
    print(f"  Issue Types:    {it_same} same, {len(itm)} changed, {len(it_ambig)} ambiguous, {len(it_miss)} missing on target")
    print(f"  Resolutions:    {r_same} same, {len(rm)} changed, {len(r_ambig)} ambiguous, {len(r_miss)} missing on target")
    print(f"  Link Types:     {lt_same} same, {len(ltm)} changed, {len(lt_ambig)} ambiguous, {len(lt_miss)} missing on target")
    print(f"  Users:          {len(DC_USER_MAP)} mapped")

    if fm:
        print(f"\n  Field ID changes:")
        for src, info in sorted(fm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if sm:
        print(f"\n  Status ID changes:")
        for src, info in sorted(sm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if pm:
        print(f"\n  Project ID changes:")
        for src, info in sorted(pm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if ptm:
        print(f"\n  Portal ID changes:")
        for src, info in ptm.items():
            print(f"    /portal/{src}/ -> /portal/{info['target_id']}/  ({info['name']})")
    if f_ambig:
        print(f"\n  ** Ambiguous fields:")
        for a in f_ambig:
            print(f"    {a['source_id']} ({a['name']}) -> candidates: {a['candidates']}")
    if s_ambig:
        print(f"\n  ** Ambiguous statuses:")
        for a in s_ambig:
            print(f"    {a['source_id']} ({a['name']}) -> candidates: {a['candidates']}")
    if f_miss:
        print(f"\n  ** Fields missing on target:")
        for m in f_miss:
            print(f"    {m['source_id']} ({m['name']})")
    if s_miss:
        print(f"\n  ** Statuses missing on target:")
        for m in s_miss:
            print(f"    {m['source_id']} ({m['name']})")
    if p_miss:
        print(f"\n  ** Projects missing on target:")
        for m in p_miss:
            print(f"    {m['source_id']} {m['key']} ({m['name']})")
    if itm:
        print(f"\n  Issue Type ID changes:")
        for src, info in sorted(itm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if it_miss:
        print(f"\n  ** Issue types missing on target:")
        for m in it_miss:
            print(f"    {m['source_id']} ({m['name']})")
    if rm:
        print(f"\n  Resolution ID changes:")
        for src, info in sorted(rm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if r_miss:
        print(f"\n  ** Resolutions missing on target:")
        for m in r_miss:
            print(f"    {m['source_id']} ({m['name']})")
    if ltm:
        print(f"\n  Link Type ID changes:")
        for src, info in sorted(ltm.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} -> {info['target_id']}  ({info['name']})")
    if lt_miss:
        print(f"\n  ** Link types missing on target:")
        for m in lt_miss:
            print(f"    {m['source_id']} ({m['name']})")

    print(f"\n  URL replacement: {args.source_site} -> {args.target_site}")
    total_changes = len(fm) + len(sm) + len(pm) + len(ptm) + len(itm) + len(rm) + len(ltm) + len(DC_USER_MAP) + 1
    print(f"\n{'=' * 65}")
    print(f"TOTAL: {total_changes} ID/URL changes to apply")
    print("=" * 65)

    if args.export:
        export_data = {
            "source": args.source_site, "target": args.target_site,
            "fields": fm, "statuses": sm, "projects": pm, "portals": ptm,
            "ambiguous_fields": f_ambig, "ambiguous_statuses": s_ambig,
            "missing_fields": f_miss, "missing_statuses": s_miss, "missing_projects": p_miss,
        }
        with open(args.export, "w") as f:
            json.dump(export_data, f, indent=2)
        print(f"\n  Mapping exported to {args.export}")

    if args.compare:
        print()
        return

    tgt_cloud_id = get_cloud_id(args.target_site, cloud_auth)
    src_cloud_id = None
    if not args.source_is_dc:
        try:
            src_cloud_id = get_cloud_id(args.source_site, (args.email, args.dc_token or args.token))
        except Exception:
            pass
    print(f"\n  Target Cloud ID: {tgt_cloud_id}")
    if src_cloud_id:
        print(f"  Source Cloud ID: {src_cloud_id}")

    # JSON File Mode (normal)
    if args.json_fix:
        print(f"\n{'=' * 65}")
        print(f"PROCESSING JSON FILE: {args.json_fix}")
        print("=" * 65)

        with open(args.json_fix, "r") as f:
            data = json.load(f)
        rules = data.get("rules", [data] if "trigger" in data else [])
        print(f"  Rules in file: {len(rules)}")

        # Filter out DC-only plugin rules
        rules, removed = filter_dc_only_rules(rules)
        if removed:
            print(f"\n  Removed {len(removed)} rule(s) with DC-only plugins:")
            for r in removed:
                print(f"    - {r['name']}  ({', '.join(r['types'])})")
            data["rules"] = rules
            print(f"  Remaining: {len(rules)} rules")

        total_counts = defaultdict(int)
        for rule in rules:
            name = rule.get("name", "?")
            counts = apply_all(rule, fm, sm, pm, ptm,
                               args.source_site, args.target_site,
                               src_cloud_id, tgt_cloud_id,
                               user_map=DC_USER_MAP, context_maps=context_maps,
                               linktype_map=ltm)
            rule_total = sum(counts.values())
            if rule_total:
                print(f"\n  [{name}] — {rule_total} replacement(s)")
                for cat, c in counts.items():
                    if c:
                        print(f"    {cat}: {c}")
            for cat, c in counts.items():
                total_counts[cat] += c

        out_file = args.json_fix.rsplit(".", 1)
        out_path = f"{out_file[0]}-fixed.{out_file[1]}" if len(out_file) == 2 else f"{args.json_fix}-fixed"

        if not args.dry_run:
            with open(out_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\n  Fixed file written to: {out_path}")
        else:
            print(f"\n  DRY RUN — would write to: {out_path}")

        print(f"\n{'=' * 65}")
        print(f"TOTALS: {dict(total_counts)}")
        print("=" * 65 + "\n")
        return

    # Live API Audit / Fix
    print(f"\n{'=' * 65}")
    print(f"SCANNING AUTOMATIONS ON: {args.target_site}")
    print("=" * 65)

    client = AutomationClient(args.target_site, tgt_cloud_id, args.email, args.token)
    print("  Fetching rule list...")
    summaries = client.list_rules()
    print(f"  Total rules: {len(summaries)}")

    tgt_field_ids = set(tf.keys())
    issues = []
    clean = 0

    for summary in summaries:
        uuid = summary.get("uuid")
        name = summary.get("name", "?")
        try:
            full = client.get_rule(uuid)
        except requests.HTTPError:
            continue

        bad_fields = find_bad_field_refs(full, tgt_field_ids)
        bad_urls = find_domain_refs(full, args.source_site)
        test_copy = copy.deepcopy(full)
        counts = apply_all(test_copy, fm, sm, pm, ptm,
                           args.source_site, args.target_site,
                           src_cloud_id, tgt_cloud_id,
                           user_map=DC_USER_MAP, context_maps=context_maps,
                           linktype_map=ltm)
        has_changes = sum(counts.values()) > 0

        if bad_fields or bad_urls or has_changes:
            issues.append({
                "uuid": uuid, "name": name, "full_rule": full,
                "bad_fields": bad_fields, "bad_urls": bad_urls, "counts": counts,
            })
            detail_parts = []
            for cat_name, cat_key in [("field(s)", "fields"), ("status(es)", "statuses"),
                                       ("project(s)", "projects"), ("portal(s)", "portals"),
                                       ("URL(s)", "urls"), ("cf[] JQL ref(s)", "cf_jql"),
                                       ("Cloud ID(s)", "cloud_ids")]:
                if counts.get(cat_key):
                    detail_parts.append(f"{counts[cat_key]} {cat_name}")
            print(f"  x {name} — {', '.join(detail_parts)}")
        else:
            clean += 1

    print(f"\n{'_' * 65}")
    print(f"Clean: {clean}  |  Need fixing: {len(issues)}")

    if issues:
        print(f"\n{'_' * 65}")
        print("DETAILS:")
        for iss in issues:
            print(f"\n  [{iss['name']}]")
            for path, ref in iss.get("bad_fields", []):
                mapped = fm.get(ref, {}).get("target_id", "??? (no mapping)")
                print(f"    FIELD  {ref} -> {mapped}")
            c = iss["counts"]
            for label, key in [("STATUS", "statuses"), ("PROJECT", "projects"),
                               ("PORTAL", "portals"), ("URL", "urls")]:
                if c.get(key):
                    print(f"    {label} replacements: {c[key]}")

    if args.fix and issues:
        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN — would fix {len(issues)} rule(s)")
        else:
            print(f"Fix {len(issues)} rule(s)? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("Aborted.")
                return

        success, fail = 0, 0
        for iss in issues:
            rule = iss["full_rule"]
            counts = apply_all(rule, fm, sm, pm, ptm,
                               args.source_site, args.target_site,
                               src_cloud_id, tgt_cloud_id,
                               user_map=DC_USER_MAP, context_maps=context_maps,
                               linktype_map=ltm)
            total = sum(counts.values())
            print(f"\n  {iss['name']}: {total} replacement(s)")
            if not args.dry_run and total > 0:
                try:
                    client.update_rule(iss["uuid"], rule)
                    print(f"    -> Updated")
                    success += 1
                except requests.HTTPError as e:
                    print(f"    -> ERROR {e.response.status_code}: {e.response.text[:200]}")
                    fail += 1
            elif args.dry_run:
                print(f"    (would update)")

        if not args.dry_run:
            print(f"\n{'=' * 65}")
            print(f"Results: {success} updated, {fail} failed")
            print("=" * 65)

    print()


if __name__ == "__main__":
    main()
