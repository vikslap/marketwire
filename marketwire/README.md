# MarketWire

A always-on news feed for Indian (NSE) and US (S&P 500) listed companies.
A Python script pulls ~19 market news feeds every 15 minutes on GitHub Actions,
tags each story with the companies it mentions, and publishes a static page on
GitHub Pages. Free, no server to run.

## Set up (10 minutes)

1. Create a new **public** GitHub repo, e.g. `marketwire`, and upload everything in this folder
   (keep the `.github/workflows` folder).
2. Repo → **Settings → Actions → General → Workflow permissions** → choose
   **Read and write permissions** → Save.
3. Repo → **Actions** tab → enable workflows if asked → open **Update news** → **Run workflow**.
   Wait for the green tick (about 1 minute). This creates the `gh-pages` branch.
4. Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch: `gh-pages`, folder `/ (root)` → Save.
5. Open `https://<your-username>.github.io/marketwire/` after a minute or two.

From then on it refreshes itself every 15 minutes, and the open page checks for new stories every 2 minutes.

## Using the page

- **Tabs**: All / India / US / ★ Watchlist.
- **Search** (press `/`): matches headline, summary, source, ticker and company name.
- **Ticker chips**: click to show only that company; **shift-click** to add/remove from your watchlist.
- **Direct links**: `?t=RELIANCE`, `?t=US:AAPL`, `?m=IN` (India tab), `&compact` (tighter list).
- **⚙** shows which sources worked on the last run.

### Inside AmiBroker

Open the Web Research window and add a profile with this URL so it follows the selected symbol:

```
https://<your-username>.github.io/marketwire/?t={t}&compact
```

AmiBroker swaps `{t}` for the current ticker. For US symbols in a separate database, use `?t=US:{t}`.

## Customising

- `feeds.json` — add or remove RSS sources (market `IN` or `US`). Dead feeds are skipped, not fatal.
- `aliases.json` — short names headlines use ("HUL", "L&T", "Zomato"). Official names from the NSE
  and S&P 500 lists are matched automatically; add aliases when a company you track isn't getting tagged.
- `KEEP_HOURS` in the workflow env — how long stories stay (default 72 hours).

## Notes

- Tagging is name matching, so expect the odd miss or false tag; aliases fix most of them.
- NSE sometimes blocks automated downloads of its company list. The script then reuses the last
  good copy it published (`companies.json`), so tagging keeps working.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. If the "Updated" time
  goes stale, open Actions and re-enable / run the workflow.
- Run locally: `pip install -r requirements.txt && python build.py`, then serve `site/`
  (`python -m http.server -d site`) and open http://localhost:8000.
