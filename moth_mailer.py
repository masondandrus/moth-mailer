"""
Moth Mailer - Sends a beautiful moth photo to your girlfriend every hour.
Uses iNaturalist API for photos and Resend for email delivery.
"""

import os
import random
import requests
from datetime import datetime

# Configuration from environment variables
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "moths@yourdomain.com")

# iNaturalist API settings
INATURALIST_API = "https://api.inaturalist.org/v1"
MOTH_TAXON_ID = 47157  # Lepidoptera (includes moths and butterflies)
# For moths specifically, we can exclude butterflies (Papilionoidea = 47224)


def fetch_random_moth():
    """
    Fetch a random high-quality moth observation from iNaturalist.
    Returns dict with photo URL, species name, observer, and location.
    """
    # Search parameters for quality moth photos
    params = {
        "taxon_id": MOTH_TAXON_ID,
        "quality_grade": "research",  # Verified identifications only
        "photos": "true",
        "photo_licensed": "true",  # Only CC-licensed photos
        "per_page": 200,
        "order_by": "random",
        # Exclude butterflies to get only moths
        "without_taxon_id": 47224,
    }
    
    response = requests.get(
        f"{INATURALIST_API}/observations",
        params=params,
        headers={"User-Agent": "MothMailer/1.0 (girlfriend-appreciation-project)"}
    )
    response.raise_for_status()
    
    results = response.json()["results"]
    
    if not results:
        raise Exception("No moth observations found")
    
    # Pick a random observation from results
    observation = random.choice(results)
    
    # Get the best photo (first one, usually highest quality)
    photo = observation["photos"][0]
    
    # Build photo URL - iNaturalist uses size suffixes
    # original, large (1024px), medium (500px), small (240px), square (75px)
    # The URL format is like: .../photos/12345/square.jpg
    # We want large for better quality in emails
    photo_url = photo["url"]
    for size in ["square", "small", "medium", "thumb"]:
        if size in photo_url:
            photo_url = photo_url.replace(size, "large")
            break
    
    # Extract species info
    taxon = observation.get("taxon", {})
    common_name = taxon.get("preferred_common_name", "Unknown moth")
    scientific_name = taxon.get("name", "Species unknown")
    
    # Location and observer info
    place = observation.get("place_guess", "Location unknown")
    observer = observation.get("user", {}).get("login", "Anonymous")
    obs_date = observation.get("observed_on", "Date unknown")
    obs_url = f"https://www.inaturalist.org/observations/{observation['id']}"
    
    return {
        "photo_url": photo_url,
        "common_name": common_name,
        "scientific_name": scientific_name,
        "place": place,
        "observer": observer,
        "observed_on": obs_date,
        "observation_url": obs_url,
        "attribution": photo.get("attribution", "Unknown photographer"),
    }


def build_email_html(moth):
    def build_email_html(moth):
    """Build a nice HTML email with the moth photo and info."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: Georgia, serif;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
                background-color: #faf9f7;
                color: #2d2d2d;
                text-align: center;
            }}
            .header {{
                padding-bottom: 20px;
                border-bottom: 1px solid #e0e0e0;
            }}
            .moth-image {{
                width: 100%;
                max-width: 550px;
                border-radius: 8px;
                margin: 20px auto;
            }}
            .species-name {{
                font-size: 24px;
                margin: 10px 0 5px 0;
            }}
            .scientific-name {{
                font-style: italic;
                color: #666;
                margin: 0 0 15px 0;
            }}
            .details {{
                font-size: 14px;
                color: #555;
                line-height: 1.6;
            }}
            .footer {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #e0e0e0;
                font-size: 12px;
                color: #888;
            }}
            a {{
                color: #6b705c;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1 style="margin: 0; font-weight: normal;">🦋 Your Hourly Moth</h1>
        </div>
        
        <img src="{moth['photo_url']}" alt="{moth['common_name']}" class="moth-image">
        
        <h2 class="species-name">{moth['common_name']}</h2>
        <p class="scientific-name">{moth['scientific_name']}</p>
        
        <div class="details">
            <p>📍 {moth['place']}</p>
            <p><a href="{moth['observation_url']}">View on iNaturalist →</a></p>
        </div>
        
        <div class="footer">
            <p>Photo: {moth['attribution']}</p>
            <p>Sent with love via the Moth Mailer 🌙</p>
        </div>
    </body>
    </html>
    """


def send_email(moth):
    """Send the moth email using Resend API."""
    if not RESEND_API_KEY:
        raise Exception("RESEND_API_KEY environment variable not set")
    if not RECIPIENT_EMAIL:
        raise Exception("RECIPIENT_EMAIL environment variable not set")
    
    html_content = build_email_html(moth)
    
    # Subject line with species name
    subject = f"🦋 {moth['common_name']} — Your Hourly Moth"
    
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
    
    if not response.ok:  # Checks for any 2xx status code
        raise Exception(f"Failed to send email: {response.status_code} {response.text}")
    
    return response.json()


def main():
    """Main function - fetch a moth and send it."""
    print(f"[{datetime.now().isoformat()}] Starting Moth Mailer...")
    
    try:
        # Fetch random moth
        print("Fetching random moth from iNaturalist...")
        moth = fetch_random_moth()
        print(f"Found: {moth['common_name']} ({moth['scientific_name']})")
        
        # Send email
        print(f"Sending to {RECIPIENT_EMAIL}...")
        result = send_email(moth)
        print(f"Email sent successfully! ID: {result.get('id', 'unknown')}")
        
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
