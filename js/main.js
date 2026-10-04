(function () {
  "use strict";

  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("main-nav");

  if (!toggle || !nav) {
    return;
  }

  toggle.addEventListener("click", function () {
    var isOpen = nav.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(isOpen));
  });
})();

/* -------------------------------------------------------------
   Campaign attribution for outbound ticket links.
   - Inbound params listed below are remembered for the session,
     so they survive internal navigation (hub → team page).
   - They are appended only to outbound links that point at our
     own redirect host (go.watchplayoffs.com) or whose partner is
     configured with "passThroughParams": true in data/*.json.
     Direct partner deep links are left untouched by default.
   ------------------------------------------------------------- */
(function () {
  "use strict";

  var CONFIG = {
    storageKey: "wp_attribution",
    params: [
      "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
      "campaign", "creative", "ad", "click_id", "clickid", "external_id"
    ],
    redirectHosts: ["go.watchplayoffs.com"],
    // CTA context sent to the redirect host only (never to partners directly).
    contextParams: {
      sport: "wp_sport", team: "wp_team", home: "wp_home", away: "wp_away",
      event: "wp_event", city: "wp_city", cta: "wp_cta", partner: "wp_partner"
    }
  };

  function readStored() {
    try {
      return JSON.parse(sessionStorage.getItem(CONFIG.storageKey)) || {};
    } catch (e) {
      return {};
    }
  }

  var attribution = readStored();
  var search = new URLSearchParams(window.location.search);
  CONFIG.params.forEach(function (name) {
    var value = search.get(name);
    if (value) {
      attribution[name] = value;
    }
  });
  try {
    sessionStorage.setItem(CONFIG.storageKey, JSON.stringify(attribution));
  } catch (e) {
    /* storage unavailable: current-page params still apply below */
  }

  function decorate(link) {
    var url;
    try {
      url = new URL(link.href);
    } catch (e) {
      return;
    }
    var isRedirect = CONFIG.redirectHosts.indexOf(url.hostname) !== -1;
    if (!isRedirect && !link.hasAttribute("data-pass-params")) {
      return;
    }
    Object.keys(attribution).forEach(function (name) {
      if (!url.searchParams.has(name)) {
        url.searchParams.set(name, attribution[name]);
      }
    });
    if (isRedirect) {
      Object.keys(CONFIG.contextParams).forEach(function (key) {
        var value = link.dataset[key];
        if (value && !url.searchParams.has(CONFIG.contextParams[key])) {
          url.searchParams.set(CONFIG.contextParams[key], value);
        }
      });
    }
    link.href = url.toString();
  }

  var outbound = document.querySelectorAll("a[data-outbound]");
  Array.prototype.forEach.call(outbound, function (link) {
    decorate(link);
    link.addEventListener("click", function () {
      if (Array.isArray(window.dataLayer)) {
        window.dataLayer.push({
          event: "ticket_outbound_click",
          sport: link.dataset.sport,
          team: link.dataset.team,
          home_team: link.dataset.home,
          away_team: link.dataset.away,
          event_id: link.dataset.event,
          city: link.dataset.city,
          cta_location: link.dataset.cta,
          partner: link.dataset.partner
        });
      }
    });
  });
})();

/* -------------------------------------------------------------
   Hide games that already started (between data rebuilds) and
   show the mobile sticky ticket CTA once the hero CTA scrolls
   out of view.
   ------------------------------------------------------------- */
(function () {
  "use strict";

  var LISTED_AFTER_START_MS = 3 * 60 * 60 * 1000;
  var now = Date.now();
  var timed = document.querySelectorAll("[data-event-start]");
  Array.prototype.forEach.call(timed, function (el) {
    var value = el.getAttribute("data-event-start");
    // Date-only values (time TBA) stay listed through the day.
    var start = Date.parse(value.length === 10 ? value + "T12:00:00" : value);
    if (!isNaN(start) && now > start + LISTED_AFTER_START_MS) {
      el.hidden = true;
    }
  });

  var sticky = document.getElementById("sticky-cta");
  var anchor = document.querySelector("[data-sticky-anchor]");
  if (!sticky || !anchor || !("IntersectionObserver" in window)) {
    return;
  }
  new IntersectionObserver(function (entries) {
    var entry = entries[0];
    sticky.hidden = entry.isIntersecting || entry.boundingClientRect.top > 0;
  }).observe(anchor);
})();
