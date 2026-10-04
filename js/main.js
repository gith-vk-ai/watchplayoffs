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
   Games that have started: marked "Started" with the ticket
   button disabled and moved to the end of their list; removed
   entirely LISTED_AFTER_START_MS after first pitch. Re-checked
   every minute so open pages update without a reload. The
   mobile sticky CTA appears once the hero CTA scrolls away.
   ------------------------------------------------------------- */
(function () {
  "use strict";

  var LISTED_AFTER_START_MS = 3 * 60 * 60 * 1000;
  var STARTED_BADGE = "Started";
  var STARTED_CTA = "No Longer Available";

  function startOf(el, attr) {
    var value = el.getAttribute(attr);
    // Date-only values (time TBA) are treated as noon local time.
    return Date.parse(value.length === 10 ? value + "T12:00:00" : value);
  }

  function disableTickets(scope, label) {
    var links = scope.querySelectorAll("a[data-outbound]");
    Array.prototype.forEach.call(links, function (link) {
      var span = document.createElement("span");
      span.className = link.className + " is-disabled";
      span.setAttribute("aria-disabled", "true");
      span.textContent = label;
      link.parentNode.replaceChild(span, link);
    });
  }

  function markStarted(el) {
    if (el.classList.contains("is-started")) {
      return;
    }
    el.classList.add("is-started");
    disableTickets(el, STARTED_CTA);
    var slot = el.hasAttribute("data-status-slot") ? el : el.querySelector("[data-status-slot]");
    if (slot) {
      var old = slot.querySelector(".status-badge");
      var badge = document.createElement("span");
      badge.className = "status-badge status-badge--started";
      badge.textContent = STARTED_BADGE;
      if (old) {
        old.parentNode.replaceChild(badge, old);
      } else {
        var after = slot.querySelector(".card-badge");
        slot.insertBefore(badge, after ? after.nextSibling : slot.firstChild);
      }
    }
    // Keep bookable games at the top of the list.
    el.parentNode.appendChild(el);
  }

  function update() {
    var now = Date.now();
    var timed = document.querySelectorAll("[data-event-start]");
    Array.prototype.forEach.call(timed, function (el) {
      var start = startOf(el, "data-event-start");
      if (isNaN(start) || now < start) {
        return;
      }
      if (now > start + LISTED_AFTER_START_MS) {
        el.hidden = true;
      } else {
        markStarted(el);
      }
    });

    var hero = document.querySelector("[data-hero-start]");
    if (hero && !hero.classList.contains("is-started") && now >= startOf(hero, "data-hero-start")) {
      hero.classList.add("is-started");
      disableTickets(hero, "This Game Has Started");
      var note = hero.querySelector(".cta-partner");
      var more = document.getElementById("home-games") || document.getElementById("road-games");
      if (note && more) {
        note.innerHTML = '<a href="#' + more.id + '">See upcoming games</a>';
      } else if (note) {
        note.hidden = true;
      }
      var sticky = document.getElementById("sticky-cta");
      if (sticky) {
        sticky.parentNode.removeChild(sticky);
      }
    }
  }

  update();
  window.setInterval(update, 60 * 1000);

  var stickyCta = document.getElementById("sticky-cta");
  var anchor = document.querySelector("[data-sticky-anchor]");
  if (!stickyCta || !anchor || !("IntersectionObserver" in window)) {
    return;
  }
  new IntersectionObserver(function (entries) {
    var entry = entries[0];
    var el = document.getElementById("sticky-cta");
    if (el) {
      el.hidden = entry.isIntersecting || entry.boundingClientRect.top > 0;
    }
  }).observe(anchor);
})();
