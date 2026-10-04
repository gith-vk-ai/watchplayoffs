#!/usr/bin/env python3
"""
WatchPlayoffs — playoff page generator.

Reads the single source of truth in data/<league>.json and writes static
HTML (no build step is needed on Cloudflare Pages; the generated files are
committed like every other page):

  /<league>/index.html                         league playoff hub
  /<league>/<team-slug>-playoff-tickets/       team / city landing pages
  /index.html                                  homepage module between the
                                               <!-- PLAYOFFS:<LEAGUE>:START/END --> markers

Usage:
  python3 scripts/build_playoffs.py                 # build every league in LEAGUES
  python3 scripts/build_playoffs.py --check         # validate data only, write nothing
  python3 scripts/build_playoffs.py --now 2026-10-06T12:00:00-04:00   # preview a moment

Only the Python 3.9+ standard library is used.
"""

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://www.watchplayoffs.com"
LEAGUES = ["mlb"]

# A game stays listed until this long after first pitch (the client-side
# script in js/main.js hides it on the same rule between rebuilds).
LISTED_AFTER_START = timedelta(hours=3)

TZ_ABBR = {
    "America/New_York": "ET",
    "America/Detroit": "ET",
    "America/Toronto": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Phoenix": "MST",
    "America/Los_Angeles": "PT",
}

NAV_LEAGUES = [("mlb", "MLB"), ("nfl", "NFL"), ("nba", "NBA"), ("nhl", "NHL"), ("mls", "MLS")]


def esc(value):
    return html.escape(str(value), quote=True)


# =========================================================================
# Data loading, validation and derived state
# =========================================================================

class BuildError(Exception):
    pass


class League:
    def __init__(self, data, now):
        self.now = now
        self.raw = data
        self.info = data["league"]
        self.id = self.info["id"]
        self.season = self.info["season"]
        self.partners = data.get("partners", {})
        self.default_partner = data.get("defaultPartner")
        self.rounds = self.info["rounds"]
        self.round_order = {r["id"]: i for i, r in enumerate(self.rounds)}
        self.conferences = {c["id"]: c["name"] for c in self.info["conferences"]}
        self.teams = {t["id"]: t for t in data["teams"]}
        self.team_order = [t["id"] for t in data["teams"]]
        post = data.get("postseason", {})
        self.series = post.get("series", [])
        self.series_by_id = {s["id"]: s for s in self.series}
        self.events = post.get("events", [])
        self.warnings = []
        self._validate()
        self._derive()

    # ---------------------------------------------------------------- checks
    def _validate(self):
        errors = []
        slugs = set()
        for t in self.raw["teams"]:
            for key in ("id", "slug", "name", "shortName", "abbr", "market", "conference", "venue", "timezone"):
                if not t.get(key):
                    errors.append("team %s: missing '%s'" % (t.get("id", "?"), key))
            if t.get("conference") not in self.conferences:
                errors.append("team %s: unknown conference %r" % (t.get("id"), t.get("conference")))
            if t.get("slug") in slugs:
                errors.append("team %s: duplicate slug %r" % (t.get("id"), t.get("slug")))
            slugs.add(t.get("slug"))
            try:
                ZoneInfo(t.get("timezone", ""))
            except Exception:
                errors.append("team %s: invalid timezone %r" % (t.get("id"), t.get("timezone")))
        if len(self.teams) != len(self.raw["teams"]):
            errors.append("duplicate team ids in 'teams'")

        for s in self.series:
            if s.get("round") not in self.round_order:
                errors.append("series %s: unknown round %r" % (s.get("id"), s.get("round")))
            for tid in s.get("teams", []):
                if tid not in self.teams:
                    errors.append("series %s: unknown team %r" % (s.get("id"), tid))
            if len(s.get("teams", [])) != 2:
                errors.append("series %s: needs exactly two teams" % s.get("id"))
            if s.get("winner") and s["winner"] not in s.get("teams", []):
                errors.append("series %s: winner %r is not in this series" % (s.get("id"), s["winner"]))

        seen = set()
        for e in self.events:
            eid = e.get("id", "?")
            if eid in seen:
                errors.append("event %s: duplicate id" % eid)
            seen.add(eid)
            s = self.series_by_id.get(e.get("series"))
            if not s:
                errors.append("event %s: unknown series %r" % (eid, e.get("series")))
                continue
            if {e.get("home"), e.get("away")} != set(s["teams"]):
                errors.append("event %s: home/away must be the two teams of series %s" % (eid, s["id"]))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", e.get("date", "")):
                errors.append("event %s: date must be YYYY-MM-DD" % eid)
            if e.get("time") and not re.match(r"^\d{2}:\d{2}$", e["time"]):
                errors.append("event %s: time must be HH:MM (24h, local to the venue) or empty" % eid)
            if e.get("partner") and e["partner"] not in self.partners:
                errors.append("event %s: unknown partner %r" % (eid, e["partner"]))
            for key in ("ticketsUrl",):
                if e.get(key) and not e[key].startswith("https://"):
                    errors.append("event %s: %s must start with https://" % (eid, key))

        for t in self.raw["teams"]:
            if t.get("ticketsUrl") and not t["ticketsUrl"].startswith("https://"):
                errors.append("team %s: ticketsUrl must start with https://" % t["id"])
            if t.get("partner") and t["partner"] not in self.partners:
                errors.append("team %s: unknown partner %r" % (t["id"], t["partner"]))

        if errors:
            raise BuildError("\n".join(" - " + e for e in errors))

    # ----------------------------------------------------------------- state
    def round_text(self, round_id, conf, key="label"):
        r = self.rounds[self.round_order[round_id]]
        return r[key].replace("{conf}", conf or "").strip()

    def _derive(self):
        # Team playoff status, derived only from the series list.
        self.status = {}
        for tid in self.teams:
            mine = [s for s in self.series if tid in s["teams"]]
            if not mine:
                self.status[tid] = None
                continue
            latest = max(mine, key=lambda s: self.round_order[s["round"]])
            winner = latest.get("winner")
            opp_id = [x for x in latest["teams"] if x != tid][0]
            st = {
                "series": latest,
                "opponent": self.teams[opp_id],
                "round_label": self.round_text(latest["round"], latest.get("conference")),
                "round_abbr": self.round_text(latest["round"], latest.get("conference"), "abbr"),
                "eliminated": bool(winner and winner != tid),
                "advanced": winner == tid,
                "champion": winner == tid and latest["round"] == self.rounds[-1]["id"],
            }
            st["active"] = not st["eliminated"] and not st["champion"]
            if st["advanced"] and not st["champion"]:
                nxt = self.rounds[self.round_order[latest["round"]] + 1]
                conf = latest.get("conference") if nxt["id"] != self.rounds[-1]["id"] else ""
                st["next_round_label"] = nxt["label"].replace("{conf}", conf or "").strip()
            self.status[tid] = st

        # Upcoming events, enriched for rendering.
        self.upcoming = []
        for e in self.events:
            s = self.series_by_id[e["series"]]
            if e.get("active") is False or s.get("winner"):
                continue
            ev = self._enrich(e, s)
            if ev["listed_until"] < self.now:
                continue
            self.upcoming.append(ev)
        self.upcoming.sort(key=lambda ev: (ev["ifNecessary"], ev["start"], ev.get("priority", 100)))

        for ev in self.upcoming:
            if not ev["url"]:
                self.warnings.append("event %s (%s): no ticketsUrl on the event or the home team" % (ev["id"], ev["title"]))
        for tid, st in self.status.items():
            if st and st["active"] and not self.teams[tid].get("ticketsUrl"):
                self.warnings.append("team %s: no team-level ticketsUrl (fallback CTA)" % tid)

    def _enrich(self, e, s):
        home, away = self.teams[e["home"]], self.teams[e["away"]]
        venue = dict(home["venue"])
        venue.update(e.get("venue") or {})
        tzname = e.get("timezone") or home["timezone"]
        tz = ZoneInfo(tzname)
        has_time = bool(e.get("time"))
        h, m = (int(x) for x in e["time"].split(":")) if has_time else (23, 59)
        y, mo, d = (int(x) for x in e["date"].split("-"))
        start = datetime(y, mo, d, h, m, tzinfo=tz)
        conf = s.get("conference")
        partner_id = e.get("partner") if e.get("ticketsUrl") else home.get("partner")
        url = e.get("ticketsUrl") or home.get("ticketsUrl") or ""
        if not partner_id and url:
            partner_id = self.default_partner
        ev = dict(e)
        ev.update({
            "home_team": home,
            "away_team": away,
            "venue": venue,
            "country": home.get("country", "US"),
            "tz_abbr": TZ_ABBR.get(tzname, start.strftime("%Z")),
            "start": start,
            "has_time": has_time,
            "listed_until": (start if has_time else start.replace(hour=12)) + LISTED_AFTER_START,
            "ifNecessary": bool(e.get("ifNecessary")),
            "round_abbr": self.round_text(s["round"], conf, "abbr"),
            "round_label": self.round_text(s["round"], conf),
            "series_obj": s,
            "title": "%s at %s" % (away["shortName"], home["shortName"]),
            "url": url,
            "url_is_event": bool(e.get("ticketsUrl")),
            "partner": partner_id,
            "partner_name": self.partners.get(partner_id, {}).get("name", "") if url else "",
            "pass_params": bool(self.partners.get(partner_id, {}).get("passThroughParams")),
        })
        return ev

    # --------------------------------------------------------------- helpers
    def participants(self):
        return [tid for tid in self.team_order if self.status.get(tid)]

    def active_teams(self):
        series_pos = {s["id"]: i for i, s in enumerate(self.series)}
        ids = [tid for tid in self.participants() if self.status[tid]["active"]]

        def key(tid):
            nxt = self.next_event(tid)
            st = self.status[tid]
            return (self.teams[tid].get("priority", 100),
                    -self.round_order[st["series"]["round"]],
                    series_pos[st["series"]["id"]],
                    nxt["start"] if nxt else datetime.max.replace(tzinfo=timezone.utc))
        return sorted(ids, key=key)

    def team_events(self, tid, home=None):
        out = [ev for ev in self.upcoming if tid in (ev["home"], ev["away"])]
        if home is True:
            out = [ev for ev in out if ev["home"] == tid]
        elif home is False:
            out = [ev for ev in out if ev["away"] == tid]
        return out

    def next_event(self, tid, home=None):
        evs = self.team_events(tid, home)
        return evs[0] if evs else None

    def current_round_id(self):
        open_series = [s for s in self.series if not s.get("winner")]
        pool = open_series or self.series
        if not pool:
            return None
        return max(pool, key=lambda s: self.round_order[s["round"]])["round"]

    def team_path(self, team):
        return "/%s/%s-playoff-tickets/" % (self.id, team["slug"])

    def champion(self):
        for tid, st in self.status.items():
            if st and st["champion"]:
                return self.teams[tid]
        return None


# =========================================================================
# Formatting
# =========================================================================

def fmt_date(dt, long=False):
    if long:
        return "%s, %s %d" % (dt.strftime("%A"), dt.strftime("%B"), dt.day)
    return "%s, %s %d" % (dt.strftime("%a"), dt.strftime("%b"), dt.day)


def fmt_time(ev):
    if not ev["has_time"]:
        return "Time TBA"
    t = ev["start"]
    hour = t.hour % 12 or 12
    return "%d:%02d %s %s" % (hour, t.minute, "AM" if t.hour < 12 else "PM", ev["tz_abbr"])


def iso_start(ev):
    return ev["start"].isoformat() if ev["has_time"] else ev["start"].date().isoformat()


def place(ev):
    return "%s, %s" % (ev["venue"]["city"], ev["venue"]["state"])


def game_tag(ev):
    return "%s · Game %s" % (ev["round_abbr"], ev["game"])


# =========================================================================
# Reusable components (sport-agnostic)
# =========================================================================

def ticket_cta(lg, ev, label, location, page_team=None, variant="primary", block=True, url=None, partner=None,
               pass_params=None, event_id=None):
    """Outbound ticket link with tracking context, or a disabled placeholder
    when no partner URL has been configured yet (never a fake link)."""
    url = ev["url"] if (url is None and ev) else url
    classes = "btn btn-%s%s" % (variant, " btn-block" if block else "")
    if not url:
        return '<span class="%s is-disabled" aria-disabled="true">Tickets link coming soon</span>' % classes
    if ev:
        partner = ev["partner"] if partner is None else partner
        pass_params = ev["pass_params"] if pass_params is None else pass_params
    attrs = {
        "class": classes,
        "href": url,
        "rel": "sponsored nofollow noopener",
        "data-outbound": "",
        "data-sport": lg.id,
        "data-cta": location,
        "data-partner": partner or "",
    }
    if page_team:
        attrs["data-team"] = page_team["id"]
    if ev:
        attrs.update({
            "data-event": event_id or ev["id"],
            "data-home": ev["home"],
            "data-away": ev["away"],
            "data-city": ev["venue"]["city"],
        })
    if pass_params:
        attrs["data-pass-params"] = ""
    attr_html = " ".join(('%s="%s"' % (k, esc(v))) if v != "" else k for k, v in attrs.items())
    return "<a %s>%s</a>" % (attr_html, esc(label))


def partner_note(ev):
    if ev.get("partner_name"):
        return '<p class="cta-partner">Tickets via %s</p>' % esc(ev["partner_name"])
    return ""


def status_badge(ev):
    if ev["ifNecessary"]:
        return '<span class="status-badge status-badge--conditional">If Necessary</span>'
    return '<span class="status-badge status-badge--confirmed">Confirmed</span>'


def event_card(lg, ev, location="games-list", page_team=None):
    cond = ev["ifNecessary"]
    label = "View Tickets" if cond else "See Available Tickets"
    return """<article class="event-card%(mod)s" data-event-start="%(iso)s">
  <div class="event-card-top">
    <span class="card-badge">%(tag)s</span>
    %(badge)s
  </div>
  <h3 class="event-matchup">
    <span class="event-away">%(away)s</span>
    <span class="event-at">at</span>
    <span class="event-home">%(home)s</span>
  </h3>
  <p class="event-when"><time datetime="%(iso)s">%(date)s · %(time)s</time></p>
  <p class="event-where">%(venue)s<br />%(place)s</p>
  %(cond_note)s
  <div class="event-cta">
    %(cta)s
    %(partner)s
  </div>
</article>""" % {
        "mod": " event-card--conditional" if cond else "",
        "iso": esc(iso_start(ev)),
        "tag": esc(game_tag(ev)),
        "badge": status_badge(ev),
        "away": esc(ev["away_team"]["name"]),
        "home": esc(ev["home_team"]["name"]),
        "date": esc(fmt_date(ev["start"])),
        "time": esc(fmt_time(ev)),
        "venue": esc(ev["venue"]["name"]),
        "place": esc(place(ev)),
        "cond_note": '<p class="event-cond-note">Played only if the series is not decided before this game.</p>' if cond else "",
        "cta": ticket_cta(lg, ev, label, location, page_team, variant="secondary" if cond else "primary"),
        "partner": partner_note(ev),
    }


def event_row(lg, ev, location, page_team):
    """Compact date-led row used on team landing pages."""
    cond = ev["ifNecessary"]
    return """<li class="event-row%(mod)s" data-event-start="%(iso)s">
  <time class="date-tile" datetime="%(iso)s"><span class="date-tile-month">%(mon)s</span><span class="date-tile-day">%(day)s</span></time>
  <div class="event-row-body">
    <p class="event-row-title">%(title)s</p>
    <p class="event-row-meta">%(tag)s · %(time)s</p>
    <p class="event-row-meta">%(venue)s, %(city)s</p>
    %(badge)s
  </div>
  <div class="event-row-cta">%(cta)s</div>
</li>""" % {
        "mod": " event-row--conditional" if cond else "",
        "iso": esc(iso_start(ev)),
        "mon": esc(ev["start"].strftime("%b").upper()),
        "day": ev["start"].day,
        "title": esc(ev["title"]),
        "tag": esc(game_tag(ev)),
        "time": esc(fmt_time(ev)),
        "venue": esc(ev["venue"]["name"]),
        "city": esc(ev["venue"]["city"]),
        "badge": '<span class="status-badge status-badge--conditional">If Necessary</span>' if cond else "",
        "cta": ticket_cta(lg, ev, "View Tickets", location, page_team,
                          variant="secondary" if cond else "primary", block=False),
    }


def team_card(lg, tid):
    team = lg.teams[tid]
    st = lg.status[tid]
    home_ev = lg.next_event(tid, home=True)
    if home_ev:
        nxt = "Next home game: %s · %s" % (fmt_date(home_ev["start"]), home_ev["venue"]["name"])
        if home_ev["ifNecessary"]:
            nxt += " (if necessary)"
    else:
        any_ev = lg.next_event(tid)
        nxt = ("Next game: %s · %s" % (any_ev["title"], fmt_date(any_ev["start"]))) if any_ev else ""
    if st["advanced"]:
        matchup = "Advanced to the %s" % st["next_round_label"]
        round_label = st["round_label"] + " winner"
    else:
        matchup = "vs. %s" % st["opponent"]["name"]
        round_label = st["round_label"]
    return """<a class="team-card" href="%(href)s">
  <span class="team-mono" aria-hidden="true">%(abbr)s</span>
  <span class="team-card-body">
    <span class="team-card-name">%(name)s</span>
    <span class="team-card-place">%(market)s, %(state)s</span>
    <span class="team-card-round">%(round)s</span>
    <span class="team-card-opp">%(matchup)s</span>
    %(next)s
  </span>
  <span class="team-card-cta">View %(short)s Playoff Tickets <span aria-hidden="true">→</span></span>
</a>""" % {
        "href": esc(lg.team_path(team)),
        "abbr": esc(team["abbr"]),
        "name": esc(team["name"]),
        "market": esc(team["market"]),
        "state": esc(team.get("stateName", "")),
        "round": esc(round_label),
        "matchup": esc(matchup),
        "next": ('<span class="team-card-next">%s</span>' % esc(nxt)) if nxt else "",
        "short": esc(team["shortName"]),
    }


def road_to_title(lg):
    current = lg.current_round_id()
    champion = lg.champion()
    items = []
    for r in lg.rounds:
        idx = lg.round_order[r["id"]]
        if current is None:
            state = "upcoming"
        elif champion or idx < lg.round_order[current]:
            state = "complete"
        elif idx == lg.round_order[current]:
            state = "current"
        else:
            state = "upcoming"
        status = {"complete": "Complete", "current": "Now playing", "upcoming": "Up next" if current and idx == lg.round_order[current] + 1 else ""}[state]
        items.append('<li class="road-step road-step--%s"%s><span class="road-step-name">%s</span>%s</li>' % (
            state,
            ' aria-current="step"' if state == "current" else "",
            esc(r["name"]),
            ('<span class="road-step-status">%s</span>' % status) if status else "",
        ))
    return '<ol class="road-list">%s</ol>' % "".join(items)


def team_directory(lg):
    cols = []
    for conf_id, conf_name in lg.conferences.items():
        groups = {}
        for tid in lg.team_order:
            t = lg.teams[tid]
            if t["conference"] == conf_id:
                groups.setdefault(t.get("division", ""), []).append(t)
        blocks = []
        for div, teams in groups.items():
            lis = []
            for t in sorted(teams, key=lambda x: x["name"]):
                st = lg.status.get(t["id"])
                if st and st["active"]:
                    lis.append('<li><a class="dir-link dir-link--playoffs" href="%s">%s <span class="dir-badge">Playoffs</span></a></li>'
                               % (esc(lg.team_path(t)), esc(t["name"])))
                elif st:
                    lis.append('<li><a class="dir-link" href="%s">%s</a></li>' % (esc(lg.team_path(t)), esc(t["name"])))
                else:
                    lis.append('<li><span class="dir-link dir-link--plain">%s</span></li>' % esc(t["name"]))
            heading = ("%s %s" % (conf_id, div)).strip()
            blocks.append('<div class="dir-group"><h4 class="dir-heading">%s</h4><ul class="dir-list">%s</ul></div>'
                          % (esc(heading), "".join(lis)))
        cols.append('<section class="dir-col" aria-labelledby="dir-%s"><h3 id="dir-%s" class="dir-conf">%s</h3>%s</section>'
                    % (conf_id.lower(), conf_id.lower(), esc(conf_name), "".join(blocks)))
    return '<div class="team-directory">%s</div>' % "".join(cols)


def disclosure_note(lg):
    return """<section class="disclosure-note" aria-label="Disclosure">
  <div class="container">
    <p>WatchPlayoffs.com is an independent website and is not affiliated with %(full)s, its teams, or venues. Tickets are sold by third-party ticket marketplaces, not by WatchPlayoffs. We may earn a commission from qualifying purchases. Prices, fees, and availability are set by the marketplace. <a href="/affiliate-disclosure.html">Affiliate disclosure</a></p>
  </div>
</section>""" % {"full": esc(lg.info["fullName"])}


def json_ld_events(lg, events):
    """SportsEvent data for confirmed games only; no offers/prices are known."""
    items = []
    for ev in events:
        if ev["ifNecessary"]:
            continue
        items.append({
            "@type": "SportsEvent",
            "name": "%s at %s — %s Game %s" % (ev["away_team"]["name"], ev["home_team"]["name"], ev["round_abbr"], ev["game"]),
            "startDate": iso_start(ev),
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "sport": lg.info["sport"],
            "location": {
                "@type": "Place",
                "name": ev["venue"]["name"],
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": ev["venue"]["city"],
                    "addressRegion": ev["venue"]["state"],
                    "addressCountry": ev["country"],
                },
            },
            "homeTeam": {"@type": "SportsTeam", "name": ev["home_team"]["name"]},
            "awayTeam": {"@type": "SportsTeam", "name": ev["away_team"]["name"]},
        })
    if not items:
        return ""
    data = {"@context": "https://schema.org", "@graph": items}
    return '<script type="application/ld+json">%s</script>' % json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


# =========================================================================
# Page shell (mirrors the hand-written pages' header and footer)
# =========================================================================

def header(active):
    links = ['<li><a class="nav-link" href="/"%s>WatchPlayoffs</a></li>' % (' aria-current="page"' if active == "home" else "")]
    for lid, name in NAV_LEAGUES:
        links.append('<li><a class="nav-link" href="/%s/"%s>%s</a></li>' % (lid, ' aria-current="page"' if active == lid else "", name))
    return """  <header class="site-header">
    <div class="container">
      <a class="brand" href="/">
        <span class="brand-mark">Watch</span>Playoffs
      </a>
      <button
        type="button"
        class="nav-toggle"
        aria-expanded="false"
        aria-controls="main-nav"
      >
        <span class="nav-toggle-icon"></span>
        <span class="visually-hidden">Open menu</span>
      </button>
      <nav id="main-nav" class="main-nav" aria-label="Main navigation">
        <ul class="nav-list">
          %s
        </ul>
      </nav>
    </div>
  </header>""" % "\n          ".join(links)


FOOTER = """  <footer class="site-footer">
    <div class="container">
      <ul class="footer-nav">
        <li><a href="/">WatchPlayoffs</a></li>
        <li><a href="/mlb/">MLB</a></li>
        <li><a href="/nfl/">NFL</a></li>
        <li><a href="/nba/">NBA</a></li>
        <li><a href="/nhl/">NHL</a></li>
        <li><a href="/mls/">MLS</a></li>
      </ul>
      <ul class="footer-nav footer-legal">
        <li><a href="/affiliate-disclosure.html">Affiliate Disclosure</a></li>
        <li><a href="/privacy.html">Privacy Policy</a></li>
        <li><a href="/terms.html">Terms of Service</a></li>
        <li><a href="/contact.html">Contact</a></li>
      </ul>
      <p class="footer-copy">&copy; 2026 WatchPlayoffs.com. All rights reserved.</p>
    </div>
  </footer>"""


def page(title, description, path, body_class, active, main, head_extra="", body_end=""):
    url = SITE_URL + path
    return """<!doctype html>
<!-- Generated by scripts/build_playoffs.py from data/ — edit the data, not this file. -->
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>%(title)s</title>
  <meta
    name="description"
    content="%(desc)s"
  />
  <link rel="canonical" href="%(url)s" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="WatchPlayoffs" />
  <meta property="og:title" content="%(title)s" />
  <meta property="og:description" content="%(desc)s" />
  <meta property="og:url" content="%(url)s" />
  <link rel="stylesheet" href="/css/style.css" />
  <link rel="stylesheet" href="/css/themes.css" />
%(head_extra)s
</head>
<body class="%(body_class)s">
%(header)s

  <main>
%(main)s
  </main>

%(footer)s
%(body_end)s
  <script src="/js/main.js"></script>
</body>
</html>
""" % {
        "title": esc(title),
        "desc": esc(description),
        "url": esc(url),
        "head_extra": ("  " + head_extra) if head_extra else "",
        "body_class": esc(body_class),
        "header": header(active),
        "main": main,
        "footer": FOOTER,
        "body_end": body_end,
    }


# =========================================================================
# Pages
# =========================================================================

def build_hub(lg):
    upcoming = lg.upcoming
    confirmed = [ev for ev in upcoming if not ev["ifNecessary"]]
    conditional = [ev for ev in upcoming if ev["ifNecessary"]]
    active = lg.active_teams()
    cur = lg.current_round_id()
    cur_name = lg.rounds[lg.round_order[cur]]["name"] if cur else ""
    champion = lg.champion()
    name = lg.info["name"]

    if upcoming or active:
        eyebrow = "%s %s Playoffs%s" % (lg.season, name, (" · " + cur_name) if cur_name else "")
        lead = ("Find tickets for the biggest %s postseason matchups and see your team live on the road to the %s."
                % (name, lg.info["championship"]))
        actions = """<a class="btn btn-primary" href="#games">See Playoff Games</a>
          <a class="btn btn-secondary" href="#teams">Browse Playoff Teams</a>"""
    else:
        eyebrow = "%s Playoffs" % name
        if champion:
            lead = "The %s %s %s is complete. Browse every %s team below for future games." % (
                lg.season, name, lg.info["championship"], name)
        else:
            lead = "There are no %s playoff games on the schedule right now. Browse every %s team below." % (name, name)
        actions = '<a class="btn btn-primary" href="#all-teams">Browse All %s Teams</a>' % name

    subnav_items = []
    if upcoming:
        subnav_items.append('<li><a href="#games">Playoff Games</a></li>')
    if active:
        subnav_items.append('<li><a href="#teams">Playoff Teams</a></li>')
    subnav_items.append('<li><a href="#all-teams">All %s Teams</a></li>' % name)

    parts = ["""    <section class="hero hero--compact">
      <div class="container">
        <p class="eyebrow">%(eyebrow)s</p>
        <h1 class="hero-title">%(name)s Playoff Tickets</h1>
        <p class="hero-subtitle">%(lead)s</p>
        <div class="hero-actions">
          %(actions)s
        </div>
      </div>
    </section>

    <nav class="sub-nav" aria-label="%(name)s sections">
      <div class="container">
        <ul class="sub-nav-list">%(subnav)s</ul>
      </div>
    </nav>""" % {"eyebrow": esc(eyebrow), "name": esc(name), "lead": esc(lead), "actions": actions,
                 "subnav": "".join(subnav_items)}]

    if upcoming:
        cond_html = ""
        if conditional:
            cond_html = """
        <details class="games-subgroup">
          <summary class="subgroup-title">If Necessary Games (%d)</summary>
          <p class="subgroup-lead">These games are played only if the series is still undecided. Check the marketplace's policy for games that are not played.</p>
          <div class="card-grid card-grid--3">
            %s
          </div>
        </details>""" % (len(conditional), "\n".join(event_card(lg, ev) for ev in conditional))
        conf_html = ""
        if confirmed:
            conf_html = '<div class="card-grid card-grid--3">\n%s\n</div>' % "\n".join(event_card(lg, ev) for ev in confirmed)
        parts.append("""    <section id="games" class="section">
      <div class="container">
        <div class="section-header">
          <p class="eyebrow">Games</p>
          <h2 class="section-title">Upcoming %(name)s Playoff Games</h2>
          <p class="section-lead">Local start times. Confirmed games first.</p>
        </div>
        %(confirmed)s%(conditional)s
      </div>
    </section>""" % {"name": esc(name), "confirmed": conf_html, "conditional": cond_html})

    if active:
        parts.append("""    <section id="teams" class="section section-alt">
      <div class="container">
        <div class="section-header">
          <p class="eyebrow">Teams</p>
          <h2 class="section-title">%(season)s %(name)s Playoff Teams</h2>
          <p class="section-lead">Teams still alive in the postseason.</p>
        </div>
        <div class="team-grid">
          %(cards)s
        </div>
      </div>
    </section>""" % {"season": lg.season, "name": esc(name), "cards": "\n".join(team_card(lg, t) for t in active)})

    if lg.series:
        parts.append("""    <section id="road" class="section">
      <div class="container">
        <div class="section-header">
          <p class="eyebrow">Bracket</p>
          <h2 class="section-title">Road to the %(champ)s</h2>
        </div>
        %(road)s
      </div>
    </section>""" % {"champ": esc(lg.info["championship"]), "road": road_to_title(lg)})

    parts.append("""    <section id="all-teams" class="section section-alt">
      <div class="container">
        <div class="section-header">
          <p class="eyebrow">Directory</p>
          <h2 class="section-title">Browse All %(name)s Teams</h2>
        </div>
        %(dir)s
      </div>
    </section>""" % {"name": esc(name), "dir": team_directory(lg)})

    parts.append(disclosure_note(lg))

    title = "%s Playoff Tickets %s | WatchPlayoffs" % (name, lg.season)
    desc = ("Find tickets for upcoming %s playoff games, browse current postseason teams, and see live %s on the road to the %s."
            % (name, lg.info["sport"].lower(), lg.info["championship"]))
    return page(title, desc, "/%s/" % lg.id, lg.info["theme"], lg.id, "\n\n".join(parts),
                head_extra=json_ld_events(lg, confirmed))


def build_team_page(lg, tid):
    team = lg.teams[tid]
    st = lg.status[tid]
    short = team["shortName"]
    home_events = lg.team_events(tid, home=True)
    road_events = lg.team_events(tid, home=False)
    featured = home_events[0] if home_events else None
    sticky = ""
    body_class = lg.info["theme"]
    eyebrow = "%s %s Playoffs · %s" % (lg.season, lg.info["name"], team["market"])
    team_url = team.get("ticketsUrl", "")
    team_partner = team.get("partner") or (lg.default_partner if team_url else "")
    team_pass = bool(lg.partners.get(team_partner, {}).get("passThroughParams"))
    disclosure_line = ('<p class="lp-disclosure">Tickets are offered through third-party ticket marketplace partners. '
                       '<a href="/affiliate-disclosure.html">Disclosure</a></p>')

    def team_cta(label, location, variant="primary", block=True, ev=None):
        return ticket_cta(lg, ev, label, location, team, variant=variant, block=block, url=team_url,
                          partner=team_partner, pass_params=team_pass,
                          event_id=(ev["id"] if ev else None))

    if st["active"] and featured:
        ev = featured
        away, home = ev["away_team"], ev["home_team"]
        cond = ev["ifNecessary"]
        if cond:
            copy = ("Game %s is played only if the series is still undecided. Postseason baseball in %s: the %s host the %s at %s."
                    % (ev["game"], team["market"], home["shortName"], away["name"], ev["venue"]["name"]))
        else:
            copy = ("Postseason baseball is back in %s. See the %s host the %s live at %s."
                    % (team["market"], home["shortName"], away["name"], ev["venue"]["name"]))
        hero = """    <section class="lp-hero">
      <div class="container">
        <p class="eyebrow">%(eyebrow)s</p>
        <h1 class="lp-title">
          <span class="lp-matchup"><span class="lp-team lp-team--away">%(away)s</span> <span class="lp-vs">vs.</span> <span class="lp-team lp-team--home">%(home)s</span></span>
          <span class="lp-title-sub">Playoff Tickets</span>
        </h1>
        <p class="lp-round"><span>%(round)s · Game %(game)s</span>%(cond_badge)s</p>
        <dl class="lp-facts">
          <div><dt>Date</dt><dd><time datetime="%(iso)s">%(date)s</time></dd></div>
          <div><dt>Time</dt><dd>%(time)s</dd></div>
          <div><dt>Stadium</dt><dd>%(venue)s</dd></div>
          <div><dt>Location</dt><dd>%(city)s, %(state)s</dd></div>
        </dl>
        <p class="lp-copy">%(copy)s</p>
        <div class="lp-cta" data-sticky-anchor>
          %(cta)s
          %(partner)s
        </div>
        %(disclosure)s
      </div>
    </section>""" % {
            "eyebrow": esc(eyebrow),
            "away": esc(away["shortName"]),
            "home": esc(home["shortName"]),
            "round": esc(ev["round_label"]),
            "game": ev["game"],
            "cond_badge": ' <span class="status-badge status-badge--conditional">If Necessary</span>' if cond else "",
            "iso": esc(iso_start(ev)),
            "date": esc(fmt_date(ev["start"], long=True)),
            "time": esc(fmt_time(ev)),
            "venue": esc(ev["venue"]["name"]),
            "city": esc(ev["venue"]["city"]),
            "state": esc(team.get("stateName") or ev["venue"]["state"]),
            "copy": esc(copy),
            "cta": ticket_cta(lg, ev, "See Available Tickets", "hero", team),
            "partner": partner_note(ev),
            "disclosure": disclosure_line,
        }
        if ev["url"]:
            body_class += " has-sticky-cta"
            sticky = """  <div class="sticky-cta" id="sticky-cta" hidden>
    <div class="sticky-cta-text">
      <strong>%(title)s</strong>
      <span>%(date)s · %(time)s%(cond)s</span>
    </div>
    %(cta)s
  </div>
""" % {"title": esc(ev["title"]), "date": esc(fmt_date(ev["start"])), "time": esc(fmt_time(ev)),
       "cond": " · If Necessary" if cond else "",
       "cta": ticket_cta(lg, ev, "See Tickets", "sticky", team, block=False)}
    elif st["active"]:
        nxt = road_events[0] if road_events else None
        if st["advanced"]:
            status_line = "Advanced to the %s. Opponent and schedule to be announced." % st["next_round_label"]
        else:
            status_line = "%s vs. %s" % (st["round_label"], st["opponent"]["name"])
        next_line = ""
        if nxt:
            next_line = '<p class="lp-copy">Next game: %s · %s, %s · %s</p>' % (
                esc(nxt["title"]), esc(fmt_date(nxt["start"])), esc(fmt_time(nxt)), esc(nxt["venue"]["name"]))
        hero = """    <section class="lp-hero">
      <div class="container">
        <p class="eyebrow">%(eyebrow)s</p>
        <h1 class="lp-title"><span class="lp-matchup">%(name)s</span> <span class="lp-title-sub">Playoff Tickets</span></h1>
        <p class="lp-round"><span>%(status)s</span></p>
        <p class="lp-copy">No %(short)s home playoff game is scheduled right now.</p>
        %(next)s
        <div class="lp-cta">
          %(cta)s
        </div>
        %(disclosure)s
      </div>
    </section>""" % {"eyebrow": esc(eyebrow), "name": esc(team["name"]), "status": esc(status_line),
                     "short": esc(short), "next": next_line,
                     "cta": team_cta("Browse All %s Playoff Tickets" % short, "hero-team"),
                     "disclosure": disclosure_line}
    else:
        how = "won the %s" % lg.info["championship"] if st["champion"] else "ended in the %s" % st["round_label"]
        hero = """    <section class="lp-hero">
      <div class="container">
        <p class="eyebrow">%(season)s %(league)s Postseason</p>
        <h1 class="lp-title"><span class="lp-matchup">%(name)s</span> <span class="lp-title-sub">Playoff Tickets</span></h1>
        <p class="lp-copy">The %(name)s' %(season)s postseason run %(how)s. See the playoff games still on the schedule.</p>
        <div class="lp-cta">
          <a class="btn btn-primary btn-block" href="/%(lid)s/#games">See Upcoming %(league)s Playoff Games</a>
        </div>
      </div>
    </section>""" % {"season": lg.season, "league": esc(lg.info["name"]), "name": esc(team["name"]),
                     "how": esc(how), "lid": lg.id}

    parts = [hero]

    if st["active"] and home_events:
        parts.append("""    <section class="section" id="home-games">
      <div class="container">
        <div class="section-header">
          <h2 class="section-title">Upcoming %(short)s Home Playoff Games</h2>
          <p class="section-lead">%(venue)s · %(city)s, %(state)s</p>
        </div>
        <ul class="event-rows">
          %(rows)s
        </ul>
      </div>
    </section>""" % {"short": esc(short), "venue": esc(team["venue"]["name"]), "city": esc(team["venue"]["city"]),
                     "state": esc(team["venue"]["state"]),
                     "rows": "\n".join(event_row(lg, ev, "home-games", team) for ev in home_events)})

    if st["active"] and road_events:
        parts.append("""    <section class="section section-alt" id="road-games">
      <div class="container">
        <div class="section-header">
          <h2 class="section-title">%(short)s Road Playoff Games</h2>
        </div>
        <ul class="event-rows">
          %(rows)s
        </ul>
      </div>
    </section>""" % {"short": esc(short),
                     "rows": "\n".join(event_row(lg, ev, "road-games", team) for ev in road_events)})

    if st["active"] and featured and team_url:
        parts.append("""    <section class="section">
      <div class="container">
        <div class="fallback-box">
          <h2 class="fallback-title">More %(short)s Playoff Tickets</h2>
          <p class="card-text">Looking for road games, future rounds, or another date?</p>
          %(cta)s
        </div>
      </div>
    </section>""" % {"short": esc(short), "cta": team_cta("Browse All %s Playoff Tickets" % short, "team-fallback",
                                                               variant="secondary", block=False)})

    if st["active"] and not st["advanced"]:
        s = st["series"]
        fmt = lg.rounds[lg.round_order[s["round"]]]["format"]
        parts.append("""    <section class="section series-context">
      <div class="container">
        <p class="card-text">The %(label)s is a %(fmt)s series between the %(a)s and the %(b)s. %(winner_line)s <a href="/%(lid)s/">See all %(league)s playoff games</a>.</p>
      </div>
    </section>""" % {"label": esc(st["round_label"]), "fmt": esc(fmt), "a": esc(team["name"]),
                     "b": esc(st["opponent"]["name"]), "lid": lg.id, "league": esc(lg.info["name"]),
                     "winner_line": esc(_winner_line(lg, s))})

    if not st["active"] and lg.upcoming:
        cards = "\n".join(event_card(lg, ev, "eliminated-team", team) for ev in [e for e in lg.upcoming if not e["ifNecessary"]][:3])
        if cards:
            parts.append("""    <section class="section">
      <div class="container">
        <div class="section-header">
          <h2 class="section-title">Upcoming %(league)s Playoff Games</h2>
        </div>
        <div class="card-grid card-grid--3">
          %(cards)s
        </div>
      </div>
    </section>""" % {"league": esc(lg.info["name"]), "cards": cards})

    parts.append(disclosure_note(lg))

    title = "%s Playoff Tickets %s | WatchPlayoffs" % (team["name"], lg.season)
    if st["active"] and home_events:
        desc = ("Find tickets for upcoming %s home playoff games at %s and see the %s postseason live in %s."
                % (team["name"], team["venue"]["name"], lg.info["name"], team["market"]))
    elif st["active"]:
        desc = "Find %s playoff tickets for the %s %s." % (team["name"], lg.season, st.get("next_round_label") or st["round_label"])
    else:
        desc = "%s %s postseason recap and upcoming %s playoff games with ticket links." % (team["name"], lg.season, lg.info["name"])
    jsonld = json_ld_events(lg, home_events + road_events) if st["active"] else ""
    return page(title, desc, lg.team_path(team), body_class, lg.id, "\n\n".join(parts),
                head_extra=jsonld, body_end=sticky)


def _winner_line(lg, s):
    idx = lg.round_order[s["round"]]
    if idx + 1 >= len(lg.rounds):
        return "The winner is the %s champion." % lg.info["name"]
    nxt = lg.rounds[idx + 1]
    conf = s.get("conference") if nxt["id"] != lg.rounds[-1]["id"] else ""
    return "The winner advances to the %s." % nxt["label"].replace("{conf}", conf or "").strip()


def build_home_module(lg, limit=4):
    confirmed = [ev for ev in lg.upcoming if not ev["ifNecessary"]][:limit]
    if not confirmed:
        return ""
    cards = []
    for ev in confirmed:
        cards.append("""<article class="event-card event-card--compact" data-event-start="%(iso)s">
              <span class="card-badge">%(tag)s</span>
              <h3 class="event-matchup"><span class="event-away">%(away)s</span> <span class="event-at">at</span> <span class="event-home">%(home)s</span></h3>
              <p class="event-when">%(city)s · <time datetime="%(iso)s">%(date)s</time></p>
              <div class="event-cta">%(cta)s</div>
            </article>""" % {
            "iso": esc(iso_start(ev)), "tag": esc(game_tag(ev)), "away": esc(ev["away_team"]["shortName"]),
            "home": esc(ev["home_team"]["shortName"]), "city": esc(ev["venue"]["city"]),
            "date": esc(fmt_date(ev["start"])),
            "cta": ticket_cta(lg, ev, "View Tickets", "home-module")})
    return """
    <section id="%(lid)s-playoffs" class="section %(theme)s home-league-module">
      <div class="container">
        <div class="section-header">
          <p class="eyebrow">%(name)s Playoffs</p>
          <h2 class="section-title">Upcoming %(name)s Playoff Games</h2>
        </div>
        <div class="card-grid card-grid--4">
            %(cards)s
        </div>
        <p class="module-more"><a class="btn btn-secondary" href="/%(lid)s/">View All %(name)s Playoff Tickets</a></p>
      </div>
    </section>
    """ % {"lid": lg.id, "theme": esc(lg.info["theme"]), "name": esc(lg.info["name"]), "cards": "\n            ".join(cards)}


# =========================================================================
# Main
# =========================================================================

def write(path, content, written):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
        written.append(str(path.relative_to(ROOT)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate data only")
    ap.add_argument("--now", help="ISO datetime to build as if it were that moment (preview/testing)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    written = []
    home_path = ROOT / "index.html"
    home_html = home_path.read_text(encoding="utf-8")
    exit_code = 0

    for lid in LEAGUES:
        data = json.loads((ROOT / "data" / ("%s.json" % lid)).read_text(encoding="utf-8"))
        try:
            lg = League(data, now)
        except BuildError as err:
            print("ERROR in data/%s.json:\n%s" % (lid, err), file=sys.stderr)
            exit_code = 1
            continue

        print("%s: %d upcoming games (%d confirmed), %d active teams, %d team pages"
              % (lid.upper(), len(lg.upcoming), len([e for e in lg.upcoming if not e["ifNecessary"]]),
                 len(lg.active_teams()), len(lg.participants())))
        for w in lg.warnings:
            print("  WARN %s" % w)
        if args.check:
            continue

        write(ROOT / lid / "index.html", build_hub(lg), written)
        for tid in lg.participants():
            write(ROOT / lid / ("%s-playoff-tickets" % lg.teams[tid]["slug"]) / "index.html",
                  build_team_page(lg, tid), written)

        start, end = "<!-- PLAYOFFS:%s:START -->" % lid.upper(), "<!-- PLAYOFFS:%s:END -->" % lid.upper()
        if start in home_html and end in home_html:
            pre, rest = home_html.split(start, 1)
            _, post = rest.split(end, 1)
            home_html = pre + start + build_home_module(lg) + end + post
        else:
            print("  WARN index.html has no %s ... %s markers; homepage module skipped" % (start, end))

    if not args.check:
        write(home_path, home_html, written)
        print("Updated %d file(s)%s" % (len(written), (":\n  " + "\n  ".join(written)) if written else ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
