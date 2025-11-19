# Telegram Terabox Reposter

This repository contains a webhook-ready Telegram bot that reposts messages from a source channel to one or more destination channels, rewrites Terabox links to a redirect domain, adds clickable buttons, and deduplicates reposts.

## Setup

1. Create a GitHub repo and paste these files.
2. In Railway (or locally), set environment variables:
   - `BOT_TOKEN` (required)
   - `DEST_CHANNELS` (required) — comma-separated (e.g. `-1003269104846,@mychannel`)
   - `REDIRECT_BASE` (required) — e.g. `https://go.example.com`
   - `SOURCE_CHANNEL_ID` (optional)
   - `CAPTION_TEMPLATE` (optional)
   - `FOOTER_TEXT` (optional)
   - `SECRET_TOKEN` (optional but recommended)
   - `PUBLIC_URL` (optional) — if set, the app will call `setWebhook` automatically on startup

3. Deploy to Railway and set `PUBLIC_URL` after Railway shows the app URL (or set webhook manually using Telegram API).

## Run locally

1. `pip install -r requirements.txt`
2. `uvicorn main:app --reload --host 0.0.0.0 --port 8000`
3. Use ngrok to expose an HTTPS URL and set webhook.

## Notes

- Do not commit secrets in the repo. Use Railway secrets or .env locally.
- Rotate your bot token if it was previously exposed publicly.
