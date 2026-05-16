"""
Moths for Anna — hourly cron (v2: dual-write to D1 + gist).

CHANGES vs v1:
  - Adds D1 write via POST /api/moth/observations (with skip_gist_sync=true)
    before the existing direct-gist PATCH. D1 stays in sync going forward.
  - Independent writes: D1 failure does NOT block gist write (Anna still gets
    her moth). Gist failure does NOT block D1 (data integrity preserved).
  - Loud per-write success/failure logging.
  - Exits nonzero if EITHER write failed, so the cron host surfaces the error.

ENV VARS REQUIRED:
    GH_TOKEN      — GitHub PAT with gist scope (existing)
    GIST_ID       — gist id 13f60f151dac089590b88b9a55c4140e (existing)
    API_SECRET    — Worker API secret (NEW; see Cloudflare Worker → Settings → Variables)

WHY NO EXPLICIT sequence_number IN THE D1 POST:
    D1 schema has UNIQUE(taxon_group, sequence_number). If we send moth_number
    (derived from gist length) as sequence_number, and D1 has any gap or has
    drifted ahead of gist from a past partial failure, the INSERT collides
    and gets ignored (INSERT OR IGNORE). By NOT sending sequence_number we
    let the Worker auto-assign MAX(seq)+1 from D1, guaranteeing a successful
    insert. moth_number (cosmetic, shown in UI) and sequence_number (D1
    internal ordering) can briefly diverge under drift; repair scripts treat
    them independently. Run repair_observations_drift.py to reconcile.

WHY WE WRITE D1 BEFORE GIST:
    D1 is the more reliable store (no rate limits, no truncation). If gist
    write fails, D1 still has the moth and is the durable record. If D1 fails
    and gist succeeds, next hour's run will pick a different moth (since the
    failed one is still missing from gist's sent_ids); the missing D1 entry
    is caught by repair_observations_drift.py.
"""

import os
import random
import requests
import json
import csv
import io
from datetime import datetime, timezone

# --- Config ----------------------------------------------------------------
GH_TOKEN   = os.environ.get("GH_TOKEN")
GIST_ID    = os.environ.get("GIST_ID")
API_SECRET = os.environ.get("API_SECRET")
WORKER_URL = os.environ.get("WORKER_URL", "https://moth-favorites.masondandrus.workers.dev")

INATURALIST_API = "https://api.inaturalist.org/v1"
MOTH_TAXON_ID = 47157

# --- Caches (in-process) ---------------------------------------------------
_cached_sent_moths = None


# ============================================================================
# Gist read helpers (unchanged from v1)
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

    # Check if content is truncated (large files)
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


def get_sent_moth_ids():
    sent_moths = get_sent_moths()
    if sent_moths and isinstance(sent_moths[0], dict):
        obs_ids = set(m["id"] for m in sent_moths)
        sent_species = set()
        for m in sent_moths:
            if "scientific_name" in m:
                sent_species.add(m["scientific_name"])
        return obs_ids, sent_species
    return set(sent_moths), set()


def get_moth_count():
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
# Write paths (D1 via Worker, gist via direct PATCH)
# ============================================================================

def save_moth_to_d1(moth):
    """
    POST the moth to the Worker's /api/moth/observations endpoint with
    skip_gist_sync=true (Worker writes to D1 only; does NOT touch gist).

    Returns (success: bool, error_message_or_None, inserted: bool|None).
    inserted=False means D1 ignored the insert (id or sequence_number collision);
    that's not an error per se but a signal that the moth was already known.
    """
    if not API_SECRET:
        return (False, "API_SECRET env var not set", None)
    try:
        body = {
            "id": moth["id"],
            # Intentionally NOT sending sequence_number — let Worker auto-assign.
            # See module docstring for rationale.
            "sent_at": moth.get("sent_at"),
            "common_name": moth.get("common_name"),
            "scientific_name": moth.get("scientific_name"),
            "family": moth.get("family"),
            "place": moth.get("place"),
            "observation_url": moth.get("observation_url"),
            "photo_url": moth.get("photo_url"),
            "attribution": moth.get("attribution"),
            "observations_count": moth.get("observations_count"),
            "source": "hourly",
            "skip_gist_sync": True,
        }
        response = requests.post(
            f"{WORKER_URL}/api/moth/observations",
            json=body,
            headers={"X-API-Secret": API_SECRET, "Content-Type": "application/json"},
            timeout=30,
        )
        if not response.ok:
            return (False, f"HTTP {response.status_code}: {response.text[:200]}", None)
        payload = response.json()
        if not payload.get("ok"):
            return (False, f"Worker returned: {payload.get('error')}", None)
        inserted = payload.get("data", {}).get("inserted")
        return (True, None, inserted)
    except Exception as e:
        return (False, f"Exception: {e}", None)


def save_moth_to_gist(moth):
    """
    Append the moth to gist's sent_moths.json + moths.csv via direct PATCH.
    Reads current sent_moths from gist (or cache), appends, writes back.

    Returns (success: bool, error_message_or_None).
    """
    if not GH_TOKEN or not GIST_ID:
        return (False, "GH_TOKEN or GIST_ID not configured")
    try:
        sent_moths = get_sent_moths()
        if sent_moths and not isinstance(sent_moths[0], dict):
            sent_moths = []
        sent_moths.append(moth)
        csv_content = moths_to_csv(sent_moths)
        response = requests.patch(
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
        if not response.ok:
            return (False, f"HTTP {response.status_code}: {response.text[:200]}")
        return (True, None)
    except Exception as e:
        return (False, f"Exception: {e}")


# ============================================================================
# iNaturalist fetch (unchanged from v1)
# ============================================================================

def get_family_info(taxon_id):
    if not taxon_id:
        return None
    response = requests.get(f"{INATURALIST_API}/taxa/{taxon_id}", headers={"User-Agent": "MothMailer/1.0"})
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
    return None


def fetch_random_moth():
    sent_ids, sent_species = get_sent_moth_ids()
    print(f"Already sent {len(sent_ids)} observations, {len(sent_species)} unique species")
    params = {
        "taxon_id": MOTH_TAXON_ID, "quality_grade": "research", "photos": "true",
        "photo_licensed": "true", "per_page": 200, "order_by": "random", "without_taxon_id": 47224
    }

    all_moths = []
    favorited_moths = []

    for attempt in range(20):
        response = requests.get(f"{INATURALIST_API}/observations", params=params, headers={"User-Agent": "MothMailer/1.0"})
        response.raise_for_status()
        results = response.json()["results"]

        new_moths = [m for m in results if m["id"] not in sent_ids]
        new_moths = [m for m in new_moths if m.get("taxon", {}).get("preferred_common_name")]
        new_moths = [m for m in new_moths if m.get("taxon", {}).get("name") not in sent_species]
        all_moths.extend(new_moths)

        favorited_moths = [m for m in all_moths if m.get("faves_count", 0) > 0]
        print(f"Attempt {attempt+1}: {len(new_moths)} new named moths, {len(favorited_moths)} total with favorites")

        if favorited_moths:
            break

    if not all_moths:
        raise Exception("No moth observations found")

    if favorited_moths:
        observation = random.choice(favorited_moths)
    else:
        observation = random.choice(all_moths)

    photo = observation["photos"][0]
    photo_url = photo["url"]
    for size in ["square", "small", "medium", "thumb"]:
        if size in photo_url:
            photo_url = photo_url.replace(size, "large")
            break
    taxon = observation.get("taxon", {})
    taxon_id = taxon.get("id", None)
    common_name = taxon.get("preferred_common_name")
    scientific_name = taxon.get("name", "Species unknown")
    place = observation.get("place_guess", "Location unknown")
    obs_url = f"https://www.inaturalist.org/observations/{observation['id']}"
    observations_count = taxon.get("observations_count", 0)
    family = get_family_info(taxon_id)
    return {
        "id": observation["id"],
        "photo_url": photo_url,
        "common_name": common_name,
        "scientific_name": scientific_name,
        "place": place,
        "observation_url": obs_url,
        "attribution": photo.get("attribution", "Unknown photographer"),
        "observations_count": observations_count,
        "family": family
    }


# ============================================================================
# Main
# ============================================================================

def main():
    print(f"[{datetime.now().isoformat()}] Starting Moth Fetcher (v2 — D1+gist dual-write)...")

    # Validate config up front
    config_problems = []
    if not GH_TOKEN:   config_problems.append("GH_TOKEN")
    if not GIST_ID:    config_problems.append("GIST_ID")
    if not API_SECRET: config_problems.append("API_SECRET")
    if config_problems:
        print(f"  ⛔ Missing env vars: {', '.join(config_problems)}")
        raise Exception(f"Missing required env vars: {', '.join(config_problems)}")

    try:
        moth_number = get_moth_count()
        print(f"This will be moth #{moth_number}")
        print("Fetching random moth from iNaturalist...")
        moth = fetch_random_moth()
        print(f"Found: {moth['common_name']} ({moth['scientific_name']})")
        if moth.get("family"):
            print(f"Family: {moth['family']}")

        moth["moth_number"] = moth_number
        moth["sent_at"] = datetime.now(timezone.utc).isoformat()

        # ----- Write 1: D1 (via Worker, no gist side-effect) -----
        print("Writing to D1 via Worker...")
        d1_ok, d1_err, d1_inserted = save_moth_to_d1(moth)
        if d1_ok:
            if d1_inserted is False:
                print(f"  ⚠ D1 reports inserted=false (id or sequence collision; probably already there)")
            else:
                print(f"  ✓ D1 write succeeded")
        else:
            print(f"  ✗ D1 WRITE FAILED: {d1_err}")
            print(f"  WARNING: D1 will be missing this moth (id={moth['id']}).")
            print(f"  Run repair_observations_drift.py from your local OneDrive copy to fix.")

        # ----- Write 2: gist (direct PATCH, append-only) -----
        print("Writing to gist via direct PATCH...")
        gist_ok, gist_err = save_moth_to_gist(moth)
        if gist_ok:
            print(f"  ✓ Gist write succeeded — Anna will see this moth.")
        else:
            print(f"  ✗ GIST WRITE FAILED: {gist_err}")
            if d1_ok:
                print(f"  Moth is in D1 but not gist; Anna won't see it this hour.")
                print(f"  Next cron run will likely pick the same moth from iNat and re-attempt.")
            else:
                print(f"  Moth is in neither store. This hour's moth was lost.")

        # Final exit semantics: nonzero if EITHER write failed
        if not d1_ok and not gist_ok:
            raise Exception(f"Both writes failed. D1: {d1_err} | Gist: {gist_err}")
        if not d1_ok:
            raise Exception(f"D1 write failed (gist succeeded): {d1_err}")
        if not gist_ok:
            raise Exception(f"Gist write failed (D1 succeeded): {gist_err}")

        print(f"Saved moth {moth['id']} to website and CSV (D1 ✓ + gist ✓)")

    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
