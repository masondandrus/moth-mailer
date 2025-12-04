# Moth Mailer

Automated hourly emails featuring random moth photos from iNaturalist.

## Setup

1. Create a [Resend](https://resend.com/) account and verify your domain
2. Fork this repo
3. Add these secrets in **Settings → Secrets → Actions**:
   - `RESEND_API_KEY`
   - `RECIPIENT_EMAIL`
   - `SENDER_EMAIL`
4. Enable GitHub Actions

## Configuration

Edit `.github/workflows/moth-mailer.yml` to change the schedule:
```yaml
schedule:
  - cron: '0 * * * *'  # Every hour
```