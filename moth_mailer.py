"""
Critters for Anna — hourly cron (v4).

WHAT v4 ADDS over v3
  1. MIN-INTERVAL GUARD. The run exits 0 without posting if this group's
     newest observation is younger than --min-interval-minutes (default 45).
     This is what makes it safe to have TWO schedulers pointed at this
     workflow: the Cloudflare Cron Trigger (punctual, primary, fires at :00)
     and GitHub's own `schedule:` (unreliable, fallback, fires at :30).
     Whichever gets there first wins; the other one no-ops.

     Background: GitHub deprioritizes schedule-triggered runs on the shared
     free runner pool. Measured on this repo — an hourly cron at :57 was
     actually delivering roughly one run every 2h40m, with start times
     scattered 12-47 minutes late. The workflow was firing about 9 times a
     day instead of 24. The script was fine: 3,748 workflow runs had produced
     3,649 moths, a 97% hit rate. The runs simply weren't happening.

  2. SOFT SPECIES DEDUP WITH AUTOMATIC FALLBACK. v3 hard-filtered every
     candidate whose species had ever been sent, which meant each section
     would eventually go silent forever once its species pool thinned.
     v4 makes it a two-tier preference:

         Tier 1  an observation of a species Anna has never seen   (preferred)
         Tier 2  a NEW observation of a species she has seen       (fallback)

     Tier 2 is only used when a full sweep of attempts turns up no Tier 1
     candidate. This is self-resetting by construction — there is no
     "exhausted" flag to set, no counter, no state. While fresh species
     exist you get fresh species; when they run out the section keeps
     running with a different animal, photographer and place. Observation-ID
     dedup is still absolute, so the same photo never appears twice.

  3. taxon_id. Dedup now keys on iNat's stable integer taxon_id, falling back
     to scientific_name for rows written before the column existed. iNat
     renames species constantly (splits, synonymies); a name-keyed set drifts.

  4. Compact dedup read. Uses GET /api/<group>/known (ids + taxon_ids +
     species, ~150 KB) instead of GET /api/<group>/observations (every column
     of every row, ~2.5 MB for moths). Falls back to the old endpoint
     automatically if the Worker hasn't been redeployed yet, so deploy order
     between this file and the Worker doesn't matter.

INHERITED FROM v3 (unchanged)
  - `--group {moth|nudibranch|amphibian}` selects the creature.
  - MOTH path dual-writes to D1 (via Worker, skip_gist_sync) AND the legacy
    gist (direct PATCH). NUDIBRANCH / AMPHIBIAN are D1-only by design.
  - `--seed N` fetches up to N observations in one run (used once per new
    section). Seed runs bypass the min-interval guard.
  - Sequence numbers are always assigned server-side by the Worker
    (MAX(seq)+1 per taxon_group). We never send an explicit one.
  - Observations annotated Dead on iNaturalist are excluded at fetch time.

COMMON-NAME POLICY (per creature)
  - moth:        REQUIRE preferred_common_name (unchanged).
  - nudibranch:  PREFER but do not require (many sea slugs lack one).
  - amphibian:   PREFER but do not require.

ENV VARS
    GH_TOKEN    — GitHub PAT (moth gist only; unused for other creatures)
    GIST_ID     — moth gist id (moth only)
    API_SECRET  — Worker API secret (all creatures; required)
    WORKER_URL  — defaults to the production Worker
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

import requests

# --- Config ----------------------------------------------------------------
GH_TOKEN   = os.environ.get("GH_TOKEN")
GIST_ID    = os.environ.get("GIST_ID")
API_SECRET = os.environ.get("API_SECRET")
WORKER_URL = os.environ.get("WORKER_URL", "https://moth-favorites.masondandrus.workers.dev")

INATURALIST_API = "https://api.inaturalist.org/v1"

# Default minimum gap between observations for one group. Sized so the
# Cloudflare trigger (:00) and the GitHub fallback schedule (:30) can both be
# live without ever double-posting:
#   Cloudflare fires :00 -> posts.  GitHub fires :30 -> row is 30m old -> skip.
#   Cloudflare fails :00.           GitHub fires :30 -> row is 90m old -> posts.
DEFAULT_MIN_INTERVAL_MINUTES = 45

# How many Tier-2 (repeat-species) candidates to bank while sweeping for
# Tier-1. Only ever drawn from if the sweep finds nothing fresh.
REPEAT_POOL_CAP = 60

# Stop sweeping for new species after this many consecutive attempts that
# turned up zero Tier-1 candidates, provided the Tier-2 bank can already fill
# the run. Each attempt is 200 random observations, so five fruitless attempts
# means ~1,000 draws produced nothing new — strong evidence the pool is dry.
# Without this, an exhausted group would hit iNaturalist 20 times every hour
# just to reach a conclusion it will reach again next hour. Costs at most one
# extra repeat if the guess is wrong, and the next run re-sweeps from scratch.
FRUITLESS_ATTEMPTS_BEFORE_FALLBACK = 5

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
        "taxon_id": 47113,
        "without_taxon_id": None,
        "require_common_name": False,  # many nudibranchs have no common name
        "uses_gist": False,            # D1-only by design
        "display": "Nudibranch",
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
_gist_read_ok = False


# ============================================================================
# Time helpers
# ============================================================================

def parse_iso_utc(value):
    """
    Parse a sent_at value into an aware UTC datetime, or None.

    The observations table has three timestamp shapes in it, for historical
    reasons: naive ('2025-12-05T21:11:48.275145'), offset-suffixed
    ('2026-06-05T17:07:51.150159+00:00'), and Z-suffixed (rows the Worker
    inserted with new Date().toISOString()). Naive values were always written
    as UTC, so we attach UTC rather than guessing local.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_since_latest(group):
    """
    Age in seconds of this group's newest observation, or None if it can't be
    determined (Worker not yet redeployed, empty group, unparseable stamp).
    None always means "no opinion" — the caller proceeds.
    """
    try:
        response = requests.get(f"{WORKER_URL}/api/{group}/latest", timeout=20)
    except Exception as exc:
        print(f"  Warning: /latest fetch exception ({exc}); skipping interval guard")
        return None

    if response.status_code == 404:
        print("  Note: /latest not found — Worker predates v3. Skipping interval guard.")
        return None
    if not response.ok:
        print(f"  Warning: /latest returned HTTP {response.status_code}; skipping interval guard")
        return None

    try:
        payload = response.json()
    except Exception:
        print("  Warning: /latest returned unparseable JSON; skipping interval guard")
        return None

    if not payload.get("ok"):
        print(f"  Warning: /latest returned not ok ({payload.get('error')}); skipping interval guard")
        return None

    data = payload.get("data") or {}
    latest = data.get("latest")
    if not latest:
        print(f"  Group '{group}' is empty — no interval to respect.")
        return None

    stamp = parse_iso_utc(latest.get("sent_at"))
    if stamp is None:
        print("  Warning: could not parse latest sent_at; skipping interval guard")
        return None

    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    seq = latest.get("sequence_number")
    print(f"  Newest {group} is #{seq}, {age / 60:.0f} min old "
          f"({data.get('count')} total in this group)")
    return age


# ============================================================================
# Moth-gist read helpers (MOTH ONLY — unchanged from v2)
# ============================================================================

def get_sent_moths():
    global _cached_sent_moths, _gist_read_ok
    if _cached_sent_moths is not None:
        return _cached_sent_moths
    _gist_read_ok = False
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
    _gist_read_ok = True
    return sent_moths


def get_sent_moth_ids_from_gist():
    """Returns (obs_ids, taxon_ids, species). The gist has never stored
    taxon_id, so the middle set is always empty — it exists to keep the
    tuple shape identical to the D1 reader."""
    sent_moths = get_sent_moths()
    if sent_moths and isinstance(sent_moths[0], dict):
        obs_ids = set(m["id"] for m in sent_moths)
        sent_species = set()
        for m in sent_moths:
            if m.get("scientific_name"):
                sent_species.add(m["scientific_name"])
        return obs_ids, set(), sent_species
    return set(sent_moths), set(), set()


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
# D1 dedup state (ALL creatures)
# ============================================================================

def get_sent_state_from_d1(group):
    """
    Returns (obs_ids, taxon_ids, species).

    Prefers GET /api/<group>/known — the Worker pre-reduces the table to three
    arrays, so we transfer ~150 KB instead of ~2.5 MB. Falls back to the old
    GET /api/<group>/observations if /known isn't there yet, which makes this
    file safe to deploy before or after the Worker.
    """
    # Preferred: compact endpoint
    try:
        response = requests.get(f"{WORKER_URL}/api/{group}/known", timeout=30)
        if response.ok:
            payload = response.json()
            if payload.get("ok"):
                data = payload.get("data") or {}
                return (
                    set(data.get("ids") or []),
                    set(data.get("taxon_ids") or []),
                    set(data.get("species") or []),
                )
            print(f"  Warning: /known returned not ok: {payload.get('error')}")
        elif response.status_code != 404:
            print(f"  Warning: /known returned HTTP {response.status_code}")
    except Exception as exc:
        print(f"  Warning: /known fetch exception: {exc}")

    # Fallback: full observations list (pre-v3 Worker)
    print("  Falling back to /observations for dedup state")
    try:
        response = requests.get(f"{WORKER_URL}/api/{group}/observations", timeout=60)
        if not response.ok:
            print(f"  Warning: D1 observations fetch failed: HTTP {response.status_code}")
            return set(), set(), set()
        payload = response.json()
        if not payload.get("ok"):
            print(f"  Warning: D1 observations returned not ok: {payload.get('error')}")
            return set(), set(), set()
        rows = payload.get("data", [])
        ids = set(row["id"] for row in rows)
        taxon_ids = set(row["taxon_id"] for row in rows if row.get("taxon_id") is not None)
        species = set(row["scientific_name"] for row in rows if row.get("scientific_name"))
        return ids, taxon_ids, species
    except Exception as exc:
        print(f"  Warning: D1 observations fetch exception: {exc}")
        return set(), set(), set()


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
            "taxon_id": obs.get("taxon_id"),
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
        # The PATCH below replaces the whole file. If the read failed,
        # sent_moths is [] and writing would erase every stored moth.
        if not _gist_read_ok:
            return (False, "gist read failed; refusing to overwrite the gist")
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
# iNaturalist fetch
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
        "taxon_id": taxon_id,
        "place": observation.get("place_guess", "Location unknown"),
        "observation_url": f"https://www.inaturalist.org/observations/{observation['id']}",
        "attribution": photo.get("attribution", "Unknown photographer"),
        "observations_count": taxon.get("observations_count", 0),
        "family": get_family_info(taxon_id),
    }


def _species_seen(taxon, sent_taxon_ids, sent_species, picked_taxon_ids, picked_species):
    """
    True if this taxon has already been shown. Checks the stable integer
    taxon_id first, then the scientific name — rows written before the
    taxon_id column existed only carry a name, so both are consulted.
    """
    tid = taxon.get("id")
    if tid is not None and (tid in sent_taxon_ids or tid in picked_taxon_ids):
        return True
    sci = taxon.get("name")
    if sci and (sci in sent_species or sci in picked_species):
        return True
    return False


def fetch_new_observations(group, sent_ids, sent_taxon_ids, sent_species, want):
    """
    Pull up to `want` observations for `group` that Anna has not seen.

    Two tiers, in strict preference order:
      Tier 1  new observation AND a species she has never seen
      Tier 2  new observation of a species she HAS seen

    Tier 2 candidates are banked while sweeping but only drawn from after the
    full sweep fails to fill `want` from Tier 1. That ordering is the whole
    trick: no exhaustion detection, no reset flag, no per-group config. While
    fresh species exist they win every time; when a pool runs dry the section
    keeps producing instead of going silent, showing a different individual
    of a familiar species. Observation-ID dedup is absolute in both tiers, so
    a photo is never repeated.

    Returns a list of shaped observation dicts (possibly shorter than `want`,
    possibly empty).
    """
    cfg = TAXA[group]
    params = {
        "taxon_id": cfg["taxon_id"],
        "quality_grade": "research",
        "photos": "true",
        "photo_licensed": "true",
        "per_page": 200,
        "order_by": "random",
        # Exclude observations explicitly annotated Dead (term_id=17 "Alive or
        # Dead", value 19 = Dead). `term_id_or_unknown` is the key trick:
        # without it, `without_term_value_id` also drops unannotated obs,
        # which is most of them. With it, we keep:
        #   - unannotated obs (no Alive/Dead value)
        #   - obs marked Alive (18) or Cannot Be Determined (20)
        # and exclude only obs marked Dead. Confirmed working per iNat forum
        # follow-up Nov 2025: forum.inaturalist.org/t/.../30148 (item #24).
        # Universal across creatures: amphibians get hit most (roadkill etc.);
        # moths/nudibranchs almost never have this annotation, so this is a
        # no-op for them but cheap insurance.
        "term_id_or_unknown": 17,
        "without_term_value_id": 19,
    }
    if cfg["without_taxon_id"] is not None:
        params["without_taxon_id"] = cfg["without_taxon_id"]

    picked = []
    picked_ids = set()
    picked_taxon_ids = set()
    picked_species = set()

    repeat_pool = []        # Tier 2 bank
    repeat_pool_ids = set()
    fruitless_streak = 0    # consecutive attempts with zero Tier-1 candidates

    # More attempts when seeding many; capped to protect iNat.
    max_attempts = max(20, want * 3)
    attempts_used = 0

    for attempt in range(max_attempts):
        if len(picked) >= want:
            break

        # Early exit: the sweep has clearly stopped producing new species and
        # we already have enough banked repeats to finish the run.
        if (fruitless_streak >= FRUITLESS_ATTEMPTS_BEFORE_FALLBACK
                and len(picked) + len(repeat_pool) >= want):
            print(f"  {fruitless_streak} consecutive attempts found no new species "
                  f"and {len(repeat_pool)} repeat candidate(s) are banked — "
                  f"stopping the sweep early instead of hammering iNaturalist.")
            break

        attempts_used = attempt + 1
        response = requests.get(
            f"{INATURALIST_API}/observations", params=params,
            headers={"User-Agent": "CritterMailer/1.0"}
        )
        response.raise_for_status()
        results = response.json()["results"]

        fresh_candidates = []
        repeat_candidates = []

        for m in results:
            mid = m["id"]
            if mid in sent_ids or mid in picked_ids:
                continue
            if not m.get("photos"):
                continue
            taxon = m.get("taxon", {}) or {}
            if not taxon.get("name"):
                continue
            if cfg["require_common_name"] and not taxon.get("preferred_common_name"):
                continue

            if _species_seen(taxon, sent_taxon_ids, sent_species,
                             picked_taxon_ids, picked_species):
                if mid not in repeat_pool_ids:
                    repeat_candidates.append(m)
            else:
                fresh_candidates.append(m)

        # Prefer favorited ones (curated). Sort so faved come first; keep order
        # otherwise (already random from the API).
        fresh_candidates.sort(key=lambda m: m.get("faves_count", 0) > 0, reverse=True)
        repeat_candidates.sort(key=lambda m: m.get("faves_count", 0) > 0, reverse=True)

        for m in fresh_candidates:
            if len(picked) >= want:
                break
            taxon = m.get("taxon", {}) or {}
            # Re-check: an earlier pick in this same loop may have claimed it.
            if _species_seen(taxon, sent_taxon_ids, sent_species,
                             picked_taxon_ids, picked_species):
                continue
            picked.append(m)
            picked_ids.add(m["id"])
            if taxon.get("id") is not None:
                picked_taxon_ids.add(taxon["id"])
            if taxon.get("name"):
                picked_species.add(taxon["name"])

        # Bank Tier 2 for the fallback, capped.
        for m in repeat_candidates:
            if len(repeat_pool) >= REPEAT_POOL_CAP:
                break
            repeat_pool.append(m)
            repeat_pool_ids.add(m["id"])

        fruitless_streak = 0 if fresh_candidates else fruitless_streak + 1

        print(f"  Attempt {attempt + 1}: picked {len(picked)}/{want} "
              f"({len(fresh_candidates)} new species, "
              f"{len(repeat_candidates)} repeat-species this page)")

    fresh_count = len(picked)

    # ---- Tier 2 fallback ----------------------------------------------------
    if len(picked) < want and repeat_pool:
        shortfall = want - len(picked)
        print(f"  No new species left to fill {shortfall} slot(s) after "
              f"{attempts_used} attempt(s) — falling back to repeat species "
              f"(new observations, previously-seen species).")
        for m in repeat_pool:
            if len(picked) >= want:
                break
            if m["id"] in picked_ids:
                continue
            picked.append(m)
            picked_ids.add(m["id"])

    if fresh_count:
        print(f"  Tier 1 (new species): {fresh_count}")
    if len(picked) > fresh_count:
        print(f"  Tier 2 (repeat species): {len(picked) - fresh_count} "
              f"— this group's fresh-species pool is thinning.")

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
    parser.add_argument("--min-interval-minutes", type=int,
                        default=DEFAULT_MIN_INTERVAL_MINUTES,
                        help=f"Skip this run (exit 0) if the group's newest "
                             f"observation is younger than this. Lets the "
                             f"Cloudflare trigger and the GitHub fallback "
                             f"schedule coexist without double-posting. "
                             f"0 disables. Default {DEFAULT_MIN_INTERVAL_MINUTES}.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the min-interval guard.")
    args = parser.parse_args()
    group = args.group
    want = max(1, args.seed)
    cfg = TAXA[group]

    print(f"[{datetime.now().isoformat()}] Critter Fetcher v4 — group={group} want={want}")

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
        # --- Min-interval guard ------------------------------------------
        # Seed runs and --force bypass it; so does --min-interval-minutes 0.
        guard_minutes = args.min_interval_minutes
        if guard_minutes > 0 and want == 1 and not args.force:
            age = seconds_since_latest(group)
            if age is not None and age < guard_minutes * 60:
                print(f"  Newest {group} is younger than the {guard_minutes}-minute "
                      f"minimum interval. Another scheduler already covered this "
                      f"hour. Exit 0 — this is expected, not a failure.")
                sys.exit(0)

        # --- Dedup state --------------------------------------------------
        # D1 for everyone; union the moth gist too so a moth present in
        # EITHER store is skipped.
        d1_ids, d1_taxon_ids, d1_species = get_sent_state_from_d1(group)
        if cfg["uses_gist"]:
            g_ids, g_taxon_ids, g_species = get_sent_moth_ids_from_gist()
            sent_ids = d1_ids | g_ids
            sent_taxon_ids = d1_taxon_ids | g_taxon_ids
            sent_species = d1_species | g_species
        else:
            sent_ids, sent_taxon_ids, sent_species = d1_ids, d1_taxon_ids, d1_species
        print(f"  Known: {len(sent_ids)} observations, {len(sent_species)} species "
              f"({len(sent_taxon_ids)} with a taxon_id)")

        print(f"  Fetching up to {want} new {group}(s) from iNaturalist...")
        observations = fetch_new_observations(
            group, sent_ids, sent_taxon_ids, sent_species, want
        )

        if not observations:
            # With the Tier 2 fallback in place this should now be rare: it
            # means iNat returned no NEW OBSERVATION AT ALL for this taxon
            # across every attempt, not merely no new species.
            print(f"  No new observations available for {group} this run; skipping. "
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
