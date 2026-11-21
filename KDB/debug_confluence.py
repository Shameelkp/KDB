"""
Test Confluence REST API access.
    python debug_confluence.py
"""
import requests, sys

CONFLUENCE_URL = "https://cmp.allianz.net"

session = requests.Session()
session.verify = False  # internal cert may not be trusted

import urllib3
urllib3.disable_warnings()

# Test 1: simple CQL search in DBM space
print("Test 1: CQL search in DBM space")
resp = session.get(
    f"{CONFLUENCE_URL}/rest/api/content/search",
    params={
        "cql": 'space=DBM AND type=page AND text~"monitoring"',
        "limit": 5,
        "expand": "space,version",
    },
    timeout=15,
)
print(f"  HTTP {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    results = data.get("results", [])
    print(f"  {len(results)} result(s)")
    for r in results:
        title   = r.get("title", "")
        space   = r.get("space", {}).get("key", "")
        version = r.get("version", {}).get("when", "")[:10]
        url     = CONFLUENCE_URL + r.get("_links", {}).get("webui", "")
        print(f"  [{space}] {title}  ({version})")
        print(f"    {url}")
else:
    print(f"  Response: {resp.text[:300]}")

# Test 2: try with Windows auth (NTLM)
print("\nTest 2: with Windows NTLM auth")
try:
    from requests_ntlm import HttpNtlmAuth
    import getpass, os
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    resp2 = requests.get(
        f"{CONFLUENCE_URL}/rest/api/content/search",
        params={"cql": 'space=DBM AND type=page AND text~"monitoring"', "limit": 3},
        auth=HttpNtlmAuth(f"{domain}\\{user}", ""),
        verify=False,
        timeout=15,
    )
    print(f"  HTTP {resp2.status_code}")
except ImportError:
    print("  requests_ntlm not installed — skipping")
except Exception as e:
    print(f"  Error: {e}")
