# Google Business Profile MCP server

Lets Claude read and edit your Google Business Profile (formerly GMB) listing:
accounts, locations, description/hours/categories/phone/website, reviews and
replies, and posts. Every write tool defaults to `dry_run=True` (preview only).

## Setup

**1. Install** (Python 3.10+)

    pip install -r requirements.txt

**2. Get Google API access (the slow part)**
Google restricts the Business Profile APIs. Create a Google Cloud project, then
submit Google's "GBP API access" request form (search: *Google Business Profile
API prerequisites*). Approval can take days. Until it's approved, API calls
return quota/403 errors.

**3. Enable APIs and create OAuth credentials**
In the Cloud project enable: *My Business Account Management API*,
*My Business Business Information API*, *Google My Business API* (v4, used
for reviews and posts), and *Business Profile Performance API* (dashboard charts). Configure the OAuth consent screen and set its publishing status to
*In production* (in *Testing* mode Google expires your login every 7 days, which
silently breaks the hosted connector), then create an OAuth client ID of type **Desktop app** and download
the JSON. Save it as `~/.gbp-mcp/client_secret.json`.

**4. Authorise once** (opens a browser; sign in with the Google account that
manages the listing)

    python server.py --auth

**5. Add to Claude**

Claude Code:

    claude mcp add gbp -- python /full/path/to/server.py

Claude Desktop: add to `claude_desktop_config.json`:

    {
      "mcpServers": {
        "gbp": { "command": "python", "args": ["/full/path/to/server.py"] }
      }
    }

Restart, then ask: "List my accounts, then audit my listing and suggest fixes."

## Using it on the claude.ai website (hosted)

claude.ai connects to custom connectors from Anthropic's servers, so the server
must be on a public HTTPS URL, not your laptop.

1. Finish steps 1-4 above on your computer (including `python server.py --auth`).
2. Make a secret: `python -c "import secrets;print(secrets.token_urlsafe(32))"`
3. Deploy this folder (it has a Dockerfile) to any host that gives you HTTPS:
   Render, Railway, Fly.io, or Google Cloud Run. Set two environment variables:
   - `GBP_SECRET` = the secret from step 2
   - `GBP_TOKEN_JSON` = the full contents of `~/.gbp-mcp/token.json`
4. In claude.ai go to Settings > Connectors > Add custom connector and enter:
   `https://YOUR-HOST/<GBP_SECRET>/mcp`
5. Enable the connector in a chat and ask Claude to audit your listing.

Security: this hosted mode has no login screen. Anyone who learns the full URL
can act on your listing, so never share or paste it, and rotate GBP_SECRET (and
update the connector URL) if it leaks. Revoke Google access any time at
myaccount.google.com/permissions. Writes still preview first (`dry_run`).

## Dashboard

A read-only, chart-based dashboard (Plotly) is built in. It shows a profile
strength score with a checklist, views (search vs maps, desktop vs mobile),
customer actions (calls, website clicks, directions), star-rating breakdown and
recent reviews flagged "needs reply". Each failed check has a "Copy fix prompt"
button to paste into Claude, which then previews the change with `dry_run`.

Open `https://YOUR-HOST/<GBP_SECRET>/dashboard` once hosted. To try it locally:

    GBP_SECRET=any-random-string-of-24-plus-chars python server.py --http

then visit `http://localhost:8000/<GBP_SECRET>/dashboard`. Same secret-URL
warning as above applies. Performance data lags about 2 days.

## Tools

list_accounts, list_locations, get_location, search_categories,
get_performance, update_location, set_description, list_reviews, reply_to_review, create_post.

## Notes

- Token is stored at `~/.gbp-mcp/token.json`; keep it private.
- Some changes (name, address, categories) can trigger Google re-verification
  or suspension review. Preview with `dry_run` and avoid keyword-stuffing names.
- Not tested against live Google APIs by the author; endpoint shapes follow
  Google's public docs. If a call errors, the message from Google is surfaced.
