"""
Critters for Anna — hourly cron (v3: multi-creature).

WHAT v3 ADDS over v2:
  - `--group {moth|nudibranch|amphibian}` selects the creature.
  - Per-creature config (taxon id, butterfly exclusion, common-name policy).
  - MOTH path is unchanged from v2: dual-write to D1 (via Worker, skip_gist_sync)
    AND the legacy gist (direct PATCH). The moth gist stays alive through the
    eventual Phase 7 retirement, so we do NOT regress it.
  - NUDIBRANCH / AMPHIBIAN are D1-ONLY. There is no gist for them by design
    (concrete-implementation-plan.md §3.4). Their dedup state is read back
    from D1 via GET /api/<group>/observations, not from a gist.
  - Optional `--seed N`: fetch up to N new observations in one run instead of
    1. Used once per new creature to pre-populate the section so it isn't
    near-empty for the birthday reveal. Normal hourly runs omit --seed (=1).

COMMON-NAME POLICY (per creature):
  - moth:        REQUIRE preferred_common_name (unchanged v2 behavior — moths
                 almost always have one; keeps the moth feed identical).
  - nudibranch:  PREFER but do not require (many lack a common name).
  - amphibian:   PREFER but do not require.
  The frontend already renders null common_name gracefully (Phase 3 work):
  it falls back to the italicized scientific name.

SPECIES DEDUP: on for all creatures (don't show Anna the same species twice).
  If a run finds no new species it logs and exits 0 (NOT an error) — expected
  once a smaller pool (nudibranchs first) starts to thin out. Produces
  cosmetic-only gaps in sequence_number, which is acceptable and documented.

ENV VARS:
    GH_TOKEN    — GitHub PAT (moth gist only; unused for other creatures)
    GIST_ID     — moth gist id (moth only)
    API_SECRET  — Worker API secret (all creatures; required)
    WORKER_URL  — defaults to the production Worker

D1 SEQUENCE NUMBERS: we never send an explicit sequence_number; the Worker
auto-assigns MAX(seq)+1 per taxon_group. Each creature has its own independent
sequence (Moth #2934, Nudibranch #1, ...). UNIQUE(taxon_group, sequence_number)
makes that a DB invariant.
"""

import argparse
import csv
import io
import json
import os
import random
import sys
from datetime import datetime, timezone

import requests

# --- Config ----------------------------------------------------------------
GH_TOKEN   = os.environ.get("GH_TOKEN")
GIST_ID    = os.environ.get("GIST_ID")
API_SECRET = os.environ.get("API_SECRET")
WORKER_URL = os.environ.get("WORKER_URL", "https://moth-favorites.masondandrus.workers.dev")

INATURALIST_API = "https://api.inaturalist.org/v1"

# Per-creature settings. taxon ids independently verified against
# iNaturalist + Wikidata (47157 moths, 47113 Nudibranchia, 20978 Amphibia).
TAXA = {
    "moth": {
        "taxon_id": 47157,
        "without_taxon_id": 47224,   # exclude butterflies (Papilionoidea)
        "require_common_name": True,  # unchanged v2 behavior
        "uses_gist": True,            # moth keeps the legacy gist dual-write
        "display": "Moth",
    },
    "nudibranch": {
        # NOTE: the dict KEY and D1 taxon_group stay "nudibranch" for data
        # continuity (existing rows/favorites/Worker config). Only the iNat
        # taxon and the human-facing display changed: we now pull the broad
        # Opisthobranchia (nudibranchs + sea hares, bubble/sap-sucking slugs,
        # sea angels/butterflies, etc.) and present the section as "Sea Slugs".
        "taxon_id": 551392,            # Opisthobranchia (infraclass)
        "without_taxon_id": None,
        "require_common_name": False,  # most of these slugs have no common name
        "uses_gist": False,            # D1-only by design
        "display": "Sea Slug",
    },
    "amphibian": {
        "taxon_id": 20978,
        "without_taxon_id": None,
        "require_common_name": False,
        "uses_gist": False,
        "display": "Amphibian",
    },
}

# --- In-process cache (moth gist only) -------------------------------------
_cached_sent_moths = None


# ============================================================================
# Moth-gist read helpers (MOTH ONLY — unchanged from v2)
# ============================================================================

def get_sent_moths():
    global _cached_sent_moths
    if _cached_sent_moths is not None:
        return _cached_sent_moths
    if not GH_TOKEN or not GIST_ID:
        print("No Gist configured, skipping duplicate check")
        return []
    response = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GH_TOKEN}"}
    )
    if not response.ok:
        print(f"Warning: Could not fetch Gist: {response.status_code}")
        return []
    gist_data = response.json()
    file_info = gist_data["files"]["sent_moths.json"]
    if file_info.get("truncated", False):
        print("File truncated, fetching full content via raw URL...")
        raw_response = requests.get(file_info["raw_url"], headers={"Authorization": f"Bearer {GH_TOKEN}"})
        if not raw_response.ok:
            print(f"Warning: Could not fetch raw content: {raw_response.status_code}")
            return []
        content = raw_response.text
    else:
        content = file_info["content"]
    sent_moths = json.loads(content)
    _cached_sent_moths = sent_moths
    return sent_moths


def get_sent_moth_ids_from_gist():
    sent_moths = get_sent_moths()
    if sent_moths and isinstance(sent_moths[0], dict):
        obs_ids = set(m["id"] for m in sent_moths)
        sent_species = set()
        for m in sent_moths:
            if "scientific_name" in m:
                sent_species.add(m["scientific_name"])
        return obs_ids, sent_species
    return set(sent_moths), set()


def get_moth_count_from_gist():
    sent_moths = get_sent_moths()
    return len(sent_moths) + 1


def moths_to_csv(moths):
    output = io.StringIO()
    fieldnames = ["moth_number", "sent_at", "common_name", "scientific_name", "family", "place", "observations_count", "observation_url", "photo_url", "attribution", "id"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for moth in moths:
        writer.writerow(moth)
    return output.getvalue()


# ============================================================================
# D1 dedup state (ALL creatures) — read existing ids+species from the Worker
# ============================================================================

def get_sent_state_from_d1(group):
    """
    GET /api/<group>/observations → list of D1 rows. Returns (ids, species).
    Used for dedup on every creature. For moths this is in ADDITION to the
    gist read (we union both so a moth already in either store is skipped).
    """
    try:
        r = requests.get(f"{WORKER_URL}/api/{group}/observations", timeout=30)
        if not r.ok:
            print(f"  Warning: D1 observations fetch failed: HTTP {r.status_code}")
            return set(), set()
        payload = r.json()
        if not payload.get("ok"):
            print(f"  Warning: D1 observations returned not ok: {payload.get('error')}")
            return set(), set()
        rows = payload.get("data", [])
        ids = set(row["id"] for row in rows)
        species = set(row["scientific_name"] for row in rows if row.get("scientific_name"))
        return ids, species
    except Exception as e:
        print(f"  Warning: D1 observations fetch exception: {e}")
        return set(), set()


# ============================================================================
# Write paths
# ============================================================================

def save_observation_to_d1(group, obs):
    """
    POST to /api/<group>/observations with skip_gist_sync=True (Worker writes
    D1 only; never touches any gist). Returns (ok, error, inserted).
    No explicit sequence_number — Worker auto-assigns MAX(seq)+1 per group.
    """
    if not API_SECRET:
        return (False, "API_SECRET env var not set", None)
    try:
        body = {
            "id": obs["id"],
            "sent_at": obs.get("sent_at"),
            "common_name": obs.get("common_name"),
            "scientific_name": obs.get("scientific_name"),
            "family": obs.get("family"),
            "place": obs.get("place"),
            "observation_url": obs.get("observation_url"),
            "photo_url": obs.get("photo_url"),
            "attribution": obs.get("attribution"),
            "observations_count": obs.get("observations_count"),
            "source": "hourly",
            "skip_gist_sync": True,
        }
        r = requests.post(
            f"{WORKER_URL}/api/{group}/observations",
            json=body,
            headers={"X-API-Secret": API_SECRET, "Content-Type": "application/json"},
            timeout=30,
        )
        if not r.ok:
            return (False, f"HTTP {r.status_code}: {r.text[:200]}", None)
        payload = r.json()
        if not payload.get("ok"):
            return (False, f"Worker returned: {payload.get('error')}", None)
        return (True, None, payload.get("data", {}).get("inserted"))
    except Exception as e:
        return (False, f"Exception: {e}", None)


def save_moth_to_gist(moth):
    """
    MOTH ONLY. Append to gist sent_moths.json + moths.csv via direct PATCH.
    Unchanged from v2. Returns (ok, error).
    """
    if not GH_TOKEN or not GIST_ID:
        return (False, "GH_TOKEN or GIST_ID not configured")
    try:
        sent_moths = get_sent_moths()
        if sent_moths and not isinstance(sent_moths[0], dict):
            sent_moths = []
        sent_moths.append(moth)
        csv_content = moths_to_csv(sent_moths)
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"files": {
                "sent_moths.json": {"content": json.dumps(sent_moths, indent=2)},
                "moths.csv": {"content": csv_content},
            }},
            timeout=60,
        )
        if not r.ok:
            return (False, f"HTTP {r.status_code}: {r.text[:200]}")
        return (True, None)
    except Exception as e:
        return (False, f"Exception: {e}")


# ============================================================================
# iNaturalist fetch (generalized)
# ============================================================================

def get_family_info(taxon_id):
    if not taxon_id:
        return None
    try:
        response = requests.get(f"{INATURALIST_API}/taxa/{taxon_id}", headers={"User-Agent": "CritterMailer/1.0"})
        if not response.ok:
            return None
        taxon_data = response.json().get("results", [])
        if not taxon_data:
            return None
        ancestors = taxon_data[0].get("ancestors", [])
        for ancestor in ancestors:
            if ancestor.get("rank") == "family":
                family_name = ancestor.get("name", "")
                family_common = ancestor.get("preferred_common_name", "")
                if family_common:
                    return f"{family_name} — {family_common}"
                return family_name
    except Exception:
        return None
    return None


def _shape_observation(observation):
    """Map a raw iNat observation into our storage dict."""
    photo = observation["photos"][0]
    photo_url = photo["url"]
    for size in ["square", "small", "medium", "thumb"]:
        if size in photo_url:
            photo_url = photo_url.replace(size, "large")
            break
    taxon = observation.get("taxon", {})
    taxon_id = taxon.get("id", None)
    return {
        "id": observation["id"],
        "photo_url": photo_url,
        "common_name": taxon.get("preferred_common_name"),  # may be None
        "scientific_name": taxon.get("name", "Species unknown"),
        "place": observation.get("place_guess", "Location unknown"),
        "observation_url": f"https://www.inaturalist.org/observations/{observation['id']}",
        "attribution": photo.get("attribution", "Unknown photographer"),
        "observations_count": taxon.get("observations_count", 0),
        "family": get_family_info(taxon_id),
    }


def fetch_new_observations(group, sent_ids, sent_species, want):
    """
    Pull up to `want` distinct new observations for `group`. Honors the
    creature's require_common_name policy and species-level dedup. Prefers
    observations that have iNat faves (the curated/pretty ones), same as v2.
    Returns a list (possibly shorter than `want`, possibly empty).
    """
    cfg = TAXA[group]
    params = {
        "taxon_id": cfg["taxon_id"],
        "quality_grade": "research",
        "photos": "true",
        "photo_licensed": "true",
        "per_page": 200,
        "order_by": "random",
    }
    if cfg["without_taxon_id"] is not None:
        params["without_taxon_id"] = cfg["without_taxon_id"]

    picked = []
    picked_ids = set()
    picked_species = set()
    # More attempts when seeding many; capped to protect iNat.
    max_attempts = max(20, want * 3)

    for attempt in range(max_attempts):
        if len(picked) >= want:
            break
        response = requests.get(
            f"{INATURALIST_API}/observations", params=params,
            headers={"User-Agent": "CritterMailer/1.0"}
        )
        response.raise_for_status()
        results = response.json()["results"]

        candidates = []
        for m in results:
            mid = m["id"]
            if mid in sent_ids or mid in picked_ids:
                continue
            taxon = m.get("taxon", {}) or {}
            sci = taxon.get("name")
            if not sci:
                continue
            if sci in sent_species or sci in picked_species:
                continue
            if cfg["require_common_name"] and not taxon.get("preferred_common_name"):
                continue
            if not m.get("photos"):
                continue
            candidates.append(m)

        # Prefer favorited ones (curated). Sort so faved come first; keep order
        # otherwise (already random from the API).
        candidates.sort(key=lambda m: m.get("faves_count", 0) > 0, reverse=True)

        for m in candidates:
            if len(picked) >= want:
                break
            sci = m["taxon"]["name"]
            if sci in picked_species:
                continue
            picked.append(m)
            picked_ids.add(m["id"])
            picked_species.add(sci)

        print(f"  Attempt {attempt + 1}: picked {len(picked)}/{want} "
              f"({len(candidates)} candidates this page)")

    return [_shape_observation(m) for m in picked]


# ============================================================================
# Main
# ============================================================================

def write_one(group, obs):
    """
    Write a single shaped observation. Moth: D1 + gist. Others: D1 only.
    Returns True on success (per that creature's definition of success).
    """
    cfg = TAXA[group]
    obs["sent_at"] = datetime.now(timezone.utc).isoformat()

    # D1 (all creatures)
    d1_ok, d1_err, d1_inserted = save_observation_to_d1(group, obs)
    if d1_ok:
        if d1_inserted is False:
            print(f"  ⚠ D1 inserted=false (id/seq collision; likely already present)")
        else:
            print(f"  ✓ D1 write succeeded")
    else:
        print(f"  ✗ D1 WRITE FAILED: {d1_err}")

    if not cfg["uses_gist"]:
        # D1-only creature: success == D1 ok
        if not d1_ok:
            print(f"  This {group} was NOT saved (D1 failed, no gist fallback).")
        return d1_ok

    # MOTH path: also write the legacy gist (must preserve v2 behavior)
    moth = dict(obs)
    moth["moth_number"] = get_moth_count_from_gist()
    gist_ok, gist_err = save_moth_to_gist(moth)
    if gist_ok:
        print(f"  ✓ Gist write succeeded — Anna will see this moth.")
    else:
        print(f"  ✗ GIST WRITE FAILED: {gist_err}")

    if not d1_ok and not gist_ok:
        print(f"  Moth in neither store. Lost this run. D1: {d1_err} | Gist: {gist_err}")
        return False
    if not d1_ok:
        print(f"  Moth in gist but not D1 — run repair_observations_drift.py.")
        return False
    if not gist_ok:
        print(f"  Moth in D1 but not gist — Anna won't see it this hour.")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Critters for Anna hourly fetcher")
    parser.add_argument("--group", default="moth", choices=sorted(TAXA.keys()),
                        help="Which creature to fetch (default: moth)")
    parser.add_argument("--seed", type=int, default=1,
                        help="How many new observations to fetch this run "
                             "(default 1 = normal hourly; use a larger number "
                             "ONCE to pre-populate a new section)")
    args = parser.parse_args()
    group = args.group
    want = max(1, args.seed)
    cfg = TAXA[group]

    print(f"[{datetime.now().isoformat()}] Critter Fetcher v3 — group={group} want={want}")

    # Validate required env. GH_TOKEN/GIST_ID only required for moths.
    problems = []
    if not API_SECRET:
        problems.append("API_SECRET")
    if cfg["uses_gist"]:
        if not GH_TOKEN:
            problems.append("GH_TOKEN")
        if not GIST_ID:
            problems.append("GIST_ID")
    if problems:
        print(f"  ⛔ Missing env vars: {', '.join(problems)}")
        raise Exception(f"Missing required env vars: {', '.join(problems)}")

    try:
        # Build dedup state. D1 for everyone; union the moth gist too so a
        # moth present in EITHER store is skipped.
        d1_ids, d1_species = get_sent_state_from_d1(group)
        if cfg["uses_gist"]:
            g_ids, g_species = get_sent_moth_ids_from_gist()
            sent_ids = d1_ids | g_ids
            sent_species = d1_species | g_species
        else:
            sent_ids, sent_species = d1_ids, d1_species
        print(f"  Known: {len(sent_ids)} observations, {len(sent_species)} species")

        print(f"  Fetching up to {want} new {group}(s) from iNaturalist...")
        observations = fetch_new_observations(group, sent_ids, sent_species, want)

        if not observations:
            # Not an error: expected once a pool thins out (nudibranchs first).
            print(f"  No new species available for {group} this run; skipping. "
                  f"(Exit 0 — this is expected, not a failure.)")
            sys.exit(0)

        print(f"  Got {len(observations)} new {group}(s) to save.")
        successes = 0
        failures = 0
        for i, obs in enumerate(observations, 1):
            label = obs.get("common_name") or obs.get("scientific_name") or "?"
            print(f"  [{i}/{len(observations)}] {label} ({obs.get('scientific_name')})")
            if write_one(group, obs):
                successes += 1
            else:
                failures += 1

        print(f"  Done: {successes} saved, {failures} failed.")
        if failures and successes == 0:
            raise Exception(f"All {failures} writes failed for {group}")
        if failures:
            # Partial success on a seed run is acceptable; surface nonzero so
            # the Action shows yellow but we don't lose the successes.
            raise Exception(f"{failures} of {len(observations)} {group} writes failed")

    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
