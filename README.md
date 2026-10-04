# WatchPlayoffs

Statický web pro watchplayoffs.com, hostovaný na Cloudflare Pages.

## Struktura

- `index.html` – hlavní stránka
- `css/style.css` – styly
- `js/main.js` – skripty (navigace, předávání kampaňových parametrů, sticky CTA)
- `data/mlb.json` – jediný zdroj dat pro MLB playoff stránky (viz `data/README.md`)
- `scripts/build_playoffs.py` – generuje `/mlb/`, `/mlb/<tym>-playoff-tickets/` a MLB modul na homepage

Po každé úpravě dat spusť `python3 scripts/build_playoffs.py` a commitni vygenerované soubory.

## Nasazení na Cloudflare Pages

1. Na [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** → **Create** → **Pages** → **Connect to Git**.
2. Vyber repozitář `gith-vk-ai/watchplayoffs`.
3. Build settings:
   - Framework preset: `None`
   - Build command: (prázdné)
   - Build output directory: `/`
4. Po nasazení v **Custom domains** přidej `watchplayoffs.com` a `www.watchplayoffs.com`.
5. Přepni DNS domény na Cloudflare (nameservery) a odpoj/zruš WordPress hosting, pokud tam běžel mimo Cloudflare.

## Vývoj

Otevřít `index.html` v prohlížeči, žádný build krok není potřeba.
