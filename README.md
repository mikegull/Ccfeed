# Commonwealth Club calendar feed (unofficial)

Builds `docs/events.ics` daily from every page of commonwealthclub.org/events, with venue from each event page.

## Setup (about 5 minutes)

1. On github.com, create a new **public** repo (e.g. `ccfeed`). Public is required for free GitHub Pages.
2. Add these files, keeping the paths exactly:
   - `build_feed.py`
   - `.github/workflows/build.yml`
   - `README.md` (optional)

   On the web, use **Add file → Create new file** and type `.github/workflows/build.yml` as the name to create the folders.
3. **Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → Save.
4. **Actions** tab → *Build Commonwealth Club calendar* → **Run workflow**. Wait about 2–5 minutes for the first run, which reads every event page. Check that it went green and that `docs/events.ics` appeared.
5. **Settings → Pages** → Source: *Deploy from a branch* → Branch `main`, folder `/docs` → Save.
6. A minute later your feed is at:
   `https://YOUR-USERNAME.github.io/ccfeed/events.ics`

## Subscribe on iPhone

Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → **Add Subscribed Calendar** → paste the URL. Or open the URL in Safari with `webcal://` in place of `https://`.

iPhone refreshes subscribed calendars on its own schedule (Settings → Apps → Calendar → Fetch). The feed itself rebuilds every morning.

## If something's off

- **Run fails with "No events found"**: the site blocked GitHub's servers or changed its layout. The previous feed stays in place.
- **Venues show only the region (e.g. "San Francisco")**: the event pages use a layout the venue finder doesn't know. Adjust `find_venue_html()` in `build_feed.py`.
- The default event length is 90 minutes when a page doesn't give an end time (`DEFAULT_DURATION`).
