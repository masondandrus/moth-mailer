import os
import random
import requests
import json
from datetime import datetime

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "moths@yourdomain.com")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

INATURALIST_API = "https://api.inaturalist.org/v1"
MOTH_TAXON_ID = 47157


def get_sent_moths():
    if not GITHUB_TOKEN or not GIST_ID:
        print("No Gist configured, skipping duplicate check")
        return set()
    response = requests.get(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}
    )
    if not response.ok:
        print(f"Warning: Could not fetch Gist: {response.status_code}")
        return set()
    gist_data = response.json()
    content = gist_data["files"]["sent_moths.json"]["content"]
    sent_ids = json.loads(content)
    return set(sent_ids)


def save_sent_moth(moth_id):
    if not GITHUB_TOKEN or not GIST_ID:
        return
    sent_moths = get_sent_moths()
    sent_moths.add(moth_id)
    response = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "files": {
                "sent_moths.json": {
                    "content": json.dumps(list(sent_moths))
                }
            }
        }
    )
    if not response.ok:
        print(f"Warning: Could not update Gist: {response.status_code}")


def get_moth_count():
    sent_moths = get_sent_moths()
    return len(sent_moths) + 1


def fetch_random_moth():
    sent_moths = get_sent_moths()
    print(f"Already sent {len(sent_moths)} unique moths")
    params = {
        "taxon_id": MOTH_TAXON_ID,
        "quality_grade": "research",
        "photos": "true",
        "photo_licensed": "true",
        "per_page": 200,
        "order_by": "random",
        "without_taxon_id": 47224,
    }
    response = requests.get(
        f"{INATURALIST_API}/observations",
        params=params,
        headers={"User-Agent": "MothMailer/1.0"}
    )
    response.raise_for_status()
    results = response.json()["results"]
    if not results:
        raise Exception("No moth observations found")
    new_moths = [m for m in results if m["id"] not in sent_moths]
    favorited_moths = [m for m in new_moths if m.get("faves_count", 0) > 0]
    print(f"Found {len(new_moths)} unsent moths, {len(favorited_moths)} with favorites")
    if favorited_moths:
        new_moths = favorited_moths
    if not new_moths:
        print("All fetched moths already sent, fetching another batch...")
        response = requests.get(
            f"{INATURALIST_API}/observations",
            params=params,
            headers={"User-Agent": "MothMailer/1.0"}
        )
        response.raise_for_status()
        results = response.json()["results"]
        new_moths = [m for m in results if m["id"] not in sent_moths]
        favorited_moths = [m for m in new_moths if m.get("faves_count", 0) > 0]
        if favorited_moths:
            new_moths = favorited_moths
        if not new_moths:
            raise Exception("Could not find unsent moth after retry")
    observation = random.choice(new_moths)
    photo = observation["photos"][0]
    photo_url = photo["url"]
    for size in ["square", "small", "medium", "thumb"]:
        if size in photo_url:
            photo_url = photo_url.replace(size, "large")
            break
    taxon = observation.get("taxon", {})
    common_name = taxon.get("preferred_common_name", "Unknown moth")
    scientific_name = taxon.get("name", "Species unknown")
    place = observation.get("place_guess", "Location unknown")
    obs_url = f"https://www.inaturalist.org/observations/{observation['id']}"
    observations_count = taxon.get("observations_count", 0)
    wikipedia_url = taxon.get("wikipedia_url", None)
    ancestors = taxon.get("ancestors", [])
    family = None
    for ancestor in ancestors:
        if ancestor.get("rank") == "family":
            family_name = ancestor.get("name", "")
            family_common = ancestor.get("preferred_common_name", "")
            if family_common:
                family = f"{family_name} — {family_common}"
            else:
                family = family_name
            break
    return {
        "id": observation["id"],
        "photo_url": photo_url,
        "common_name": common_name,
        "scientific_name": scientific_name,
        "place": place,
        "observation_url": obs_url,
        "attribution": photo.get("attribution", "Unknown photographer"),
        "observations_count": observations_count,
        "wikipedia_url": wikipedia_url,
        "family": family,
    }


def build_email_html(moth, moth_number):
    rarity_text = ""
    if moth["observations_count"] > 0:
        count = moth["observations_count"]
        if count < 500:
            rarity_text = f"🌟 Rare — only {count:,} observations worldwide"
        elif count < 2000:
            rarity_text = f"📊 {count:,} observations worldwide"
        else:
            rarity_text = f"📊 {count:,} observations worldwide"
    family_text = ""
    if moth["family"]:
        family_text = f"<p>🔬 Family: {moth['family']}</p>"
    search_name = moth["scientific_name"].replace(" ", "+")
    if moth["wikipedia_url"]:
        wiki_text = f'<p>📚 <a href="{moth["wikipedia_url"]}">Learn more on Wikipedia</a></p>'
    else:
        wiki_text = f'<p>📚 <a href="https://www.google.com/search?q={search_name}+moth">Search for more info</a></p>'
    etymology_url = f"https://www.google.com/search?q={search_name}+etymology+meaning"
    etymology_text = f'<p>📖 <a href="{etymology_url}">What does the name mean?</a></p>'
    return f"""<!DOCTYPE html>
<html>
<head>
<style>
body {{ font-family: Georgia, serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #faf9f7; color: #2d2d2d; text-align: center; }}
.moth-number {{ font-size: 14px; color: #888; margin-bottom: 5px; }}
.moth-image {{ width: 100%; max-width: 550px; border-radius: 8px; margin: 20px auto; }}
.species-name {{ font-size: 24px; margin: 10px 0 5px 0; }}
.scientific-name {{ font-style: italic; color: #666; margin: 0 0 15px 0; }}
.details {{ font-size: 14px; color: #555; line-height: 1.8; }}
.rarity {{ font-size: 13px; color: #888; margin-top: 10px; }}
.footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #e0e0e0; font-size: 12px; color: #888; }}
a {{ color: #6b705c; }}
</style>
</head>
<body>
<p class="moth-number">Moth #{moth_number}</p>
<h1 style="margin: 0; font-weight: normal; padding-bottom: 20px; border-bottom: 1px solid #e0e0e0;">🦋 Your Hourly Moth</h1>
<img src="{moth['photo_url']}" alt="{moth['common_name']}" class="moth-image">
<h2 class="species-name">{moth['common_name']}</h2>
<p class="scientific-name">{moth['scientific_name']}</p>
<div class="details">
{family_text}
<p>📍 {moth['place']}</p>
<p><a href="{moth['observation_url']}">View on iNaturalist →</a></p>
{wiki_text}
{etymology_text}
</div>
<p class="rarity">{rarity_text}</p>
<div class="footer">
<p>Photo: {moth['attribution']}</p>
</div>
</body>
</html>"""


def send_email(moth, moth_number):
    if not RESEND_API_KEY:
        raise Exception("RESEND_API_KEY environment variable not set")
    if not RECIPIENT_EMAIL:
        raise Exception("RECIPIENT_EMAIL environment variable not set")
    html_content = build_email_html(moth, moth_number)
    subject = f"🦋 Your Hourly Moth: {moth['common_name']}"
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": SENDER_EMAIL,
            "to": [RECIPIENT_EMAIL],
            "subject": subject,
            "html": html_content,
        },
    )
    if not response.ok:
        raise Exception(f"Failed to send email: {response.status_code} {response.text}")
    return response.json()


def main():
    print(f"[{datetime.now().isoformat()}] Starting Moth Mailer...")
    try:
        moth_number = get_moth_count()
        print(f"This will be moth #{moth_number}")
        print("Fetching random moth from iNaturalist...")
        moth = fetch_random_moth()
        print(f"Found: {moth['common_name']} ({moth['scientific_name']})")
        print(f"Sending to {RECIPIENT_EMAIL}...")
        result = send_email(moth, moth_number)
        print(f"Email sent successfully! ID: {result.get('id', 'unknown')}")
        save_sent_moth(moth["id"])
        print(f"Saved moth {moth['id']} to sent list")
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
