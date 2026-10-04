# Playoff data

`data/mlb.json` is the single source of truth for the MLB playoff hub (`/mlb/`),
the team landing pages (`/mlb/<team>-playoff-tickets/`) and the MLB module on
the homepage. After every edit:

```sh
python3 scripts/build_playoffs.py          # regenerate pages (prints warnings)
python3 scripts/build_playoffs.py --check  # validate only
```

Then commit and push. Cloudflare Pages deploys `main` as-is, so do not edit the
generated HTML by hand; it is overwritten on the next build.

Games stay listed until 3 hours after first pitch, and `js/main.js` hides
finished games in the browser between rebuilds. Rebuild at least daily during
the postseason so the hero game on team pages stays current.

## Common updates

**Series finished / team eliminated.** Set the series winner:
`"winner": "lad"`. The loser drops out of the hub automatically, its page
switches to the "season ended" state, and the series' remaining
if-necessary games disappear.

**New round (e.g. NLCS).** Add a series
`{ "id": "nlcs-lad-mil", "round": "CS", "conference": "NL", "teams": ["lad", "mil"], "winner": null }`
and its games in `events`. Teams that won a series but have no next series yet
show as "Advanced to the …".

**Add a game.** Add an event object:
`date` is `YYYY-MM-DD`, `time` is `HH:MM` 24-hour **local to the stadium**
(leave `""` if TBA). `home`/`away` are team ids. Venue and city come from the
home team (override with `"venue": { "name", "city", "state" }` for neutral sites).

**If Necessary → confirmed.** Change `"ifNecessary": true` to `false`.

**Cancel / postpone a game.** Set `"active": false` (or fix date/time).

**Ordering.** Optional `"priority"` (lower first, default 100) on teams and events
breaks ties.

## Ticket links

- `events[].ticketsUrl`: exact event deep link (used first).
- `teams[].ticketsUrl`: team-level partner page (fallback, and the
  "Browse All … Playoff Tickets" buttons).
- Optional `"partner"` on an event or team picks an entry from `partners`
  (default `defaultPartner`). Add new marketplaces to `partners`.
- Links must start with `https://`. Without a URL the button shows
  "Tickets link coming soon" and the build prints a warning; no fake links are
  generated.
- Links on `go.watchplayoffs.com` automatically receive the visitor's campaign
  parameters (`utm_*`, `campaign`, `creative`, `ad`, `click_id`, `clickid`,
  `external_id`) plus CTA context (`wp_sport`, `wp_team`, `wp_home`, `wp_away`,
  `wp_event`, `wp_city`, `wp_cta`, `wp_partner`). Direct partner links get them
  only if the partner has `"passThroughParams": true`. The parameter lists live
  in `js/main.js`.
