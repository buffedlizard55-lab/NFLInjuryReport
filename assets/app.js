/* NFL Injury Report — static front end.
 *
 * Data comes from data/latest/*.json, which a scheduled GitHub Actions run
 * regenerates and commits. On top of that snapshot the page attempts a direct
 * browser fetch of the two keyless public feeds (ESPN injuries JSON and Bluesky
 * search) so the feed can be fresher than the last CI run. Those calls are
 * subject to the upstream CORS policy, so failure is handled silently and the
 * committed snapshot is used instead — the UI always says which one it is
 * showing. It never pretends to be live when it is not.
 */
(function () {
  "use strict";

  var DATA_FILES = ["report", "alerts", "flags", "social", "scorecard", "meta", "health"];
  var state = {
    data: {},
    base: null,
    tab: "report",
    statusFilter: "",
    teamFilter: "",
    query: "",
    onlyDiscrepant: false,
    liveSource: "",
    autoRefresh: true,
    liveItems: [],
    liveAt: null,
    collapsed: {}
  };

  var ESPN_INJURIES =
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries";
  var BSKY_SEARCH =
    "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts";

  /* ------------------------------------------------------------ helpers */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function link(href, label, cls) {
    var a = el("a", cls || "", label);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    return a;
  }

  function xSearch(player, team) {
    var q = '"' + player + '" injury' + (team ? " OR #" + team : "");
    return "https://x.com/search?q=" + encodeURIComponent(q) + "&src=typed_query&f=live";
  }

  function relative(iso) {
    if (!iso) return "";
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var diff = Math.round((Date.now() - t) / 1000);
    var future = diff < 0;
    diff = Math.abs(diff);
    var out;
    if (diff < 45) out = diff + "s";
    else if (diff < 3600) out = Math.round(diff / 60) + "m";
    else if (diff < 86400) out = Math.round(diff / 3600) + "h";
    else out = Math.round(diff / 86400) + "d";
    return future ? "in " + out : out + " ago";
  }

  function stamp(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });
  }

  function badge(status) {
    var b = el("span", "badge b-" + (status || "UNKNOWN"), status || "UNKNOWN");
    return b;
  }

  function fmtNum(n, suffix) {
    if (n === null || n === undefined) return "—";
    return (typeof n === "number" ? Math.round(n * 10) / 10 : n) + (suffix || "");
  }

  /* ------------------------------------------------------- data loading */

  function tryBase(base) {
    // Each file is fetched independently. A single absent file (health.json is
    // only written by `pipeline verify`) must not take the whole page down --
    // the load succeeds as long as the report itself is present.
    return Promise.all(
      DATA_FILES.map(function (name) {
        return fetch(base + "data/latest/" + name + ".json", { cache: "no-store" })
          .then(function (r) {
            if (!r.ok) throw new Error(name + " -> HTTP " + r.status);
            return r.json();
          })
          .then(function (j) { return [name, j]; })
          .catch(function () { return [name, null]; });
      })
    ).then(function (pairs) {
      var out = {};
      pairs.forEach(function (p) { if (p[1]) out[p[0]] = p[1]; });
      if (!out.report && !out.meta) {
        throw new Error("no report.json or meta.json at '" + base + "data/latest/'");
      }
      return out;
    });
  }

  function load() {
    // Pages serves the site with data/ beside index.html; a local checkout of
    // docs/ has it one level up. Try both rather than hardcoding an assumption.
    var candidates = state.base ? [state.base] : ["", "../"];
    return candidates
      .reduce(function (chain, base) {
        return chain.catch(function () {
          return tryBase(base).then(function (data) {
            state.base = base;
            return data;
          });
        });
      }, Promise.reject(new Error("start")))
      .then(function (data) {
        state.data = data;
        renderAll();
      })
      .catch(function (err) {
        showBanner("No committed data yet — the collector has not run, or GitHub Pages is " +
                   "not serving data/. (" + err.message + ")", true);
        renderAll();
      });
  }

  /* ------------------------------------------------------------- render */

  function renderAll() {
    renderStatus();
    renderCounters();
    renderTeamChips();
    renderReport();
    renderFeed();
    renderAlerts();
    renderScorecard();
    renderSources();
    renderFlags();
    renderBadges();
    renderBootstrapWarning();
  }

  function renderBootstrapWarning() {
    // Never let sample data masquerade as a live snapshot.
    var r = state.data.report || {};
    var banner = document.getElementById("bootstrapBanner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "bootstrapBanner";
      banner.className = "notice warn";
      var main = document.querySelector("main");
      main.insertBefore(banner, main.firstChild);
    }
    if (r.bootstrap) {
      banner.textContent = "⚠ " + (r.note ||
        "Showing the bundled SAMPLE dataset, not a live snapshot. It is replaced in " +
        "full by the first scheduled collector run.");
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function renderStatus() {
    var meta = state.data.meta || {};
    var report = state.data.report || {};
    var dot = $("#statusDot"), txt = $("#statusText");
    var gen = report.generated_at || meta.generated_at;
    if (!gen) {
      dot.className = "dot dead";
      txt.textContent = "no data";
      return;
    }
    if (state.liveAt) {
      dot.className = "dot live";
      txt.textContent = "live · snapshot " + relative(gen);
      return;
    }
    var mins = Math.round((Date.now() - Date.parse(gen)) / 60000);
    dot.className = "dot " + (mins <= 45 ? "fresh" : mins <= 60 * 30 ? "stale" : "dead");
    txt.textContent = "snapshot " + relative(gen) +
      (report.season ? " · " + report.season + (report.week ? " wk " + report.week : "") : "");
  }

  function renderCounters() {
    var c = (state.data.report || {}).counts || {};
    var host = $("#reportCounters");
    host.innerHTML = "";
    [["players", "Players"], ["teams", "Clubs"], ["out", "Out/IR"],
     ["questionable", "Questionable"], ["doubtful", "Doubtful"],
     ["irregularities", "Flags"]].forEach(function (pair) {
      var box = el("div", "counter");
      box.appendChild(el("b", null, c[pair[0]] === undefined ? "—" : c[pair[0]]));
      box.appendChild(el("span", null, pair[1]));
      host.appendChild(box);
    });
    $("#seasonWeek").textContent =
      (state.data.report || {}).season
        ? (state.data.report.season + " · Week " + (state.data.report.week || "?"))
        : "no snapshot";
  }

  function renderTeamChips() {
    var host = $("#teamChips");
    if (host.childElementCount > 1) return; // built once
    host.innerHTML = "";
    var all = el("button", "chip active", "All teams");
    all.dataset.team = "";
    all.addEventListener("click", function () { setTeam(""); });
    host.appendChild(all);
    ((state.data.report || {}).teams || []).forEach(function (t) {
      var b = el("button", "chip", t.code + " (" + t.count + ")");
      b.dataset.team = t.code;
      b.addEventListener("click", function () { setTeam(t.code); });
      host.appendChild(b);
    });
  }

  function setTeam(code) {
    state.teamFilter = code;
    $$("#teamChips .chip").forEach(function (c) {
      c.classList.toggle("active", (c.dataset.team || "") === code);
    });
    renderReport();
  }

  function matches(p) {
    if (state.statusFilter) {
      if (state.statusFilter === "OUT") {
        if (p.game_status !== "OUT" && p.game_status !== "IR") return false;
      } else if (p.game_status !== state.statusFilter) return false;
    }
    if (state.teamFilter && p.team !== state.teamFilter) return false;
    if (state.onlyDiscrepant && !(p.discrepancies || []).length) return false;
    if (state.query) {
      var hay = (p.name + " " + p.team + " " + p.position + " " + p.injury +
                 " " + p.game_status + " " + (p.comment || "")).toLowerCase();
      if (hay.indexOf(state.query) === -1) return false;
    }
    return true;
  }

  function renderReport() {
    var host = $("#teamSections");
    host.innerHTML = "";
    var report = state.data.report || {};
    var teams = (report.teams || []).filter(function (t) {
      return !state.teamFilter || t.code === state.teamFilter;
    });
    var shown = 0;

    teams.forEach(function (t) {
      var rows = (t.players || []).filter(matches);
      if (!rows.length && (state.query || state.statusFilter || state.onlyDiscrepant)) return;
      shown += rows.length;

      var block = el("div", "team-block" + (state.collapsed[t.code] ? " collapsed" : ""));
      var head = el("div", "team-head");
      head.appendChild(el("span", "caret", "▾"));
      head.appendChild(el("span", "team-code", t.code));
      head.appendChild(el("h3", null, t.name));
      var meta = el("div", "team-meta");
      meta.appendChild(el("span", null, rows.length + " listed"));
      if (t.out) meta.appendChild(el("span", null, t.out + " out/IR"));
      if (t.doubtful) meta.appendChild(el("span", null, t.doubtful + " doubtful"));
      if (t.questionable) meta.appendChild(el("span", null, t.questionable + " questionable"));
      head.appendChild(meta);
      head.addEventListener("click", function () {
        state.collapsed[t.code] = !state.collapsed[t.code];
        block.classList.toggle("collapsed");
      });
      block.appendChild(head);

      var body = el("div", "team-body");
      if (!rows.length) {
        body.appendChild(el("div", "team-empty",
          "No entries in the current official report for this club."));
      } else {
        var table = el("table", "data");
        var thead = el("thead");
        var hr = el("tr");
        ["Player", "Pos", "Injury", "Practice", "Status", "Detail / attribution",
         "Verify"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
        thead.appendChild(hr);
        table.appendChild(thead);
        var tb = el("tbody");
        rows.forEach(function (p) { tb.appendChild(playerRow(p)); });
        table.appendChild(tb);
        body.appendChild(table);
      }
      block.appendChild(body);
      host.appendChild(block);
    });

    if (!shown) {
      host.appendChild(el("div", "empty",
        "Nothing matches the current filters."));
    }
  }

  function playerRow(p) {
    var tr = el("tr");

    var tdName = el("td");
    var nameCell = el("div");
    if (p.url) nameCell.appendChild(link(p.url, p.name, "pname"));
    else nameCell.appendChild(el("span", "pname", p.name));
    if ((p.discrepancies || []).length) {
      var f = el("span", "badge b-flag", "⚑ " + p.discrepancies.length);
      f.title = p.discrepancies.join(", ") + " — see Flags tab";
      nameCell.appendChild(document.createTextNode(" "));
      nameCell.appendChild(f);
    }
    tdName.appendChild(nameCell);
    if (p.attribution) {
      tdName.appendChild(el("div", "attrib",
        "reported by " + p.attribution + (p.outlet ? " · " + p.outlet : "")));
    }
    tr.appendChild(tdName);

    tr.appendChild(el("td", "pos", p.position || ""));
    tr.appendChild(el("td", null, p.injury || "—"));

    var prac = { FULL: "Full", LIMITED: "Limited", DNP: "DNP", NONE: "—" };
    tr.appendChild(el("td", null, prac[p.practice_status] || p.practice_status || "—"));

    var tdS = el("td");
    tdS.appendChild(badge(p.game_status));
    if (p.designation_source === "inferred") {
      var inf = el("div", "attrib", "inferred*");
      inf.title = "nfl.com printed no Game Status for this player; they practised " +
                  "without a designation, so 'ACTIVE' is inferred from the practice " +
                  "line rather than published by the league.";
      tdS.appendChild(inf);
    }
    tr.appendChild(tdS);

    var tdD = el("td");
    if (p.comment) tdD.appendChild(el("div", null, p.comment));
    else tdD.appendChild(el("span", "muted", "—"));
    if (p.observed_at) tdD.appendChild(el("div", "attrib", "as of " + stamp(p.observed_at)));
    tr.appendChild(tdD);

    var tdV = el("td");
    var box = el("div", "src-links");
    (p.sources || []).forEach(function (s) {
      if (s.url) box.appendChild(link(s.url, s.source));
    });
    box.appendChild(link(xSearch(p.name, p.team), "X", null));
    if (!p.url && p.nfl_slug) {
      box.appendChild(link("https://www.nfl.com/players/" + p.nfl_slug + "/", "nfl.com"));
    }
    tdV.appendChild(box);
    tr.appendChild(tdV);

    return tr;
  }

  /* ---------------------------------------------------------- live feed */

  function feedItems() {
    var out = [];
    ((state.data.alerts || {}).alerts || []).forEach(function (a) {
      out.push({
        src: "official", platform: "official", sev: a.severity || "low",
        ts: a.ts, who: a.player + " (" + a.team + ")",
        text: a.detail, url: (a.sources && a.sources[0] && a.sources[0].url) ||
              "https://www.nfl.com/injuries/",
        badge: a.to_status, kind: a.kind
      });
    });
    ((state.data.social || {}).posts || []).forEach(function (p) {
      out.push({
        src: p.platform, platform: p.platform, sev: p.predicted_status === "OUT" ? "high" : "medium",
        ts: p.posted_at, who: p.author_name || p.author,
        text: p.text, url: p.url, badge: p.predicted_status,
        extra: p.matched_player ? "re: " + p.matched_player : ""
      });
    });
    state.liveItems.forEach(function (p) { out.push(p); });

    out.sort(function (a, b) {
      var ta = Date.parse(a.ts || 0) || 0, tb = Date.parse(b.ts || 0) || 0;
      return tb - ta;
    });
    return out;
  }

  function renderFeed() {
    var host = $("#feedList");
    host.innerHTML = "";
    var items = feedItems().filter(function (i) {
      return !state.liveSource || i.src === state.liveSource;
    }).slice(0, 120);

    $("#liveCount").textContent = feedItems().length || "";

    if (!items.length) {
      host.appendChild(el("li", null, ""))
        .appendChild(el("div", "empty",
          "No items yet. Official changes appear as soon as the collector sees the " +
          "designation move; social items appear when a platform probe succeeds and a " +
          "post matches a known player."));
      return;
    }
    items.forEach(function (i) {
      var li = el("li", "sev-" + i.sev);
      var r1 = el("div", "row1");
      r1.appendChild(el("span", "platform-tag", i.platform));
      r1.appendChild(el("span", "who", i.who));
      if (i.badge && i.badge !== "UNKNOWN") r1.appendChild(badge(i.badge));
      else if (i.src !== "official") r1.appendChild(el("span", "platform-tag", "mention"));
      if (i.live) r1.appendChild(el("span", "badge b-ACTIVE", "LIVE"));
      r1.appendChild(el("span", "when",
        (relative(i.ts) ? relative(i.ts) + " · " : "") + stamp(i.ts)));
      li.appendChild(r1);
      if (i.text) li.appendChild(el("p", "body", i.text));
      var r3 = el("div", "row3");
      if (i.url) r3.appendChild(link(i.url, "Open source ↗"));
      if (i.who && i.badge) {
        r3.appendChild(link(xSearch(i.who.replace(/\s*\(.*$/, ""), ""), "Verify on X ↗"));
      }
      if (i.extra) r3.appendChild(el("span", "muted", i.extra));
      li.appendChild(r3);
      host.appendChild(li);
    });

    var probed = (state.data.social || {}).platforms_probed || {};
    var names = Object.keys(probed);
    var up = names.filter(function (n) { return probed[n].reachable; });
    $("#liveNote").textContent = names.length
      ? "Social platforms reachable on the last collector run: " +
        (up.length ? up.join(", ") : "none") +
        ". Platforms that failed their probe are skipped for that run and listed under Flags."
      : "";
    $("#liveCounters").innerHTML = "";
    [["items", feedItems().length], ["platforms up", up.length + "/" + names.length]]
      .forEach(function (pair) {
        var box = el("div", "counter");
        box.appendChild(el("b", null, pair[1]));
        box.appendChild(el("span", null, pair[0]));
        $("#liveCounters").appendChild(box);
      });
  }

  /* ------------------------------------------------------------ alerts */

  function renderAlerts() {
    var host = $("#alertList");
    host.innerHTML = "";
    var alerts = (state.data.alerts || {}).alerts || [];
    $("#alertCount").textContent = alerts.length || "";
    if (!alerts.length) {
      host.appendChild(el("div", "empty",
        "No status changes since the previous snapshot. Alerts are produced by diffing " +
        "consecutive official reports, so an empty list means the designations are stable."));
      return;
    }
    alerts.forEach(function (a) {
      var card = el("div", "alert-card sev-" + (a.severity || "low"));
      var head = el("div", "head");
      head.appendChild(el("span", "platform-tag", a.kind));
      head.appendChild(el("strong", null, a.player));
      head.appendChild(el("span", "muted", a.team + " " + (a.position || "")));
      if (a.from_status) {
        head.appendChild(badge(a.from_status));
        head.appendChild(el("span", "arrow", "→"));
      }
      head.appendChild(badge(a.to_status));
      head.appendChild(el("span", "when muted", relative(a.ts) + " · " + stamp(a.ts)));
      card.appendChild(head);
      card.appendChild(el("p", "detail", a.detail));
      var row = el("div", "row3");
      (a.sources || []).forEach(function (s) {
        if (s.url) row.appendChild(link(s.url, s.source));
      });
      row.appendChild(link("https://www.nfl.com/injuries/", "official report"));
      card.appendChild(row);
      host.appendChild(card);
    });
  }

  /* --------------------------------------------------------- scorecard */

  function renderScorecard() {
    var card = state.data.scorecard || {};
    var tb = $("#scoreTable tbody");
    tb.innerHTML = "";
    $("#minResolved").textContent = card.min_resolved_for_rate || 5;

    var rows = card.reporters || [];
    $("#scoreCounters").innerHTML = "";
    var s = card.summary || {};
    [["reporters", card.count || 0], ["claims", s.claims_total || 0],
     ["resolved", s.resolved || 0],
     ["accuracy", s.accuracy === undefined || s.accuracy === null
        ? "—" : Math.round(s.accuracy * 100) + "%"]]
      .forEach(function (pair) {
        var box = el("div", "counter");
        box.appendChild(el("b", null, pair[1]));
        box.appendChild(el("span", null, pair[0]));
        $("#scoreCounters").appendChild(box);
      });

    var resolved = rows.reduce(function (n, r) { return n + (r.resolved || 0); }, 0);
    var box = $("#scorecardNotice");
    box.className = "notice" + (resolved ? "" : " warn");
    box.innerHTML = "";
    if (!resolved) {
      box.appendChild(document.createTextNode(
        "No claims have resolved yet. Scores appear only after " +
        (card.min_resolved_for_rate || 5) + " claims per reporter have been checked " +
        "against the official report, so an early lucky call cannot read as 100%. " +
        "A historical backfill of past reporter posts is not possible from free " +
        "sources (there is no keyless archive of past X/Bluesky/Reddit posts, and X " +
        "has had no free read tier since February 2026), so this scorecard is " +
        "forward-collected from the moment the collector is enabled."));
    }
    // Always show the methodology caveats: they change how the numbers read.
    (s.caveats || []).forEach(function (c) {
      var p = document.createElement("p");
      p.style.margin = resolved ? "6px 0 0" : "10px 0 0";
      p.textContent = "⚠ " + c;
      box.appendChild(p);
    });
    var bp = s.by_platform || {};
    if (Object.keys(bp).length) {
      var line = document.createElement("p");
      line.style.margin = "8px 0 0";
      line.textContent = "By platform: " + Object.keys(bp).map(function (k) {
        var r = bp[k];
        return k + " " + r.resolved + " resolved" +
               (r.accuracy === null || r.accuracy === undefined
                 ? "" : " (" + Math.round(r.accuracy * 100) + "%)");
      }).join(" · ");
      box.appendChild(line);
    }

    if (!rows.length) {
      tb.appendChild(el("tr")).appendChild(el("td", "empty",
        "No reporters observed yet. Reporters enter the registry automatically, either " +
        "from the beat writer ESPN names on each update or from a social author who posts " +
        "an injury claim."));
      return;
    }
    rows.forEach(function (r) {
      var tr = el("tr");
      var td = el("td");
      td.appendChild(el("div", "pname", r.name || r.handle || r.key));
      if (r.outlet) td.appendChild(el("div", "attrib", r.outlet));
      tr.appendChild(td);
      tr.appendChild(el("td", null, r.platform || ""));
      var tdT = el("td");
      tdT.appendChild(el("span", "badge b-" +
        (r.tier === "flagged" ? "DOUBTFUL" : r.tier === "established" ? "ACTIVE" : "UNKNOWN"),
        r.tier || "observed"));
      tr.appendChild(tdT);
      tr.appendChild(el("td", "num", r.claims_correct || 0));
      tr.appendChild(el("td", "num", r.claims_wrong || 0));
      tr.appendChild(el("td", "num", r.resolved || 0));
      tr.appendChild(el("td", "num", r.accuracy === null || r.accuracy === undefined
        ? "—" : Math.round(r.accuracy * 100) + "%"));
      tr.appendChild(el("td", "num", r.wilson_lower === null || r.wilson_lower === undefined
        ? "—" : Math.round(r.wilson_lower * 100) + "%"));
      var tdScore = el("td", "num");
      tdScore.appendChild(el("strong", null, r.score === null || r.score === undefined
        ? "—" : r.score));
      tr.appendChild(tdScore);
      tr.appendChild(el("td", "num", r.avg_lead_minutes === null ||
        r.avg_lead_minutes === undefined ? "—" : Math.round(r.avg_lead_minutes) + "m"));
      var tdE = el("td");
      var box = el("div", "src-links");
      (r.evidence_urls || []).slice(0, 3).forEach(function (u, i) {
        box.appendChild(link(u, "#" + (i + 1)));
      });
      if (r.url) box.appendChild(link(r.url, "profile"));
      tdE.appendChild(box);
      tr.appendChild(tdE);
      tb.appendChild(tr);
    });
  }

  /* ----------------------------------------------------------- sources */

  function renderSources() {
    var health = state.data.health || {};
    var tb = $("#sourceTable tbody");
    tb.innerHTML = "";
    (health.sources || []).forEach(function (s) {
      var tr = el("tr");
      tr.appendChild(el("td", null, s.key));
      tr.appendChild(el("td", "muted", s.note || ""));
      tr.appendChild(el("td", "num", s.status === null || s.status === undefined
        ? "—" : s.status));
      tr.appendChild(el("td", "num", s.latency_ms === undefined || s.latency_ms === null
        ? "—" : s.latency_ms + "ms"));
      var tdR = el("td");
      tdR.appendChild(el("span", "badge b-" + (s.reachable ? "ACTIVE" : "OUT"),
        s.reachable ? "reachable" : "failed"));
      if (!s.reachable && s.error) tdR.appendChild(el("div", "attrib", s.error));
      tr.appendChild(tdR);
      var tdL = el("td");
      tdL.appendChild(link(s.url, "open ↗"));
      tr.appendChild(tdL);
      tb.appendChild(tr);
    });
    if (!(health.sources || []).length) {
      tb.appendChild(el("tr")).appendChild(el("td", "empty",
        "No probe results yet — run `python3 -m collectors.pipeline verify`."));
    }

    var stb = $("#socialTable tbody");
    stb.innerHTML = "";
    var platforms = health.social_platforms || {};
    Object.keys(platforms).forEach(function (name) {
      var p = platforms[name];
      var tr = el("tr");
      tr.appendChild(el("td", null, name));
      tr.appendChild(el("td", "num", p.status === null || p.status === undefined ? "—" : p.status));
      var tdR = el("td");
      tdR.appendChild(el("span", "badge b-" + (p.reachable ? "ACTIVE" : "OUT"),
        p.reachable ? "reachable" : "failed"));
      if (!p.reachable && p.error) tdR.appendChild(el("div", "attrib", p.error));
      tr.appendChild(tdR);
      var tdL = el("td");
      tdL.appendChild(link(p.url, "open ↗"));
      tr.appendChild(tdL);
      stb.appendChild(tr);
    });

    var x = health.x_twitter;
    var box = $("#xNotice");
    box.innerHTML = "";
    if (x) {
      box.appendChild(el("strong", null, "X / Twitter — why it is link-out only. "));
      box.appendChild(document.createTextNode(x.reason + " "));
      (x.refs || []).forEach(function (r) {
        box.appendChild(link(r, "source"));
        box.appendChild(document.createTextNode(" "));
      });
    }
  }

  /* ------------------------------------------------------------- flags */

  function renderFlags() {
    var host = $("#flagList");
    host.innerHTML = "";
    var flags = (state.data.flags || {}).irregularities || [];
    var errs = (state.data.flags || {}).source_errors || [];
    var socialFlags = (state.data.social || {}).irregularities || [];
    var all = flags.concat(socialFlags);
    $("#flagCount").textContent = (all.length + errs.length) || "";

    errs.forEach(function (e) {
      var card = el("div", "flag-card sev-critical");
      var h = el("h4");
      h.appendChild(el("span", "flag-code", "SOURCE_ERROR"));
      h.appendChild(el("span", null, e.source + " could not be fetched"));
      card.appendChild(h);
      card.appendChild(el("p", null,
        (e.reason || "unknown error") + (e.status ? " (HTTP " + e.status + ")" : "")));
      if (e.url) card.appendChild(link(e.url, e.url));
      host.appendChild(card);
    });

    if (!all.length && !errs.length) {
      host.appendChild(el("div", "empty",
        "No irregularities on the last run. Every cross-source disagreement, " +
        "unattributable row and unreachable platform is recorded here rather than " +
        "resolved by guesswork."));
      return;
    }
    all.forEach(function (f) {
      var card = el("div", "flag-card sev-" + (f.severity || "low"));
      var h = el("h4");
      h.appendChild(el("span", "flag-code", f.code));
      h.appendChild(el("span", null, f.title));
      h.appendChild(el("span", "badge b-" +
        (f.severity === "critical" || f.severity === "high" ? "OUT" : "QUESTIONABLE"),
        f.severity));
      card.appendChild(h);
      card.appendChild(el("p", null, f.detail));
      var row = el("div", "src-links");
      (f.evidence || []).forEach(function (e) {
        if (e.url) row.appendChild(link(e.url, e.label || "evidence"));
      });
      card.appendChild(row);
      host.appendChild(card);
    });
  }

  function renderBadges() {
    var rep = state.data.report || {};
    $("#alertCount").textContent = (rep.counts && rep.counts.alerts) || "";
  }

  /* ------------------------------------------- opportunistic live layer */

  function classifyText(text) {
    var t = (text || "").toLowerCase();
    var status = "UNKNOWN";
    if (/ruled out|out for|will not play|placed on (injured reserve|ir)/.test(t)) status = "OUT";
    else if (/injured reserve/.test(t)) status = "IR";
    else if (/doubtful|unlikely to play/.test(t)) status = "DOUBTFUL";
    else if (/questionable|game-time decision|50\/50/.test(t)) status = "QUESTIONABLE";
    else if (/full go|cleared|will play|is active/.test(t)) status = "ACTIVE";
    return status;
  }

  function tryLive() {
    // Both endpoints are keyless. Whether a browser may call them depends on
    // their CORS policy, which is not something this page can promise -- so any
    // failure leaves the committed snapshot in place and says so.
    fetch(ESPN_INJURIES, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (j) {
        var items = [];
        ((j.injuries || [])).forEach(function (block) {
          (block.injuries || []).forEach(function (it) {
            var a = it.athlete || {};
            var team = (a.team || {}).abbreviation || "";
            var card = ((a.links || []).filter(function (l) {
              return (l.rel || []).indexOf("playercard") >= 0;
            })[0] || {}).href || "https://www.espn.com/nfl/";
            items.push({
              src: "official", platform: "espn-live", sev:
                it.status === "Out" ? "high" : "medium",
              ts: (it.date || "").replace(/(\d{2}:\d{2})Z$/, "$1:00Z"),
              who: (a.displayName || "") + " (" + team + ")",
              text: it.shortComment || it.longComment || "",
              url: card, badge: (it.status || "UNKNOWN").toUpperCase(), live: true
            });
          });
        });
        if (items.length) {
          state.liveItems = items;
          state.liveAt = new Date().toISOString();
          renderFeed();
          renderStatus();
        }
      })
      .catch(function () { /* CORS or offline: snapshot stays authoritative */ });

    fetch(BSKY_SEARCH + "?q=" + encodeURIComponent("NFL injury ruled out") +
          "&limit=30&sort=latest", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (j) {
        var extra = [];
        ((j.posts || []).forEach || []).call(j.posts || [], function (p) {
          var rec = p.record || {};
          var auth = p.author || {};
          var text = rec.text || "";
          var st = classifyText(text);
          if (st === "UNKNOWN") return;
          var rkey = ((p.uri || "").match(/app\.bsky\.feed\.post\/([^/]+)$/) || [])[1] || "";
          extra.push({
            src: "bluesky", platform: "bluesky", sev: st === "OUT" ? "high" : "medium",
            ts: rec.createdAt || "", who: auth.displayName || auth.handle || "",
            text: text, badge: st, live: true,
            url: rkey ? "https://bsky.app/profile/" + auth.handle + "/post/" + rkey : ""
          });
        });
        if (extra.length) {
          state.liveItems = state.liveItems.concat(extra);
          state.liveAt = new Date().toISOString();
          renderFeed();
          renderStatus();
        }
      })
      .catch(function () { /* same */ });
  }

  /* ------------------------------------------------------------- chrome */

  function showBanner(msg, isErr) {
    var b = $("#banner");
    b.textContent = msg;
    b.className = "banner" + (isErr ? " err" : "");
    b.hidden = false;
    clearTimeout(showBanner._t);
    showBanner._t = setTimeout(function () { b.hidden = true; }, 9000);
  }

  function wire() {
    $$(".tab").forEach(function (t) {
      t.addEventListener("click", function () {
        if (!t.dataset.tab) return; /* e.g. the directory.html link */
        state.tab = t.dataset.tab;
        $$(".tab").forEach(function (x) { x.classList.toggle("active", x === t); });
        $$(".panel").forEach(function (p) {
          p.classList.toggle("active", p.id === "panel-" + state.tab);
        });
      });
    });

    $("#playerSearch").addEventListener("input", function (e) {
      state.query = e.target.value.trim().toLowerCase();
      renderReport();
    });

    $$("#statusChips .chip").forEach(function (c) {
      c.addEventListener("click", function () {
        state.statusFilter = c.dataset.status || "";
        $$("#statusChips .chip").forEach(function (x) {
          x.classList.toggle("active", x === c);
        });
        renderReport();
      });
    });

    $("#onlyDiscrepant").addEventListener("change", function (e) {
      state.onlyDiscrepant = e.target.checked;
      renderReport();
    });

    $$("#liveSourceChips .chip").forEach(function (c) {
      c.addEventListener("click", function () {
        state.liveSource = c.dataset.src || "";
        $$("#liveSourceChips .chip").forEach(function (x) {
          x.classList.toggle("active", x === c);
        });
        renderFeed();
      });
    });

    $("#autoRefresh").addEventListener("change", function (e) {
      state.autoRefresh = e.target.checked;
    });

    $("#refreshBtn").addEventListener("click", function () {
      showBanner("Refreshing…");
      load().then(tryLive);
    });
  }

  wire();
  load().then(function () {
    tryLive();
    setInterval(function () {
      if (state.autoRefresh) { load(); tryLive(); }
    }, 30000);
  });
})();
