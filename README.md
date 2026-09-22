# WatchPlayoffs

Statický web pro watchplayoffs.com, hostovaný na Cloudflare Pages.

## Struktura

- `index.html` – hlavní stránka
- `css/style.css` – styly
- `js/main.js` – skripty

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
