# Fair Model vs. Markets

Ray Fair's 2026 House and Senate predictions next to Kalshi, PredictIt and Polymarket prices. The Senate table follows the layout of Fair's ranking page (14 states, three markets, pivot state, Dem. control).

## Files

- `index.html` – the page. Reads `prices.json` every minute.
- `fetch_prices.py` – pulls Kalshi, PredictIt and Polymarket and writes `prices.json` (plus a running log in `history.jsonl`). Python 3.9+, no installs needed.
- `history.json` – prices over time, for the two over-time charts and the 24h column. Created on the first run; one point every 15 minutes for the last two days, hourly before that.
- `prices.json` – latest prices. The copy here is from Oct 14, 2026; the page also has a copy built in, in case the file can't be read.
- `.github/workflows/update-prices.yml` – runs the script every 5 minutes on GitHub.

## Run it locally

```
pip3 install certifi            # once, on a Mac (fixes CERTIFICATE_VERIFY_FAILED)
python fetch_prices.py          # pull once
python -m http.server 8000      # then open http://localhost:8000
```

`python fetch_prices.py --loop 60` keeps pulling every 60 seconds.

## Put it online (GitHub Pages)

1. New public repo on github.com (no README).
2. Upload `index.html`, `fetch_prices.py`, `prices.json`, `README.md` (and `history.json` / `history.jsonl` if you have them).
3. Add file → Create new file → name it `.github/workflows/update-prices.yml` and paste in that file's contents.
4. Settings → Pages → Source: **GitHub Actions**.
5. Settings → Actions → General → Workflow permissions: **Read and write**.
6. Actions tab → "Update market prices" → Run workflow.

The site is then at `https://<your-username>.github.io/<repo-name>/`. The job runs every 5 minutes and whenever you change a file. GitHub sometimes runs scheduled jobs late, so on election night run `python fetch_prices.py --loop 60` on your laptop and push, or use a server.

## When Fair posts a new prediction

Edit the `FAIR` block near the top of the script in `index.html`: `mean`, `date` and `parts`.

## Notes

- PredictIt markets are found by name in PredictIt's full market list. On the first run, check `errors` in `prices.json`: if a state says "no market found", open that race on predictit.org and adjust the name pattern in `build()`.
- The state list and market names are at the top of `fetch_prices.py` (`STATES`).

- Prices are the midpoint of the best bid and ask, scaled so each market adds to 100%.
- If one source fails, the script keeps its last good numbers and lists the problem under `errors` in `prices.json`.
- Polymarket's "Other" option in the popular vote market is skipped (it's an inactive placeholder).
- The page shows "Live" when `prices.json` is under 30 minutes old.
- The over-time charts and the 24h column stay empty until the script has run for a while (24h needs a day of history).
