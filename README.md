# 🦋 Moth Mailer

Send your girlfriend a beautiful moth photo every hour. Because she deserves it.

## How It Works

1. Fetches a random research-grade moth observation from [iNaturalist](https://www.inaturalist.org/)
2. Builds a pretty HTML email with the photo, species info, and photographer credit
3. Sends it via [Resend](https://resend.com/)
4. Runs hourly via GitHub Actions (free!)

## Setup (15 minutes)

### 1. Get a Resend Account

1. Go to [resend.com](https://resend.com/) and sign up (free tier = 3,000 emails/month = ~4 emails/hour for a month)
2. Create an API key in the dashboard
3. **Important:** You'll need to verify a domain OR use their test sending. For production:
   - Add your domain to Resend
   - Add the DNS records they provide
   - Use `moths@yourdomain.com` as your sender

   For testing, you can use `onboarding@resend.dev` as the sender, but it only sends to your own email.

### 2. Set Up the Repository

1. Fork or clone this repo
2. Go to **Settings → Secrets and variables → Actions**
3. Add these repository secrets:

   | Secret | Value |
   |--------|-------|
   | `RESEND_API_KEY` | Your Resend API key |
   | `RECIPIENT_EMAIL` | Your girlfriend's email |
   | `SENDER_EMAIL` | Your verified sender (e.g., `moths@yourdomain.com`) |

### 3. Enable GitHub Actions

1. Go to the **Actions** tab in your repo
2. Enable workflows if prompted
3. Click on "Hourly Moth Mailer"
4. Click "Run workflow" to test it manually

That's it! Moths will now arrive every hour.

## Customization

### Change the Frequency

Edit `.github/workflows/moth-mailer.yml` and modify the cron schedule:

```yaml
schedule:
  # Every hour
  - cron: '0 * * * *'
  
  # Every 6 hours
  - cron: '0 */6 * * *'
  
  # Once daily at 8am UTC
  - cron: '0 8 * * *'
  
  # Once daily at 9am Pacific (UTC-8)
  - cron: '0 17 * * *'
```

### Include Butterflies Too

In `moth_mailer.py`, remove this line from the API params:

```python
"without_taxon_id": 47224,  # Remove this to include butterflies
```

### Change the Email Style

Edit the `build_email_html()` function in `moth_mailer.py`. It's just HTML/CSS.

## Local Development

```bash
# Clone the repo
git clone https://github.com/yourusername/moth-mailer.git
cd moth-mailer

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export RESEND_API_KEY="re_xxxxx"
export RECIPIENT_EMAIL="girlfriend@email.com"
export SENDER_EMAIL="moths@yourdomain.com"

# Run it
python moth_mailer.py
```

## Troubleshooting

**"No moth observations found"**
- iNaturalist API might be temporarily down. The next hourly run will work.

**Email not arriving**
- Check spam folder
- Verify your domain is properly set up in Resend
- Check the Actions tab for error logs

**Want to test without sending real emails?**
- Just run `fetch_random_moth()` locally and print the result

## Credits

- Moth photos from [iNaturalist](https://www.inaturalist.org/) contributors (CC licensed)
- Built with love for moth appreciation and girlfriend appreciation

## License

MIT — do whatever you want with it.
