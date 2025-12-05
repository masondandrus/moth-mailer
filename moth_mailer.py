import os
import random
import requests
import json
from datetime import datetime

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "moths@yourdomain.com")
GH_TOKEN = os.environ.get("GH_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

INATURALIST_API = "https://api.inaturalist.org/v1"
MOTH_TAXON_ID = 47157


def get_sent_moths():
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
    content = gist_data["files"]["sent_moths.json"]["content"]
    sent_moths = json.loads(content)
    return sent_moths


def get_sent_moth_ids():
    sent_moths = get_sent_moths()
    if sent_moths and isinstance(sent_moths[0], dict):
        return set(m["id"] for m in sent_moths)
    return set(sent_moths)


def save_sent_moth(moth):
    if not GH_TOKEN or not GIST_ID:
        return
    sent_moths = get_sent_moths()
    if sent_moths and not isinstance(sent_moths[0], dict):
        sent_moths = []
    sent_moths.append(moth)
    response = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers={"Authorization": f"Bearer {GH_TOKEN}","Content-Type": "application/json"},
        json={"files": {"sent_moths.json": {"content": json.dumps(sent_moths, indent=2)}}}
    )
    if not response.ok:
        print(f"Warning: Could not update Gist: {response.status_code}")


def get_moth_count():
    sent_moths = get_sent_moths()
    return len(sent_moths) + 1


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
    sent_ids = get_sent_moth_ids()
    print(f"Already sent {len(sent_ids)} unique moths")
    params = {"taxon_id": MOTH_TAXON_ID, "quality_grade": "research", "photos": "true", "photo_licensed": "true", "per_page": 200, "order_by": "random", "without_taxon_id": 47224}
    
    all_moths = []
    favorited_moths = []
    
    for attempt in range(20):
        response = requests.get(f"{INATURALIST_API}/observations", params=params, headers={"User-Agent": "MothMailer/1.0"})
        response.raise_for_status()
        results = response.json()["results"]
        
        new_moths = [m for m in results if m["id"] not in sent_ids]
        new_moths = [m for m in new_moths if m.get("taxon", {}).get("preferred_common_name")]
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
    return {"id": observation["id"], "photo_url": photo_url, "common_name": common_name, "scientific_name": scientific_name, "place": place, "observation_url": obs_url, "attribution": photo.get("attribution", "Unknown photographer"), "observations_count": observations_count, "family": family}


def build_email_html(moth, moth_number):
    obs_text = ""
    if moth["observations_count"] > 0:
        obs_text = f"{moth['observations_count']:,} observations on iNaturalist"
    family_text = ""
    if moth["family"]:
        family_text = f"<p style='text-align:center;'>Family: {moth['family']}</p>"
    return f"""<!DOCTYPE html>
<html>
<head>
<style>
body {{ font-family: Georgia, serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #faf9f7; color: #2d2d2d; text-align: center; }}
p {{ text-align: center; }}
h1, h2 {{ text-align: center; }}
.moth-number {{ font-size: 14px; color: #888; margin-bottom: 5px; text-align: center; }}
.moth-image {{ display: block; width: 100%; max-width: 550px; border-radius: 8px; margin: 20px auto; }}
.species-name {{ font-size: 24px; margin: 10px 0 5px 0; text-align: center; }}
.scientific-name {{ font-style: italic; color: #666; margin: 0 0 15px 0; text-align: center; }}
.details {{ font-size: 14px; color: #555; line-height: 1.8; text-align: center; }}
.obs-count {{ font-size: 13px; color: #888; margin-top: 10px; text-align: center; }}
.footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #e0e0e0; font-size: 12px; color: #888; text-align: center; }}
a {{ color: #6b705c; }}
</style>
</head>
<body>
<p class="moth-number">Moth #{moth_number}</p>
<h1 style="margin: 0; font-weight: normal; padding-bottom: 20px; border-bottom: 1px solid #e0e0e0;">Hourly Moth 🦋</h1>
<img src="{moth['photo_url']}" alt="{moth['common_name']}" class="moth-image">
<h2 class="species-name">{moth['common_name']}</h2>
<p class="scientific-name">{moth['scientific_name']}</p>
<div class="details">
{family_text}
<p style="text-align:center;">📍 {moth['place']}</p>
<p style="text-align:center;"><a href="{moth['observation_url']}">View on iNaturalist →</a></p>
</div>
<p class="obs-count">{obs_text}</p>
<div class="footer">
<p style="text-align:center;">Photo: {moth['attribution']}</p>
</div>
</body>
</html>"""


def send_email(moth, moth_number):
    if not RESEND_API_KEY:
        raise Exception("RESEND_API_KEY environment variable not set")
    if not RECIPIENT_EMAIL:
        raise Exception("RECIPIENT_EMAIL environment variable not set")
    html_content = build_email_html(moth, moth_number)
    subject = f"Hourly Moth: {moth['common_name']} 🦋"
    recipients = [e.strip() for e in RECIPIENT_EMAIL.split(",") if e.strip()]
    print(f"Sending to {len(recipients)} recipients")
    response = requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}, json={"from": SENDER_EMAIL, "to": [SENDER_EMAIL], "bcc": recipients, "subject": subject, "html": html_content})
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
        if moth["family"]:
            print(f"Family: {moth['family']}")
        print(f"Sending to {len(RECIPIENT_EMAIL.split(','))} recipients...")
        result = send_email(moth, moth_number)
        print(f"Email sent successfully! ID: {result.get('id', 'unknown')}")
        moth["moth_number"] = moth_number
        moth["sent_at"] = datetime.now().isoformat()
        save_sent_moth(moth)
        print(f"Saved moth {moth['id']} to sent list")
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()

